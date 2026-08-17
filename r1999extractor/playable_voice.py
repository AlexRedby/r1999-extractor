import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.voice_manifest import normalize_character_name

from r1999extractor.reverse1999_config import (
    Reverse1999ConfigError,
    extract_character_identities,
    find_game_config_directory,
    load_config_directory,
)
from r1999extractor.reverse1999_index import (
    default_output as default_bank_index,
)
from r1999extractor.reverse1999_index import (
    index_version as bank_index_version,
)
from r1999extractor.settings import get_local_data_directory
from r1999extractor.story_audio import (
    StoryAudioResolutionError,
    StoryAudioResolver,
    build_audio_registry,
    wwise_event_id,
)
from r1999extractor.story_index import default_output as default_story_index
from r1999extractor.wwise import WwiseBankError, extract_embedded_media, inspect_bank_data

index_version = 2
timing_suffix = re.compile(r"#-?\d+(?:\.\d+)?$")
html_tag = re.compile(r"<[^>]+>")


class PlayableVoiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlayableVoiceLine:
    character_id: str
    character: str
    voice_id: str
    title: str
    text: str
    text_sha256: str
    source_table: str
    source_kind: str
    source_audio_status: str
    source_audio_reason: str
    source_event: str | None
    source_bank: str | None
    source_media_ids: tuple[int, ...]
    available_media_ids: tuple[int, ...]
    bank_sha256: str | None = None
    media_sha256: tuple[dict, ...] = ()


def playable_voice_output(character):
    normalized = normalize_character_name(character) or "character"
    return get_local_data_directory() / "reverse1999" / f"playable-voice-{normalized}.json"


def clean_voice_text(value):
    if not isinstance(value, str):
        return ""
    parts = []
    for segment in value.split("|"):
        cleaned = timing_suffix.sub("", segment).strip()
        if cleaned:
            parts.append(cleaned)
    return " ".join(parts)


def clean_voice_title(value):
    return html_tag.sub("", str(value or "")).strip()


def resolve_character_identity(character, identities):
    requested = normalize_character_name(str(character))
    matches = [
        identity
        for identity in identities.values()
        if identity.character_id == str(character)
        or normalize_character_name(identity.display_name) == requested
    ]
    if not matches:
        raise PlayableVoiceError(f"Playable character is not in json_character: {character!r}")
    if len(matches) != 1:
        names = ", ".join(sorted(identity.display_name for identity in matches))
        raise PlayableVoiceError(f"Playable character identity is ambiguous: {names}")
    return matches[0]


def extract_playable_voice_lines(language, tables, character, resolver):
    identity = resolve_character_identity(character, extract_character_identities(language, tables))
    lines = []
    for row in tables.get("json_character_voice", ()):
        if not isinstance(row, list) or len(row) <= 16 or str(row[0]) != identity.character_id:
            continue
        voice_id = str(row[1]).strip()
        if not voice_id:
            continue
        resolution = resolver.resolve(voice_id)
        title_key = row[3] if isinstance(row[3], str) else ""
        title = clean_voice_title(language.get(title_key, title_key))
        lines.append(
            PlayableVoiceLine(
                character_id=identity.character_id,
                character=identity.display_name,
                voice_id=voice_id,
                title=title,
                text=(text := clean_voice_text(row[16])),
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                source_table="json_character_voice",
                source_kind="playable_character_voice",
                source_audio_status=resolution.status,
                source_audio_reason=resolution.reason,
                source_event=resolution.event,
                source_bank=resolution.bank,
                source_media_ids=resolution.media_ids,
                available_media_ids=resolution.available_media_ids,
            )
        )
    if not lines:
        raise PlayableVoiceError(
            f"No English json_character_voice rows found for {identity.display_name!r}"
        )
    return tuple(lines)


