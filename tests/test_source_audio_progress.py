import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from r1999extractor import source_audio_duration, source_audio_semantics
from r1999extractor.source_audio_progress import (
    SourceAudioProgressFile,
    report_source_audio_progress,
)


class SourceAudioProgressTest(unittest.TestCase):
    def test_atomic_snapshot_throttles_only_intermediate_same_state_updates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "progress.json"
            writer = SourceAudioProgressFile(path)
            with patch("r1999extractor.source_audio_progress.monotonic", return_value=10.0):
                report_source_audio_progress(writer, "duration", 0, 3)
                initial = json.loads(path.read_text())
                report_source_audio_progress(writer, "duration", 1, 3)
                self.assertEqual(json.loads(path.read_text()), initial)
            with patch("r1999extractor.source_audio_progress.monotonic", return_value=10.21):
                report_source_audio_progress(writer, "duration", 2, 3)
                self.assertEqual(json.loads(path.read_text())["processed"], 2)
                report_source_audio_progress(writer, "duration", 3, 3)
                self.assertEqual(json.loads(path.read_text())["processed"], 3)
                report_source_audio_progress(writer, "duration", 3, 3, "publishing")
                self.assertEqual(json.loads(path.read_text())["state"], "publishing")
                report_source_audio_progress(writer, "semantics", 0, 2)
                self.assertEqual(json.loads(path.read_text())["stage"], "semantics")
            self.assertEqual(initial, {
                "schema": "r1999.source-audio-progress",
                "schema_version": 1,
                "stage": "duration",
                "processed": 0,
                "total": 3,
                "state": "analyzing",
            })

    def test_default_cli_stdout_and_optional_side_channel(self):
        commands = [
            (
                source_audio_duration,
                "annotate_story_index_source_audio_durations",
                ["--story-index", "source", "--bank-index", "banks", "--output", "out"],
                {"source_audio_timing": {
                    "measured_count": 1, "eligible_count": 2,
                    "cache_hits": 1, "decoder_probes": 0,
                }},
                "Measured 1/2 exact source voices (1 cached, 0 decoder probes); wrote out\n",
            ),
            (
                source_audio_semantics,
                "publish_source_audio_semantic_evidence",
                ["--story-index", "source", "--bank-index", "banks", "--model", "model",
                 "--evidence-output", "evidence", "--story-output", "out"],
                {"source_audio_semantics": {
                    "applied_count": 2, "cache_hits": 1, "asr_calls": 1,
                }},
                "Applied 2 exact semantic decisions (1 cached, 1 ASR calls); wrote out\n",
            ),
        ]
        for module, publisher, arguments, metadata, expected in commands:
            with self.subTest(module=module.__name__):
                with patch.object(module, publisher, return_value=SimpleNamespace(
                    metadata=metadata, path=Path("out")
                )) as publish:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(module.main(arguments), 0)
                    self.assertEqual(stdout.getvalue(), expected)
                    self.assertIsNone(publish.call_args.kwargs["progress"])
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(module.main(arguments + ["--progress-file", "progress"]), 0)
                    self.assertIsInstance(
                        publish.call_args.kwargs["progress"], SourceAudioProgressFile
                    )
                    publish.reset_mock()
                    with redirect_stderr(io.StringIO()) as stderr:
                        self.assertEqual(module.main(arguments + ["--progress-file", "source"]), 1)
                    self.assertIn("must differ", stderr.getvalue())
                    publish.assert_not_called()
                    if module is source_audio_semantics:
                        with redirect_stderr(io.StringIO()):
                            self.assertEqual(module.main(
                                arguments + ["--progress-file", "model/config.json"]
                            ), 1)
                        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
