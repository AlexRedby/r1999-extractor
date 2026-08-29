import hashlib
import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory

from vntts_artifacts.story_index import write_story_index_document

from r1999extractor.source_audio_duration import (
    SourceAudioDurationProbe,
    SourceAudioTiming,
    annotate_story_index_source_audio_durations,
    classify_source_audio_completeness,
)
from r1999extractor.story_audio import AudioResolution


class SourceAudioDurationTest(unittest.TestCase):
    def test_completeness_classifier_proves_partial_but_never_assumes_full(self):
        short = SourceAudioTiming(1.0, 1, "a" * 64, 24000, 24000, "r-test")

        self.assertEqual(
            classify_source_audio_completeness(
                "This displayed sentence contains far too many words for one second.",
                short,
            ),
            ("partial", "duration-too-short-for-displayed-text"),
        )
        self.assertEqual(
            classify_source_audio_completeness("Stop it.", short),
            ("unknown", "duration-plausible-but-semantic-coverage-unverified"),
        )

    def test_binds_decoder_sample_duration_to_exact_media_checksum(self):
        payload = b"exact-wem"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 99, payload

        calls = []

        def runner(command, **options):
            calls.append((command, options))
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "version": "r-test",
                        "sampleRate": 24000,
                        "playSamples": 30000,
                    }
                ),
                stderr="",
            )

        probe = SourceAudioDurationProbe(Resolver(), decoder="/usr/bin/true", runner=runner)
        resolution = AudioResolution(
            "installed",
            "resolved_local_media",
            bank="voice.bnk",
            media_ids=(99,),
            available_media_ids=(99,),
        )

        timing = probe.probe(resolution)
        cached = probe.probe(resolution)

        self.assertEqual(timing.duration_seconds, 1.25)
        self.assertEqual(timing.media_id, 99)
        self.assertEqual(timing.media_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(timing.sample_rate, 24000)
        self.assertEqual(timing.sample_count, 30000)
        self.assertEqual(timing.decoder_version, "r-test")
        self.assertEqual(cached, timing)
        self.assertEqual(len(calls), 1)

    def test_leaves_ambiguous_media_route_untimed(self):
        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return None

        probe = SourceAudioDurationProbe(Resolver(), decoder="/usr/bin/true")

        self.assertIsNone(
            probe.probe(
                AudioResolution(
                    "installed",
                    "resolved_local_media",
                    bank="voice.bnk",
                    media_ids=(1, 2),
                    available_media_ids=(1, 2),
                )
            )
        )

    def test_publishes_lossless_story_index_with_verified_timing_contract(self):
        class Probe:
            @staticmethod
            def probe(resolution):
                self.assertEqual(resolution.media_ids, (99,))
                return SourceAudioTiming(1.25, 99, "b" * 64, 24000, 30000, "r-test")

        metadata = {
            "game": "Reverse: 1999",
            "language": "en",
            "producer_note": "preserve me",
        }
        record = {
            "record_type": "line",
            "line_id": "reverse1999:7:1",
            "chapter": "7",
            "sequence": 1,
            "speaker": "Ada",
            "text": "Hello.",
            "kind": "dialogue",
            "source_audio_status": "available",
            "source_audio_id": "voice-7",
            "source_event": "play_voice_7",
            "source_bank": "voice.bnk",
            "source_media_ids": [99],
            "available_media_ids": [99],
        }
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.jsonl"
            output = root / "timed.jsonl"
            write_story_index_document(source, metadata, [record])

            result = annotate_story_index_source_audio_durations(
                source,
                root / "unused-bank-index.json",
                output,
                chapters=("7",),
                probe=Probe(),
            )

        self.assertEqual(result.metadata["producer_note"], "preserve me")
        self.assertEqual(
            result.metadata["source_audio_completion"],
            "verified-media-duration-seconds",
        )
        self.assertEqual(result.metadata["source_audio_timing"]["measured_count"], 1)
        timed = result.records[0].to_record()
        self.assertEqual(timed["source_audio_duration_seconds"], 1.25)
        self.assertEqual(timed["source_audio_duration_media_id"], 99)
        self.assertEqual(timed["source_audio_duration_media_sha256"], "b" * 64)
        self.assertEqual(timed["source_audio_completeness"], "unknown")


if __name__ == "__main__":
    unittest.main()
