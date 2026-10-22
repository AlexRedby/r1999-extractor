import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts_artifacts.live_sequence import load_live_sequence_plan
from vntts_artifacts.story_index import write_story_index

from r1999extractor.live_sequence import (
    Reverse1999LiveSequenceError,
    build_live_sequence_chapter,
    build_live_sequence_document,
    extract_story_documents,
    write_live_sequence_plan,
)


def payload(speaker="", text=""):
    values = [None] * 18
    values[0] = 1
    values[11] = ["", "", speaker]
    values[15] = ["", "", text]
    return values


def story_line(sequence, text, speaker="Ada"):
    return {
        "record_type": "line",
        "line_id": f"reverse1999:314601:{sequence}",
        "chapter": "314601",
        "sequence": sequence,
        "speaker": speaker,
        "text": text,
        "kind": "dialogue",
        "source_audio_status": "absent",
    }


def story_document():
    return [
        "title",
        "",
        [
            [1, "step-1", payload()],
            [2, "step-2", payload("Ada", "Canonical speech.")],
            [3, "step-3", payload("Ada", "...")],
            [4, "step-4", payload()],
        ],
    ]


def story_group(*step_ids):
    step_ids = step_ids or (1, 2, 3, 4)
    return [[[source, target, ""]] for source, target in zip(step_ids, step_ids[1:])]


def story_documents():
    return {
        "json_story_step_314601": story_document(),
        "json_story_group_314601": story_group(),
    }


def choice(text, target):
    values = [False, 0, "", ["", "", ""], ["", "", text], 0, 0, "0", False, 0, target]
    return values


