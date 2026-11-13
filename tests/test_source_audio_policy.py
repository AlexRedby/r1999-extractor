import unittest
from copy import deepcopy

from r1999extractor.source_audio_policy import (
    MAIN_STORY_SOURCE_AUDIO_REASON,
    SOURCE_AUDIO_COMPLETENESS_POLICY,
    apply_main_story_source_audio_policy,
    has_full_main_story_source_audio,
)


class SourceAudioPolicyTest(unittest.TestCase):
    def test_only_available_main_story_audio_is_full_by_policy(self):
        for kind in ("main_story", "anecdote", "character_story", None):
            for status in ("available", "absent", "unavailable", "unknown", "unresolved"):
                with self.subTest(kind=kind, status=status):
                    record = {
                        "collection_kind": kind,
                        "source_audio_status": status,
                        "source_voice_id": "123",
                    }
                    self.assertEqual(
                        has_full_main_story_source_audio(record),
                        kind == "main_story" and status == "available",
                    )

    def test_replaces_old_main_asr_without_changing_other_content_and_is_idempotent(self):
        main = {
            "collection_kind": "main_story",
            "source_audio_status": "available",
            "source_audio_completeness": "partial",
            "source_audio_completeness_reason": "asr-transcript-mismatch",
            "source_audio_semantic_evidence_id": "old-evidence",
            "source_audio_semantic_evidence_entry_id": "old-main",
        }
        character = {
            **main,
            "collection_kind": "character_story",
            "source_audio_semantic_evidence_entry_id": "retained-character",
        }
        unavailable = {**main, "source_audio_status": "unavailable"}
        records = [main, character, unavailable]
        retained = deepcopy(records[1:])
        binding = {"evidence_id": "old-evidence", "applied_count": 3}
        metadata = {"source_audio_semantics": binding, "producer_note": "keep"}
        self.assertTrue(apply_main_story_source_audio_policy(metadata, records))
        self.assertEqual(main["source_audio_completeness"], "full")
        self.assertEqual(main["source_audio_completeness_reason"], MAIN_STORY_SOURCE_AUDIO_REASON)
        self.assertNotIn("source_audio_semantic_evidence_entry_id", main)
        self.assertNotIn("source_audio_semantic_evidence_id", main)
        self.assertEqual(records[1:], retained)
        self.assertEqual(metadata["source_audio_semantics"]["applied_count"], 2)
        self.assertEqual(binding["applied_count"], 3)
        self.assertEqual(
            metadata["source_audio_completeness_policy"], SOURCE_AUDIO_COMPLETENESS_POLICY
        )
        self.assertEqual(metadata["producer_note"], "keep")
        self.assertFalse(apply_main_story_source_audio_policy(metadata, records))

    def test_removes_semantic_binding_when_main_policy_replaces_every_annotation(self):
        record = {
            "collection_kind": "main_story",
            "source_audio_status": "available",
            "source_audio_semantic_evidence_id": "old-evidence",
            "source_audio_semantic_evidence_entry_id": "old-main",
        }
        metadata = {"source_audio_semantics": {"applied_count": 1}}
        self.assertTrue(apply_main_story_source_audio_policy(metadata, [record]))
        self.assertNotIn("source_audio_semantics", metadata)


if __name__ == "__main__":
    unittest.main()
