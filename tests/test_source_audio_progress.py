import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from r1999extractor import source_audio_duration, source_audio_semantics
from r1999extractor.source_audio_progress import (
    SourceAudioProgressStream,
    report_source_audio_progress,
)


class SourceAudioProgressTest(unittest.TestCase):
    def test_stream_flushes_events_and_throttles_only_intermediate_same_state_updates(self):
        stdout = io.StringIO()
        stream = Mock(wraps=stdout)
        writer = SourceAudioProgressStream(stream)
        with (
            patch("builtins.open", side_effect=AssertionError("Progress must not open files")),
            patch("r1999extractor.source_audio_progress.monotonic", return_value=10.0),
        ):
            report_source_audio_progress(writer, "duration", 0, 4)
            self.assertEqual(stream.flush.call_count, 1)
            initial = stdout.getvalue()
            report_source_audio_progress(writer, "duration", 1, 4)
            self.assertEqual(stdout.getvalue(), initial)
            self.assertEqual(stream.flush.call_count, 1)
        with patch("r1999extractor.source_audio_progress.monotonic", return_value=10.21):
            report_source_audio_progress(writer, "duration", 2, 4)
            self.assertEqual(stream.flush.call_count, 2)
            report_source_audio_progress(writer, "duration", 4, 4)
            report_source_audio_progress(writer, "duration", 2, 4, "publishing")
            report_source_audio_progress(writer, "semantics", 1, 2)
            report_source_audio_progress(writer, "semantics", 0, 2)
        events = [
            json.loads(line.removeprefix("R1999_PROGRESS "))
            for line in stdout.getvalue().splitlines()
            if line
        ]
        self.assertEqual(stream.flush.call_count, 6)
        self.assertEqual(
            events[0],
            {
                "schema": "r1999.source-audio-progress",
                "schema_version": 1,
                "stage": "duration",
                "processed": 0,
                "total": 4,
                "state": "analyzing",
            },
        )
        self.assertEqual([event["processed"] for event in events], [0, 2, 4, 2, 1, 0])
        self.assertEqual(events[3]["state"], "publishing")
        self.assertEqual(events[4]["stage"], "semantics")
        self.assertNotIn(": ", initial)

    def test_default_stream_starts_event_on_new_line_after_unterminated_output(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print("decoder status", end="")
            report_source_audio_progress(SourceAudioProgressStream(), "duration", 0, 1)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0], "decoder status")
        self.assertTrue(lines[1].startswith("R1999_PROGRESS "))
        self.assertEqual(json.loads(lines[1].removeprefix("R1999_PROGRESS "))["processed"], 0)

    def test_default_cli_stdout_and_optional_progress_events(self):
        commands = [
            (
                source_audio_duration,
                "annotate_story_index_source_audio_durations",
                ["--story-index", "source", "--bank-index", "banks", "--output", "out"],
                {
                    "source_audio_timing": {
                        "measured_count": 1,
                        "eligible_count": 2,
                        "cache_hits": 1,
                        "decoder_probes": 0,
                    }
                },
                "Measured 1/2 exact source voices (1 cached, 0 decoder probes); wrote out\n",
            ),
            (
                source_audio_semantics,
                "publish_source_audio_semantic_evidence",
                [
                    "--story-index",
                    "source",
                    "--bank-index",
                    "banks",
                    "--model",
                    "model",
                    "--evidence-output",
                    "evidence",
                    "--story-output",
                    "out",
                ],
                {
                    "source_audio_semantics": {
                        "applied_count": 2,
                        "cache_hits": 1,
                        "asr_calls": 1,
                    }
                },
                "Applied 2 exact semantic decisions (1 cached, 1 ASR calls); wrote out\n",
            ),
        ]
        for module, publisher, arguments, metadata, expected in commands:
            with self.subTest(module=module.__name__):
                with patch.object(
                    module,
                    publisher,
                    return_value=SimpleNamespace(metadata=metadata, path=Path("out")),
                ) as publish:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(module.main(arguments), 0)
                    self.assertEqual(stdout.getvalue(), expected)
                    self.assertIsNone(publish.call_args.kwargs["progress"])

                    def publish_with_progress(*_args, **kwargs):
                        stage = "duration" if module is source_audio_duration else "semantics"
                        report_source_audio_progress(kwargs["progress"], stage, 0, 2)
                        report_source_audio_progress(kwargs["progress"], stage, 2, 2)
                        return SimpleNamespace(metadata=metadata, path=Path("out"))

                    publish.side_effect = publish_with_progress
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(module.main(arguments + ["--progress-stdout"]), 0)
                    self.assertIsInstance(
                        publish.call_args.kwargs["progress"], SourceAudioProgressStream
                    )
                    lines = [line for line in stdout.getvalue().splitlines() if line]
                    events = [
                        json.loads(line.removeprefix("R1999_PROGRESS ")) for line in lines[:-1]
                    ]
                    self.assertEqual([event["processed"] for event in events], [0, 2])
                    self.assertEqual(lines[-1] + "\n", expected)


if __name__ == "__main__":
    unittest.main()
