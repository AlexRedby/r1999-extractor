import hashlib
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.playable_voice import (
    PlayableVoiceError,
    bind_playable_voice_provenance,
    clean_voice_text,
    extract_character_story_voice_lines,
    extract_playable_voice_lines,
    resolve_character_identity,
)
from r1999extractor.reverse1999_config import extract_character_identities
from r1999extractor.reverse1999_index import index_version
from r1999extractor.story_audio import StoryAudioResolver, build_audio_registry, wwise_event_id


def character_row(character_id, name_key, fallback):
    return [character_id, name_key, *([""] * 22), fallback]


def voice_row(character_id, voice_id, title_key, text):
    row = [""] * 17
    row[0] = character_id
    row[1] = voice_id
    row[3] = title_key
    row[16] = text
    return row


def hirc_object(object_type, object_id, payload=b""):
    body = struct.pack("<I", object_id) + payload
    return bytes([object_type]) + struct.pack("<I", len(body)) + body


def synthetic_bank(media_id, content, event_id):
    didx = struct.pack("<III", media_id, 0, len(content))
    sound_id = 101
    action_id = 303
    sound = hirc_object(0x02, sound_id, struct.pack("<IBI", 0x00040001, 0, media_id))
    action = hirc_object(0x03, action_id, struct.pack("<HIB", 0x0403, sound_id, 0))
    event = hirc_object(0x04, event_id, b"\x01" + struct.pack("<I", action_id))
    hirc = struct.pack("<I", 3) + sound + action + event
    return (
        b"BKHD"
        + struct.pack("<II", 4, 154)
        + b"DIDX"
        + struct.pack("<I", len(didx))
        + didx
        + b"DATA"
        + struct.pack("<I", len(content))
        + content
        + b"HIRC"
        + struct.pack("<I", len(hirc))
        + hirc
    )


