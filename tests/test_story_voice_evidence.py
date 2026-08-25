import hashlib
import json
import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION
from r1999extractor.story_voice_evidence import (
    StoryVoiceEvidenceError,
    _word_error_rate,
    analyze_story_voice_evidence,
    load_story_voice_evidence,
    main,
)


class StoryVoiceEvidenceTest(unittest.TestCase):
    def _write_wav(self, path, frequency):
        path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 8000
        frames = [
            int(8000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(sample_rate * 3)
        ]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(struct.pack(f"<{len(frames)}h", *frames))

    def _make_report(self, root, transcripts=("Clean spoken line", "Second spoken line")):
        root = Path(root)
        candidates = []
        media_ids = (10, 20)
        for index, (media_id, transcript) in enumerate(zip(media_ids, transcripts, strict=True)):
            reference = root / "references" / f"{media_id}.wav"
            self._write_wav(reference, 220 + index * 110)
            candidates.append(
                {
                    "character": "Aderyn",
                    "portrait": "314601.png",
                    "source_bank": "aderyn.bnk",
                    "media_id": media_id,
                    "candidate_origin": "story_line_route",
                    "source_event_ids": [1000 + media_id],
                    "reference": f"references/{media_id}.wav",
                    "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                    "technical_pass": True,
                    "transcript_conflict": False,
                    "source_lines": [
                        {
                            "line_id": f"reverse1999:test:{index}",
                            "text": transcript,
                        }
                    ],
                }
            )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "schema_version": REPORT_VERSION,
                    "groups": [
                        {
                            "character": "Aderyn",
                            "portrait": "314601.png",
                            "source_bank": "aderyn.bnk",
                            "recommended_media_ids_for_audition": list(media_ids),
                        }
                    ],
                    "candidates": candidates,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return report

    def test_word_error_rate_is_exact_and_normalized(self):
        self.assertEqual(_word_error_rate("Hello, brave world!", "hello brave world"), 0.0)
        self.assertEqual(_word_error_rate("one two", "one three"), 0.5)

    def test_writes_bound_advisory_asr_and_speaker_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root)

            output, document = analyze_story_voice_evidence(
                report,
                root / "evidence.json",
                transcriber=lambda path: (
                    "Clean spoken line" if Path(path).stem == "10" else "Second spoken line"
                ),
                speaker_embedder=lambda _path: [1.0, 0.0],
            )

            written = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(document["candidate_count"], 2)
        self.assertEqual(written["candidate_report_sha256"], document["candidate_report_sha256"])
        self.assertEqual(document["candidates"][0]["asr"]["best_word_error_rate"], 0.0)
        self.assertEqual(document["candidates"][0]["content"]["classification"], "speech-observed")
        self.assertEqual(document["candidates"][0]["speaker"]["speaker_count_estimate"], 1)
        self.assertFalse(document["candidates"][0]["speaker"]["group_similarity"]["outlier_risk"])

    def test_nonverbal_expected_plus_asr_noise_is_obvious_rejection_candidate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root, transcripts=("*whimper*", "Normal words"))

            _output, document = analyze_story_voice_evidence(
                report,
                root / "evidence.json",
                transcriber=lambda path: "[noise]" if Path(path).stem == "10" else "Normal words",
            )

        first = document["candidates"][0]
        self.assertEqual(first["content"]["classification"], "non-speech-risk")
        self.assertTrue(first["content"]["obvious_rejection_candidate"])
        self.assertIn("asr-non-speech-marker", first["content"]["reasons"])

    def test_repetitive_laughter_vocalization_is_non_speech_risk(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root, transcripts=("Please come back.", "Normal words"))

            _output, document = analyze_story_voice_evidence(
                report,
                root / "evidence.json",
                transcriber=lambda path: (
                    "Ha ha ha ha!" if Path(path).stem == "10" else "Normal words"
                ),
            )

        first = document["candidates"][0]
        self.assertIn("laughter-vocalization", first["asr"]["non_speech_markers"])
        self.assertTrue(first["content"]["obvious_rejection_candidate"])

    def test_speaker_group_outlier_is_prioritized_but_never_merged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root)

            def embed(path):
                return [0.0, 1.0] if Path(path).stem == "20" else [1.0, 0.0]

            _output, document = analyze_story_voice_evidence(
                report,
                root / "evidence.json",
                speaker_embedder=embed,
            )

        self.assertTrue(document["candidates"][0]["speaker"]["group_similarity"]["outlier_risk"])
        self.assertIn(
            "no automatic merge",
            document["candidates"][0]["speaker"]["group_similarity"]["policy"],
        )

    def test_existing_output_is_not_replaced_and_cli_normalizes_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root)
            output = root / "evidence.json"
            output.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(StoryVoiceEvidenceError, "already exists"):
                analyze_story_voice_evidence(report, output)
            exit_code = main([str(report), "--output", str(output)])

        self.assertEqual(exit_code, 1)

    def test_loader_rejects_sidecar_from_changed_candidate_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._make_report(root)
            output, _document = analyze_story_voice_evidence(report, root / "evidence.json")
            document = json.loads(output.read_text(encoding="utf-8"))
            document["candidates"][0]["candidate_evidence_sha256"] = "0" * 64
            output.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(StoryVoiceEvidenceError, "source evidence changed"):
                load_story_voice_evidence(report, output)


if __name__ == "__main__":
    unittest.main()