class Reverse1999LiveSequenceTest(unittest.TestCase):
    def test_raw_step_ids_are_not_coerced_into_different_game_ids(self):
        for value in (True, 2.5, "2"):
            with self.subTest(value=value):
                document = story_document()
                document[2][1][0] = value
                with self.assertRaisesRegex(Reverse1999LiveSequenceError, "no integer sequence"):
                    build_live_sequence_chapter(
                        document,
                        "json_story_step_314601",
                        {"reverse1999:314601:2"},
                        group_document=story_group(),
                    )

    def test_declared_order_preserves_real_transitions_without_inventing_gaps(self):
        chapter = build_live_sequence_chapter(
            story_document(),
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(),
        )

        events = chapter["events"]
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual(
            [event["kind"] for event in events],
            ["transition", "speech", "silent", "transition"],
        )
        self.assertEqual(
            [event["control"] for event in events],
            ["passive", "automatic", "automatic", "terminal"],
        )
        self.assertEqual(
            events[0]["successors"],
            ["reverse1999:314601:event:2"],
        )
        self.assertNotIn("line_id", events[2])

    def test_serialized_reverse_array_uses_story_group_order(self):
        document = story_document()
        document[2].reverse()

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(),
        )

        self.assertEqual(
            [event["sequence"] for event in chapter["events"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            chapter["events"][0]["successors"],
            ["reverse1999:314601:event:2"],
        )

    def test_game_edges_preserve_inserted_step_ids_and_lower_id_terminal(self):
        document = [
            "title",
            "",
            [
                [step_id, "step", payload("Ada", f"Line {step_id}.")]
                for step_id in (26, 111, 24, 25)
            ],
        ]
        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {f"reverse1999:314601:{step_id}" for step_id in (24, 25, 111, 26)},
            group_document=story_group(24, 25, 111, 26),
        )

        self.assertEqual([event["sequence"] for event in chapter["events"]], [24, 25, 111, 26])
        self.assertEqual(chapter["events"][1]["successors"], ["reverse1999:314601:event:111"])
        self.assertEqual(chapter["events"][2]["successors"], ["reverse1999:314601:event:26"])
        self.assertEqual(chapter["events"][-1]["control"], "terminal")
        self.assertEqual(chapter["events"][-1]["successors"], [])

    def test_none_and_background_effect_conversation_text_is_not_visible_speech(self):
        for conversation_type in (0, 10):
            with self.subTest(conversation_type=conversation_type):
                document = story_document()
                document[2][0][2] = payload("Hidden", "\u9690\u85cf\u6587\u672c")
                document[2][0][2][0] = conversation_type
                chapter = build_live_sequence_chapter(
                    document,
                    "json_story_step_314601",
                    {"reverse1999:314601:2"},
                    group_document=story_group(),
                )

                self.assertEqual(chapter["events"][0]["kind"], "transition")
                self.assertEqual(chapter["events"][0]["control"], "passive")
                self.assertNotIn("line_id", chapter["events"][0])

    def test_hidden_orphan_raw_step_is_not_promoted_to_game_entry(self):
        document = story_document()
        document[2].append([999, "orphan", payload()])
        document[2][-1][2][0] = 10

        with self.assertRaisesRegex(Reverse1999LiveSequenceError, "unreachable from game entries"):
            build_live_sequence_chapter(
                document,
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(),
            )

    def test_disconnected_loop_is_not_promoted_to_game_entry(self):
        with self.assertRaisesRegex(Reverse1999LiveSequenceError, "unreachable from game entries"):
            build_live_sequence_chapter(
                story_document(),
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(1, 2) + story_group(3, 4, 3),
            )

    def test_graph_without_game_root_is_rejected(self):
        with self.assertRaisesRegex(Reverse1999LiveSequenceError, "no game entry step"):
            build_live_sequence_chapter(
                story_document(),
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(1, 2, 3, 4, 1),
            )

    def test_single_raw_step_is_entry_with_empty_group(self):
        chapter = build_live_sequence_chapter(
            ["title", "", [[42, "step", payload()]]],
            "json_story_step_314601",
            set(),
            group_document=[],
        )

        self.assertEqual(chapter["entry_event_ids"], ["reverse1999:314601:event:42"])
        self.assertEqual(chapter["events"][0]["control"], "terminal")

    def test_game_entries_keep_serialized_group_order(self):
        chapter = build_live_sequence_chapter(
            story_document(),
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(4, 3) + story_group(1, 2),
        )

        self.assertEqual(
            chapter["entry_event_ids"],
            ["reverse1999:314601:event:4", "reverse1999:314601:event:1"],
        )
        self.assertEqual([event["sequence"] for event in chapter["events"]], [4, 3, 1, 2])

    def test_multiple_group_edges_without_decoded_choice_require_manual_control(self):
        chapter = build_live_sequence_chapter(
            story_document(),
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=[[[1, 2, ""], [1, 3, "alternate"]], [[3, 4, ""]]],
        )

        self.assertEqual(chapter["events"][0]["kind"], "choice")
        self.assertEqual(chapter["events"][0]["control"], "manual")
        self.assertEqual(
            chapter["events"][0]["successors"],
            ["reverse1999:314601:event:2", "reverse1999:314601:event:3"],
        )

    def test_group_edges_referencing_a_missing_step_are_rejected(self):
        with self.assertRaises(Reverse1999LiveSequenceError):
            build_live_sequence_chapter(
                story_document(),
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(1, 999),
            )

    def test_malformed_story_group_is_rejected(self):
        with self.assertRaises(Reverse1999LiveSequenceError):
            build_live_sequence_chapter(
                story_document(),
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=None,
            )

    def test_numeric_gap_uses_explicit_story_group_edge(self):
        document = [
            "title",
            "",
            [
                [1, "step-1", payload()],
                [3, "step-3", payload()],
            ],
        ]

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            set(),
            group_document=story_group(1, 3),
        )

        first = chapter["events"][0]
        self.assertEqual(first["kind"], "transition")
        self.assertEqual(first["control"], "passive")
        self.assertEqual(first["successors"], ["reverse1999:314601:event:3"])
        self.assertEqual(
            chapter["entry_event_ids"],
            [
                "reverse1999:314601:event:1",
            ],
        )

    def test_visible_text_omitted_from_story_index_becomes_manual_wait(self):
        document = story_document()
        document[2].insert(3, [20, "step-20", payload("", "Unsupported visible text")])

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(1, 2, 3, 20, 4),
        )

        event = next(item for item in chapter["events"] if item["sequence"] == 20)
        self.assertEqual(event["kind"], "wait")
        self.assertEqual(event["control"], "manual")

    def test_final_unbound_visible_text_remains_a_manual_wait(self):
        document = story_document()
        document[2].append([30, "step-30", payload("", "Unsupported ending")])

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(1, 2, 3, 4, 30),
        )

        event = chapter["events"][-1]
        self.assertEqual(event["kind"], "wait")
        self.assertEqual(event["control"], "manual")
        self.assertEqual(event["successors"], [])

    def test_raw_choice_targets_replace_the_linear_successor(self):
        document = story_document()
        document[2][1].extend([[], [], [], [], [], [], [], []])
        document[2][1][10] = [
            choice("Stay silent", 3),
            choice("Change the subject", 4),
        ]

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(),
        )

        event = next(item for item in chapter["events"] if item["sequence"] == 2)
        self.assertEqual(event["kind"], "speech")
        self.assertEqual(event["control"], "manual")
        self.assertEqual(
            event["successors"],
            [
                "reverse1999:314601:event:3",
                "reverse1999:314601:event:4",
            ],
        )

    def test_no_text_choice_is_a_manual_choice_event(self):
        document = story_document()
        choice_step = [0, "step-0", payload()]
        choice_step.extend([[], [], [], [], [], [], [], []])
        choice_step[10] = [choice("Continue", 2)]
        document[2].insert(1, choice_step)

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(1, 0, 2, 3, 4),
        )

        event = next(item for item in chapter["events"] if item["sequence"] == 0)
        self.assertEqual(event["kind"], "choice")
        self.assertEqual(event["control"], "manual")
        self.assertEqual(event["successors"], ["reverse1999:314601:event:2"])

    def test_choice_target_must_be_zero_or_an_existing_raw_sequence(self):
        document = story_document()
        document[2][1].extend([[], [], [], [], [], [], [], []])
        document[2][1][10] = [choice("Missing branch", 999)]

        with self.assertRaisesRegex(
            Reverse1999LiveSequenceError,
            "missing raw sequence 999",
        ):
            build_live_sequence_chapter(
                document,
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(),
            )

    def test_zero_choice_target_is_not_a_successor_event(self):
        document = story_document()
        document[2] = document[2][:2]
        document[2][1].extend([[], [], [], [], [], [], [], []])
        document[2][1][10] = [choice("Finish", 0)]

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
            group_document=story_group(1, 2),
        )

        event = next(item for item in chapter["events"] if item["sequence"] == 2)
        self.assertEqual(event["control"], "manual")
        self.assertEqual(event["successors"], [])

    def test_duplicate_raw_sequence_is_rejected(self):
        document = story_document()
        document[2].append([2, "duplicate", payload()])

        with self.assertRaisesRegex(
            Reverse1999LiveSequenceError,
            "repeats or invalidates raw sequence 2",
        ):
            build_live_sequence_chapter(
                document,
                "json_story_step_314601",
                {"reverse1999:314601:2"},
                group_document=story_group(),
            )

    def test_story_line_missing_from_raw_asset_is_rejected(self):
        with self.assertRaisesRegex(
            Reverse1999LiveSequenceError,
            "did not bind story line",
        ):
            build_live_sequence_chapter(
                story_document(),
                "json_story_step_314601",
                {
                    "reverse1999:314601:2",
                    "reverse1999:314601:99",
                },
                group_document=story_group(),
            )

    def test_document_binds_exact_story_and_source_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            source = root / "story.dat"
            source.write_bytes(b"exact source bundle bytes")
            write_story_index(
                story,
                {"game": "Reverse: 1999", "language": "en"},
                [story_line(2, "Canonical speech.")],
            )

            plan = build_live_sequence_document(
                story_documents(),
                story,
                source,
                producer_version="test",
            )

            with self.assertRaisesRegex(Reverse1999LiveSequenceError, "json_story_group_314601"):
                build_live_sequence_document(
                    {"json_story_step_314601": story_document()}, story, source
                )

        self.assertEqual(plan["schema"], "vntts.live-sequence-plan")
        self.assertEqual(plan["producer"]["version"], "test")
        self.assertRegex(plan["story_index_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(plan["source_extract_sha256"], r"^[0-9a-f]{64}$")

    def test_documents_without_an_indexed_chapter_are_not_exported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            source = root / "story.dat"
            source.write_bytes(b"exact source bundle bytes")
            write_story_index(
                story,
                {"game": "Reverse: 1999", "language": "en"},
                [story_line(2, "Canonical speech.")],
            )
            unrelated = ["title", "", [[1, "step", payload("Other", "Other line")]]]

            plan = build_live_sequence_document(
                {
                    **story_documents(),
                    "json_story_step_999999": unrelated,
                },
                story,
                source,
                producer_version="test",
            )

        self.assertEqual(
            [chapter["chapter"] for chapter in plan["chapters"]],
            ["314601"],
        )

    def test_writer_delegates_complete_validation_to_shared_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            source = root / "story.dat"
            output = root / "live-sequence.json"
            source.write_bytes(b"exact source bundle bytes")
            write_story_index(
                story,
                {"game": "Reverse: 1999", "language": "en"},
                [story_line(2, "Canonical speech.")],
            )
            document = build_live_sequence_document(
                story_documents(),
                story,
                source,
                producer_version="test",
            )

            written = write_live_sequence_plan(document, output, story)
            plan = load_live_sequence_plan(written, story)

        self.assertEqual(plan.game_id, "reverse1999")
        self.assertEqual(len(plan.events), 4)

    def test_bundle_extraction_selects_both_step_and_group_assets(self):
        documents = {**story_documents(), "json_story_step_999999": story_document()}
        objects = [
            SimpleNamespace(
                type=SimpleNamespace(name="TextAsset"),
                read=lambda name=name, document=document: SimpleNamespace(
                    m_Name=name, m_Script=json.dumps(document)
                ),
            )
            for name, document in documents.items()
        ]
        with patch(
            "r1999extractor.live_sequence._load_unity_environment",
            return_value=SimpleNamespace(objects=objects),
        ):
            self.assertEqual(
                extract_story_documents("bundle", chapters=("314601",)), story_documents()
            )
            with self.assertRaisesRegex(Reverse1999LiveSequenceError, "json_story_group_999999"):
                extract_story_documents("bundle", chapters=("999999",))
            with self.assertRaisesRegex(Reverse1999LiveSequenceError, "json_story_step_123"):
                extract_story_documents("bundle", chapters=("123",))


if __name__ == "__main__":
    unittest.main()
