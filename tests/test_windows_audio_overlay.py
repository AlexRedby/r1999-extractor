import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.bootstrap import bootstrap_local_artifacts
from r1999extractor.playable_voice import (
    bind_playable_voice_provenance,
    extract_playable_voice_lines,
)
from r1999extractor.reverse1999_index import (
    Reverse1999IndexError,
    bank_external_media_root,
    bank_index_staleness_reasons,
    build_bank_index,
)
from r1999extractor.story_audio import StoryAudioResolver, build_audio_registry, wwise_event_id
from r1999extractor.story_voice_candidates import snapshot_bank
from tests.test_playable_voice import character_row, synthetic_bank, voice_row


class WindowsAudioOverlayTest(unittest.TestCase):
    def test_indexes_both_roots_and_keeps_bank_specific_references_and_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "StreamingAssets"
            downloaded = root / "PersistentRoot/audios/Windows/en"
            packaged = root / "Windows/audios/Windows/en"
            for folder, name, payload in (
                (downloaded, "story.bnk", b"new story"),
                (packaged, "story.bnk", b"old story"),
                (packaged, "hero3032_mainstory.bnk", b"Centurion reference"),
                (packaged.parent / "cn", "chinese_only.bnk", b"wrong language"),
            ):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(
                    synthetic_bank(7, payload, wwise_event_id("play_voice"), routed_media_id=99)
                )
                media = folder.parent / "Media"
                media.mkdir(exist_ok=True)
                if folder.name == "en":
                    (media / "99.wem").write_bytes(folder.parent.parent.parent.name.encode())

            index, _ = build_bank_index(downloaded, output=Path(directory) / "index.json")
            self.assertEqual(index["game_audio_directory"], str(root))
            self.assertEqual(index["bank_count"], 2)
            self.assertEqual(bank_index_staleness_reasons(index, packaged), [])
            self.assertEqual(bank_index_staleness_reasons(index, downloaded), [])
            entries = {entry["filename"]: entry for entry in index["banks"]}
            self.assertTrue(entries["story.bnk"]["path"].startswith("PersistentRoot/"))
            self.assertEqual(snapshot_bank(index, "story.bnk").media[7], b"new story")
            self.assertEqual(
                snapshot_bank(index, "hero3032_mainstory.bnk").media[7], b"Centurion reference"
            )
            tables = {
                "json_character": [character_row(3032, "centurion", "Centurion")],
                "json_character_voice": [
                    voice_row(3032, 1, "first", "Hello there."),
                    voice_row(3032, 2, "second", "A story line."),
                ],
                "json_story_audio_role": [
                    [1, "play_voice", "hero3032_mainstory"],
                    [2, "play_voice", "story"],
                ],
            }
            resolver = StoryAudioResolver(build_audio_registry(tables), index)
            for voice_id, expected in ((1, b"Windows"), (2, b"PersistentRoot")):
                resolved = resolver.resolve(voice_id)
                self.assertEqual(resolved.status, "installed")
                self.assertEqual(resolver.read_single_available_media(resolved), (99, expected))
            lines = extract_playable_voice_lines(
                {"centurion": "Centurion"}, tables, "3032", resolver
            )
            bound = bind_playable_voice_provenance(lines, index)
            self.assertEqual(len(bound), 2)
            self.assertEqual(
                bound[0].media_sha256[0]["source_sha256"], hashlib.sha256(b"Windows").hexdigest()
            )
            with (
                patch("r1999extractor.bootstrap.load_config_directory", return_value=({}, tables)),
                patch(
                    "r1999extractor.bootstrap.find_story_bundle", return_value=root / "story.dat"
                ),
                patch("r1999extractor.bootstrap.extract_story_lines", return_value=[]),
                patch("r1999extractor.bootstrap.enrich_story_sources", return_value=[]),
            ):
                bootstrap_local_artifacts(
                    config_directory=root, game_audio_directory=downloaded, data_directory=directory
                )
            catalog = json.loads((Path(directory) / "reverse1999/narrator-banks.json").read_text())
            self.assertEqual(catalog["Centurion"], "hero3032_mainstory.bnk")

            # Old indexes must refresh even though their chosen folder still exists.
            old = dict(index, game_audio_directory=str(downloaded))
            self.assertIn("directory changed", " ".join(bank_index_staleness_reasons(old)))
            (downloaded / "story.bnk").unlink()
            self.assertTrue(bank_index_staleness_reasons(index))
            refreshed, _ = build_bank_index(root, output=Path(directory) / "index.json")
            self.assertEqual(snapshot_bank(refreshed, "story.bnk").media[7], b"old story")

    def test_rejects_unsafe_media_paths_and_ambiguous_same_layer_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("../outside", "/outside", "Windows\\Media"):
                with self.subTest(relative=relative), self.assertRaises(Reverse1999IndexError):
                    bank_external_media_root(
                        {"game_audio_directory": str(root)}, {"media_directory": relative}
                    )
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one/voice.bnk").touch()
            (root / "two/voice.bnk").touch()
            with self.assertRaisesRegex(Reverse1999IndexError, "Duplicate bank"):
                build_bank_index(root, output=root / "index.json")
