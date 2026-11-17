import unittest
from copy import deepcopy

from r1999extractor.source_audio_policy import (
    MAIN_STORY_SOURCE_AUDIO_REASON,
    SOURCE_AUDIO_COMPLETENESS_POLICY,
    apply_main_story_source_audio_policy,
    effective_main_story_source_audio,
    has_full_main_story_source_audio,
)


class SourceAudioPolicyTest(unittest.TestCase):
    def test_projects_installed_plotvoc_from_100631_59_but_not_53_sfx(self):
        cue = {
            "cue_index": 1,
            "source_audio_id": "610018168",
            "source_audio_status": "available",
            "audio_status": "installed",
            "audio_reason": "resolved_local_media",
            "source_bank": "plotvoc_npc512101chapter6_part05.bnk",
            "source_event": "play_plotvoc_chap6_npc512101_11129",
            "source_media_ids": [927465334],
            "available_media_ids": [927465334],
        }
        spoken = {
            "line_id": "reverse1999:100631:59",
            "collection_kind": "main_story",
            "source_audio_status": "absent",
            "source_voice_spec": None,
            "story_audio_cues": [cue],
            "source_audio_duration_seconds": 99,
            "source_audio_duration_media_sha256": "old",
        }
        sound_effect = {
            **spoken,
            "line_id": "reverse1999:100631:53",
            "story_audio_cues": [
                {
                    **cue,
                    "source_audio_id": "500240",
                    "source_bank": "prologuechapter_3",
                    "source_event": "play_chap3_newspaper_turn",
                }
            ],
        }
        before_sfx = deepcopy(sound_effect)
        metadata = {}
        self.assertIs(effective_main_story_source_audio(spoken), cue)
        self.assertTrue(apply_main_story_source_audio_policy(metadata, [spoken, sound_effect]))
        self.assertEqual(spoken["source_audio_status"], "available")
        self.assertEqual(spoken["audio_status"], "installed")
        self.assertEqual(spoken["source_audio_id"], "610018168")
        self.assertEqual(spoken["source_voice_id"], "610018168")
        self.assertEqual(spoken["source_bank"], cue["source_bank"])
        self.assertEqual(spoken["source_event"], cue["source_event"])
        self.assertEqual(spoken["available_media_ids"], [927465334])
        self.assertEqual(spoken["source_audio_completeness"], "full")
        self.assertIsNone(spoken["source_voice_spec"])
        self.assertEqual(spoken["story_audio_cues"], [cue])
        self.assertNotIn("source_audio_duration_seconds", spoken)
        self.assertNotIn("source_audio_duration_media_sha256", spoken)
        self.assertEqual(sound_effect, before_sfx)
        self.assertFalse(apply_main_story_source_audio_policy(metadata, [spoken, sound_effect]))

    def test_primary_precedes_multiple_cues_and_fallback_keeps_game_order(self):
        cues = [
            {
                "cue_index": index,
                "source_audio_id": str(audio_id),
                "source_audio_status": "available",
                "source_bank": "plotvoc_npcnoname259chapter12.bnk",
                "source_event": f"play_plotvoc_chap12_{audio_id}",
                "source_media_ids": [audio_id, audio_id + 1],
                "available_media_ids": [audio_id, audio_id + 1],
            }
            for index, audio_id in enumerate((613302394, 613302393, 613302392), 1)
        ]
        record = {
            "collection_kind": "main_story",
            "source_audio_status": "available",
            "source_audio_id": "primary",
            "story_audio_cues": cues,
        }
        self.assertIs(effective_main_story_source_audio(record), record)
        record["source_audio_status"] = "absent"
        self.assertIs(effective_main_story_source_audio(record), cues[0])

    def test_unavailable_sfx_activity_and_non_main_plotvoc_do_not_override_asr(self):
        cue = {
            "cue_index": 1,
            "source_audio_id": "610018168",
            "source_audio_status": "available",
            "source_bank": "plotvoc_npc512101chapter6_part05.bnk",
            "source_event": "play_plotvoc_chap6_npc512101_11129",
        }
        for kind, changes in (
            ("character_story", {}),
            ("anecdote", {}),
            (None, {}),
            ("main_story", {"source_audio_status": "unavailable"}),
            ("main_story", {"source_bank": "plotvoc_npc1_sfx.bnk"}),
            ("main_story", {"source_event": "play_plotvoc_npc1_sfx_1"}),
            (
                "main_story",
                {
                    "source_bank": "activityvoc_hero3072.bnk",
                    "source_event": "play_activityvoc_hero3072_846",
                },
            ),
        ):
            with self.subTest(kind=kind, changes=changes):
                record = {
                    "collection_kind": kind,
                    "source_audio_status": "absent",
                    "source_audio_completeness": "partial",
                    "source_audio_completeness_reason": "asr-transcript-mismatch",
                    "story_audio_cues": [{**cue, **changes}],
                }
                before = deepcopy(record)
                self.assertIsNone(effective_main_story_source_audio(record))
                self.assertFalse(apply_main_story_source_audio_policy({}, [record]))
                self.assertEqual(record, before)

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
