import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_cache_path, user_data_path
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from r1999extractor.bulk_generation import (
    BulkGenerationError,
    generation_state_codec,
    load_generation_queue,
)
from r1999extractor.generation_queue import (
    build_generation_queue,
    load_story_records,
    write_generation_queue,
)
from r1999extractor.moss_generation import is_spoken_item
from r1999extractor.reverse1999_catalog import normalize_name
from r1999extractor.settings import get_local_data_directory

pregeneration_job_schema = "r1999.pregeneration-job"
pregeneration_job_version = 1
default_jobs_root = get_local_data_directory() / "reverse1999" / "pregeneration-jobs"
default_moss_model_id = "shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8"
default_moss_model_directory_name = "moss-tts-local-v1.5-mlx-int8"


class PregenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PregenerationTarget:
    target_id: str
    category: str
    title: str
    chapters: tuple[str, ...]
    episode_titles: tuple[str, ...]
    episode_count: int
    line_count: int
    voice_count: int
    cast_count: int
    sort_key: tuple


@dataclass(frozen=True)
class NarratorVoiceChoice:
    character: str
    speaker: str
    aliases: tuple[str, ...]
    references: tuple[Path, ...]


@dataclass(frozen=True)
class GenerationProgress:
    status: str
    generated: int
    failed: int
    pending: int
    eligible: int
    skipped_missing_voice: int
    skipped_sound_effects: int
    missing_voice_names: tuple[str, ...]
    latest_line: str | None
    latest_text: str | None
    updated_at: str | None
    rate_per_minute: float | None
    eta_seconds: int | None
    active_line: str | None
    active_text: str | None
    active_speaker: str | None
    active_phase: str | None
    active_attempt: int | None
    active_attempt_limit: int | None
    active_started_at: str | None
    active_last_error: str | None


def discover_default_story_index(data_directory=None):
    root = Path(data_directory or get_local_data_directory()).expanduser().resolve() / "reverse1999"
    candidates = [path for path in root.glob("story-index*.jsonl") if path.is_file()]
    if not candidates:
        return root / "story-index.jsonl"
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def discover_default_vntts_python(project_root=None):
    root = Path(project_root or Path(__file__).resolve().parents[1])
    executable = "python.exe" if os.name == "nt" else "python"
    candidates = (
        root.parent / "VisualNovelTextToSpeach" / ".venv" / "bin" / executable,
        root.parent / "VisualNovelTextToSpeach" / ".venv" / "Scripts" / executable,
    )
    return next(
        (path.absolute() for path in candidates if path.is_file()), candidates[0].absolute()
    )


def discover_default_voice_manifest(project_root=None):
    root = Path(project_root or Path(__file__).resolve().parents[1])
    return (
        root.parent / "VisualNovelTextToSpeach" / "data" / "reverse1999-voices" / "manifest.json"
    ).resolve()


def discover_default_moss_model(*, environment=None, cache_root=None, data_root=None):
    environment = os.environ if environment is None else environment
    configured = str(environment.get("VNTTS_MOSS_MODEL") or "").strip()
    if configured:
        return configured
    cache_root = Path(cache_root or user_cache_path("VNTTS", appauthor=False)).expanduser()
    data_root = Path(
        data_root or user_data_path("VisualNovelTextToSpeech", appauthor=False)
    ).expanduser()
    candidates = (
        cache_root / "models" / default_moss_model_directory_name,
        data_root / "models" / default_moss_model_directory_name,
    )
    for candidate in candidates:
        if (candidate / "config.json").is_file() and (candidate / "model.safetensors").is_file():
            return str(candidate.resolve())
    return default_moss_model_id


