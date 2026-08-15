import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_output_path
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.hashing import text_sha256

from r1999extractor.cli import cli_error
from r1999extractor.delivery import annotate_delivery, delivery_annotation_version
from r1999extractor.settings import get_local_data_directory
from r1999extractor.story_audio import audio_statuses
from r1999extractor.story_index import default_output as default_story_index

queue_schema = "vntts.voice-generation-queue"
queue_schema_version = 1
default_output = get_local_data_directory() / "reverse1999" / "generation-queue.jsonl"


class GenerationQueueError(RuntimeError):
    pass


def _required_text(record, name):
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GenerationQueueError(f"Story line {name} must be non-empty text")
    return value.strip()


def load_story_records(path):
    path = Path(path).expanduser().resolve()
    try:
        stream = path.open(encoding="utf-8")
    except OSError as error:
        raise GenerationQueueError(f"Unable to open story index {path}: {error}") from error
    with stream:
        try:
            metadata = json.loads(next(stream))
        except StopIteration as error:
            raise GenerationQueueError(f"Story index is empty: {path}") from error
        except json.JSONDecodeError as error:
            raise GenerationQueueError(f"Invalid story-index metadata: {error}") from error
        if not isinstance(metadata, dict) or metadata.get("record_type") != "metadata":
            raise GenerationQueueError("Story index must begin with a metadata record")
        if metadata.get("schema") != "vntts.story-index" or metadata.get("schema_version") != 1:
            raise GenerationQueueError("Unsupported story-index schema")

        records = []
        seen = set()
        for row_number, row in enumerate(stream, start=2):
            try:
                record = json.loads(row)
            except json.JSONDecodeError as error:
                raise GenerationQueueError(
                    f"Invalid story-index record at {path}:{row_number}: {error}"
                ) from error
            if not isinstance(record, dict) or record.get("record_type") != "line":
                raise GenerationQueueError(
                    f"Invalid story-index record at {path}:{row_number}: expected a line"
                )
            line_id = _required_text(record, "line_id")
            if line_id in seen:
                raise GenerationQueueError(f"Duplicate story line ID: {line_id}")
            seen.add(line_id)
            records.append(record)
    if isinstance(metadata.get("line_count"), int) and metadata["line_count"] != len(records):
        raise GenerationQueueError("Story-index line count does not match its metadata")
    return metadata, records


def generation_action(audio_status):
    return {
        "no_audio": "generate",
        "configured_unavailable": "prefer_source_audio",
        "unresolved": "manual_review",
        "unchecked": "resolve_audio",
    }.get(audio_status, "manual_review")


def build_generation_queue(
    records,
    *,
    source_kinds=None,
    included_audio_statuses=None,
    chapter_ranges=None,
):
    source_kind_filter = None if source_kinds is None else set(source_kinds)
    audio_status_filter = None if included_audio_statuses is None else set(included_audio_statuses)
    chapter_range_filter = None if chapter_ranges is None else tuple(chapter_ranges)
    queue = []
    for record in records:
        if record.get("speakable", True) is False:
            continue
        source_kind = str(record.get("source_kind") or "story")
        if source_kind_filter is not None and source_kind not in source_kind_filter:
            continue
        if chapter_range_filter is not None:
            try:
                chapter = int(record.get("chapter"))
            except (TypeError, ValueError):
                continue
            if not any(start <= chapter <= end for start, end in chapter_range_filter):
                continue
        audio_status = str(record.get("audio_status") or "unchecked")
        if audio_status_filter is not None and audio_status not in audio_status_filter:
            continue
        if audio_status == "installed":
            continue
        if audio_status not in audio_statuses and audio_status != "unchecked":
            raise GenerationQueueError(f"Unsupported audio status: {audio_status}")
        line_id = _required_text(record, "line_id")
        text = _required_text(record, "text")
        declared_text_hash = str(record.get("text_sha256") or "").strip()
        calculated_hash = text_sha256(text)
        if declared_text_hash and declared_text_hash != calculated_hash:
            raise GenerationQueueError(f"Text hash does not match line {line_id}")
        text_hash = calculated_hash
        voice_character = str(
            record.get("voice_character") or record.get("speaker") or "Narrator"
        ).strip()
        item = {
            "record_type": "generation_item",
            "queue_id": f"{line_id}:{text_hash[:16]}",
            "line_id": line_id,
            "text_sha256": text_hash,
            "speaker": _required_text(record, "speaker"),
            "voice_character": voice_character,
            "text": text,
            "kind": str(record.get("kind") or "dialogue"),
            "previous_text": record.get("previous_text"),
            "next_text": record.get("next_text"),
            "source_kind": source_kind,
            "story_group": record.get("story_group"),
            "story_title": record.get("story_title"),
            "episode_title": record.get("episode_title"),
            "chapter": str(record.get("chapter") or ""),
            "sequence": int(record.get("sequence", 0)),
            "story_order": record.get("story_order"),
            "source_audio_status": audio_status,
            "source_audio_reason": str(record.get("audio_reason") or "not_resolved"),
            "action": generation_action(audio_status),
            "state": "pending",
        }
        item.update(
            annotate_delivery(
                text,
                speaker=item["speaker"],
                previous_text=item["previous_text"],
                next_text=item["next_text"],
                kind=item["kind"],
            )
        )
        queue.append(item)
    queue.sort(
        key=lambda item: (
            item["voice_character"].casefold(),
            item["source_kind"],
            str(item["story_group"] or ""),
            item["story_order"] if isinstance(item["story_order"], int) else 2**63,
            item["chapter"],
            item["sequence"],
            item["line_id"],
        )
    )
    return queue


