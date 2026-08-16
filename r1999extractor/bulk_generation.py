import argparse
import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    Pcm16MonoWavError,
    read_pcm16_mono_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.text_utils import slugify

from r1999extractor.cli import cli_error, cli_success
from r1999extractor.settings import get_local_data_directory
from r1999extractor.versioned_json import VersionedJSONCodec, VersionedJSONError

generation_state_schema = "r1999.bulk-generation-state"
generation_state_version = 1
generation_state_codec = VersionedJSONCodec(
    generation_state_schema, generation_state_version, "generation state"
)
default_output = get_local_data_directory() / "reverse1999" / "generated-audio"
default_queue = get_local_data_directory() / "reverse1999" / "generation-queue.jsonl"


class BulkGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioQuality:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_count: int
    peak: float


def inspect_generated_wav(path):
    path = Path(path)
    try:
        _samples, info = read_pcm16_mono_wav(path)
    except Pcm16MonoWavError as error:
        raise BulkGenerationError(f"Generated output is not a readable WAV: {error}") from error
    if info.sample_rate < 16000:
        raise BulkGenerationError("Generated WAV must be 16-bit mono at 16 kHz or higher")
    duration = info.duration_seconds
    if duration < 0.1 or duration > 180:
        raise BulkGenerationError(f"Generated WAV duration is implausible: {duration:.2f}s")
    peak = info.peak
    if peak < 0.001:
        raise BulkGenerationError("Generated WAV is effectively silent")
    if peak >= 1.0:
        raise BulkGenerationError("Generated WAV is clipped")
    return AudioQuality(round(duration, 4), info.sample_rate, 1, info.sample_count, round(peak, 6))


class CommandProvider:
    def __init__(self, command, *, provider, model):
        self.command = tuple(shlex.split(command))
        if not self.command:
            raise BulkGenerationError("Provider command cannot be empty")
        self.provider = provider
        self.model = model

    def generate(self, item, output, *, seed):
        output = Path(output).resolve()
        request = output.with_suffix(".request.json")
        atomic_write_json(
            request,
            {
                "text": item["text"],
                "speaker": item["voice_character"],
                "seed": seed,
                "emotion": item.get("emotion"),
                "delivery": item.get("delivery"),
                "prompt_adapters": item.get("prompt_adapters"),
            },
            sort_keys=True,
        )
        replacements = {
            "{request}": str(request),
            "{output}": str(output),
            "{text}": item["text"],
            "{seed}": str(seed),
        }
        command = [replacements.get(argument, argument) for argument in self.command]
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise BulkGenerationError(f"Provider command failed: {error}") from error
        finally:
            request.unlink(missing_ok=True)


def load_generation_queue(path):
    path = Path(path).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as stream:
            metadata = json.loads(next(stream))
            items = [json.loads(row) for row in stream]
    except (OSError, StopIteration, json.JSONDecodeError) as error:
        raise BulkGenerationError(f"Unable to read generation queue {path}: {error}") from error
    if (
        metadata.get("schema") != "vntts.voice-generation-queue"
        or metadata.get("schema_version") != 1
    ):
        raise BulkGenerationError("Unsupported voice generation queue")
    if metadata.get("item_count") != len(items):
        raise BulkGenerationError("Generation queue count does not match metadata")
    return metadata, items


def _load_state(path, queue_sha256):
    path = Path(path)
    if not path.is_file():
        return generation_state_codec.new(queue_sha256=queue_sha256, items={})
    try:
        state = generation_state_codec.load(path)
    except VersionedJSONError as error:
        raise BulkGenerationError(str(error)) from error
    if state.get("queue_sha256") != queue_sha256:
        raise BulkGenerationError("Generation queue changed; start a new output directory")
    if not isinstance(state.get("items"), dict):
        raise BulkGenerationError("Generation state items must be an object")
    return state