def load_narrator_voice_choices(manifest_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        _document, entries = load_voice_manifest(manifest_path)
    except VoiceManifestError as error:
        raise PregenerationError(str(error)) from error
    return tuple(
        sorted(
            (
                NarratorVoiceChoice(
                    character=entry.character,
                    speaker=entry.speaker,
                    aliases=entry.aliases,
                    references=tuple(
                        (manifest_path.parent / reference).resolve()
                        for reference in entry.references
                    ),
                )
                for entry in entries
                if entry.references
            ),
            key=lambda voice: voice.character.casefold(),
        )
    )


def _target_descriptor(record):
    source_kind = str(record.get("source_kind") or "story")
    chapter_text = str(record.get("chapter") or "")
    try:
        chapter = int(chapter_text)
    except ValueError:
        return None

    if source_kind == "story":
        story_group = chapter // 100
        if 1000 <= story_group <= 1019:
            number = story_group - 1000
            title = "Prologue" if number == 0 else f"Chapter {number}"
            return (
                f"main-story:{number}",
                "Main story",
                title,
                (0, number),
            )
        return (
            f"story-group:{story_group}",
            "Other story",
            f"Story group {story_group} ({story_group}xx)",
            (2, story_group),
        )

    story_title = str(record.get("story_title") or "").strip()
    if source_kind == "anecdote":
        group = str(record.get("story_group") or chapter)
        title = story_title or f"Anecdote {group}"
        return (f"anecdote:{group}", "Anecdotes", title, (1, title.casefold(), group))
    if source_kind == "hero_story_plot":
        title = story_title or f"Character story {chapter // 100}"
        digest = hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:12]
        return (
            f"hero-story:{digest}",
            "Anecdotes",
            title,
            (1, title.casefold(), chapter),
        )
    if source_kind == "activity_story":
        group = str(record.get("story_group") or chapter)
        title = story_title or f"Character story {group}"
        return (
            f"activity-story:{group}",
            "Character stories",
            title,
            (1, title.casefold(), group),
        )
    return None


def target_id_for_record(record):
    descriptor = _target_descriptor(record)
    return None if descriptor is None else descriptor[0]


def discover_pregeneration_targets(records):
    grouped = {}
    for record in records:
        if record.get("speakable", True) is False:
            continue
        descriptor = _target_descriptor(record)
        if descriptor is None:
            continue
        target_id, category, title, sort_key = descriptor
        item = grouped.setdefault(
            target_id,
            {
                "category": category,
                "title": title,
                "chapters": set(),
                "episode_titles": set(),
                "line_count": 0,
                "voices": set(),
                "all_voices": set(),
                "sort_key": sort_key,
            },
        )
        item["chapters"].add(str(record.get("chapter") or ""))
        episode = str(record.get("episode_title") or "").strip()
        if episode:
            item["episode_titles"].add(episode)
        voice = str(record.get("voice_character") or record.get("speaker") or "Narrator")
        item["all_voices"].add(voice)
        if record.get("audio_status") != "no_audio":
            continue
        item["line_count"] += 1
        item["voices"].add(voice)
    targets = [
        PregenerationTarget(
            target_id=target_id,
            category=item["category"],
            title=item["title"],
            chapters=tuple(sorted(item["chapters"], key=lambda value: int(value))),
            episode_titles=tuple(sorted(item["episode_titles"], key=str.casefold)),
            episode_count=len(item["episode_titles"] or item["chapters"]),
            line_count=item["line_count"],
            voice_count=len(item["voices"]),
            cast_count=len(item["all_voices"]),
            sort_key=item["sort_key"],
        )
        for target_id, item in grouped.items()
        if item["line_count"]
    ]
    return tuple(sorted(targets, key=lambda target: target.sort_key))


def records_for_targets(records, target_ids):
    selected = set(target_ids)
    return [record for record in records if target_id_for_record(record) in selected]


