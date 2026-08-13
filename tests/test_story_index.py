import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.story_index import parse_story_document, write_story_index


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
        self.assertEqual(lines[1].speaker, "Narrator")
        self.assertEqual(lines[1].kind, "narration")

    def test_writes_versioned_jsonl_contract(self):
        lines = parse_story_document(
            ["title", "", [[1, "step", payload("A", "Text")]]], "json_story_step_1"
        )
        with TemporaryDirectory() as temporary_directory:
            output = write_story_index(lines, Path(temporary_directory) / "story.jsonl")
            records = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["schema"], "vntts.story-index")
        self.assertEqual(records[0]["schema_version"], 1)
        self.assertEqual(records[1]["record_type"], "line")


if __name__ == "__main__":
    unittest.main()
