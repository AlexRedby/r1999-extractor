import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.reverse1999_index import index_version
from r1999extractor.story_audio import (
    StoryAudioResolutionError,
    StoryAudioResolver,
    build_audio_registry,
    normalize_audio_id,
    wwise_event_id,
)


def bank_index(root, *, event="play_voice_7", include_event=True, embedded=(99,)):
    events = []
    if include_event:
        events.append({"event_id": wwise_event_id(event), "media_ids": [99]})
    return {
        "version": index_version,
        "game_audio_directory": str(root),
        "banks": [
            {
                "filename": "voice_bank.bnk",
                "events": events,
                "embedded_media_ids": list(embedded),
            }
        ],
    }


class StoryAudioTest(unittest.TestCase):
    def test_normalizes_decorated_cue_ids_and_uses_wwise_fnv1(self):
        self.assertEqual(normalize_audio_id("610021279#1.5|1.5"), "610021279")
        self.assertEqual(normalize_audio_id("612001194&1111111"), "612001194")
        self.assertEqual(
            wwise_event_id("play_activityvoc_story_plot20_npc505701_99"),
            1232314,
        )

    def test_builds_registry_from_story_and_role_audio_tables(self):
        registry = build_audio_registry(
            {
                "json_story_audio_main": [[7, "play_voice_7", "voice_bank"]],
                "json_role_audio": [[8, "play_voice_8", "role_bank"]],
                "json_character": [[9, "not_audio", "ignored"]],
            }
        )
        self.assertEqual(set(registry), {"7", "8"})

    def test_rejects_conflicting_duplicate_audio_ids(self):
        with self.assertRaisesRegex(StoryAudioResolutionError, "Conflicting"):
            build_audio_registry(
                {
                    "json_story_audio": [[7, "play_a", "bank_a"]],
                    "json_story_audio_main": [[7, "play_b", "bank_b"]],
                }
            )

    def test_story_audio_route_takes_precedence_over_role_audio_id_collision(self):
        registry = build_audio_registry(
            {
                "json_role_audio": [[7, "play_combat", "combat_bank"]],
                "json_story_audio_role": [[7, "play_story", "story_bank"]],
            }
        )
        self.assertEqual(registry["7"].event, "play_story")

    def test_resolves_installed_embedded_and_external_media(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "en"
            root.mkdir()
            registry = build_audio_registry(
                {"json_story_audio_main": [[7, "play_voice_7", "voice_bank"]]}
            )
            embedded = StoryAudioResolver(registry, bank_index(root)).resolve("7")

            document = bank_index(root, embedded=())
            media = root.parent / "Media" / "99.wem"
            media.parent.mkdir()
            media.write_bytes(b"wem")
            external = StoryAudioResolver(registry, document).resolve("7#1|1")

        self.assertEqual(embedded.status, "installed")
        self.assertEqual(embedded.available_media_ids, (99,))
        self.assertEqual(external.status, "installed")

    def test_distinguishes_no_audio_unavailable_and_unresolved(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = build_audio_registry(
                {"json_story_audio_main": [[7, "play_voice_7", "voice_bank"]]}
            )
            resolver = StoryAudioResolver(registry, bank_index(root, embedded=()))

            self.assertEqual(resolver.resolve("").status, "no_audio")
            self.assertEqual(resolver.resolve("0").status, "no_audio")
            self.assertEqual(resolver.resolve("8").status, "unresolved")
            unavailable = resolver.resolve("7")

        self.assertEqual(unavailable.status, "configured_unavailable")
        self.assertEqual(unavailable.reason, "media_not_installed")


if __name__ == "__main__":
    unittest.main()
