import argparse
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.bulk_generation import (
    generation_state_codec,
    load_generation_queue,
)
from r1999extractor.cli import cli_error, cli_success
from r1999extractor.compatibility import legacy_workflow_notice
from r1999extractor.model_benchmark import benchmark_codec, default_output
from r1999extractor.versioned_json import VersionedJSONCodec, VersionedJSONError

listening_session_schema = "r1999.model-listening-session"
listening_key_schema = "r1999.model-listening-key"
listening_report_schema = "r1999.model-listening-report"
listening_schema_version = 1
legacy_listening_dimensions = ("timbre", "accent", "naturalness", "pronunciation")
listening_session_codec = VersionedJSONCodec(
    listening_session_schema,
    listening_schema_version,
    "model listening session",
)
listening_key_codec = VersionedJSONCodec(
    listening_key_schema,
    listening_schema_version,
    "model listening key",
)
listening_report_codec = VersionedJSONCodec(
    listening_report_schema,
    listening_schema_version,
    "model listening report",
)
default_session_directory = default_output / "listening-session"


class ModelListeningError(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _model_id(model):
    provider = str(model.get("provider", "")).strip()
    name = str(model.get("model", "")).strip()
    if not provider or not name:
        raise ModelListeningError("Every benchmark model needs a provider and model name")
    return f"{provider}/{name}"


def _state_path(model):
    configured = model.get("state")
    if configured:
        return Path(configured).expanduser().resolve()
    manifest = model.get("manifest")
    if not manifest:
        raise ModelListeningError(f"Benchmark model {_model_id(model)} has no generation state")
    return Path(manifest).expanduser().resolve().with_name("generation-state.json")


def _link_blind_audio(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _source_digest(paths):
    sources = [
        {
            "path": str(Path(path).expanduser().resolve()),
            "sha256": sha256_file(path),
        }
        for path in paths
    ]
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sources, hashlib.sha256(payload).hexdigest()


def _normalized_text(text):
    normalized = unicodedata.normalize("NFKC", str(text)).replace("…", "...")
    return re.sub(r"\s+", " ", normalized).strip()


def _load_benchmark(path):
    try:
        return benchmark_codec.load(path)
    except VersionedJSONError as error:
        raise ModelListeningError(str(error)) from error


def _load_generation_state(path):
    try:
        return generation_state_codec.load(path)
    except VersionedJSONError as error:
        raise ModelListeningError(str(error)) from error


def _available_model_audio(report, queue_items, queue_sha256):
    models = report.get("models")
    if not isinstance(models, list) or len(models) < 2:
        raise ModelListeningError("A listening session requires at least two benchmark models")
    audio_by_model = {}
    model_metadata = []
    for model in models:
        if not isinstance(model, dict):
            raise ModelListeningError("Benchmark models must be JSON objects")
        model_id = _model_id(model)
        if model_id in audio_by_model:
            raise ModelListeningError(f"Duplicate benchmark model: {model_id}")
        state_path = _state_path(model)
        state = _load_generation_state(state_path)
        if state.get("queue_sha256") != queue_sha256:
            raise ModelListeningError(
                f"Generation state for {model_id} does not use the benchmark sample queue"
            )
        model_audio = {}
        for item in queue_items:
            queue_id = item.get("queue_id")
            generated = state.get("items", {}).get(queue_id, {})
            source = (state_path.parent / str(generated.get("path", ""))).resolve()
            if generated.get("status") not in {"generated", "approved"} or not source.is_file():
                continue
            if state_path.parent not in source.parents:
                raise ModelListeningError(f"Generated audio leaves its model directory: {source}")
            if generated.get("text_sha256") != item.get("text_sha256"):
                continue
            expected_hash = generated.get("file_sha256")
            if expected_hash and sha256_file(source) != expected_hash:
                raise ModelListeningError(f"Generated audio checksum changed: {source}")
            model_audio[queue_id] = source.resolve()
        audio_by_model[model_id] = model_audio
        model_metadata.append(
            {
                "model_id": model_id,
                "provider": str(model["provider"]),
                "model": str(model["model"]),
                "state": str(state_path),
            }
        )
    return model_metadata, audio_by_model


def _write_listening_session(
    output_directory,
    model_metadata,
    audio_by_model,
    queue_items,
    *,
    source_kind,
    source_paths,
    source_sha256,
    seed,
):
    output_directory = Path(output_directory).expanduser().resolve()
    session_path = output_directory / "session.json"
    if session_path.exists():
        raise ModelListeningError(f"Listening session already exists: {session_path}")
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ModelListeningError(f"Listening session directory is not empty: {output_directory}")

    queue_by_id = {item["queue_id"]: item for item in queue_items}
    if len(queue_by_id) != len(queue_items):
        raise ModelListeningError("Benchmark sample queue contains duplicate queue IDs")

    pairs = []
    model_ids = [item["model_id"] for item in model_metadata]
    for queue_id in queue_by_id:
        available = [model_id for model_id in model_ids if queue_id in audio_by_model[model_id]]
        for left, right in itertools.combinations(available, 2):
            pairs.append((queue_id, left, right))
    if not pairs:
        raise ModelListeningError("No same-text generated samples are shared by two models")

    generator = random.Random(seed)
    generator.shuffle(pairs)
    output_directory.mkdir(parents=True, exist_ok=True)
    public_trials = []
    assignments = []
    for index, (queue_id, left, right) in enumerate(pairs, start=1):
        sides = [left, right]
        generator.shuffle(sides)
        trial_id = f"trial-{index:04d}"
        aliases = {
            "a": Path("audio") / f"{trial_id}-a.wav",
            "b": Path("audio") / f"{trial_id}-b.wav",
        }
        for side, model_id in zip(("a", "b"), sides, strict=True):
            _link_blind_audio(
                audio_by_model[model_id][queue_id],
                output_directory / aliases[side],
            )
        item = queue_by_id[queue_id]
        public_trials.append(
            {
                "trial_id": trial_id,
                "queue_id": queue_id,
                "line_id": item.get("line_id"),
                "text_sha256": item.get("text_sha256"),
                "text": item.get("text", ""),
                "audio": {side: path.as_posix() for side, path in aliases.items()},
                "rating": None,
            }
        )
        assignments.append(
            {
                "trial_id": trial_id,
                "a": {
                    "model_id": sides[0],
                    "source": str(audio_by_model[sides[0]][queue_id]),
                },
                "b": {
                    "model_id": sides[1],
                    "source": str(audio_by_model[sides[1]][queue_id]),
                },
            }
        )

    key_path = output_directory / ".blind-key.json"
    key = listening_key_codec.new(
        created_at=_utc_now(),
        source_kind=source_kind,
        source_sha256=source_sha256,
        sources=source_paths,
        models=model_metadata,
        assignments=assignments,
    )
    listening_key_codec.write(key_path, key, sort_keys=True)
    session = listening_session_codec.new(
        created_at=_utc_now(),
        updated_at=_utc_now(),
        source_kind=source_kind,
        source_sha256=source_sha256,
        blind_key_sha256=sha256_file(key_path),
        seed=seed,
        decision_mode="preference-only",
        trial_count=len(public_trials),
        completed_count=0,
        trials=public_trials,
    )
    listening_session_codec.write(session_path, session, sort_keys=True)
    return session_path


def create_listening_session(benchmark_path, output_directory, *, seed=0):
    benchmark_path = Path(benchmark_path).expanduser().resolve()
    report = _load_benchmark(benchmark_path)
    sample_queue = report.get("sample_queue")
    if not sample_queue:
        raise ModelListeningError("Benchmark report has no sample queue")
    try:
        _metadata, queue_items = load_generation_queue(sample_queue)
    except Exception as error:
        raise ModelListeningError(str(error)) from error
    queue_sha256 = sha256_file(sample_queue)
    model_metadata, audio_by_model = _available_model_audio(
        report,
        queue_items,
        queue_sha256,
    )
    source_paths, source_sha256 = _source_digest((benchmark_path,))
    return _write_listening_session(
        output_directory,
        model_metadata,
        audio_by_model,
        queue_items,
        source_kind="benchmark",
        source_paths=source_paths,
        source_sha256=source_sha256,
        seed=seed,
    )


def create_listening_session_from_reports(report_paths, output_directory, *, seed=0):
    resolved_paths = [Path(path).expanduser().resolve() for path in report_paths]
    if len(resolved_paths) < 2:
        raise ModelListeningError("At least two model reports are required")
    model_metadata = {}
    audio_by_model = defaultdict(dict)
    queue_items = {}
    for report_path in resolved_paths:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelListeningError(
                f"Unable to read model report {report_path}: {error}"
            ) from error
        backend = str(report.get("backend", "")).strip()
        samples = report.get("samples")
        if not backend or not isinstance(samples, list) or not samples:
            raise ModelListeningError(f"Model report is missing backend samples: {report_path}")
        language = str(report.get("language", "")).strip()
        for sample in samples:
            if not isinstance(sample, dict):
                raise ModelListeningError(f"Model report sample is invalid: {report_path}")
            text = str(sample.get("text", "")).strip()
            normalized_text = _normalized_text(text)
            audio = Path(str(sample.get("audio", ""))).expanduser().resolve()
            if not normalized_text or not audio.is_file():
                raise ModelListeningError(
                    f"Model report sample text or audio is missing: {report_path}"
                )
            label = str(sample.get("label", "")).strip()
            variant_parts = [backend]
            if language:
                variant_parts.append(language)
            if label:
                variant_parts.append(label)
            model_name = " / ".join(variant_parts)
            model_id = f"legacy/{model_name}"
            metadata = model_metadata.setdefault(
                model_id,
                {
                    "model_id": model_id,
                    "provider": "legacy",
                    "model": model_name,
                    "reports": [],
                },
            )
            report_name = str(report_path)
            if report_name not in metadata["reports"]:
                metadata["reports"].append(report_name)
            text_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
            queue_id = f"legacy:{text_sha256}"
            existing = audio_by_model[model_id].get(queue_id)
            if existing is not None and existing != audio:
                raise ModelListeningError(
                    f"Model {model_name} has multiple outputs for the same normalized text"
                )
            audio_by_model[model_id][queue_id] = audio
            reference = str(sample.get("reference", "")).strip()
            line_id = str(sample.get("id", "")).strip()
            if not line_id and reference:
                line_id = Path(reference).stem
            queue_items.setdefault(
                queue_id,
                {
                    "queue_id": queue_id,
                    "line_id": line_id or queue_id,
                    "text_sha256": text_sha256,
                    "text": text,
                },
            )
    source_paths, source_sha256 = _source_digest(resolved_paths)
    return _write_listening_session(
        output_directory,
        list(model_metadata.values()),
        dict(audio_by_model),
        list(queue_items.values()),
        source_kind="model-reports",
        source_paths=source_paths,
        source_sha256=source_sha256,
        seed=seed,
    )


def load_listening_session(path):
    path = Path(path).expanduser().resolve()
    try:
        session = listening_session_codec.load(path)
    except VersionedJSONError as error:
        raise ModelListeningError(str(error)) from error
    trials = session.get("trials")
    if not isinstance(trials, list) or session.get("trial_count") != len(trials):
        raise ModelListeningError("Listening session trial count is invalid")
    decision_mode = session.get("decision_mode")
    legacy_dimensions = session.get("dimensions")
    if decision_mode != "preference-only" and legacy_dimensions != list(
        legacy_listening_dimensions
    ):
        raise ModelListeningError("Listening session decision mode is invalid")
    trial_ids = [trial.get("trial_id") for trial in trials if isinstance(trial, dict)]
    if len(trial_ids) != len(trials) or len(set(trial_ids)) != len(trials):
        raise ModelListeningError("Listening session trial IDs are invalid")
    completed, _total = listening_progress(session)
    if session.get("completed_count") != completed:
        raise ModelListeningError("Listening session progress is inconsistent")
    for trial in trials:
        audio = trial.get("audio")
        if not isinstance(audio, dict) or set(audio) != {"a", "b"}:
            raise ModelListeningError(f"Listening trial audio is invalid: {trial['trial_id']}")
        for relative in audio.values():
            candidate = (path.parent / str(relative)).resolve()
            if path.parent not in candidate.parents or not candidate.is_file():
                raise ModelListeningError(f"Listening trial audio is missing: {candidate}")
    return session


def _load_blind_key(session_path, session):
    key_path = Path(session_path).expanduser().resolve().with_name(".blind-key.json")
    if not key_path.is_file() or sha256_file(key_path) != session.get("blind_key_sha256"):
        raise ModelListeningError("Listening session blind key is missing or changed")
    try:
        key = listening_key_codec.load(key_path)
    except VersionedJSONError as error:
        raise ModelListeningError(str(error)) from error
    if key.get("source_kind") != session.get("source_kind") or key.get(
        "source_sha256"
    ) != session.get("source_sha256"):
        raise ModelListeningError("Listening session source identity changed")
    models = key.get("models")
    assignments = key.get("assignments")
    if not isinstance(models, list) or not isinstance(assignments, list):
        raise ModelListeningError("Listening session blind key is invalid")
    if {item.get("trial_id") for item in assignments if isinstance(item, dict)} != {
        trial["trial_id"] for trial in session["trials"]
    }:
        raise ModelListeningError("Listening session blind assignments are incomplete")
    return key


def next_pending_trial(session):
    return next((trial for trial in session["trials"] if trial.get("rating") is None), None)


def listening_progress(session):
    completed = sum(trial.get("rating") is not None for trial in session["trials"])
    return completed, len(session["trials"])


def record_trial_preference(
    session_path,
    trial_id,
    preference,
    *,
    overwrite=False,
):
    if preference not in {"a", "b", "tie"}:
        raise ModelListeningError("Preference must be a, b, or tie")
    session_path = Path(session_path).expanduser().resolve()
    session = load_listening_session(session_path)
    trial = next((item for item in session["trials"] if item.get("trial_id") == trial_id), None)
    if trial is None:
        raise ModelListeningError(f"Unknown listening trial: {trial_id}")
    if trial.get("rating") is not None and not overwrite:
        raise ModelListeningError(f"Listening trial is already rated: {trial_id}")
    trial["rating"] = {
        "preference": preference,
        "reviewed_at": _utc_now(),
    }
    completed, _total = listening_progress(session)
    session["completed_count"] = completed
    session["updated_at"] = _utc_now()
    listening_session_codec.write(session_path, session, sort_keys=True)
    return session


def aggregate_listening_report(session_path, output_path=None):
    session_path = Path(session_path).expanduser().resolve()
    session = load_listening_session(session_path)
    key = _load_blind_key(session_path, session)
    assignments = {item["trial_id"]: item for item in key.get("assignments", [])}
    model_stats = {
        item["model_id"]: {
            "model_id": item["model_id"],
            "provider": item["provider"],
            "model": item["model"],
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "reviewed_trials": 0,
        }
        for item in key.get("models", [])
    }
    pairwise = defaultdict(lambda: {"trials": 0, "left_wins": 0, "right_wins": 0, "ties": 0})
    for trial in session["trials"]:
        rating = trial.get("rating")
        if rating is None:
            continue
        assignment = assignments.get(trial["trial_id"])
        if not assignment:
            raise ModelListeningError(f"Blind key is missing {trial['trial_id']}")
        side_models = {side: assignment[side]["model_id"] for side in ("a", "b")}
        for side, model_id in side_models.items():
            stats = model_stats[model_id]
            stats["reviewed_trials"] += 1
        preferred = rating["preference"]
        if preferred == "tie":
            model_stats[side_models["a"]]["ties"] += 1
            model_stats[side_models["b"]]["ties"] += 1
        else:
            winner = side_models[preferred]
            loser = side_models["b" if preferred == "a" else "a"]
            model_stats[winner]["wins"] += 1
            model_stats[loser]["losses"] += 1

        left, right = sorted(side_models.values())
        comparison = pairwise[(left, right)]
        comparison["trials"] += 1
        if preferred == "tie":
            comparison["ties"] += 1
        elif side_models[preferred] == left:
            comparison["left_wins"] += 1
        else:
            comparison["right_wins"] += 1

    models = []
    for stats in model_stats.values():
        preference_trials = stats["wins"] + stats["losses"] + stats["ties"]
        models.append(
            {
                "model_id": stats["model_id"],
                "provider": stats["provider"],
                "model": stats["model"],
                "reviewed_trials": stats["reviewed_trials"],
                "preference": {
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "ties": stats["ties"],
                    "rate": (
                        round((stats["wins"] + 0.5 * stats["ties"]) / preference_trials, 4)
                        if preference_trials
                        else None
                    ),
                },
            }
        )
    models.sort(
        key=lambda item: (
            -(item["preference"]["rate"] if item["preference"]["rate"] is not None else -1),
            -item["preference"]["wins"],
            item["model_id"],
        )
    )
    for rank, model in enumerate(models, start=1):
        model["rank"] = rank

    completed, total = listening_progress(session)
    report = listening_report_codec.new(
        generated_at=_utc_now(),
        session=str(session_path),
        complete=completed == total,
        completed_trials=completed,
        pending_trials=total - completed,
        manual_selection_required=True,
        models=models,
        pairwise=[
            {"left_model": left, "right_model": right, **values}
            for (left, right), values in sorted(pairwise.items())
        ],
    )
    if output_path is not None:
        listening_report_codec.write(output_path, report, sort_keys=True)
    return report


def create_parser():
    parser = argparse.ArgumentParser(
        description="Run blind, resumable same-text A/B listening sessions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--benchmark", type=Path, default=default_output / "benchmark-report.json")
    start.add_argument("--output", type=Path, default=default_session_directory)
    start.add_argument("--seed", type=int, default=0)
    start_reports = subparsers.add_parser("start-reports")
    start_reports.add_argument("--reports", type=Path, nargs="+", required=True)
    start_reports.add_argument("--output", type=Path, default=default_session_directory)
    start_reports.add_argument("--seed", type=int, default=0)
    status = subparsers.add_parser("status")
    status.add_argument("--session", type=Path, default=default_session_directory / "session.json")
    next_trial = subparsers.add_parser("next")
    next_trial.add_argument(
        "--session", type=Path, default=default_session_directory / "session.json"
    )
    score = subparsers.add_parser("score")
    score.add_argument("trial_id")
    score.add_argument("--session", type=Path, default=default_session_directory / "session.json")
    score.add_argument("--preference", choices=("a", "b", "tie"), required=True)
    score.add_argument("--overwrite", action="store_true")
    report = subparsers.add_parser("report")
    report.add_argument("--session", type=Path, default=default_session_directory / "session.json")
    report.add_argument("--output", type=Path)
    ui = subparsers.add_parser("ui")
    ui.add_argument("--session", type=Path, default=default_session_directory / "session.json")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    legacy_workflow_notice(
        "r1999-listen",
        tuple(
            getattr(options, name)
            for name in ("benchmark", "reports", "output", "session")
            if getattr(options, name, None) is not None
        ),
    )
    try:
        if options.command == "start":
            path = create_listening_session(options.benchmark, options.output, seed=options.seed)
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "start-reports":
            path = create_listening_session_from_reports(
                options.reports,
                options.output,
                seed=options.seed,
            )
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "ui":
            from r1999extractor.model_listening_ui import launch_listening_workbench

            return launch_listening_workbench(options.session)
        session = load_listening_session(options.session)
        if options.command == "status":
            completed, total = listening_progress(session)
            return cli_success(f"Listening progress: {completed}/{total} trials")
        if options.command == "next":
            trial = next_pending_trial(session)
            if trial is None:
                return cli_success("Listening session is complete")
            print(json.dumps(trial, ensure_ascii=False, indent=2))
            return 0
        if options.command == "score":
            updated = record_trial_preference(
                options.session,
                options.trial_id,
                options.preference,
                overwrite=options.overwrite,
            )
            aggregate_listening_report(
                options.session,
                Path(options.session).expanduser().resolve().with_name("report.json"),
            )
            completed, total = listening_progress(updated)
            return cli_success(f"Saved {options.trial_id}; progress: {completed}/{total}")
        output = options.output or Path(options.session).expanduser().resolve().with_name(
            "report.json"
        )
        report = aggregate_listening_report(options.session, output)
        return cli_success(
            f"Listening report: {output} ({report['completed_trials']} completed, "
            f"{report['pending_trials']} pending)"
        )
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return cli_error("Qt UI is not installed; run `uv sync --extra ui` first")
        raise
    except (ModelListeningError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
