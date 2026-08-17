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
from r1999extractor.reverse1999_index import default_output as default_bank_index
from r1999extractor.settings import get_local_data_directory
from r1999extractor.story_audio import (
    StoryAudioResolutionError,
    StoryAudioResolver,
    build_audio_registry,
    wwise_event_id,
)
from r1999extractor.story_index import default_output as default_story_index
from r1999extractor.wwise import WwiseBankError, extract_embedded_media, inspect_bank_data

index_version = 1
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
            voice_id = str(record.get("source_audio_id") or "").strip()
            if not voice_id:
                raise PlayableVoiceError(
                    f"Character-story line {record.get('line_id')} has no source audio ID"
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
                    f"story index and installed audio registry disagree"
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


def _bank_entries(bank_index):
    entries = {}
    for entry in bank_index.get("banks", ()):
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(filename, str) or not filename:
            continue
        entries[Path(filename).stem.casefold()] = entry
    return entries


def bind_playable_voice_provenance(lines, bank_index):
    audio_root = Path(bank_index["game_audio_directory"]).expanduser().resolve()
    external_root = audio_root.parent / "Media"
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
                media_data = external.read_bytes()
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
    if not isinstance(document, dict):
        raise PlayableVoiceError("Bank index must be a JSON object")
    return document


def build_playable_voice_index(config_directory, bank_index_path, story_index_path, character):
    language, tables = load_config_directory(config_directory)
    bank_index = load_bank_index(bank_index_path)
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
    conflicting_media = sum(len(texts) > 1 for texts in media_texts.values())
    return {
        "schema": "r1999.playable-voice-index",
        "schema_version": index_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "character": lines[0].character,
        "character_id": lines[0].character_id,
        "line_count": len(lines),
        "audio_status_counts": dict(sorted(statuses.items())),
        "lines": [asdict(line) for line in lines],
        "character_story_line_count": len(character_story),
        "character_story_distinct_media_count": len(media_texts),
        "character_story_media_text_conflict_count": conflicting_media,
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
    parser.add_argument("--story-index", type=Path, default=default_story_index)
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
        document = build_playable_voice_index(
            config_directory,
            arguments.bank_index,
            arguments.story_index,
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