class PlayableVoiceTest(unittest.TestCase):
    def test_cleans_official_timing_markup_without_dropping_text(self):
        self.assertEqual(
            clean_voice_text("First line.#0|Second line ...#3.25|Last.#-1"),
            "First line. Second line ... Last.",
        )

    def test_extracts_official_english_text_and_exact_local_route(self):
        language = {"paper_heron": "Paper Heron", "first": "First Encounter"}
        tables = {
            "json_character": [character_row(3141, "paper_heron", "Paper Heron")],
            "json_character_voice": [
                voice_row(3141, 1314101, "first", "Hello there.#0|Welcome.#2.5")
            ],
            "json_story_audio_role": [[1314101, "play_hero3141_mainvoc_1", "hero3141_mainvoc"]],
        }
        bank_index = {
            "version": index_version,
            "game_audio_directory": "/game/en",
            "banks": [
                {
                    "filename": "hero3141_mainvoc.bnk",
                    "events": [
                        {
                            "event_id": wwise_event_id("play_hero3141_mainvoc_1"),
                            "media_ids": [42],
                        }
                    ],
                    "embedded_media_ids": [42],
                }
            ],
        }
        resolver = StoryAudioResolver(build_audio_registry(tables), bank_index)

        lines = extract_playable_voice_lines(language, tables, "Paper Heron", resolver)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].voice_id, "1314101")
        self.assertEqual(lines[0].title, "First Encounter")
        self.assertEqual(lines[0].text, "Hello there. Welcome.")
        self.assertEqual(lines[0].source_bank, "hero3141_mainvoc.bnk")
        self.assertEqual(lines[0].available_media_ids, (42,))

    def test_reconciles_character_story_text_with_the_same_exact_audio_route(self):
        language = {"paper_heron": "Paper Heron"}
        tables = {
            "json_character": [character_row(3141, "paper_heron", "Paper Heron")],
            "json_story_audio_role": [
                [6214101, "play_hero3141_mainstory_39", "hero3141_mainstory"]
            ],
        }
        bank_index = {
            "version": index_version,
            "game_audio_directory": "/game/en",
            "banks": [
                {
                    "filename": "hero3141_mainstory.bnk",
                    "events": [
                        {
                            "event_id": wwise_event_id("play_hero3141_mainstory_39"),
                            "media_ids": [84],
                        }
                    ],
                    "embedded_media_ids": [84],
                }
            ],
        }
        resolver = StoryAudioResolver(build_audio_registry(tables), bank_index)
        identity = resolve_character_identity(
            "Paper Heron",
            extract_character_identities(language, tables),
        )
        text = "A verified story line."
        record = {
            "record_type": "line",
            "line_id": "reverse1999:314104:74",
            "voice_character": "Paper Heron",
            "source_audio_id": "6214101",
            "source_event": "play_hero3141_mainstory_39",
            "source_bank": "hero3141_mainstory.bnk",
            "source_media_ids": [84],
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "story_title": "Spring Comes Slowly",
        }
        with TemporaryDirectory() as temporary_directory:
            index = Path(temporary_directory) / "story.jsonl"
            index.write_text(json.dumps(record) + "\n", encoding="utf-8")

            lines = extract_character_story_voice_lines(index, identity, resolver)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].source_kind, "character_story")
        self.assertEqual(lines[0].source_media_ids, (84,))
        self.assertEqual(lines[0].title, "Spring Comes Slowly")

    def test_binds_bank_and_media_hashes_from_one_exact_snapshot(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = b"RIFF-voice"
            event_id = wwise_event_id("play_hero3141_mainvoc_1")
            bank_data = synthetic_bank(42, content, event_id)
            bank = root / "hero3141_mainvoc.bnk"
            bank.write_bytes(bank_data)
            stat = bank.stat()
            language = {"paper_heron": "Paper Heron", "first": "First Encounter"}
            tables = {
                "json_character": [character_row(3141, "paper_heron", "Paper Heron")],
                "json_character_voice": [voice_row(3141, 1314101, "first", "Hello there.#0")],
                "json_story_audio_role": [[1314101, "play_hero3141_mainvoc_1", "hero3141_mainvoc"]],
            }
            bank_index = {
                "version": index_version,
                "game_audio_directory": str(root),
                "banks": [
                    {
                        "path": bank.name,
                        "filename": bank.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "events": [
                            {
                                "event_id": wwise_event_id("play_hero3141_mainvoc_1"),
                                "media_ids": [42],
                            }
                        ],
                        "embedded_media_ids": [42],
                    }
                ],
            }
            resolver = StoryAudioResolver(build_audio_registry(tables), bank_index)
            lines = extract_playable_voice_lines(language, tables, "3141", resolver)

            bound = bind_playable_voice_provenance(lines, bank_index)

        self.assertEqual(bound[0].bank_sha256, hashlib.sha256(bank_data).hexdigest())
        self.assertEqual(
            bound[0].media_sha256,
            (
                {
                    "media_id": 42,
                    "location": "embedded",
                    "source_sha256": hashlib.sha256(content).hexdigest(),
                },
            ),
        )

    def test_rejects_a_stale_bank_fingerprint(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank = root / "voice.bnk"
            bank.write_bytes(synthetic_bank(42, b"voice", wwise_event_id("play_voice")))
            language = {"name": "Paper Heron", "title": "First Encounter"}
            tables = {
                "json_character": [character_row(3141, "name", "Paper Heron")],
                "json_character_voice": [voice_row(3141, 1314101, "title", "Hello.#0")],
                "json_story_audio_role": [[1314101, "play_voice", "voice"]],
            }
            index = {
                "version": index_version,
                "game_audio_directory": str(root),
                "banks": [
                    {
                        "path": bank.name,
                        "filename": bank.name,
                        "size": 1,
                        "mtime_ns": 1,
                        "events": [{"event_id": wwise_event_id("play_voice"), "media_ids": [42]}],
                        "embedded_media_ids": [42],
                    }
                ],
            }
            resolver = StoryAudioResolver(build_audio_registry(tables), index)
            lines = extract_playable_voice_lines(language, tables, "Paper Heron", resolver)

            with self.assertRaisesRegex(PlayableVoiceError, "index is stale"):
                bind_playable_voice_provenance(lines, index)

    def test_rejects_route_metadata_not_proven_by_exact_bank_bytes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank = root / "voice.bnk"
            bank.write_bytes(synthetic_bank(42, b"voice", wwise_event_id("play_other")))
            stat = bank.stat()
            language = {"name": "Paper Heron", "title": "First Encounter"}
            tables = {
                "json_character": [character_row(3141, "name", "Paper Heron")],
                "json_character_voice": [voice_row(3141, 1314101, "title", "Hello.#0")],
                "json_story_audio_role": [[1314101, "play_voice", "voice"]],
            }
            index = {
                "version": index_version,
                "game_audio_directory": str(root),
                "banks": [
                    {
                        "path": bank.name,
                        "filename": bank.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "events": [{"event_id": wwise_event_id("play_voice"), "media_ids": [42]}],
                        "embedded_media_ids": [42],
                    }
                ],
            }
            resolver = StoryAudioResolver(build_audio_registry(tables), index)
            lines = extract_playable_voice_lines(language, tables, "Paper Heron", resolver)

            with self.assertRaisesRegex(PlayableVoiceError, "route drift"):
                bind_playable_voice_provenance(lines, index)


if __name__ == "__main__":
    unittest.main()