def _unique_job_directory(root, title, now=None):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    stem = f"{timestamp}-{slugify(title)[:48]}"
    candidate = root / stem
    suffix = 2
    while candidate.exists():
        candidate = root / f"{stem}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def create_pregeneration_job(
    story_index,
    targets,
    selected_target_ids,
    jobs_root=default_jobs_root,
    *,
    voice_manifest,
    vntts_python,
    model=None,
    narrator_character="Matilda",
    now=None,
):
    selected_ids = set(selected_target_ids)
    selected_targets = [target for target in targets if target.target_id in selected_ids]
    if not selected_targets:
        raise PregenerationError("Select at least one chapter or anecdote")
    _metadata, records = load_story_records(story_index)
    selected_records = records_for_targets(records, selected_ids)
    queue = build_generation_queue(
        selected_records,
        included_audio_statuses=("no_audio",),
    )
    if not queue:
        raise PregenerationError("The selected stories have no voiceless lines to generate")

    label = selected_targets[0].title
    if len(selected_targets) > 1:
        label = f"{label}-and-{len(selected_targets) - 1}-more"
    job_directory = _unique_job_directory(jobs_root, label, now=now)
    queue_path = job_directory / "queue.jsonl"
    chapters = sorted({int(chapter) for target in selected_targets for chapter in target.chapters})
    source_kinds = sorted(
        {str(record.get("source_kind") or "story") for record in selected_records}
    )
    write_generation_queue(
        queue,
        story_index,
        queue_path,
        source_kinds=source_kinds,
        included_audio_statuses=("no_audio",),
        chapter_ranges=tuple((chapter, chapter) for chapter in chapters),
    )
    job = {
        "schema": pregeneration_job_schema,
        "schema_version": pregeneration_job_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "ready",
        "title": ", ".join(target.title for target in selected_targets),
        "targets": [
            {
                "target_id": target.target_id,
                "category": target.category,
                "title": target.title,
                "chapters": list(target.chapters),
                "episode_count": target.episode_count,
                "line_count": target.line_count,
            }
            for target in selected_targets
        ],
        "story_index": str(Path(story_index).expanduser().resolve()),
        "queue": str(queue_path),
        "output": str(job_directory / "generated-audio"),
        "voice_manifest": str(Path(voice_manifest).expanduser().resolve()),
        "vntts_python": str(Path(vntts_python).expanduser().absolute()),
        "model": str(model or discover_default_moss_model()),
        "narrator_character": narrator_character,
    }
    atomic_write_json(job_directory / "job.json", job, sort_keys=True)
    return job_directory


def register_existing_job(
    queue,
    output,
    jobs_root=default_jobs_root,
    *,
    title,
    voice_manifest,
    vntts_python,
    model=None,
    narrator_character="Matilda",
    status="running",
):
    queue = Path(queue).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    try:
        metadata, _items = load_generation_queue(queue)
    except BulkGenerationError as error:
        raise PregenerationError(str(error)) from error
    digest = sha256_file(queue)[:12]
    jobs_root = Path(jobs_root).expanduser().resolve()
    jobs_root.mkdir(parents=True, exist_ok=True)
    job_directory = jobs_root / f"existing-{slugify(title)[:40]}-{digest}"
    job_directory.mkdir(exist_ok=True)
    job_path = job_directory / "job.json"
    if job_path.is_file():
        return job_directory
    job = {
        "schema": pregeneration_job_schema,
        "schema_version": pregeneration_job_version,
        "created_at": datetime.fromtimestamp(queue.stat().st_mtime, timezone.utc).isoformat(),
        "status": status,
        "title": title,
        "targets": [],
        "story_index": metadata.get("source_story_index"),
        "queue": str(queue),
        "output": str(output),
        "voice_manifest": str(Path(voice_manifest).expanduser().resolve()),
        "vntts_python": str(Path(vntts_python).expanduser().absolute()),
        "model": str(model or discover_default_moss_model()),
        "narrator_character": narrator_character,
        "registered_existing_job": True,
    }
    atomic_write_json(job_path, job, sort_keys=True)
    return job_directory


