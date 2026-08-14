import unittest

from r1999extractor.structured_story import (
    StructuredSourceSpec,
    audit_story_like_tables,
    extract_structured_story_lines,
)


class StructuredStoryTest(unittest.TestCase):
    def test_extracts_schema_declared_dialogue_and_voice_cue(self):
        language = {"speaker": "Test Speaker", "line": "A synthetic spoken line."}
        tables = {
            "json_test_dialog": [
                [10010, 2, "dialog", 0, 12345, "speaker", "line"],
                [10010, 3, "options", 0, 0, "", "line"],
            ]
        }
        specs = (
            StructuredSourceSpec(
                "json_test_dialog",
                6,
                type_index=2,
                allowed_types=("dialog",),
                speaker_name_index=5,
                voice_index=4,
            ),
        )

        lines = extract_structured_story_lines(language, tables, specs=specs)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].speaker, "Test Speaker")
        self.assertEqual(lines[0].source_voice_spec, "12345")
        self.assertEqual(lines[0].source_kind, "structured_dialogue")
        self.assertEqual(lines[0].audio_status, "unchecked")

    def test_resolves_room_dialog_character_from_interaction_table(self):
        character = ["1001", "speaker"] + [""] * 23
        tables = {
            "json_character": [character],
            "json_room_character_interaction": [
                [1, "1001"] + [0] * 14 + ["7001"]
            ],
            "json_room_character_dialog": [["7001", 1, "", "line"]],
        }
        specs = (StructuredSourceSpec("json_room_character_dialog", 3),)

        lines = extract_structured_story_lines(
            {"speaker": "Test Speaker", "line": "A synthetic room line."},
            tables,
            specs=specs,
        )

        self.assertEqual(lines[0].speaker, "Test Speaker")
        self.assertEqual(lines[0].kind, "dialogue")

    def test_audit_marks_only_explicit_specs_as_handled(self):
        language = {"one": "One", "two": "Two"}
        tables = {
            "json_test_dialog": [[1, 1, "one"]],
            "json_other_story": [[1, "two"]],
            "json_ui": [[1, "one"]],
        }
        specs = (StructuredSourceSpec("json_test_dialog", 2),)

        report = audit_story_like_tables(language, tables, specs)

        self.assertEqual(report["handled_table_count"], 1)
        self.assertEqual(report["reviewed_table_count"], 2)
        self.assertEqual(report["tables"]["json_other_story"]["status"], "reviewed_not_extracted")


if __name__ == "__main__":
    unittest.main()