def extract_character_story_voice_lines(story_index, identity, resolver):
    path = Path(story_index).expanduser().resolve()
    expected_bank = f"hero{identity.character_id}_mainstory.bnk".casefold()
    lines = []
    try:
        source = path.open(encoding="utf-8")
    except OSError as error:
        raise PlayableVoiceError(f"Unable to read story index {path}: {error}") from error
    with source:
        for number, raw in enumerate(source, start=1):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise PlayableVoiceError(
                    f"Story index line {number} is not valid JSON: {error}"
                ) from error
            if not isinstance(record, dict) or record.get("record_type") != "line":
                continue
            if normalize_character_name(str(record.get("voice_character") or "")) != (
                normalize_character_name(identity.display_name)
            ):
                continue
            source_bank = str(record.get("source_bank") or "")
            if source_bank.casefold() != expected_bank:
                continue
            voice_id = str(
                record.get("source_audio_id") or record.get("source_voice_id") or ""
            ).strip()
            if not voice_id:
                raise PlayableVoiceError(
                    f"Character-story line {record.get('line_id')} has neither "
                    "source_audio_id nor source_voice_id; regenerate the story index "
                    "with the current r1999-story-index"
                )
            resolution = resolver.resolve(voice_id)
            expected_event = record.get("source_event")
            expected_media = tuple(record.get("source_media_ids") or ())
            if (
                resolution.status != "installed"
                or resolution.event != expected_event
                or resolution.bank != source_bank
                or resolution.media_ids != expected_media
            ):
                raise PlayableVoiceError(
                    f"Character-story route drift for {record.get('line_id')}: "
                    "story index and installed audio registry disagree; regenerate it "
                    "with the current r1999-story-index or pass a compatible --story-index"
                )
            text = str(record.get("text") or "").strip()
            text_sha256 = str(record.get("text_sha256") or "")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
                raise PlayableVoiceError(
                    f"Character-story text hash mismatch for {record.get('line_id')}"
                )
            lines.append(
                PlayableVoiceLine(
                    character_id=identity.character_id,
                    character=identity.display_name,
                    voice_id=voice_id,
                    title=str(record.get("story_title") or record.get("collection_title") or ""),
                    text=text,
                    text_sha256=text_sha256,
                    source_table=f"story-index:{record.get('line_id')}",
                    source_kind="character_story",
                    source_audio_status=resolution.status,
                    source_audio_reason=resolution.reason,
                    source_event=resolution.event,
                    source_bank=resolution.bank,
                    source_media_ids=resolution.media_ids,
                    available_media_ids=resolution.available_media_ids,
                )
            )
    return tuple(lines)


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_bank_index_document(document):
    if not isinstance(document, dict):
        raise PlayableVoiceError("Bank index must be a JSON object")
    if document.get("version") != bank_index_version:
        raise PlayableVoiceError(
            f"Bank index version {document.get('version')!r} is unsupported; rebuild it "
            f"with r1999-bank-index (expected {bank_index_version})"
        )
    audio_directory = document.get("game_audio_directory")
    if not isinstance(audio_directory, str) or not audio_directory.strip():
        raise PlayableVoiceError(
            "Bank index has no valid game_audio_directory; rebuild it with r1999-bank-index"
        )
    entries = document.get("banks")
    if not isinstance(entries, list):
        raise PlayableVoiceError(
            "Bank index has no valid banks list; rebuild it with r1999-bank-index"
        )
    if "bank_count" in document and (
        not _is_integer(document["bank_count"]) or document["bank_count"] != len(entries)
    ):
        raise PlayableVoiceError("Bank index bank_count does not match its banks list")

    filenames = set()
    paths = set()
    for position, entry in enumerate(entries):
        label = f"Bank index entry {position}"
        if not isinstance(entry, dict):
            raise PlayableVoiceError(f"{label} must be a JSON object")
        filename = entry.get("filename")
        relative = entry.get("path")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise PlayableVoiceError(f"{label} has an invalid filename")
        if not isinstance(relative, str) or not relative:
            raise PlayableVoiceError(f"{label} has no valid path")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\\" in relative:
            raise PlayableVoiceError(f"{label} has an unsafe relative path: {relative}")
        if relative_path.name != filename:
            raise PlayableVoiceError(
                f"{label} filename/path mismatch: {filename!r} != {relative!r}"
            )
        filename_key = filename.casefold()
        path_key = relative.casefold()
        if filename_key in filenames:
            raise PlayableVoiceError(f"Bank index contains duplicate filename: {filename}")
        if path_key in paths:
            raise PlayableVoiceError(f"Bank index contains duplicate path: {relative}")
        filenames.add(filename_key)
        paths.add(path_key)
        for field in ("size", "mtime_ns"):
            value = entry.get(field)
            if not _is_integer(value) or value < 0:
                raise PlayableVoiceError(f"{label} has an invalid {field}")
        embedded = entry.get("embedded_media_ids")
        if not isinstance(embedded, list) or any(
            not _is_integer(media_id) or media_id < 0 for media_id in embedded
        ):
            raise PlayableVoiceError(f"{label} has invalid embedded_media_ids")
        if len(embedded) != len(set(embedded)):
            raise PlayableVoiceError(f"{label} has duplicate embedded_media_ids")
        events = entry.get("events")
        if not isinstance(events, list):
            raise PlayableVoiceError(f"{label} has no valid events list")
        event_ids = set()
        for event_position, event in enumerate(events):
            event_label = f"{label} event {event_position}"
            if (
                not isinstance(event, dict)
                or not _is_integer(event.get("event_id"))
                or event["event_id"] < 0
            ):
                raise PlayableVoiceError(f"{event_label} has an invalid event_id")
            event_id = event["event_id"]
            if event_id in event_ids:
                raise PlayableVoiceError(f"{label} has duplicate event_id {event_id}")
            event_ids.add(event_id)
            media_ids = event.get("media_ids")
            if not isinstance(media_ids, list) or any(
                not _is_integer(media_id) or media_id < 0 for media_id in media_ids
            ):
                raise PlayableVoiceError(f"{event_label} has invalid media_ids")
            if len(media_ids) != len(set(media_ids)):
                raise PlayableVoiceError(f"{event_label} has duplicate media_ids")
    return document


