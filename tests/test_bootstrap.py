import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.bootstrap import BootstrapError, bootstrap_local_artifacts


class BootstrapTest(unittest.TestCase):
    def test_requires_discoverable_installed_inputs(self):
        with (
            patch("r1999extractor.bootstrap.find_game_config_directory", return_value=None),
            self.assertRaisesRegex(BootstrapError, "configs"),
        ):
            bootstrap_local_artifacts()

    def test_uses_local_output_directory_for_every_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "configs"
            audio = root / "audio"
            bundle = root / "story.dat"
            config.mkdir()
            audio.mkdir()
            bundle.write_bytes(b"synthetic")
            bank_index = {"version": 4, "game_audio_directory": str(audio), "banks": []}
            with (
                patch("r1999extractor.bootstrap.load_config_directory", return_value=({}, {})),
                patch(
                    "r1999extractor.bootstrap.build_bank_index",
                    return_value=(bank_index, root / "local/reverse1999/english-bank-index.json"),
                ),
                patch("r1999extractor.bootstrap.find_story_bundle", return_value=bundle),
                patch("r1999extractor.bootstrap.extract_story_lines", return_value=[]),
                patch("r1999extractor.bootstrap.enrich_story_sources", return_value=[]),
                patch("r1999extractor.bootstrap.StoryAudioResolver"),
                patch("r1999extractor.bootstrap.write_story_index") as write_story,
            ):
                story = root / "story.jsonl"
                story.write_text(
                    '{"record_type":"metadata","schema":"vntts.story-index",'
                    '"schema_version":1,"line_count":0}\n'
                )
                write_story.return_value = story
                result = bootstrap_local_artifacts(
                    config_directory=config,
                    game_audio_directory=audio,
                    data_directory=root / "local",
                )

        self.assertIn("reverse1999", str(result["catalog"]))
        self.assertIn("reverse1999", str(result["generation_queue"]))


if __name__ == "__main__":
    unittest.main()
