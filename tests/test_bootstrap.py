import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.bootstrap import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    BootstrapError,
    bootstrap_local_artifacts,
    main,
    prepare_player_voice_candidates,
)
from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION


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

    def test_prepares_and_reuses_player_voice_candidate_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "reverse1999"
            output.mkdir()
            story = output / "story-index.jsonl"
            bundles = root / "installed" / "bundles"
            bundles.mkdir(parents=True)
            source_bundle = bundles / "story.dat"
            source_bundle.write_bytes(b"story bundle")
            story.write_text(
                json.dumps(
                    {
                        "record_type": "metadata",
                        "source_bundle": str(source_bundle),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            story_digest = hashlib.sha256(story.read_bytes()).hexdigest()
            (output / "english-bank-index.json").write_text("{}", encoding="utf-8")
            builds = []

            def build(_story, _banks, roles, destination):
                builds.append(tuple(roles))
                destination.mkdir(parents=True)
                reference = destination / "references" / "hero.wav"
                reference.parent.mkdir()
                reference.write_bytes(b"voice")
                source_line = {
                    "line_id": "line:source",
                    "source_audio_id": "play_hero_7",
                }
                report = {
                    "schema": REPORT_SCHEMA,
                    "schema_version": REPORT_VERSION,
                    "story_index": str(story.resolve()),
                    "story_index_sha256": story_digest,
                    "groups": [
                        {
                            "character": "Hero",
                            "portrait": "hero.png",
                            "source_bank": "hero.bnk",
                            "recommended_media_ids_for_audition": [7],
                        }
                    ],
                    "candidates": [
                        {
                            "character": "Hero",
                            "portrait": "hero.png",
                            "source_bank": "hero.bnk",
                            "media_id": 7,
                            "source_event_ids": [70],
                            "reference": "references/hero.wav",
                            "reference_sha256": hashlib.sha256(b"voice").hexdigest(),
                            "source_lines": [source_line],
                            "metrics": {"duration_seconds": 3.0, "quality_score": 100},
                        }
                    ],
                }
                report_path = destination / "report.json"
                report_path.write_text(json.dumps(report), encoding="utf-8")
                return report_path, report

            with patch(
                "r1999extractor.bootstrap.build_story_voice_candidates",
                side_effect=build,
            ), patch(
                "r1999extractor.bootstrap.extract_story_portraits",
                return_value={"hero.png": "a" * 64},
            ) as portraits:
                first = prepare_player_voice_candidates(
                    roles=("Hero",), data_directory=root
                )
                second = prepare_player_voice_candidates(
                    roles=("Hero",), data_directory=root
                )

            manifest = json.loads(first.read_text(encoding="utf-8"))
            evidence = manifest[PLAYER_VOICE_CANDIDATES_FIELD]

        self.assertEqual(first, second)
        self.assertEqual(builds, [("Hero",)])
        portraits.assert_called_once_with(
            bundles.resolve(),
            {"hero.png"},
            (output / "portraits").resolve(),
            cache_key=story_digest,
        )
        self.assertEqual(len(manifest["voices"]), 1)
        self.assertEqual(evidence["story_index_sha256"], story_digest)
        self.assertEqual(evidence["variants"][0]["source_voice_ids"], ["play_hero_7"])
        self.assertEqual(evidence["variants"][0]["source_line_ids"], ["line:source"])
        self.assertEqual(evidence["variants"][0]["portrait_image_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
