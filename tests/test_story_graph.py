import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from r1999extractor.story_graph import StoryGraphError, ordered_step_ids, parse_story_graph
from r1999extractor.story_index import (
    Reverse1999StoryError,
    annotate_activity220_story_lines,
    annotate_anecdote_lines,
    annotate_main_story_episode_lines,
    extract_story_lines,
    parse_story_document,
)


def step(step_id, text, *, conversation_type=1):
    payload = [None] * 18
    payload[0] = conversation_type
    payload[11] = ["Speaker", "", "Speaker"]
    payload[13] = ""
    payload[14] = ""
    payload[15] = [text, "", text]
    return [step_id, f"step-{step_id}", payload]


def regression_documents():
    # Minimal 314001 topology; dialogue text is synthetic, IDs remain game-owned.
    document = [
        "title",
        "",
        [
            step(24, "Before the insertion."),
            step(25, "Retained hidden text.", conversation_type=0),
            step(26, "After the insertion."),
            step(111, "Inserted visible dialogue."),
        ],
    ]
    return document, [[[24, 25, ""]], [[25, 111, ""]], [[111, 26, ""]], []]


class StoryGraphTest(unittest.TestCase):
    def test_graph_order_is_independent_of_numeric_or_serialized_step_order(self):
        graph = parse_story_graph(
            [[[24, 25, ""]], [[25, 111, ""]], [[111, 26, ""]], []],
            [111, 26, 25, 24],
            source="test",
        )
        self.assertEqual(ordered_step_ids(graph), [24, 25, 111, 26])
        self.assertEqual(graph[26], ())

    def test_branch_order_and_cycles_do_not_duplicate_nodes(self):
        graph = parse_story_graph(
            [[[1, 3, "left"], [1, 2, "right"]], [[3, 1, ""]]],
            [1, 2, 3, 4],
            source="test",
        )
        self.assertEqual(graph[1], (3, 2))
        self.assertEqual(ordered_step_ids(graph), [4, 1, 3, 2])

    def test_dangling_references_remain_explicit(self):
        graph = parse_story_graph([[[1, 90, ""]], [[90, 2, ""]]], [1, 2], source="test")
        self.assertEqual(ordered_step_ids(graph), [1, 90, 2])

    def test_invalid_graphs_are_rejected(self):
        for document in (
            None,
            [1],
            [[[1, 2]]],
            [[[True, 2, ""]]],
            [[[1, -1, ""]]],
            [[[1, 2, ""], [3, 4, ""]]],
            [[[1, 2, ""]], [[1, 3, ""]]],
            [[[1, 2, ""], [1, 2, ""]]],
        ):
            with self.subTest(document=document), self.assertRaises(StoryGraphError):
                parse_story_graph(document, [1, 2, 3, 4], source="test")

    def test_extraction_preserves_ids_and_uses_graph_context_in_both_locales(self):
        document, groups = regression_documents()
        for locale in (0, 2):
            with self.subTest(locale=locale):
                lines = parse_story_document(
                    document, "json_story_step_314001", group_document=groups, language_index=locale
                )
                self.assertEqual([line.sequence for line in lines], [24, 111, 26])
                self.assertEqual(lines[1].line_id, "reverse1999:314001:111")
                self.assertEqual(lines[0].next_text, lines[1].text)
                self.assertEqual(lines[1].previous_text, lines[0].text)
                self.assertEqual(lines[1].next_text, lines[2].text)
                self.assertEqual(lines[2].previous_text, lines[1].text)
                self.assertIsNone(lines[-1].next_text)
                annotated = annotate_activity220_story_lines(
                    lines,
                    {},
                    {
                        "json_activity220_episode": [[1, 1, 0, 0, "", 314001]],
                    },
                )
                self.assertEqual([line.story_order for line in annotated], [1001, 1003, 1004])

    def test_main_story_and_anecdote_annotations_preserve_graph_order(self):
        document, groups = regression_documents()
        for source, annotate, tables in (
            (
                "json_story_step_101201",
                annotate_main_story_episode_lines,
                {"json_episode": [[1, 1, 1, "", "", "", "", 101201]]},
            ),
            (
                "json_story_step_301801",
                annotate_anecdote_lines,
                {
                    "json_hero_story": [[1, 1901, "", "", "", 0, 0, 1, "", ""]],
                    "json_episode": [[1, 1901, 4, "", "", "", "", 301801]],
                },
            ),
        ):
            with self.subTest(source=source):
                lines = parse_story_document(document, source, group_document=groups)
                annotated = annotate(lines, {}, tables)
                self.assertEqual(
                    [
                        line.sequence
                        for line in sorted(annotated, key=lambda line: line.story_order)
                    ],
                    [24, 111, 26],
                )

    def test_hidden_editor_text_is_not_a_dialogue_even_when_non_speakable_is_requested(self):
        for conversation_type in (0, 10):
            with self.subTest(conversation_type=conversation_type):
                document = [
                    "title",
                    "",
                    [step(1, "Hidden English text.", conversation_type=conversation_type)],
                ]
                self.assertEqual(
                    parse_story_document(
                        document, "json_story_step_24006", include_non_speakable=True
                    ),
                    [],
                )

    def test_context_stops_at_forks_joins_and_missing_raw_steps(self):
        document = ["title", "", [step(i, f"Line number {i}.") for i in (1, 2, 3, 4)]]
        groups = [[[1, 2, "left"], [1, 3, "right"]], [[2, 4, ""]], [[3, 4, ""]]]
        lines = parse_story_document(document, "json_story_step_24006", group_document=groups)
        by_id = {line.sequence: line for line in lines}
        self.assertIsNone(by_id[1].next_text)
        self.assertIsNone(by_id[4].previous_text)
        self.assertIsNone(by_id[2].next_text)
        self.assertIsNone(by_id[3].previous_text)
        lines = parse_story_document(
            ["title", "", document[2][:2]],
            "json_story_step_24006",
            group_document=[[[1, 90, ""]], [[90, 2, ""]]],
        )
        self.assertIsNone(lines[0].next_text)
        self.assertIsNone(lines[1].previous_text)

    def test_bundle_extraction_reads_paired_graph_and_does_not_resort_result(self):
        document, groups = regression_documents()
        assets = []
        for name, value in (
            ("json_story_step_314001", document),
            ("json_story_group_314001", groups),
        ):
            asset = SimpleNamespace(m_Name=name, m_Script=json.dumps(value))
            assets.append(
                SimpleNamespace(
                    type=SimpleNamespace(name="TextAsset"), read=lambda asset=asset: asset
                )
            )
        with patch(
            "r1999extractor.story_index._load_unity_environment",
            return_value=SimpleNamespace(objects=assets),
        ):
            lines = extract_story_lines("unused")
        self.assertEqual([line.sequence for line in lines], [24, 111, 26])
        with patch(
            "r1999extractor.story_index._load_unity_environment",
            return_value=SimpleNamespace(objects=assets[:1]),
        ):
            with self.assertRaisesRegex(Reverse1999StoryError, "missing graph asset"):
                extract_story_lines("unused")


if __name__ == "__main__":
    unittest.main()