def write_generation_queue(
    queue,
    story_index,
    output=default_output,
    *,
    source_kinds=None,
    included_audio_statuses=None,
    chapter_ranges=None,
):
    story_index = Path(story_index).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    metadata = {
        "record_type": "metadata",
        "schema": queue_schema,
        "schema_version": queue_schema_version,
        "game": "Reverse: 1999",
        "language": "en",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_story_index": str(story_index),
        "source_story_index_sha256": sha256_file(story_index),
        "item_count": len(queue),
        "character_count": len({item["voice_character"] for item in queue}),
        "source_audio_status_counts": dict(
            sorted(Counter(item["source_audio_status"] for item in queue).items())
        ),
        "action_counts": dict(sorted(Counter(item["action"] for item in queue).items())),
        "source_kind_counts": dict(sorted(Counter(item["source_kind"] for item in queue).items())),
        "delivery_annotation_version": delivery_annotation_version,
    }
    if (
        source_kinds is not None
        or included_audio_statuses is not None
        or chapter_ranges is not None
    ):
        metadata["filters"] = {
            "source_kinds": sorted(set(source_kinds or ())),
            "audio_statuses": sorted(set(included_audio_statuses or ())),
            "chapter_ranges": [f"{start}:{end}" for start, end in chapter_ranges or ()],
        }
    with atomic_output_path(output) as temporary:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
            for item in queue:
                stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return output, metadata


def parse_chapter_range(value):
    try:
        start_text, end_text = value.split(":", 1)
        start = int(start_text)
        end = int(end_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "chapter range must use integer START:END format"
        ) from error
    if start > end:
        raise argparse.ArgumentTypeError("chapter range start must not exceed its end")
    return start, end


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Build a versioned, character-grouped voice pregeneration queue from every "
            "story line without installed source audio."
        )
    )
    parser.add_argument("--story-index", type=Path, default=default_story_index)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument(
        "--source-kind",
        action="append",
        dest="source_kinds",
        help="Include only this source kind; repeat to include multiple kinds.",
    )
    parser.add_argument(
        "--audio-status",
        action="append",
        dest="included_audio_statuses",
        choices=sorted(audio_statuses | {"unchecked"}),
        help="Include only this source-audio status; repeat to include multiple statuses.",
    )
    parser.add_argument(
        "--chapter-range",
        action="append",
        dest="chapter_ranges",
        type=parse_chapter_range,
        help="Include only numeric chapters in inclusive START:END; repeat for more ranges.",
    )
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        _metadata, records = load_story_records(options.story_index)
        queue = build_generation_queue(
            records,
            source_kinds=options.source_kinds,
            included_audio_statuses=options.included_audio_statuses,
            chapter_ranges=options.chapter_ranges,
        )
        output, metadata = write_generation_queue(
            queue,
            options.story_index,
            options.output,
            source_kinds=options.source_kinds,
            included_audio_statuses=options.included_audio_statuses,
            chapter_ranges=options.chapter_ranges,
        )
    except (GenerationQueueError, OSError) as error:
        return cli_error(error)
    actions = ", ".join(f"{key}={value}" for key, value in metadata["action_counts"].items())
    print(
        f"Wrote {metadata['item_count']} items for {metadata['character_count']} "
        f"characters ({actions}) to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
