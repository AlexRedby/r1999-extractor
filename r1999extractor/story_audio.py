import json
import re
from dataclasses import dataclass
from pathlib import Path

from r1999extractor.reverse1999_index import index_version

audio_config_tables = {
    "json_role_audio",
    "json_story_role_audio",
}
audio_statuses = {
    "installed",
    "no_audio",
    "configured_unavailable",
    "unresolved",
}
cue_id_pattern = re.compile(r"^\s*(\d+)")


class StoryAudioResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioConfiguration:
    audio_id: str
    event: str
    bank: str
    table: str


@dataclass(frozen=True)
class AudioResolution:
    status: str
    reason: str
    audio_id: str | None = None
    event: str | None = None
    bank: str | None = None
    media_ids: tuple[int, ...] = ()
    available_media_ids: tuple[int, ...] = ()


def normalize_audio_id(value):
    if value is None:
        return None
    match = cue_id_pattern.match(str(value))
    return match.group(1) if match else None


def wwise_event_id(value):
    result = 2166136261
    for byte in str(value).casefold().encode("utf-8"):
        result = ((result * 16777619) & 0xFFFFFFFF) ^ byte
    return result


def build_audio_registry(tables):
    registry = {}
    for table, rows in tables.items():
        if not (table.startswith("json_story_audio") or table in audio_config_tables):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            audio_id = normalize_audio_id(row[0])
            if audio_id is None:
                continue
            configuration = AudioConfiguration(
                audio_id=audio_id,
                event=str(row[1]).strip(),
                bank=str(row[2]).strip(),
                table=table,
            )
            previous = registry.get(audio_id)
            if previous is not None and (previous.event, previous.bank) != (
                configuration.event,
                configuration.bank,
            ):
                previous_priority = _audio_table_priority(previous.table)
                current_priority = _audio_table_priority(table)
                if current_priority < previous_priority:
                    registry[audio_id] = configuration
                    continue
                if current_priority > previous_priority:
                    continue
                raise StoryAudioResolutionError(
                    f"Conflicting audio configuration for {audio_id}: {previous.table} and {table}"
                )
            registry.setdefault(audio_id, configuration)
    return registry


def _audio_table_priority(table):
    return 0 if table.startswith("json_story_") else 1


class StoryAudioResolver:
    def __init__(self, registry, bank_index):
        if bank_index.get("version") != index_version:
            raise StoryAudioResolutionError(
                f"Bank index version {bank_index.get('version')!r} is unsupported; "
                f"rebuild it with r1999-bank-index (expected {index_version})"
            )
        self.registry = dict(registry)
        self.audio_root = Path(bank_index["game_audio_directory"]).expanduser().resolve()
        self.external_media_root = self.audio_root.parent / "Media"
        self.banks = {}
        for entry in bank_index.get("banks", ()):
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            key = Path(filename).stem.casefold()
            if key in self.banks:
                raise StoryAudioResolutionError(f"Duplicate bank filename: {filename}")
            self.banks[key] = entry

    @classmethod
    def from_file(cls, registry, path):
        path = Path(path).expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoryAudioResolutionError(f"Unable to read bank index {path}: {error}") from error
        if not isinstance(document, dict):
            raise StoryAudioResolutionError("Bank index must be a JSON object")
        return cls(registry, document)

    def resolve(self, source_voice_id):
        audio_id = normalize_audio_id(source_voice_id)
        if audio_id is None:
            return AudioResolution("no_audio", "blank_voice_id")
        if audio_id == "0":
            return AudioResolution("no_audio", "zero_voice_id", audio_id=audio_id)
        configuration = self.registry.get(audio_id)
        if configuration is None:
            return AudioResolution("unresolved", "audio_id_not_in_config", audio_id=audio_id)
        if not configuration.event or not configuration.bank:
            return AudioResolution(
                "no_audio",
                "empty_config_route",
                audio_id=audio_id,
                event=configuration.event or None,
                bank=configuration.bank or None,
            )

        bank = self.banks.get(Path(configuration.bank).stem.casefold())
        if bank is None:
            return self._configured_unavailable(configuration, "bank_not_installed")
        event_id = wwise_event_id(configuration.event)
        event = next(
            (route for route in bank.get("events", ()) if route.get("event_id") == event_id),
            None,
        )
        if event is None:
            return self._configured_unavailable(configuration, "event_not_in_bank")
        media_ids = tuple(
            int(media_id)
            for media_id in event.get("media_ids", ())
            if isinstance(media_id, int) and not isinstance(media_id, bool)
        )
        if not media_ids:
            return self._configured_unavailable(
                configuration, "event_has_no_media", media_ids=media_ids
            )

        embedded = set(bank.get("embedded_media_ids", ()))
        available = tuple(
            media_id
            for media_id in media_ids
            if media_id in embedded or (self.external_media_root / f"{media_id}.wem").is_file()
        )
        if not available:
            return self._configured_unavailable(
                configuration, "media_not_installed", media_ids=media_ids
            )
        return AudioResolution(
            "installed",
            "resolved_local_media",
            audio_id=configuration.audio_id,
            event=configuration.event,
            bank=bank["filename"],
            media_ids=media_ids,
            available_media_ids=available,
        )

    @staticmethod
    def _configured_unavailable(configuration, reason, *, media_ids=()):
        return AudioResolution(
            "configured_unavailable",
            reason,
            audio_id=configuration.audio_id,
            event=configuration.event,
            bank=configuration.bank,
            media_ids=tuple(media_ids),
        )
