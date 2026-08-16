import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.bootstrap import BootstrapError, bootstrap_local_artifacts, main


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
            progress = []
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
                write_story.side_effect = lambda _lines, path, **_kwargs: path
                result = bootstrap_local_artifacts(
                    config_directory=config,
                    game_audio_directory=audio,
                    data_directory=root / "local",
                    progress=progress.append,
                )

        self.assertIn("reverse1999", str(result["catalog"]))
        self.assertIn("reverse1999", str(result["story_index"]))
        self.assertIn("reverse1999", str(result["source_audit"]))
        self.assertNotIn("generation_queue", result)
        self.assertNotIn("generation_item_count", result)
        self.assertFalse((root / "local/reverse1999/generation-queue.jsonl").exists())
        self.assertEqual(progress[-1], "Auditing story source coverage")
        self.assertFalse(any("generation" in message.casefold() for message in progress))

    def test_cli_reports_source_only_summary(self):
        result = {
            "bank_index": Path("bank.json"),
            "catalog": Path("catalog.json"),
            "story_index": Path("story.jsonl"),
            "source_audit": Path("audit.json"),
            "story_line_count": 7,
        }
        with (
            patch("r1999extractor.bootstrap.bootstrap_local_artifacts", return_value=result),
            patch("builtins.print") as output,
        ):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        output.assert_called_once_with("Built 7 story lines and source artifacts")


if __name__ == "__main__":
    unittest.main()