def _write_active_attempt(
    state_path,
    state,
    item,
    *,
    phase,
    attempt,
    attempt_limit,
    total_attempts,
    seed,
    started_at,
    last_error=None,
):
    state["active"] = {
        "queue_id": item["queue_id"],
        "line_id": item["line_id"],
        "speaker": item.get("speaker"),
        "voice_character": item.get("voice_character"),
        "text": item.get("text"),
        "phase": phase,
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "total_attempts": total_attempts,
        "seed": seed,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_error": last_error,
    }
    generation_state_codec.write(state_path, state, sort_keys=True)


def run_bulk_generation(
    queue_path,
    output_directory,
    provider,
    *,
    limit=None,
    retries=2,
    include_prefer_source=False,
    include_characters=None,
    item_filter=None,
    seed=0,
):
    queue_path = Path(queue_path).expanduser().resolve()
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    state_path = output_directory / "generation-state.json"
    manifest_path = output_directory / "manifest.json"
    queue_sha256 = sha256_file(queue_path)
    queue_metadata, items = load_generation_queue(queue_path)
    state = _load_state(state_path, queue_sha256)
    state.setdefault("game", queue_metadata.get("game"))
    state.setdefault("language", queue_metadata.get("language"))
    eligible_actions = {"generate"}
    if include_prefer_source:
        eligible_actions.add("prefer_source_audio")
    candidates = [item for item in items if item.get("action") in eligible_actions]
    character_filter = None if include_characters is None else set(include_characters)
    skipped_characters = 0
    if character_filter is not None:
        filtered = [item for item in candidates if item.get("voice_character") in character_filter]
        skipped_characters = len(candidates) - len(filtered)
        candidates = filtered
    skipped_items = 0
    if item_filter is not None:
        filtered = [item for item in candidates if item_filter(item)]
        skipped_items = len(candidates) - len(filtered)
        candidates = filtered
    if limit is not None:
        candidates = candidates[:limit]
    generated = 0
    for item in candidates:
        queue_id = item["queue_id"]
        existing = state["items"].get(queue_id, {})
        existing_path = output_directory / existing.get("path", "")
        if existing.get("status") in {"generated", "approved"} and existing_path.is_file():
            continue
        prompt = item.get("prompt_adapters", {}).get("generic", "")
        prompt_sha256 = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
        relative = (
            Path("audio")
            / slugify(item["voice_character"])
            / (hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24] + ".wav")
        )
        destination = output_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        attempts = int(existing.get("attempts", 0))
        run_attempts = 0
        last_error = None
        while run_attempts <= retries:
            attempts += 1
            run_attempts += 1
            attempt_seed = int(seed) + attempts - 1
            attempt_started_at = datetime.now(timezone.utc).isoformat()
            temporary = destination.with_suffix(".partial.wav")
            temporary.unlink(missing_ok=True)
            _write_active_attempt(
                state_path,
                state,
                item,
                phase="generating",
                attempt=run_attempts,
                attempt_limit=retries + 1,
                total_attempts=attempts,
                seed=attempt_seed,
                started_at=attempt_started_at,
                last_error=last_error,
            )
            try:
                provider.generate(item, temporary, seed=attempt_seed)
                _write_active_attempt(
                    state_path,
                    state,
                    item,
                    phase="validating",
                    attempt=run_attempts,
                    attempt_limit=retries + 1,
                    total_attempts=attempts,
                    seed=attempt_seed,
                    started_at=attempt_started_at,
                    last_error=last_error,
                )
                quality = inspect_generated_wav(temporary)
                os.replace(temporary, destination)
                state["items"][queue_id] = {
                    "status": "generated",
                    "review_status": "pending_review",
                    "attempts": attempts,
                    "path": relative.as_posix(),
                    "line_id": item["line_id"],
                    "text_sha256": item["text_sha256"],
                    "file_sha256": sha256_file(destination),
                    "provider": provider.provider,
                    "model": provider.model,
                    "prompt_sha256": prompt_sha256,
                    "seed": attempt_seed,
                    "quality": asdict(quality),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                state["active"] = None
                generation_state_codec.write(state_path, state, sort_keys=True)
                generated += 1
                break
            except (BulkGenerationError, OSError) as error:
                temporary.unlink(missing_ok=True)
                last_error = str(error)
                state["items"][queue_id] = {
                    "status": "failed",
                    "attempts": attempts,
                    "seed": attempt_seed,
                    "last_error": last_error,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if run_attempts <= retries:
                    state["active"] = {
                        **state["active"],
                        "phase": "retrying",
                        "last_error": last_error,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    state["active"] = None
                generation_state_codec.write(state_path, state, sort_keys=True)
        if state["items"][queue_id].get("status") == "failed":
            continue
    publish_generated_manifest(state, manifest_path)
    return {
        "generated": generated,
        "failed": sum(value.get("status") == "failed" for value in state["items"].values()),
        "skipped_characters": skipped_characters,
        "skipped_items": skipped_items,
        "manifest": manifest_path,
        "state": state_path,
    }


def publish_generated_manifest(state, path):
    entries = []
    for queue_id, result in state["items"].items():
        if result.get("status") != "approved" or result.get("review_status") != "approved":
            continue
        entries.append(
            {
                "queue_id": queue_id,
                "line_id": result["line_id"],
                "text_sha256": result["text_sha256"],
                "audio": result["path"],
                "audio_format": PCM16_MONO_WAV_FORMAT,
                "audio_sha256": result["file_sha256"],
                "sample_rate": result["quality"]["sample_rate"],
                "sample_count": result["quality"]["sample_count"],
                "provider": result["provider"],
                "model": result["model"],
                "prompt_sha256": result["prompt_sha256"],
                "seed": result.get("seed"),
                "review_status": result.get("review_status", "pending_review"),
            }
        )
    entries.sort(key=lambda entry: (entry["line_id"], entry["text_sha256"]))
    write_generated_audio_manifest(
        path,
        {
            "game": state.get("game"),
            "language": state.get("language"),
            "source_queue_sha256": state["queue_sha256"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        entries,
    )
    return Path(path)


def review_item(state_path, queue_id, decision):
    if decision not in {"approved", "rejected"}:
        raise BulkGenerationError("Review decision must be approved or rejected")
    state_path = Path(state_path).expanduser().resolve()
    try:
        state = generation_state_codec.load(state_path)
    except VersionedJSONError as error:
        raise BulkGenerationError(str(error)) from error
    item = state.get("items", {}).get(queue_id)
    if item is None or item.get("status") not in {"generated", "approved"}:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    item["review_status"] = decision
    item["status"] = "approved" if decision == "approved" else "generated"
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    generation_state_codec.write(state_path, state, sort_keys=True)
    publish_generated_manifest(state, state_path.parent / "manifest.json")
    return state


def create_parser():
    parser = argparse.ArgumentParser(description="Run resumable ahead-of-time voice generation.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--queue", type=Path, default=default_queue)
    generate.add_argument("--output", type=Path, default=default_output)
    generate.add_argument("--provider", required=True)
    generate.add_argument("--model", required=True)
    generate.add_argument("--provider-command", required=True)
    generate.add_argument("--limit", type=int)
    generate.add_argument("--retries", type=int, default=2)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--include-prefer-source", action="store_true")
    generate.add_argument(
        "--character",
        action="append",
        dest="include_characters",
        help="Generate only this voice character; repeat to include more characters.",
    )
    review = subparsers.add_parser("review")
    review.add_argument("--state", type=Path, default=default_output / "generation-state.json")
    review.add_argument("queue_id")
    review.add_argument("decision", choices=("approved", "rejected"))
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        if options.command == "review":
            review_item(options.state, options.queue_id, options.decision)
            return cli_success(f"Marked {options.queue_id} as {options.decision}")
        provider = CommandProvider(
            options.provider_command,
            provider=options.provider,
            model=options.model,
        )
        result = run_bulk_generation(
            options.queue,
            options.output,
            provider,
            limit=options.limit,
            retries=options.retries,
            include_prefer_source=options.include_prefer_source,
            include_characters=options.include_characters,
            seed=options.seed,
        )
    except (BulkGenerationError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)
    print(
        f"Generated {result['generated']} item(s), {result['failed']} failed, "
        f"{result['skipped_characters']} skipped by character filter; "
        f"{result['skipped_items']} skipped by item filter; "
        f"manifest: {result['manifest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
