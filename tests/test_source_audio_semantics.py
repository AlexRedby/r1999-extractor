import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import write_story_index_document

from r1999extractor.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
    normalize_semantic_text,
    publish_source_audio_semantic_evidence,
)


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

            with patch(
                "r1999extractor.source_audio_semantics._decode_wem",
                side_effect=lambda payload, _media_id, _decoder: b"wav:" + payload,
            ):
                result = publish_source_audio_semantic_evidence(
                    source,
                    root / "unused-bank-index.json",
                    evidence,
                    successor,
                    model,
                    chapters=("7",),
                    resolver=Resolver(),
                    transcriber=transcripts.__getitem__,
                )

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
                [_record(1, "Stop it.", 11, payload), untimed, absent],
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
            authority = load_source_audio_semantic_evidence(evidence)

        outcomes = {record.sequence: record.to_record() for record in result.records}
        self.assertEqual(len(authority["entries"]), 1)
        self.assertEqual(result.metadata["source_audio_semantics"]["applied_count"], 1)
        self.assertEqual(outcomes[1]["source_audio_completeness"], "full")
        self.assertEqual(outcomes[2]["source_audio_completeness"], "unknown")
        self.assertEqual(outcomes[3]["source_audio_completeness"], "unknown")

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

            def transcribe(wav):
                calls.append(wav)
                return transcripts[wav]

            def publish(name, chapters, *, changed_records=None, language="en", device="cpu"):
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
                    )

            first = publish("first", ("7",))
            second = publish("second", ("7", "8"))
            self.assertEqual(first.metadata["source_audio_semantics"]["asr_calls"], 1)
            self.assertEqual(second.metadata["source_audio_semantics"]["cache_hits"], 1)
            self.assertEqual(second.metadata["source_audio_semantics"]["asr_calls"], 1)
            self.assertEqual(calls, [b"stage-a", b"stage-b"])
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