def load_pregeneration_job(job_directory):
    job_directory = Path(job_directory).expanduser().resolve()
    try:
        job = json.loads((job_directory / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PregenerationError(
            f"Unable to read pregeneration job {job_directory}: {error}"
        ) from error
    if (
        job.get("schema") != pregeneration_job_schema
        or job.get("schema_version") != pregeneration_job_version
    ):
        raise PregenerationError(f"Unsupported pregeneration job: {job_directory}")
    return job


def update_job_status(job_directory, status, **fields):
    job_directory = Path(job_directory).expanduser().resolve()
    job = load_pregeneration_job(job_directory)
    job["status"] = status
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    if status == "running":
        fields.setdefault("exit_code", None)
    job.update(fields)
    atomic_write_json(job_directory / "job.json", job, sort_keys=True)
    return job


def process_is_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def resolve_job_runtime_status(job, *, local_running=False, process_checker=process_is_alive):
    recorded = str(job.get("status") or "ready")
    if recorded != "running":
        return recorded
    if local_running:
        return "running_here"
    if process_checker(job.get("pid")):
        return "running_external"
    return "interrupted"


def _available_voice_names(manifest_path):
    try:
        document = json.loads(Path(manifest_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PregenerationError(
            f"Unable to read voice manifest {manifest_path}: {error}"
        ) from error
    available = set()
    for voice in document.get("voices", []):
        if not voice.get("references"):
            continue
        for name in (voice.get("character"), *voice.get("aliases", [])):
            if name:
                available.add(normalize_name(str(name)))
    return available


def queue_eligibility(items, voice_manifest, narrator_character="Matilda"):
    available = _available_voice_names(voice_manifest)
    narrator_available = normalize_name(narrator_character) in available
    eligible_ids = set()
    missing_names = set()
    skipped_sound_effects = 0
    for item in items:
        if item.get("action") != "generate":
            continue
        character = str(item.get("voice_character") or "Narrator")
        has_voice = (
            narrator_available
            if character == "Narrator"
            else normalize_name(character) in available
        )
        if not has_voice:
            missing_names.add(character)
            continue
        if not is_spoken_item(item):
            skipped_sound_effects += 1
            continue
        eligible_ids.add(item["queue_id"])
    skipped_missing = sum(
        item.get("action") == "generate"
        and (
            not narrator_available
            if str(item.get("voice_character") or "Narrator") == "Narrator"
            else normalize_name(str(item.get("voice_character") or "Narrator")) not in available
        )
        for item in items
    )
    return (
        eligible_ids,
        skipped_missing,
        skipped_sound_effects,
        tuple(sorted(missing_names, key=str.casefold)),
    )


def read_generation_progress(job_directory):
    job_directory = Path(job_directory).expanduser().resolve()
    job = load_pregeneration_job(job_directory)
    try:
        _metadata, items = load_generation_queue(job["queue"])
        eligible_ids, skipped_missing, skipped_sfx, missing_names = queue_eligibility(
            items,
            job["voice_manifest"],
            job.get("narrator_character", "Matilda"),
        )
    except (BulkGenerationError, PregenerationError) as error:
        raise PregenerationError(str(error)) from error

    state_path = Path(job["output"]) / "generation-state.json"
    state_document = {}
    state_items = {}
    if state_path.is_file():
        try:
            state_document = generation_state_codec.load(state_path)
            state_items = state_document.get("items", {})
        except Exception as error:
            raise PregenerationError(f"Unable to read generation state: {error}") from error
    relevant = {key: value for key, value in state_items.items() if key in eligible_ids}
    generated = sum(value.get("status") in {"generated", "approved"} for value in relevant.values())
    failed = sum(value.get("status") == "failed" for value in relevant.values())
    pending = max(0, len(eligible_ids) - generated - failed)
    latest_line = None
    latest_text = None
    updated_at = None
    if relevant:
        queue_by_id = {item["queue_id"]: item for item in items}
        latest_id, latest = max(
            relevant.items(), key=lambda pair: str(pair[1].get("updated_at") or "")
        )
        source = queue_by_id.get(latest_id, {})
        latest_line = str(source.get("line_id") or latest.get("line_id") or "") or None
        latest_text = str(source.get("text") or "") or None
        updated_at = str(latest.get("updated_at") or "") or None

    active = state_document.get("active")
    if not isinstance(active, dict) or active.get("queue_id") not in eligible_ids:
        active = {}
    active_line = str(active.get("line_id") or "") or None
    active_text = str(active.get("text") or "") or None
    active_speaker = str(
        active.get("speaker") or active.get("voice_character") or ""
    ) or None
    active_phase = str(active.get("phase") or "") or None
    active_attempt = active.get("attempt")
    active_attempt_limit = active.get("attempt_limit")
    active_started_at = str(active.get("started_at") or "") or None
    active_last_error = str(active.get("last_error") or "") or None
    if active.get("updated_at"):
        updated_at = str(active["updated_at"])

    recorded_status = str(job.get("status") or "ready")
    session_started = None
    if recorded_status == "running" and job.get("updated_at"):
        try:
            session_started = datetime.fromisoformat(job["updated_at"])
            if session_started.tzinfo is None:
                session_started = session_started.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            session_started = None

    completed_times = []
    for value in relevant.values():
        if value.get("status") not in {"generated", "approved"} or not value.get("updated_at"):
            continue
        try:
            completed_at = datetime.fromisoformat(value["updated_at"])
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            if session_started is None or completed_at >= session_started:
                completed_times.append(completed_at)
        except (TypeError, ValueError):
            continue
    completed_times.sort()
    recent_times = completed_times[-20:]
    rate_per_minute = None
    eta_seconds = None
    if len(recent_times) >= 2:
        elapsed = (recent_times[-1] - recent_times[0]).total_seconds()
        if elapsed > 0:
            rate_per_minute = round((len(recent_times) - 1) * 60 / elapsed, 2)
            if pending and rate_per_minute > 0:
                eta_seconds = round(pending * 60 / rate_per_minute)

    if generated == len(eligible_ids) and failed == 0:
        status = "incomplete" if skipped_missing else "complete"
    elif recorded_status == "running":
        status = "running"
    elif failed:
        status = "failed"
    elif generated:
        status = "paused"
    else:
        status = recorded_status
    return GenerationProgress(
        status=status,
        generated=generated,
        failed=failed,
        pending=pending,
        eligible=len(eligible_ids),
        skipped_missing_voice=skipped_missing,
        skipped_sound_effects=skipped_sfx,
        missing_voice_names=missing_names,
        latest_line=latest_line,
        latest_text=latest_text,
        updated_at=updated_at,
        rate_per_minute=rate_per_minute,
        eta_seconds=eta_seconds,
        active_line=active_line,
        active_text=active_text,
        active_speaker=active_speaker,
        active_phase=active_phase,
        active_attempt=active_attempt,
        active_attempt_limit=active_attempt_limit,
        active_started_at=active_started_at,
        active_last_error=active_last_error,
    )


def discover_jobs(jobs_root=default_jobs_root):
    root = Path(jobs_root).expanduser().resolve()
    if not root.is_dir():
        return ()
    jobs = [path.parent for path in root.glob("*/job.json")]
    return tuple(sorted(jobs, key=lambda path: path.name, reverse=True))


def generation_command(job_directory):
    job = load_pregeneration_job(job_directory)
    command = [
        job["vntts_python"],
        "-m",
        "r1999extractor.moss_generation",
        "--queue",
        job["queue"],
        "--voice-manifest",
        job["voice_manifest"],
        "--narrator-character",
        job.get("narrator_character", "Matilda"),
        "--output",
        job["output"],
    ]
    model = str(job.get("model") or discover_default_moss_model()).strip()
    if model:
        command.extend(("--model", model))
    return tuple(command)


__all__ = [
    "GenerationProgress",
    "NarratorVoiceChoice",
    "PregenerationError",
    "PregenerationTarget",
    "create_pregeneration_job",
    "discover_default_story_index",
    "discover_default_moss_model",
    "discover_default_vntts_python",
    "discover_default_voice_manifest",
    "discover_jobs",
    "discover_pregeneration_targets",
    "generation_command",
    "load_pregeneration_job",
    "load_narrator_voice_choices",
    "read_generation_progress",
    "register_existing_job",
    "records_for_targets",
    "resolve_job_runtime_status",
    "target_id_for_record",
    "update_job_status",
]