def _bank_entries(bank_index):
    entries = {}
    for entry in bank_index.get("banks", ()):
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(filename, str) or not filename:
            continue
        entries[Path(filename).stem.casefold()] = entry
    return entries


def bind_playable_voice_provenance(lines, bank_index):
    validate_bank_index_document(bank_index)
    audio_root = Path(bank_index["game_audio_directory"]).expanduser().resolve()
    external_root = (audio_root.parent / "Media").resolve()
    entries = _bank_entries(bank_index)
    banks = {}
    bound = []
    for line in lines:
        if line.source_audio_status != "installed":
            bound.append(line)
            continue
        if not line.source_bank:
            raise PlayableVoiceError(f"Installed voice {line.voice_id} has no source bank")
        key = Path(line.source_bank).stem.casefold()
        entry = entries.get(key)
        if entry is None:
            raise PlayableVoiceError(
                f"Installed voice bank is absent from the index: {line.source_bank}"
            )
        if key not in banks:
            relative = entry.get("path") or entry.get("filename")
            if not isinstance(relative, str) or not relative:
                raise PlayableVoiceError(f"Voice bank index has no path: {line.source_bank}")
            bank_path = (audio_root / relative).resolve()
            try:
                bank_path.relative_to(audio_root)
            except ValueError as error:
                raise PlayableVoiceError(
                    f"Voice bank path escapes audio root: {relative}"
                ) from error
            before = bank_path.stat()
            bank_data = bank_path.read_bytes()
            after = bank_path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise PlayableVoiceError(f"Voice bank changed while it was read: {bank_path.name}")
            if entry.get("size") != before.st_size or entry.get("mtime_ns") != before.st_mtime_ns:
                raise PlayableVoiceError(
                    f"Voice bank index is stale for {bank_path.name}; rebuild r1999-bank-index"
                )
            try:
                media = {item.media_id: item.data for item in extract_embedded_media(bank_data)}
                summary = inspect_bank_data(bank_data)
            except WwiseBankError as error:
                raise PlayableVoiceError(f"Unable to parse {bank_path.name}: {error}") from error
            routes = {route.event_id: route.media_ids for route in summary.event_routes}
            banks[key] = (hashlib.sha256(bank_data).hexdigest(), media, routes)
        bank_sha256, embedded, routes = banks[key]
        if not line.source_event:
            raise PlayableVoiceError(f"Installed voice {line.voice_id} has no source event")
        fresh_media_ids = routes.get(wwise_event_id(line.source_event))
        if fresh_media_ids != line.source_media_ids:
            raise PlayableVoiceError(
                f"Voice bank route drift for {line.voice_id}: exact bank bytes and index disagree"
            )
        media_hashes = []
        for media_id in line.available_media_ids:
            media_data = embedded.get(media_id)
            location = "embedded"
            if media_data is None:
                external = external_root / f"{media_id}.wem"
                if not external.is_file():
                    raise PlayableVoiceError(
                        f"Resolved media {media_id} is no longer installed for {line.voice_id}"
                    )
                try:
                    resolved_external = external.resolve(strict=True)
                    resolved_external.relative_to(external_root)
                except ValueError as error:
                    raise PlayableVoiceError(
                        f"Resolved media {media_id} escapes media root for {line.voice_id}"
                    ) from error
                except OSError as error:
                    raise PlayableVoiceError(
                        f"Unable to resolve media {media_id} for {line.voice_id}: {error}"
                    ) from error
                before = resolved_external.stat()
                media_data = resolved_external.read_bytes()
                after = resolved_external.stat()
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise PlayableVoiceError(f"Resolved media {media_id} changed while it was read")
                location = "external"
            media_hashes.append(
                {
                    "media_id": media_id,
                    "location": location,
                    "source_sha256": hashlib.sha256(media_data).hexdigest(),
                }
            )
        bound.append(
            replace(
                line,
                bank_sha256=bank_sha256,
                media_sha256=tuple(media_hashes),
            )
        )
    return tuple(bound)


