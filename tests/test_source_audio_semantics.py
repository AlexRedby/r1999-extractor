import hashlib
import io
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.story_index import load_story_index_document, write_story_index_document

from r1999extractor.source_audio_semantics import (
    SEMANTIC_EVIDENCE_PREPROCESSING,
    SourceAudioSemanticEvidenceError,
    WhisperTranscriber,
    _canonical_sha256,
    load_source_audio_semantic_evidence,
    normalize_semantic_text,
    publish_source_audio_semantic_evidence,
)


class WhisperTranscriberTest(unittest.TestCase):
    def _transcribe(self, payload):
        transcriber = WhisperTranscriber.__new__(WhisperTranscriber)
        transcriber._pipeline = Mock(return_value={"text": " Words. "})
        self.assertEqual(transcriber(payload), "Words.")
        args, kwargs = transcriber._pipeline.call_args
        self.assertEqual(kwargs, {"return_timestamps": True})
        self.assertEqual(args[0]["sampling_rate"], 16_000)
        self.assertEqual(args[0]["array"].dtype, np.float32)
        return args[0]["array"]

    def test_native_16k_preserves_normalized_pcm_exactly(self):
        pcm = np.array([-32768, -32000, -1, 0, 1, 12345, 32767], dtype="<i2")
        with patch("scipy.signal.resample_poly") as resample:
            actual = self._transcribe(_wav_bytes(pcm, 16_000))
        np.testing.assert_array_equal(actual, pcm.astype(np.float32) / 32768.0)
        resample.assert_not_called()

    def test_resampling_preserves_duration_pitch_amplitude_and_event_time(self):
        for rate in (24_000, 48_000, 44_100):
            with self.subTest(rate=rate):
                time = np.arange(rate) / rate
                pcm = 8_000 * np.sin(2 * np.pi * 440 * time)
                pcm[(time < 0.25) | (time >= 0.75)] = 0
                actual = self._transcribe(_wav_bytes(pcm, rate))
                self.assertEqual(len(actual), 16_000)
                spectrum = np.abs(np.fft.rfft(actual))
                self.assertEqual(int(spectrum.argmax()), 440)
                self.assertAlmostEqual(float(np.max(actual)), 8_000 / 32768.0, places=2)
                active = np.flatnonzero(np.abs(actual) > 0.05)
                self.assertLess(abs(active[0] / 16_000 - 0.25), 0.002)
                self.assertLess(abs(active[-1] / 16_000 - 0.75), 0.002)

    def test_non_integral_output_duration_rounds_up_one_sample(self):
        for rate in (24_000, 48_000, 44_100):
            with self.subTest(rate=rate):
                pcm = np.ones(1001, dtype="<i2")
                actual = self._transcribe(_wav_bytes(pcm, rate))
                self.assertEqual(len(actual), (len(pcm) * 16_000 + rate - 1) // rate)

    def test_rejects_non_mono_pcm16_and_malformed_wav_before_pipeline(self):
        transcriber = WhisperTranscriber.__new__(WhisperTranscriber)
        transcriber._pipeline = Mock()
        for channels, sample_width in ((2, 2), (1, 1), (1, 3)):
            with self.subTest(channels=channels, sample_width=sample_width):
                payload = _wav_bytes(np.zeros(20), 24_000, channels, sample_width)
                with self.assertRaisesRegex(SourceAudioSemanticEvidenceError, "mono PCM16 WAV"):
                    transcriber(payload)
        with self.assertRaisesRegex(SourceAudioSemanticEvidenceError, "Unable to transcribe"):
            transcriber(b"not a WAV")
        transcriber._pipeline.assert_not_called()


def _wav_bytes(pcm, rate, channels=1, sample_width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(channels)
        destination.setsampwidth(sample_width)
        destination.setframerate(rate)
        destination.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    return output.getvalue()


class SourceAudioSemanticsTest(unittest.TestCase):
    def test_normalizes_spoken_identity_without_trusting_punctuation(self):
        self.assertEqual(normalize_semantic_text(" R-Right… don’t! "), "r right don't")

    def test_publishes_exact_full_and_safe_partial_evidence(self):
        payloads = {11: b"exact-stop", 22: b"different-words"}

        class Resolver:
            @staticmethod
            def read_single_available_media(resolution):
                media_id = resolution.media_ids[0]
                return media_id, payloads[media_id]

        transcripts = {
            b"wav:exact-stop": "Stop it!",
            b"wav:different-words": "That will be extra.",
        }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "timed.jsonl"
            evidence = root / "evidence.json"
            successor = root / "semantic.jsonl"
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("model", encoding="utf-8")
            records = [
                _record(1, "Stop it.", 11, payloads[11]),
                _record(2, "They're not parrots.", 22, payloads[22]),
            ]
            write_story_index_document(
                source,
                {"game": "Reverse: 1999", "language": "en"},
                records,
            )

            with (
                patch(
                    "r1999extractor.source_audio_semantics._decode_wem",
                    side_effect=lambda payload, _media_id, _decoder: b"wav:" + payload,
                ),
                patch(
                    "r1999extractor.source_audio_semantics.load_story_index_document",
                    wraps=load_story_index_document,
                ) as load,
            ):
                events = []
                result = publish_source_audio_semantic_evidence(
                    source,
                    root / "unused-bank-index.json",
                    evidence,
                    successor,
                    model,
                    chapters=("7",),
                    resolver=Resolver(),
                    transcriber=transcripts.__getitem__,
                    progress=events.append,
                )
                self.assertEqual(load.call_count, 1)
                self.assertEqual([event["processed"] for event in events], [0, 1, 2, 2])
                self.assertEqual({event["total"] for event in events}, {2})
                self.assertEqual(events[-1]["state"], "publishing")

            authority = load_source_audio_semantic_evidence(evidence)

        self.assertEqual(len(authority["entries"]), 2)
        verdicts = {entry["media_id"]: entry["verdict"] for entry in authority["entries"]}
        self.assertEqual(verdicts, {11: "full", 22: "partial"})
        outcomes = {record.sequence: record.to_record() for record in result.records}
        self.assertEqual(outcomes[1]["source_audio_completeness"], "full")
        self.assertEqual(
            outcomes[1]["source_audio_completeness_reason"],
            "exact-normalized-asr-transcript",
        )
        self.assertEqual(outcomes[2]["source_audio_completeness"], "partial")
        self.assertEqual(
            result.metadata["source_audio_semantics"]["applied_count"],
            2,
        )

    def test_ignores_untimed_and_absent_unknown_cues_in_selected_chapter(self):
        payload = b"exact-stop"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        untimed = _record(2, "No measured duration.", 22, b"untimed")
        untimed.update(
            source_audio_duration_seconds=None,
            source_audio_duration_media_id=None,
            source_audio_duration_media_sha256=None,
        )
        absent = _record(3, "No game audio.", 33, b"absent")
        absent.update(
            source_audio_status="absent",
            source_audio_duration_seconds=None,
            source_audio_duration_media_id=None,
            source_audio_duration_media_sha256=None,
        )
        other_chapter = _record(4, "Unselected chapter.", 44, b"other")
        other_chapter["chapter"] = "8"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "timed.jsonl"
            evidence = root / "evidence.json"
            successor = root / "semantic.jsonl"
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("model", encoding="utf-8")
            write_story_index_document(
                source,
                {"game": "Reverse: 1999", "language": "en"},
                [_record(1, "Stop it.", 11, payload), untimed, absent, other_chapter],
            )

            with patch(
                "r1999extractor.source_audio_semantics._decode_wem",
                return_value=b"wav",
            ):
                events = []
                result = publish_source_audio_semantic_evidence(
                    source,
                    root / "unused-bank-index.json",
                    evidence,
                    successor,
                    model,
                    chapters=("7",),
                    resolver=Resolver(),
                    transcriber=lambda _payload: "Stop it.",
                    progress=events.append,
                )
            authority = load_source_audio_semantic_evidence(evidence)

        outcomes = {record.sequence: record.to_record() for record in result.records}
        self.assertEqual(len(authority["entries"]), 1)
        self.assertEqual(result.metadata["source_audio_semantics"]["applied_count"], 1)
        self.assertEqual(outcomes[1]["source_audio_completeness"], "full")
        self.assertEqual(outcomes[2]["source_audio_completeness"], "unknown")
        self.assertEqual(outcomes[3]["source_audio_completeness"], "unknown")
        self.assertEqual([event["processed"] for event in events], [0, 1, 1])
        self.assertEqual({event["total"] for event in events}, {1})

    def test_rejects_changed_story_before_publication_and_does_not_finish_aborted_asr(self):
        payload = b"exact-stop"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "timed.jsonl"
            write_story_index_document(
                source,
                {"language": "en"},
                [
                    _record(1, "Stop it.", 11, payload),
                    _record(2, "Stop it.", 11, payload),
                ],
            )

            def publish(name, transcriber, events):
                with patch(
                    "r1999extractor.source_audio_semantics._decode_wem", return_value=b"wav"
                ):
                    return publish_source_audio_semantic_evidence(
                        source,
                        "unused",
                        root / f"{name}-evidence.json",
                        root / f"{name}-story.jsonl",
                        root / "unused-model",
                        resolver=Resolver(),
                        transcriber=transcriber,
                        model_sha256="a" * 64,
                        progress=events.append,
                    )

            def change_story(_payload):
                source.write_bytes(source.read_bytes().replace(b"{", b"{ ", 1))
                return "Stop it."

            events = []
            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError, "Source story index changed"
            ):
                publish("changed", change_story, events)
            self.assertFalse((root / "changed-evidence.json").exists())
            self.assertEqual(events[-1]["state"], "analyzing")

            events = []
            with self.assertRaisesRegex(SourceAudioSemanticEvidenceError, "no transcript"):
                publish("aborted", lambda _payload: "", events)
            self.assertEqual([event["processed"] for event in events], [0])
            self.assertFalse((root / "aborted-story.jsonl").exists())

            events = []
            with patch(
                "r1999extractor.source_audio_semantics.write_story_index_document",
                side_effect=OSError("publication failed"),
            ):
                with self.assertRaisesRegex(OSError, "publication failed"):
                    publish("failed", lambda _payload: "Stop it.", events)
            self.assertEqual(events[-1]["state"], "publishing")
            self.assertFalse((root / "failed-story.jsonl").exists())

    def test_same_media_with_different_displayed_text_is_not_reused(self):
        payload = b"shared-media"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            evidence = root / "evidence.json"
            successor = root / "semantic.jsonl"
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("model", encoding="utf-8")
            write_story_index_document(
                source,
                {"game": "Reverse: 1999", "language": "en"},
                [
                    _record(1, "Stop it.", 11, payload),
                    _record(2, "A different sentence.", 11, payload),
                ],
            )

            with patch(
                "r1999extractor.source_audio_semantics._decode_wem",
                return_value=b"wav",
            ):
                result = publish_source_audio_semantic_evidence(
                    source,
                    root / "unused-bank-index.json",
                    evidence,
                    successor,
                    model,
                    chapters=("7",),
                    resolver=Resolver(),
                    transcriber=lambda _payload: "Stop it.",
                )

        outcomes = {record.sequence: record.to_record() for record in result.records}
        self.assertEqual(outcomes[1]["source_audio_completeness"], "full")
        self.assertEqual(outcomes[2]["source_audio_completeness"], "partial")
        self.assertNotEqual(
            outcomes[1]["source_audio_semantic_evidence_entry_id"],
            outcomes[2]["source_audio_semantic_evidence_entry_id"],
        )

    def test_rejects_tampered_model_binding(self):
        payload = b"exact-stop"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            evidence = root / "evidence.json"
            successor = root / "semantic.jsonl"
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("model", encoding="utf-8")
            write_story_index_document(
                source,
                {"game": "Reverse: 1999", "language": "en"},
                [_record(1, "Stop it.", 11, payload)],
            )
            with patch(
                "r1999extractor.source_audio_semantics._decode_wem",
                return_value=b"wav",
            ):
                publish_source_audio_semantic_evidence(
                    source,
                    root / "unused-bank-index.json",
                    evidence,
                    successor,
                    model,
                    resolver=Resolver(),
                    transcriber=lambda _payload: "Stop it.",
                )
            document = json.loads(evidence.read_text(encoding="utf-8"))
            document["entries"][0]["model_sha256"] = "f" * 64
            evidence.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError,
                "model binding changed",
            ):
                load_source_audio_semantic_evidence(evidence)

    def test_changed_model_or_decoder_does_not_publish_evidence_or_cache(self):
        payload = b"exact-stop"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        for changed_input in ("model", "decoder"):
            with self.subTest(changed_input=changed_input), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.jsonl"
                write_story_index_document(
                    source, {"language": "en"}, [_record(1, "Stop it.", 11, payload)]
                )
                model = root / "model"
                model.mkdir()
                model_file = model / "config.json"
                model_file.write_bytes(b"model")
                decoder = root / "decoder"
                decoder.write_bytes(b"decoder")

                def transcribe(_wav):
                    target = model_file if changed_input == "model" else decoder
                    target.write_bytes(b"changed bytes")
                    return "Stop it."

                with (
                    patch("r1999extractor.source_audio_semantics._decode_wem", return_value=b"wav"),
                    self.assertRaisesRegex(SourceAudioSemanticEvidenceError, "changed during"),
                ):
                    publish_source_audio_semantic_evidence(
                        source,
                        "unused-bank",
                        root / "evidence.json",
                        root / "story.jsonl",
                        model,
                        decoder=decoder,
                        resolver=Resolver(),
                        transcriber=transcribe,
                        cache_dir=root / "cache",
                    )
                self.assertFalse((root / "evidence.json").exists())
                self.assertFalse((root / "story.jsonl").exists())
                self.assertFalse((root / "cache").exists())

    def test_legacy_cache_is_retranscribed_and_new_recipe_is_reused(self):
        payload = b"exact-stop"

        class Resolver:
            @staticmethod
            def read_single_available_media(_resolution):
                return 11, payload

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_story_index_document(
                source,
                {"language": "en"},
                [_record(1, "Stop it.", 11, payload)],
            )
            decoder = root / "decoder"
            decoder.write_bytes(b"decoder")
            transcriber = Mock(return_value="Stop it.")

            def publish(name):
                with patch(
                    "r1999extractor.source_audio_semantics._decode_wem", return_value=b"wav"
                ):
                    return publish_source_audio_semantic_evidence(
                        source,
                        "unused-bank",
                        root / f"{name}-evidence.json",
                        root / f"{name}-story.jsonl",
                        "unused-model",
                        model_sha256="a" * 64,
                        decoder=decoder,
                        resolver=Resolver(),
                        transcriber=transcriber,
                        cache_dir=root / "cache",
                    )

            publish("initial")
            cache_path = next((root / "cache" / "semantics").glob("*.json"))
            legacy = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                legacy["identity"].pop("preprocessing"), SEMANTIC_EVIDENCE_PREPROCESSING
            )
            legacy.pop("checksum")
            legacy["checksum"] = _canonical_sha256(legacy)
            legacy_path = cache_path.with_name(f"{_canonical_sha256(legacy['identity'])}.json")
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            cache_path.unlink()
            transcriber.reset_mock()

            migrated = publish("migrated")
            self.assertEqual(migrated.metadata["source_audio_semantics"]["cache_hits"], 0)
            self.assertEqual(migrated.metadata["source_audio_semantics"]["asr_calls"], 1)
            transcriber.assert_called_once_with(b"wav")
            self.assertEqual(json.loads(legacy_path.read_text(encoding="utf-8")), legacy)
            warm = publish("warm")
            self.assertEqual(warm.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(warm.metadata["source_audio_semantics"]["asr_calls"], 0)
            transcriber.assert_called_once()
            authority = load_source_audio_semantic_evidence(root / "warm-evidence.json")
            self.assertEqual(authority["model"]["preprocessing"], SEMANTIC_EVIDENCE_PREPROCESSING)
            authority["model"].pop("preprocessing")
            authority.pop("generated_at")
            authority.pop("evidence_id")
            authority["evidence_id"] = _canonical_sha256(authority)
            legacy_evidence = root / "legacy-evidence.json"
            legacy_evidence.write_text(json.dumps(authority), encoding="utf-8")
            self.assertEqual(load_source_audio_semantic_evidence(legacy_evidence), authority)

    def test_shared_cache_analyzes_only_new_stage_and_invalidates_changed_inputs(self):
        payloads = {11: b"stage-a", 22: b"stage-b"}

        class Resolver:
            @staticmethod
            def read_single_available_media(resolution):
                media_id = resolution.media_ids[0]
                return media_id, payloads[media_id]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("model-one", encoding="utf-8")
            decoder = root / "decoder"
            decoder.write_bytes(b"decoder-one")
            records = [
                _record(1, "Stop it.", 11, payloads[11]),
                _record(2, "Come back.", 22, payloads[22]),
            ]
            records[1]["chapter"] = "8"
            transcripts = {b"stage-a": "Stop it.", b"stage-b": "Come back."}
            calls = []
            progress_events = {}

            def transcribe(wav):
                calls.append(wav)
                return transcripts[wav]

            def publish(name, chapters, *, changed_records=None, language="en", device="cpu"):
                progress_events[name] = []
                source = root / f"{name}-timed.jsonl"
                write_story_index_document(
                    source,
                    {"game": "Reverse: 1999", "language": language},
                    changed_records or records,
                )
                with patch(
                    "r1999extractor.source_audio_semantics._decode_wem",
                    side_effect=lambda payload, _media_id, _decoder: payload,
                ):
                    return publish_source_audio_semantic_evidence(
                        source,
                        root / "unused-bank-index.json",
                        root / f"{name}-evidence.json",
                        root / f"{name}-story.jsonl",
                        model,
                        chapters=chapters,
                        decoder=decoder,
                        resolver=Resolver(),
                        transcriber=transcribe,
                        cache_dir=root / "cache",
                        device=device,
                        progress=progress_events[name].append,
                    )

            first = publish("first", ("7",))
            second = publish("second", ("7", "8"))
            self.assertEqual(first.metadata["source_audio_semantics"]["asr_calls"], 1)
            self.assertEqual(second.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(second.metadata["source_audio_semantics"]["asr_calls"], 1)
            self.assertEqual(calls, [b"stage-a", b"stage-b"])
            self.assertEqual(
                [event["processed"] for event in progress_events["second"]], [0, 1, 2, 2]
            )
            self.assertEqual({event["total"] for event in progress_events["second"]}, {2})
            self.assertEqual(
                len(load_source_audio_semantic_evidence(root / "second-evidence.json")["entries"]),
                2,
            )

            for cache_file in (root / "cache" / "semantics").glob("*.json"):
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached["identity"]["media_sha256"] == hashlib.sha256(payloads[22]).hexdigest():
                    cache_file.write_text("{", encoding="utf-8")
                    break
            else:
                self.fail("Stage B semantic cache entry was not saved")
            repaired = publish("repaired", ("7", "8"))
            self.assertEqual(repaired.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(repaired.metadata["source_audio_semantics"]["asr_calls"], 1)

            changed = [dict(value) for value in records]
            changed[1]["text"] = "Please come back."
            changed[1]["text_sha256"] = hashlib.sha256(
                changed[1]["text"].encode("utf-8")
            ).hexdigest()
            third = publish("third", ("7", "8"), changed_records=changed)
            self.assertEqual(third.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(third.metadata["source_audio_semantics"]["asr_calls"], 1)

            payloads[22] = b"changed-stage-b"
            transcripts[payloads[22]] = "Come back."
            changed_media = [dict(value) for value in records]
            changed_media[1]["source_audio_duration_media_sha256"] = hashlib.sha256(
                payloads[22]
            ).hexdigest()
            media_result = publish("changed-media", ("7", "8"), changed_records=changed_media)
            self.assertEqual(media_result.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(media_result.metadata["source_audio_semantics"]["asr_calls"], 1)

            (model / "config.json").write_text("model-two", encoding="utf-8")
            fourth = publish("fourth", ("7", "8"), changed_records=changed_media)
            self.assertEqual(fourth.metadata["source_audio_semantics"]["asr_calls"], 2)

            decoder.write_bytes(b"decoder-two")
            fifth = publish("fifth", ("7", "8"), changed_records=changed_media)
            self.assertEqual(fifth.metadata["source_audio_semantics"]["asr_calls"], 2)

            sixth = publish("sixth", ("7", "8"), changed_records=changed_media, device="cuda")
            self.assertEqual(sixth.metadata["source_audio_semantics"]["asr_calls"], 2)


def _record(sequence, text, media_id, payload):
    return {
        "record_type": "line",
        "line_id": f"reverse1999:7:{sequence}",
        "chapter": "7",
        "sequence": sequence,
        "speaker": "Ada",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "kind": "dialogue",
        "source_audio_status": "available",
        "source_audio_id": f"voice-{sequence}",
        "source_event": f"play_voice_{sequence}",
        "source_bank": "voice.bnk",
        "source_media_ids": [media_id],
        "available_media_ids": [media_id],
        "source_audio_duration_seconds": 1.0,
        "source_audio_duration_media_id": media_id,
        "source_audio_duration_media_sha256": hashlib.sha256(payload).hexdigest(),
        "source_audio_duration_sample_rate": 24000,
        "source_audio_duration_sample_count": 24000,
        "source_audio_duration_decoder": "r-test",
        "source_audio_completeness": "unknown",
        "source_audio_completeness_reason": ("duration-plausible-but-semantic-coverage-unverified"),
    }


if __name__ == "__main__":
    unittest.main()
