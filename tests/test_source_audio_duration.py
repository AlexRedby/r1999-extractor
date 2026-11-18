import hashlib
import json
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.story_index import write_story_index_document

from r1999extractor.source_audio_duration import (
    SourceAudioDurationProbe,
    SourceAudioTiming,
    _cache_checksum,
    annotate_story_index_source_audio_durations,
    classify_source_audio_completeness,
    probe_source_audio_timing,
)
from r1999extractor.source_audio_policy import (
    MAIN_STORY_SOURCE_AUDIO_REASON,
    apply_main_story_source_audio_policy,
)
from r1999extractor.story_audio import AudioResolution


class SourceAudioDurationTest(unittest.TestCase):
    def test_measures_projected_plotvoc_on_old_index_before_classification(self):
        resolutions = []

        class Probe:
            @staticmethod
            def probe(resolution):
                resolutions.append(resolution)
                return SourceAudioTiming(6.25, 927465334, "b" * 64, 24000, 150000, "r-test")

        record = {
            "record_type": "line",
            "line_id": "reverse1999:100631:59",
            "chapter": "100631",
            "sequence": 59,
            "speaker": "Vertin",
            "text": "What really scared me was the possibility of dying ignorant.",
            "kind": "dialogue",
            "collection_kind": "main_story",
            "source_audio_status": "absent",
            "story_audio_cues": [
                {
                    "cue_index": 1,
                    "source_audio_id": "610018168",
                    "source_audio_status": "available",
                    "source_bank": "plotvoc_npc512101chapter6_part05.bnk",
                    "source_event": "play_plotvoc_chap6_npc512101_11129",
                    "source_media_ids": [927465334],
                    "available_media_ids": [927465334],
                }
            ],
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_story_index_document(source, {"language": "en"}, [record])
            result = annotate_story_index_source_audio_durations(
                source, "unused", root / "timed.jsonl", probe=Probe(), chapters=("100631",)
            )
        self.assertEqual(resolutions[0].audio_id, "610018168")
        self.assertEqual(resolutions[0].bank, "plotvoc_npc512101chapter6_part05.bnk")
        self.assertEqual(resolutions[0].media_ids, (927465334,))
        main = result.records[0].to_record()
        self.assertEqual(main["source_audio_status"], "available")
        self.assertEqual(main["source_audio_duration_seconds"], 6.25)
        self.assertEqual(main["source_audio_duration_media_sha256"], "b" * 64)
        self.assertEqual(main["source_audio_completeness"], "full")
        self.assertEqual(result.metadata["source_audio_timing"]["measured_count"], 1)
        self.assertEqual(main["source_audio_cue_index"], 1)
        self.assertNotIn("source_audio_wait_seconds", main)
        self.assertFalse(apply_main_story_source_audio_policy(dict(result.metadata), [main]))
        self.assertEqual(main["source_audio_duration_media_sha256"], "b" * 64)

    def test_multiple_delayed_cues_extend_wait_without_changing_exact_media_timing(self):
        durations = {1014251675: 3.0, 245492838: 6.0, 159582217: 4.0}

        class Probe:
            @staticmethod
            def probe(resolution):
                media_id = resolution.media_ids[0]
                seconds = durations[media_id]
                return SourceAudioTiming(
                    seconds, media_id, "b" * 64, 24000, int(seconds * 24000), "r-test"
                )

        cues = [
            {
                "cue_index": index,
                "source_audio_id": str(media_id),
                "source_audio_status": "available",
                "source_bank": "plotvoc_npcnoname259chapter12.bnk",
                "source_event": f"play_plotvoc_chap12_{media_id}",
                "source_media_ids": [media_id],
                "available_media_ids": [media_id],
                "localized_parameter_2": start,
            }
            for index, (media_id, start) in enumerate(zip(durations, (1.0, 1.5, 1.7)), start=5)
        ]
        record = {
            "collection_kind": "main_story",
            "source_audio_status": "absent",
            "story_audio_cues": cues,
        }
        apply_main_story_source_audio_policy({}, [record])
        timing, wait = probe_source_audio_timing(record, Probe())
        self.assertEqual(wait, 7.5)
        self.assertEqual(timing.duration_seconds, 3.0)
        self.assertEqual(timing.media_id, 1014251675)
        self.assertEqual(timing.sample_count, 72000)

        record.pop("source_audio_cue_index")
        self.assertEqual(probe_source_audio_timing(record, Probe()), (timing, None))

    def test_failed_secondary_cue_leaves_original_available_but_untimed(self):
        class Probe:
            @staticmethod
            def probe(resolution):
                if resolution.media_ids == (22,):
                    return None
                return SourceAudioTiming(3.0, 11, "b" * 64, 24000, 72000, "r-test")

        cues = [
            {
                "cue_index": index,
                "source_audio_id": str(media_id),
                "source_audio_status": "available",
                "source_bank": "plotvoc_npcnoname259chapter12.bnk",
                "source_event": f"play_plotvoc_chap12_{media_id}",
                "source_media_ids": [media_id],
                "available_media_ids": [media_id],
                "localized_parameter_2": float(index),
            }
            for index, media_id in enumerate((11, 22), 1)
        ]
        record = {
            **cues[0],
            "record_type": "line",
            "line_id": "main:1",
            "chapter": "1",
            "sequence": 1,
            "speaker": "A",
            "text": "Original speech.",
            "kind": "dialogue",
            "collection_kind": "main_story",
            "story_audio_cues": cues,
            "source_audio_cue_index": 1,
            "source_audio_duration_seconds": 3.0,
            "source_audio_duration_media_sha256": "b" * 64,
            "source_audio_wait_seconds": 6.0,
        }
        self.assertEqual(probe_source_audio_timing(record, Probe()), (None, None))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_story_index_document(source, {"language": "en"}, [record])
            result = annotate_story_index_source_audio_durations(
                source, "unused", root / "timed.jsonl", probe=Probe()
            )
        updated = result.records[0].to_record()
        self.assertEqual(updated["source_audio_status"], "available")
        self.assertEqual(updated["source_audio_completeness"], "full")
        self.assertFalse(any(key.startswith("source_audio_duration_") for key in updated))
        self.assertNotIn("source_audio_wait_seconds", updated)
        self.assertEqual(result.metadata["source_audio_timing"]["untimed_count"], 1)

    def test_timing_replaces_legacy_main_asr_and_keeps_character_story_partial(self):
        class Probe:
            @staticmethod
            def probe(_resolution):
                return SourceAudioTiming(0.1, 99, "b" * 64, 24000, 2400, "r-test")

        base = {
            "record_type": "line",
            "line_id": "main:1",
            "chapter": "1",
            "sequence": 1,
            "speaker": "A",
            "text": "An English translation that is longer than the foreign speech.",
            "kind": "dialogue",
            "collection_kind": "main_story",
            "source_audio_status": "available",
            "source_bank": "voice.bnk",
            "source_media_ids": [99],
            "available_media_ids": [99],
            "source_audio_completeness": "partial",
            "source_audio_completeness_reason": "asr-transcript-mismatch",
            "source_audio_semantic_evidence_id": "legacy",
            "source_audio_semantic_evidence_entry_id": "legacy-main",
        }
        character = {**base, "line_id": "character:1", "collection_kind": "character_story"}
        character.pop("source_audio_semantic_evidence_id")
        character.pop("source_audio_semantic_evidence_entry_id")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_story_index_document(
                source,
                {"language": "en", "source_audio_semantics": {"applied_count": 1}},
                [base, character],
            )
            result = annotate_story_index_source_audio_durations(
                source, "unused", root / "timed.jsonl", probe=Probe()
            )
        main, other = [line.to_record() for line in result.records]
        self.assertEqual(main["source_audio_completeness"], "full")
        self.assertEqual(main["source_audio_completeness_reason"], MAIN_STORY_SOURCE_AUDIO_REASON)
        self.assertNotIn("source_audio_semantic_evidence_id", main)
        self.assertNotIn("source_audio_semantics", result.metadata)
        self.assertEqual(other["source_audio_completeness"], "partial")
        self.assertEqual(
            other["source_audio_completeness_reason"], "duration-too-short-for-displayed-text"
        )

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

        probe = SourceAudioDurationProbe(Resolver(), decoder=sys.executable, runner=runner)
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

        runner = Mock()
        probe = SourceAudioDurationProbe(Resolver(), decoder=sys.executable, runner=runner)

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
        runner.assert_not_called()

    def test_handles_large_decoder_integers_by_ratio(self):
        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 99, b"exact-wem"

        for sample_count, sample_rate, expected in (
            (10**1000, 1, None),
            (10**300, 1, None),
            (10**1000, 10**999, 10.0),
        ):
            with self.subTest(sample_count=sample_count, sample_rate=sample_rate):

                def runner(command, **_options):
                    return CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps(
                            {
                                "version": "r-test",
                                "sampleRate": sample_rate,
                                "playSamples": sample_count,
                            }
                        ),
                    )

                probe = SourceAudioDurationProbe(Resolver(), decoder=sys.executable, runner=runner)
                timing = probe.probe(None)
                if expected is None:
                    self.assertIsNone(timing)
                else:
                    self.assertEqual(timing.duration_seconds, expected)
                    self.assertEqual(timing.sample_count, sample_count)
                    self.assertEqual(timing.sample_rate, sample_rate)

    def test_shared_cache_reuses_only_exact_media_and_decoder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            decoder = root / "decoder"
            decoder.write_bytes(b"decoder-one")
            payload = [b"first-wem"]

            class Resolver:
                @staticmethod
                def read_single_available_media(_resolution):
                    return 99, payload[0]

            calls = []

            def runner(command, **_options):
                calls.append(command)
                return CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {"version": "r-test", "sampleRate": 24000, "playSamples": 30000}
                    ),
                    stderr="",
                )

            resolution = AudioResolution(
                "installed",
                "resolved_local_media",
                bank="voice.bnk",
                media_ids=(99,),
                available_media_ids=(99,),
            )

            def fresh_probe():
                return SourceAudioDurationProbe(
                    Resolver(), decoder=decoder, runner=runner, cache_dir=root / "cache"
                )

            first = fresh_probe()
            self.assertEqual(first.probe(resolution).duration_seconds, 1.25)
            second = fresh_probe()
            self.assertEqual(second.probe(resolution).duration_seconds, 1.25)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(len(calls), 1)

            cache_file = next((root / "cache" / "duration").glob("*.json"))
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["sample_count"] = 1
            cache_file.write_text(json.dumps(cached), encoding="utf-8")
            self.assertEqual(fresh_probe().probe(resolution).duration_seconds, 1.25)
            self.assertEqual(len(calls), 2)

            payload[0] = b"changed-wem"
            self.assertEqual(
                fresh_probe().probe(resolution).media_sha256, hashlib.sha256(payload[0]).hexdigest()
            )
            self.assertEqual(len(calls), 3)

            decoder.write_bytes(b"decoder-two")
            self.assertEqual(fresh_probe().probe(resolution).duration_seconds, 1.25)
            self.assertEqual(len(calls), 4)

            cache_file = next(
                path
                for path in (root / "cache" / "duration").glob("*.json")
                if json.loads(path.read_text())["decoder_sha256"]
                == hashlib.sha256(b"decoder-two").hexdigest()
            )
            for sample_count, sample_rate, expected, cache_hits in (
                (10**1000, 1, 1.25, 0),
                (10**300, 1, 1.25, 0),
                (10**1000, 10**999, 10.0, 1),
            ):
                with self.subTest(sample_count=sample_count, sample_rate=sample_rate):
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    cached.pop("checksum")
                    cached.update(sample_count=sample_count, sample_rate=sample_rate)
                    cached["checksum"] = _cache_checksum(cached)
                    cache_file.write_text(json.dumps(cached), encoding="utf-8")
                    probe = fresh_probe()
                    self.assertEqual(probe.probe(resolution).duration_seconds, expected)
                    self.assertEqual(probe.cache_hits, cache_hits)
                    self.assertEqual(probe.decoder_probes, 1 - cache_hits)

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
            events = []

            result = annotate_story_index_source_audio_durations(
                source,
                root / "unused-bank-index.json",
                output,
                chapters=("7",),
                probe=Probe(),
                progress=events.append,
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
        self.assertEqual(
            [(event["processed"], event["total"], event["state"]) for event in events],
            [(0, 1, "analyzing"), (1, 1, "analyzing"), (1, 1, "publishing")],
        )

    def test_progress_counts_selected_available_lines_including_untimed_and_cache_hits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            records = [
                {
                    "record_type": "line",
                    "line_id": f"line:{sequence}",
                    "chapter": chapter,
                    "sequence": sequence,
                    "text": "Hello.",
                    "speaker": "Ada",
                    "kind": "dialogue",
                    "source_audio_status": status,
                    "source_bank": "voice.bnk",
                    "source_media_ids": [99],
                    "available_media_ids": [99],
                }
                for sequence, (chapter, status) in enumerate(
                    [
                        ("7", "available"),
                        ("7", "available"),
                        ("8", "available"),
                        ("7", "absent"),
                    ],
                    start=1,
                )
            ]
            write_story_index_document(source, {"language": "en"}, records)

            class Probe:
                cache_hits = 1
                decoder_probes = 0

                def __init__(self):
                    self.results = iter(
                        [
                            SourceAudioTiming(1.25, 99, "b" * 64, 24000, 30000, "r-test"),
                            None,
                        ]
                    )

                def probe(self, _resolution):
                    return next(self.results)

            events = []
            result = annotate_story_index_source_audio_durations(
                source,
                "unused",
                root / "out.jsonl",
                chapters=("7",),
                probe=Probe(),
                progress=events.append,
            )
            self.assertEqual(result.metadata["source_audio_timing"]["cache_hits"], 1)
            self.assertEqual(result.metadata["source_audio_timing"]["untimed_count"], 1)
            self.assertEqual([event["processed"] for event in events], [0, 1, 2, 2])
            self.assertEqual({event["total"] for event in events}, {2})
            failed_events = []
            with patch(
                "r1999extractor.source_audio_duration.write_story_index_document",
                side_effect=OSError("publication failed"),
            ):
                with self.assertRaisesRegex(OSError, "publication failed"):
                    annotate_story_index_source_audio_durations(
                        source,
                        "unused",
                        root / "failed.jsonl",
                        chapters=("7",),
                        probe=Probe(),
                        progress=failed_events.append,
                    )
            self.assertEqual(failed_events[-1]["state"], "publishing")
            self.assertFalse((root / "failed.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