def load_bank_index(path):
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlayableVoiceError(f"Unable to read bank index {path}: {error}") from error
    return validate_bank_index_document(document)


def resolve_story_index_path(path=None, *, directory=None):
    if path is not None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            raise PlayableVoiceError(
                f"Story index does not exist: {candidate}; run r1999-story-index or pass "
                "a compatible --story-index"
            )
        return candidate

    root = Path(directory or default_story_index.parent).expanduser().resolve()
    try:
        candidates = [
            candidate.resolve()
            for candidate in root.glob("story-index*.jsonl")
            if candidate.is_file()
        ]
        candidates.sort(
            key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name.casefold()),
            reverse=True,
        )
    except OSError as error:
        raise PlayableVoiceError(f"Unable to inspect story indexes in {root}: {error}") from error
    if not candidates:
        raise PlayableVoiceError(
            f"No story index found in {root}; run r1999-story-index or pass --story-index"
        )
    return candidates[0]


def text_binding_conflicts(lines, binding_kind):
    grouped = {}
    for line in lines:
        if binding_kind == "voice_id":
            bindings = (("voice_id", line.voice_id),)
        elif binding_kind == "event":
            bindings = (("source_bank", line.source_bank), ("source_event", line.source_event))
        elif binding_kind == "media":
            bindings = tuple(
                (("source_bank", line.source_bank), ("media_id", media_id))
                for media_id in line.source_media_ids
            )
        else:
            raise ValueError(f"Unsupported binding kind: {binding_kind}")
        if binding_kind != "media":
            bindings = (bindings,)
        for binding in bindings:
            if any(value is None or value == "" for _, value in binding):
                continue
            key = tuple(value for _, value in binding)
            record = grouped.setdefault(
                key,
                {
                    "binding": {name: value for name, value in binding},
                    "text_sha256": set(),
                    "voice_ids": set(),
                    "source_tables": set(),
                },
            )
            record["text_sha256"].add(line.text_sha256)
            record["voice_ids"].add(line.voice_id)
            record["source_tables"].add(line.source_table)
    conflicts = []
    for record in grouped.values():
        if len(record["text_sha256"]) <= 1:
            continue
        conflicts.append(
            {
                "binding": record["binding"],
                "text_sha256": sorted(record["text_sha256"]),
                "voice_ids": sorted(record["voice_ids"]),
                "source_tables": sorted(record["source_tables"]),
            }
        )
    return tuple(
        sorted(
            conflicts,
            key=lambda item: json.dumps(item["binding"], sort_keys=True),
        )
    )


