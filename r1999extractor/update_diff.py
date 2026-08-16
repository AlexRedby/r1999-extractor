import argparse
import json
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from r1999extractor.cli import cli_error
from r1999extractor.settings import get_local_data_directory

report_schema = "r1999.update-diff"
report_schema_version = 1
default_output = get_local_data_directory() / "reverse1999" / "update-diff.json"


class UpdateDiffError(RuntimeError):
    pass


def load_raw_story_index(path):
    path = Path(path).expanduser().resolve()
    try:
        rows = [
            json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateDiffError(f"Unable to read story index {path}: {error}") from error
    if not rows or not isinstance(rows[0], dict) or rows[0].get("record_type") != "metadata":
        raise UpdateDiffError(f"Story index must begin with metadata: {path}")

    records = {}
    for row_number, record in enumerate(rows[1:], start=2):
        if not isinstance(record, dict) or record.get("record_type") != "line":
            raise UpdateDiffError(f"Invalid line record at {path}:{row_number}")
        line_id = record.get("line_id")
        if not isinstance(line_id, str) or not line_id.strip():
            raise UpdateDiffError(f"Missing line ID at {path}:{row_number}")
        line_id = line_id.strip()
        if line_id in records:
            raise UpdateDiffError(f"Duplicate line ID {line_id!r} in {path}")
        records[line_id] = record

    declared_count = rows[0].get("line_count")
    if isinstance(declared_count, int) and declared_count != len(records):
        raise UpdateDiffError(
            f"Story-index line count mismatch in {path}: "
            f"metadata says {declared_count}, read {len(records)}"
        )
    return path, rows[0], records


def compare_story_indexes(before, after):
    before_path, before_metadata, before_records = load_raw_story_index(before)
    after_path, after_metadata, after_records = load_raw_story_index(after)
    before_ids = set(before_records)
    after_ids = set(after_records)
    common_ids = before_ids & after_ids

    new_line_ids = sorted(after_ids - before_ids)
    removed_line_ids = sorted(before_ids - after_ids)
    changed_line_ids = sorted(
        line_id for line_id in common_ids if before_records[line_id] != after_records[line_id]
    )
    speaker_mapping_changes = [
        {
            "line_id": line_id,
            "before": _speaker_mapping(before_records[line_id]),
            "after": _speaker_mapping(after_records[line_id]),
        }
        for line_id in sorted(common_ids)
        if _speaker_mapping(before_records[line_id]) != _speaker_mapping(after_records[line_id])
    ]

    before_unresolved = {
        line_id for line_id, record in before_records.items() if _is_unresolved(record)
    }
    after_unresolved = {
        line_id for line_id, record in after_records.items() if _is_unresolved(record)
    }
    before_eligible = {
        line_id for line_id, record in before_records.items() if is_synthesis_eligible(record)
    }
    after_eligible = {
        line_id for line_id, record in after_records.items() if is_synthesis_eligible(record)
    }

    return {
        "schema": report_schema,
        "schema_version": report_schema_version,
        "before": _index_summary(before_path, before_metadata, before_records),
        "after": _index_summary(after_path, after_metadata, after_records),
        "line_changes": {
            "new_line_ids": new_line_ids,
            "removed_line_ids": removed_line_ids,
            "changed_line_ids": changed_line_ids,
        },
        "schema_drift": _schema_drift(
            before_metadata,
            before_records,
            after_metadata,
            after_records,
        ),
        "speaker_mapping_changes": speaker_mapping_changes,
        "unresolved_audio": {
            "before_count": len(before_unresolved),
            "after_count": len(after_unresolved),
            "delta": len(after_unresolved) - len(before_unresolved),
            "spike": len(after_unresolved) > len(before_unresolved),
            "newly_unresolved_line_ids": sorted(
                (after_unresolved - before_unresolved) & common_ids
            ),
            "resolved_line_ids": sorted((before_unresolved - after_unresolved) & common_ids),
        },
        "synthesis_eligibility": {
            "before_count": len(before_eligible),
            "after_count": len(after_eligible),
            "delta": len(after_eligible) - len(before_eligible),
            "became_eligible_line_ids": sorted(after_eligible - before_eligible),
            "became_ineligible_line_ids": sorted(before_eligible - after_eligible),
        },
    }


def is_synthesis_eligible(record):
    """Return source-only eligibility without creating a synthesis queue."""
    if record.get("speakable", True) is False:
        return False
    extractor_status = str(record.get("audio_status") or "").strip()
    canonical_status = str(record.get("source_audio_status") or "").strip()
    return extractor_status != "installed" and canonical_status != "available"


def write_update_diff(report, output=default_output):
    return atomic_write_json(output, report, sort_keys=True)


def format_summary(report, output):
    changes = report["line_changes"]
    unresolved = report["unresolved_audio"]
    eligibility = report["synthesis_eligibility"]
    speaker_changes = report["speaker_mapping_changes"]
    drift = "yes" if report["schema_drift"]["changed"] else "no"
    spike = " spike" if unresolved["spike"] else ""
    return (
        f"Compared {report['before']['line_count']} -> {report['after']['line_count']} lines: "
        f"+{len(changes['new_line_ids'])} new, -{len(changes['removed_line_ids'])} removed, "
        f"{len(changes['changed_line_ids'])} changed; "
        f"{len(speaker_changes)} speaker mapping changes; "
        f"unresolved {unresolved['before_count']} -> {unresolved['after_count']}"
        f" ({unresolved['delta']:+d}{spike}); "
        f"synthesis-eligible {eligibility['before_count']} -> {eligibility['after_count']} "
        f"({eligibility['delta']:+d}); schema drift: {drift}. Report: {output}"
    )


def _speaker_mapping(record):
    return {
        "speaker": str(record.get("speaker") or ""),
        "voice_character": str(record.get("voice_character") or record.get("speaker") or ""),
    }


def _is_unresolved(record):
    return str(record.get("audio_status") or "").strip() == "unresolved"


def _index_summary(path, metadata, records):
    return {
        "path": str(path),
        "story_schema": metadata.get("schema"),
        "story_schema_version": metadata.get("schema_version"),
        "line_count": len(records),
    }


def _schema_shape(metadata, records):
    collections = metadata.get("collections")
    collection_fields = set()
    if isinstance(collections, list):
        for collection in collections:
            if isinstance(collection, dict):
                collection_fields.update(collection)
    line_fields = set()
    for record in records.values():
        line_fields.update(record)
    return {
        "story_schema": metadata.get("schema"),
        "story_schema_version": metadata.get("schema_version"),
        "metadata_fields": sorted(metadata),
        "line_fields": sorted(line_fields),
        "collection_fields": sorted(collection_fields),
    }


def _schema_drift(before_metadata, before_records, after_metadata, after_records):
    before_shape = _schema_shape(before_metadata, before_records)
    after_shape = _schema_shape(after_metadata, after_records)
    field_changes = {}
    for name in ("metadata_fields", "line_fields", "collection_fields"):
        before_fields = set(before_shape[name])
        after_fields = set(after_shape[name])
        field_changes[name] = {
            "added": sorted(after_fields - before_fields),
            "removed": sorted(before_fields - after_fields),
        }
    return {
        "changed": before_shape != after_shape,
        "before": before_shape,
        "after": after_shape,
        "field_changes": field_changes,
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description="Compare two Reverse: 1999 story indexes without building a synthesis queue."
    )
    parser.add_argument("before", type=Path, help="Older story-index JSONL")
    parser.add_argument("after", type=Path, help="Newer story-index JSONL")
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        report = compare_story_indexes(options.before, options.after)
        output = write_update_diff(report, options.output)
    except (OSError, UpdateDiffError) as error:
        return cli_error(error)
    print(format_summary(report, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
