"""Build checksum-bound audition sets from installed same-speaker story audio."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_manifest import normalize_character_name

from r1999extractor.playable_voice import (
    PlayableVoiceError,
    resolve_story_index_path,
    validate_bank_index_document,
)
from r1999extractor.reverse1999_index import default_output as default_bank_index
from r1999extractor.reverse1999_voice_import import (
    GameVoiceImportError,
    decode_reference_data,
)
from r1999extractor.story_audio import wwise_event_id
from r1999extractor.voice_reference_quality import (
    VoiceReferenceQualityError,
    analyze_voice_reference,
)
from r1999extractor.wwise import (
    AudioConversionError,
    WwiseBankError,
    extract_embedded_media,
    inspect_bank_data,
    resolve_decoder,
)

REPORT_SCHEMA = "r1999.story-voice-reference-candidates"
REPORT_VERSION = 2
SUPPORTED_REPORT_VERSIONS = frozenset({1, REPORT_VERSION})

STORY_LINE_ROUTE = "story_line_route"
EXACT_BANK_UNROUTED_MEDIA = "exact_bank_unrouted_media"


class StoryVoiceCandidateError(RuntimeError):
    """Installed story audio cannot be bound into a trustworthy audition set."""


@dataclass(frozen=True)
class StoryVoiceLine:
    line_id: str
    character: str
    speaker: str
    portrait: str | None
    text: str
    text_sha256: str
    source_audio_id: str
    source_event: str
    source_bank: str
    source_media_ids: tuple[int, ...]
    previous_text: str | None = None
    next_text: str | None = None
    collection_title: str | None = None


@dataclass(frozen=True)
class BankSnapshot:
    path: Path
    sha256: str
    media: dict[int, bytes]
    routes: dict[int, tuple[int, ...]]


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise StoryVoiceCandidateError(f"{label} must be non-empty text")
    return value.strip()


def collect_story_voice_lines(story_index, roles):
    """Collect exact installed source lines for requested role names."""
    story_index = Path(story_index).expanduser().resolve()
    requested = {}
    for role in roles:
        role = _required_text(role, "Role")
        key = normalize_character_name(role)
        if key in requested and requested[key] != role:
            raise StoryVoiceCandidateError(
                f"Requested roles normalize to the same identity: {requested[key]!r}, {role!r}"
            )
        requested[key] = role
    if not requested:
        raise StoryVoiceCandidateError("At least one --role is required")
    try:
        payload = story_index.read_bytes()
    except OSError as error:
        raise StoryVoiceCandidateError(
            f"Unable to read story index {story_index}: {error}"
        ) from error
    lines = []
    seen = set()
    observed_roles = set()
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StoryVoiceCandidateError(f"Story index is not UTF-8: {story_index}") from error
    for number, raw in enumerate(decoded.splitlines(), start=1):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StoryVoiceCandidateError(
                f"Story index line {number} is invalid JSON: {error}"
            ) from error
        if not isinstance(record, dict) or record.get("record_type") != "line":
            continue
        character = str(record.get("voice_character") or "").strip()
        key = normalize_character_name(character)
        if key not in requested or record.get("source_audio_status") != "available":
            continue
        observed_roles.add(key)
        text = _required_text(record.get("text"), f"Story line {number} text")
        text_sha256 = _required_text(record.get("text_sha256"), f"Story line {number} text_sha256")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise StoryVoiceCandidateError(f"Story line {number} text checksum does not match")
        media_ids = record.get("source_media_ids")
        if (
            not isinstance(media_ids, list)
            or not media_ids
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in media_ids
            )
        ):
            raise StoryVoiceCandidateError(f"Story line {number} has no valid installed media IDs")
        line = StoryVoiceLine(
            line_id=_required_text(record.get("line_id"), f"Story line {number} ID"),
            character=requested[key],
            speaker=_required_text(record.get("speaker"), f"Story line {number} speaker"),
            portrait=(
                str(record["portrait"]).strip()
                if isinstance(record.get("portrait"), str) and str(record["portrait"]).strip()
                else None
            ),
            text=text,
            text_sha256=text_sha256,
            source_audio_id=_required_text(
                record.get("source_audio_id") or record.get("source_voice_id"),
                f"Story line {number} source audio ID",
            ),
            source_event=_required_text(
                record.get("source_event"), f"Story line {number} source event"
            ),
            source_bank=_required_text(
                record.get("source_bank"), f"Story line {number} source bank"
            ),
            source_media_ids=tuple(media_ids),
            previous_text=(
                str(record["previous_text"]).strip()
                if isinstance(record.get("previous_text"), str)
                and str(record["previous_text"]).strip()
                else None
            ),
            next_text=(
                str(record["next_text"]).strip()
                if isinstance(record.get("next_text"), str) and str(record["next_text"]).strip()
                else None
            ),
            collection_title=(
                str(record["collection_title"]).strip()
                if isinstance(record.get("collection_title"), str)
                and str(record["collection_title"]).strip()
                else None
            ),
        )
        identity = (line.line_id, line.text_sha256)
        if identity in seen:
            raise StoryVoiceCandidateError(
                f"Story index contains duplicate line identity: {line.line_id}"
            )
        seen.add(identity)
        lines.append(line)
    missing = [requested[key] for key in requested if key not in observed_roles]
    if missing:
        raise StoryVoiceCandidateError(
            "No installed same-speaker story audio found for: " + ", ".join(missing)
        )
    return tuple(lines), hashlib.sha256(payload).hexdigest()


def affected_story_line_counts(story_index, roles):
    """Count missing-source speakable lines by exact role and portrait."""
    requested = {
        normalize_character_name(_required_text(role, "Role")): _required_text(role, "Role")
        for role in roles
    }
    try:
        payload = Path(story_index).expanduser().resolve().read_bytes()
        decoded = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise StoryVoiceCandidateError(
            f"Unable to read story coverage from {story_index}: {error}"
        ) from error
    character_counts = {role: 0 for role in requested.values()}
    portrait_counts = {}
    for number, raw in enumerate(decoded.splitlines(), start=1):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StoryVoiceCandidateError(
                f"Story index line {number} is invalid JSON: {error}"
            ) from error
        if not isinstance(record, dict) or record.get("record_type") != "line":
            continue
        key = normalize_character_name(str(record.get("voice_character") or ""))
        role = requested.get(key)
        if (
            role is None
            or record.get("speakable") is False
            or record.get("source_audio_status") == "available"
        ):
            continue
        portrait = (
            str(record["portrait"]).strip()
            if isinstance(record.get("portrait"), str) and str(record["portrait"]).strip()
            else None
        )
        character_counts[role] += 1
        portrait_counts[(role, portrait)] = portrait_counts.get((role, portrait), 0) + 1
    return character_counts, portrait_counts


def _bank_entries(bank_index):
    return {entry["filename"].casefold(): entry for entry in bank_index["banks"]}


def snapshot_bank(bank_index, filename):
    """Read and validate one exact bank byte snapshot and all routed media."""
    entries = _bank_entries(bank_index)
    entry = entries.get(filename.casefold())
    if entry is None:
        raise StoryVoiceCandidateError(f"Story bank is absent from the index: {filename}")
    audio_root = Path(bank_index["game_audio_directory"]).expanduser().resolve()
    relative = Path(entry["path"])
    path = (audio_root / relative).resolve()
    try:
        path.relative_to(audio_root)
    except ValueError as error:
        raise StoryVoiceCandidateError(
            f"Story bank path escapes the indexed audio root: {entry['path']}"
        ) from error
    if path.name != entry["filename"]:
        raise StoryVoiceCandidateError(
            f"Story bank filename/path mismatch: {entry['filename']} != {entry['path']}"
        )
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise StoryVoiceCandidateError(f"Unable to read story bank {path}: {error}") from error
    fingerprint = (before.st_size, before.st_mtime_ns)
    if fingerprint != (after.st_size, after.st_mtime_ns):
        raise StoryVoiceCandidateError(f"Story bank changed while it was read: {path.name}")
    if fingerprint != (entry["size"], entry["mtime_ns"]):
        raise StoryVoiceCandidateError(
            f"Bank index is stale for {path.name}; rebuild r1999-bank-index"
        )
    try:
        summary = inspect_bank_data(payload)
        media = {item.media_id: item.data for item in extract_embedded_media(payload)}
    except WwiseBankError as error:
        raise StoryVoiceCandidateError(f"Unable to parse {path.name}: {error}") from error
    return BankSnapshot(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        media=media,
        routes={route.event_id: route.media_ids for route in summary.event_routes},
    )


def build_story_voice_candidates(
    story_index,
    bank_index_path,
    roles,
    output,
    *,
    decoder="vgmstream-cli",
    bank_loader=snapshot_bank,
    media_decoder=decode_reference_data,
    analyzer=analyze_voice_reference,
    include_all_bank_media=False,
):
    """Publish a non-authoritative audition set without changing a voice manifest."""
    story_index = resolve_story_index_path(story_index)
    bank_index_path = Path(bank_index_path).expanduser().resolve()
    lines, story_sha256 = collect_story_voice_lines(story_index, roles)
    character_affected, portrait_affected = affected_story_line_counts(story_index, roles)
    try:
        bank_index_payload = bank_index_path.read_bytes()
        bank_index = validate_bank_index_document(json.loads(bank_index_payload.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoryVoiceCandidateError(
            f"Unable to read bank index {bank_index_path}: {error}"
        ) from error
    bank_index_sha256 = hashlib.sha256(bank_index_payload).hexdigest()
    decoder = resolve_decoder(decoder)
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise StoryVoiceCandidateError(f"Candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)).resolve()
    try:
        snapshots = {
            bank: bank_loader(bank_index, bank)
            for bank in sorted({line.source_bank for line in lines}, key=str.casefold)
        }
        grouped = {}
        for line in lines:
            snapshot = snapshots[line.source_bank]
            routed = snapshot.routes.get(wwise_event_id(line.source_event))
            if routed != line.source_media_ids:
                raise StoryVoiceCandidateError(
                    f"Exact bank route changed for {line.line_id}: {line.source_event}"
                )
            for media_id in line.source_media_ids:
                if media_id not in snapshot.media:
                    raise StoryVoiceCandidateError(
                        f"Routed media {media_id} is not embedded in {line.source_bank}"
                    )
                key = (line.character, line.portrait, line.source_bank, media_id)
                grouped.setdefault(key, []).append(line)

        if include_all_bank_media:
            identities_by_bank = {}
            for line in lines:
                identity = (line.character, line.portrait, line.source_bank)
                identities_by_bank.setdefault(line.source_bank, set()).add(identity)
            for bank, identities in identities_by_bank.items():
                if len(identities) != 1:
                    rendered = ", ".join(
                        f"{character!r}/{portrait!r}"
                        for character, portrait, _bank in sorted(
                            identities,
                            key=lambda value: tuple(str(part or "").casefold() for part in value),
                        )
                    )
                    raise StoryVoiceCandidateError(
                        "--include-all-bank-media requires one exact role/portrait "
                        f"identity per bank; {bank} maps to {rendered}"
                    )
                character, portrait, _bank = next(iter(identities))
                for media_id in snapshots[bank].media:
                    grouped.setdefault((character, portrait, bank, media_id), [])

        candidates = []
        for (character, portrait, bank, media_id), source_lines in sorted(
            grouped.items(),
            key=lambda value: tuple(str(part or "").casefold() for part in value[0]),
        ):
            group_slug = "-".join(
                (
                    slugify(character, fallback="character"),
                    slugify(Path(portrait).stem if portrait else "no-portrait"),
                    slugify(Path(bank).stem),
                    str(media_id),
                )
            )
            relative = Path("references") / group_slug / f"{group_slug}.wav"
            snapshot = snapshots[bank]
            event_ids = sorted(
                event_id for event_id, media_ids in snapshot.routes.items() if media_id in media_ids
            )
            if not event_ids:
                raise StoryVoiceCandidateError(
                    f"Embedded media {media_id} has no exact event route in {bank}"
                )
            imported = media_decoder(
                snapshot.media[media_id],
                staging / relative,
                media_id,
                decoder,
                bank=bank,
            )
            metrics = analyzer(imported.path)
            metrics_document = asdict(metrics)
            metrics_document["path"] = relative.as_posix()
            text_hashes = sorted({line.text_sha256 for line in source_lines})
            candidate_origin = STORY_LINE_ROUTE if source_lines else EXACT_BANK_UNROUTED_MEDIA
            candidates.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": bank,
                    "source_bank_sha256": snapshot.sha256,
                    "media_id": media_id,
                    "source_sha256": imported.source_sha256,
                    "candidate_origin": candidate_origin,
                    "source_event_ids": event_ids,
                    "reference": relative.as_posix(),
                    "reference_sha256": imported.reference_sha256,
                    "source_lines": [asdict(line) for line in source_lines],
                    "transcript_conflict": len(text_hashes) > 1,
                    "metrics": metrics_document,
                    "technical_pass": not metrics.technical_flags,
                    "manual_content_review_required": True,
                    "affected_character_line_count": character_affected[character],
                    "affected_portrait_line_count": portrait_affected.get((character, portrait), 0),
                }
            )

        groups = []
        group_keys = sorted(
            {(value["character"], value["portrait"], value["source_bank"]) for value in candidates},
            key=lambda value: tuple(str(part or "").casefold() for part in value),
        )
        for character, portrait, bank in group_keys:
            members = [
                value
                for value in candidates
                if (value["character"], value["portrait"], value["source_bank"])
                == (character, portrait, bank)
            ]
            ranked = sorted(
                members,
                key=lambda value: (
                    not value["technical_pass"],
                    value["transcript_conflict"],
                    -value["metrics"]["quality_score"],
                    abs(value["metrics"]["duration_seconds"] - 4.0),
                    value["media_id"],
                ),
            )
            groups.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": bank,
                    "candidate_count": len(members),
                    "recommended_media_ids_for_audition": [
                        value["media_id"]
                        for value in ranked
                        if value["technical_pass"] and not value["transcript_conflict"]
                    ][:3],
                    "manual_content_review_required": True,
                    "affected_character_line_count": character_affected[character],
                    "affected_portrait_line_count": portrait_affected.get((character, portrait), 0),
                }
            )
        report = {
            "schema": REPORT_SCHEMA,
            "schema_version": REPORT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "story_index": str(story_index),
            "story_index_sha256": story_sha256,
            "bank_index": str(bank_index_path),
            "bank_index_sha256": bank_index_sha256,
            "roles": sorted({line.character for line in lines}, key=str.casefold),
            "source_line_count": len(lines),
            "group_count": len(groups),
            "candidate_count": len(candidates),
            "bank_inventory_scope": (
                "complete_exact_bank" if include_all_bank_media else "story_routed_only"
            ),
            "groups": groups,
            "candidates": candidates,
            "publication_policy": (
                "Audition evidence only. Do not update a voice manifest until manual "
                "speaker, contamination and multi-speaker review is complete."
            ),
        }
        atomic_write_json(staging / "report.json", report)
        if sha256_file(story_index) != story_sha256:
            raise StoryVoiceCandidateError("Story index changed during candidate extraction")
        if sha256_file(bank_index_path) != bank_index_sha256:
            raise StoryVoiceCandidateError("Bank index changed during candidate extraction")
        for bank, snapshot in snapshots.items():
            if sha256_file(snapshot.path) != snapshot.sha256:
                raise StoryVoiceCandidateError(
                    f"Story bank changed during candidate extraction: {bank}"
                )
        try:
            output.mkdir()
        except FileExistsError as error:
            raise StoryVoiceCandidateError(f"Candidate output already exists: {output}") from error
        try:
            for child in sorted(staging.iterdir(), key=lambda value: value.name == "report.json"):
                child.rename(output / child.name)
        except Exception:
            shutil.rmtree(output)
            raise
        staging.rmdir()
        return output / "report.json", report
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def create_parser():
    parser = argparse.ArgumentParser(
        description=("Build a checksum-bound audition set from installed same-speaker story audio")
    )
    parser.add_argument(
        "--role",
        action="append",
        dest="roles",
        required=True,
        help="Exact story voice character; repeat for multiple roles",
    )
    parser.add_argument("--story-index", type=Path)
    parser.add_argument("--bank-index", type=Path, default=default_bank_index)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument(
        "--include-all-bank-media",
        action="store_true",
        help=(
            "Include every event-routed medium from a bank only when that bank "
            "maps to one requested role/portrait identity"
        ),
    )
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        report_path, report = build_story_voice_candidates(
            options.story_index,
            options.bank_index,
            options.roles,
            options.output,
            decoder=options.decoder,
            include_all_bank_media=options.include_all_bank_media,
        )
    except (
        AudioConversionError,
        GameVoiceImportError,
        OSError,
        PlayableVoiceError,
        StoryVoiceCandidateError,
        VoiceReferenceQualityError,
        WwiseBankError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    print(
        f"Prepared {report['candidate_count']} checksum-bound candidates across "
        f"{report['group_count']} portrait/bank groups in {report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
