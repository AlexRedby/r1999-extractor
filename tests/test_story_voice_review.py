import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION
from r1999extractor.story_voice_review import (
    StoryVoiceReviewError,
    load_review_session,
    main,
    record_review_decision,
)


class StoryVoiceReviewTest(unittest.TestCase):
    def make_report(self, root):
        root = Path(root)
        reference = root / "references" / "voice.wav"
        reference.parent.mkdir(exist_ok=True)
        reference.write_bytes(b"RIFF synthetic voice bytes")
        reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
        report = {
            "schema": REPORT_SCHEMA,
            "schema_version": REPORT_VERSION,
            "groups": [
                {
                    "character": "Dobharchú",
                    "portrait": "534704.png",
                    "source_bank": "voice.bnk",
                    "recommended_media_ids_for_audition": [951691760],
                }
            ],
            "candidates": [
                {
                    "character": "Dobharchú",
                    "portrait": "534704.png",
                    "source_bank": "voice.bnk",
                    "media_id": 951691760,
                    "reference": "references/voice.wav",
                    "reference_sha256": reference_sha256,
                    "technical_pass": True,
                    "transcript_conflict": False,
                    "source_lines": [{"text": "I never imagined you felt that way."}],
                }
            ],
        }
        path = root / "report.json"
        path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        return path, reference

    def test_loads_checksum_bound_candidate_and_recommendation(self):
        with TemporaryDirectory() as directory:
            report, _reference = self.make_report(directory)

            session = load_review_session(report)

            self.assertEqual(len(session.candidates), 1)
            self.assertTrue(session.candidates[0].recommended)
            self.assertEqual(
                session.candidates[0].transcripts, ("I never imagined you felt that way.",)
            )
            self.assertEqual(session.pending_count, 1)

    def test_records_and_reopens_one_exact_decision(self):
        with TemporaryDirectory() as directory:
            report, _reference = self.make_report(directory)
            candidate = load_review_session(report).candidates[0]

            updated = record_review_decision(
                report,
                candidate.key,
                "accept",
                notes="Longer clean reference",
            )
            reopened = load_review_session(report)

            self.assertEqual(updated.pending_count, 0)
            self.assertEqual(reopened.decisions[candidate.key]["decision"], "accept")
            self.assertEqual(reopened.decisions[candidate.key]["notes"], "Longer clean reference")

    def test_changed_report_or_reference_invalidates_review(self):
        with TemporaryDirectory() as directory:
            report, reference = self.make_report(directory)
            candidate = load_review_session(report).candidates[0]
            record_review_decision(report, candidate.key, "accept")

            document = json.loads(report.read_text(encoding="utf-8"))
            document["generated_at"] = "changed"
            report.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(StoryVoiceReviewError, "report changed"):
                load_review_session(report)

            report.unlink()
            report, reference = self.make_report(directory)
            reference.write_bytes(b"replacement")
            with self.assertRaisesRegex(StoryVoiceReviewError, "checksum changed"):
                load_review_session(report)

    def test_reference_escape_is_rejected(self):
        with TemporaryDirectory() as directory:
            report, _reference = self.make_report(directory)
            document = json.loads(report.read_text(encoding="utf-8"))
            document["candidates"][0]["reference"] = "../outside.wav"
            report.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(StoryVoiceReviewError, "not contained"):
                load_review_session(report)

    def test_cli_requires_key_and_decision_together(self):
        with TemporaryDirectory() as directory:
            report, _reference = self.make_report(directory)

            self.assertEqual(main([str(report), "--decision", "accept"]), 1)


if __name__ == "__main__":
    unittest.main()
