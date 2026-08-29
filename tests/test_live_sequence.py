import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.live_sequence import load_live_sequence_plan
from vntts_artifacts.story_index import write_story_index

from r1999extractor.live_sequence import (
    Reverse1999LiveSequenceError,
    build_live_sequence_chapter,
    build_live_sequence_document,
    write_live_sequence_plan,
)


def payload(speaker="", text=""):
    values = [None] * 18
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


def choice(text, target):
    values = [False, 0, "", ["", "", ""], ["", "", text], 0, 0, "0", False, 0, target]
    return values


class Reverse1999LiveSequenceTest(unittest.TestCase):
    def test_declared_order_preserves_real_transitions_without_inventing_gaps(self):
        chapter = build_live_sequence_chapter(
            story_document(),
            "json_story_step_314601",
            {"reverse1999:314601:2"},
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

    def test_serialized_reverse_array_uses_declared_sequence_order(self):
        document = story_document()
        document[2].reverse()

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
        )

        self.assertEqual(
            [event["sequence"] for event in chapter["events"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            chapter["events"][0]["successors"],
            ["reverse1999:314601:event:2"],
        )

    def test_missing_raw_sequence_is_a_manual_boundary_not_an_inferred_jump(self):
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
        )

        first = chapter["events"][0]
        self.assertEqual(first["kind"], "wait")
        self.assertEqual(first["control"], "manual")
        self.assertEqual(first["successors"], [])
        self.assertEqual(
            chapter["entry_event_ids"],
            [
                "reverse1999:314601:event:1",
                "reverse1999:314601:event:3",
            ],
        )

    def test_visible_text_omitted_from_story_index_becomes_manual_wait(self):
        document = story_document()
        document[2].insert(3, [20, "step-20", payload("", "Unsupported visible text")])

        chapter = build_live_sequence_chapter(
            document,
            "json_story_step_314601",
            {"reverse1999:314601:2"},
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
            )

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
                {"json_story_step_314601": story_document()},
                story,
                source,
                producer_version="test",
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
                    "json_story_step_314601": story_document(),
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
                {"json_story_step_314601": story_document()},
                story,
                source,
                producer_version="test",
            )

            written = write_live_sequence_plan(document, output, story)
            plan = load_live_sequence_plan(written, story)

        self.assertEqual(plan.game_id, "reverse1999")
        self.assertEqual(len(plan.events), 4)


if __name__ == "__main__":
    unittest.main()
