import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.story_index import (
    add_story_context,
    classify_speakable_english,
    parse_story_document,
    write_story_index,
)


def payload(speaker, text, *, voice="", portrait=""):
    values = [None] * 18
    values[1] = [1.0, 1.0, 2.5]
    values[11] = ["", "", speaker]
    values[13] = portrait
    values[14] = voice
    values[15] = ["", "", text]
    return values


class StoryIndexTest(unittest.TestCase):
    def test_parses_dialogue_and_narration_into_generic_lines(self):
        document = [
            "title",
            "",
            [
                [7, "step", payload("Brimley", "Hello.", voice="play_7", portrait="7.png")],
                [8, "step", payload("", "Rain falls.")],
            ],
        ]

        lines = parse_story_document(document, "json_story_step_24006")

        self.assertEqual(lines[0].line_id, "reverse1999:24006:7")
        self.assertEqual(lines[0].speaker, "Brimley")
        self.assertEqual(lines[0].source_voice_id, "play_7")
        self.assertEqual(lines[0].source_voice_spec, "play_7")
        self.assertEqual(lines[0].voice_character, "Brimley")
        self.assertEqual(lines[1].speaker, "Narrator")
        self.assertEqual(lines[1].kind, "narration")

    def test_writes_versioned_jsonl_contract(self):
        lines = parse_story_document(
            ["title", "", [[1, "step", payload("A", "Text")]]],
            "json_story_step_1001",
        )
        with TemporaryDirectory() as temporary_directory:
            output = write_story_index(lines, Path(temporary_directory) / "story.jsonl")
            records = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["schema"], "vntts.story-index")
        self.assertEqual(records[0]["schema_version"], 1)
        self.assertEqual(records[1]["record_type"], "line")
        self.assertEqual(len(records[1]["text_sha256"]), 64)

    def test_filters_non_english_and_test_placeholder_lines_by_default(self):
        document = [
            "title",
            "",
            [
                [1, "step", payload("", "只有中文")],
                [2, "step", payload("", "A proper English line.")],
            ],
        ]
        lines = parse_story_document(document, "json_story_step_24006")
        all_lines = parse_story_document(
            document, "json_story_step_24006", include_non_speakable=True
        )
        self.assertEqual([line.text for line in lines], ["A proper English line."])
        self.assertEqual(len(all_lines), 2)
        self.assertFalse(all_lines[0].speakable)
        self.assertEqual(all_lines[0].filter_reason, "no_latin_text")
        self.assertEqual(classify_speakable_english("over", "128"), (False, "test_asset"))

    def test_strips_rich_text_and_adds_neighbor_context(self):
        lines = parse_story_document(
            [
                "title",
                "",
                [
                    [1, "step", payload("A", "<b>First</b> line.")],
                    [2, "step", payload("B", "Second line.")],
                ],
            ],
            "json_story_step_24006",
        )
        lines = add_story_context(lines)
        self.assertEqual(lines[0].text, "First line.")
        self.assertEqual(lines[0].next_text, "Second line.")
        self.assertEqual(lines[1].previous_text, "First line.")


if __name__ == "__main__":
    unittest.main()