def build_playable_voice_index(config_directory, bank_index_path, story_index_path, character):
    bank_index = load_bank_index(bank_index_path)
    language, tables = load_config_directory(config_directory)
    resolver = StoryAudioResolver(build_audio_registry(tables), bank_index)
    identity = resolve_character_identity(
        character,
        extract_character_identities(language, tables),
    )
    lines = extract_playable_voice_lines(language, tables, character, resolver)
    lines = bind_playable_voice_provenance(lines, bank_index)
    character_story = extract_character_story_voice_lines(story_index_path, identity, resolver)
    character_story = bind_playable_voice_provenance(character_story, bank_index)
    statuses = {}
    for line in lines:
        statuses[line.source_audio_status] = statuses.get(line.source_audio_status, 0) + 1
    media_texts = {}
    for line in character_story:
        for media_id in line.available_media_ids:
            media_texts.setdefault((line.source_bank, media_id), set()).add(line.text_sha256)
    playable_voice_conflicts = text_binding_conflicts(lines, "voice_id")
    playable_event_conflicts = text_binding_conflicts(lines, "event")
    playable_media_conflicts = text_binding_conflicts(lines, "media")
    character_story_media_conflicts = text_binding_conflicts(character_story, "media")
    return {
        "schema": "r1999.playable-voice-index",
        "schema_version": index_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "character": lines[0].character,
        "character_id": lines[0].character_id,
        "line_count": len(lines),
        "audio_status_counts": dict(sorted(statuses.items())),
        "playable_voice_id_text_conflict_count": len(playable_voice_conflicts),
        "playable_voice_id_text_conflicts": playable_voice_conflicts,
        "playable_event_text_conflict_count": len(playable_event_conflicts),
        "playable_event_text_conflicts": playable_event_conflicts,
        "playable_media_text_conflict_count": len(playable_media_conflicts),
        "playable_media_text_conflicts": playable_media_conflicts,
        "lines": [asdict(line) for line in lines],
        "character_story_line_count": len(character_story),
        "character_story_distinct_media_count": len(media_texts),
        "character_story_media_text_conflict_count": len(character_story_media_conflicts),
        "character_story_media_text_conflicts": character_story_media_conflicts,
        "character_story_lines": [asdict(line) for line in character_story],
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Bind official playable-character voice text to exact installed Wwise "
            "events, banks, media IDs, and source hashes."
        )
    )
    parser.add_argument("character", help="Playable character name or numeric character ID.")
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--bank-index", type=Path, default=default_bank_index)
    parser.add_argument(
        "--story-index",
        type=Path,
        help="Compatible story index. By default, use the newest local story-index*.jsonl.",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    config_directory = arguments.config_directory or find_game_config_directory()
    if config_directory is None:
        print(
            "Unable to find Reverse: 1999 configs; pass --config-directory",
            file=sys.stderr,
        )
        return 1
    output = (arguments.output or playable_voice_output(arguments.character)).expanduser().resolve()
    try:
        story_index = resolve_story_index_path(arguments.story_index)
        document = build_playable_voice_index(
            config_directory,
            arguments.bank_index,
            story_index,
            arguments.character,
        )
        atomic_write_json(output, document)
    except (
        OSError,
        PlayableVoiceError,
        Reverse1999ConfigError,
        StoryAudioResolutionError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    print(
        f"Bound {document['line_count']} official voice lines for "
        f"{document['character']} into {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
