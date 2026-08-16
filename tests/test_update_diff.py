import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.update_diff import (
    UpdateDiffError,
    compare_story_indexes,
    main,
)


def line(line_id, *, speaker="Matilda", voice=None, audio_status="installed", text="Text"):
    return {
        "record_type": "line",
        "line_id": line_id,
        "chapter": "1",
        "sequence": 1,
        "speaker": speaker,
        "voice_character": voice or speaker,
        "text": text,
        "kind": "dialogue",
        "speakable": True,
        "audio_status": audio_status,
        "source_audio_status": "available" if audio_status == "installed" else "unknown",
    }


def write_index(path, records, *, extra_metadata=None):
    metadata = {
        "record_type": "metadata",
        "schema": "vntts.story-index",
        "schema_version": 1,
        "line_count": len(records),
    }
    metadata.update(extra_metadata or {})
    path.write_text(
        "\n".join(json.dumps(row) for row in (metadata, *records)) + "\n",
        encoding="utf-8",
    )


class UpdateDiffTest(unittest.TestCase):
    def test_reports_line_mapping_audio_and_eligibility_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.jsonl"
            after = root / "after.jsonl"
            write_index(
                before,
                [
                    line("same"),
                    line("mapping", speaker="Old", voice="Old Voice", audio_status="no_audio"),
                    line("removed", audio_status="unresolved"),
                    line("resolved", audio_status="unresolved"),
                    line("became", audio_status="installed"),
                ],
            )
            write_index(
                after,
                [
                    line("same"),
                    line("mapping", speaker="New", voice="New Voice", audio_status="no_audio"),
                    line("resolved", audio_status="installed"),
                    line("became", audio_status="no_audio"),
                    line("new", audio_status="no_audio"),
                ],
            )

            report = compare_story_indexes(before, after)

        self.assertEqual(report["schema"], "r1999.update-diff")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["line_changes"]["new_line_ids"], ["new"])
        self.assertEqual(report["line_changes"]["removed_line_ids"], ["removed"])
        self.assertEqual(
            report["line_changes"]["changed_line_ids"],
            ["became", "mapping", "resolved"],
        )
        self.assertEqual(
            report["speaker_mapping_changes"],
            [
                {
                    "line_id": "mapping",
                    "before": {"speaker": "Old", "voice_character": "Old Voice"},
                    "after": {"speaker": "New", "voice_character": "New Voice"},
                }
            ],
        )
        self.assertEqual(report["unresolved_audio"]["before_count"], 2)
        self.assertEqual(report["unresolved_audio"]["after_count"], 0)
        self.assertFalse(report["unresolved_audio"]["spike"])
        self.assertEqual(report["unresolved_audio"]["resolved_line_ids"], ["resolved"])
        self.assertEqual(
            report["synthesis_eligibility"]["became_eligible_line_ids"],
            ["became", "new"],
        )
        self.assertEqual(
            report["synthesis_eligibility"]["became_ineligible_line_ids"],
            ["removed", "resolved"],
        )

    def test_reports_schema_drift_and_unresolved_spike(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.jsonl"
            after = root / "after.jsonl"
            write_index(before, [line("line")])
            changed = line("line", audio_status="unresolved")
            changed["collection_id"] = "reverse1999:main-story:1"
            write_index(
                after,
                [changed],
                extra_metadata={
                    "schema_version": 2,
                    "collections": [
                        {
                            "collection_id": "reverse1999:main-story:1",
                            "title": "Chapter 1",
                            "kind": "main_story",
                            "order": 1,
                        }
                    ],
                },
            )

            report = compare_story_indexes(before, after)

        self.assertTrue(report["schema_drift"]["changed"])
        self.assertEqual(report["schema_drift"]["before"]["story_schema_version"], 1)
        self.assertEqual(report["schema_drift"]["after"]["story_schema_version"], 2)
        self.assertEqual(
            report["schema_drift"]["field_changes"]["line_fields"]["added"],
            ["collection_id"],
        )
        self.assertEqual(
            report["schema_drift"]["field_changes"]["collection_fields"]["added"],
            ["collection_id", "kind", "order", "title"],
        )
        self.assertTrue(report["unresolved_audio"]["spike"])
        self.assertEqual(report["unresolved_audio"]["newly_unresolved_line_ids"], ["line"])

    def test_cli_writes_sorted_json_report_and_prints_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.jsonl"
            after = root / "after.jsonl"
            output = root / "report.json"
            write_index(before, [line("old")])
            write_index(after, [line("new", audio_status="no_audio")])
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main([str(before), str(after), "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            queue_created = (root / "generation-queue.jsonl").exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["line_changes"]["new_line_ids"], ["new"])
        self.assertIn("+1 new, -1 removed", stdout.getvalue())
        self.assertIn("schema drift: no", stdout.getvalue())
        self.assertFalse(queue_created)

    def test_rejects_duplicate_line_ids(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.jsonl"
            write_index(path, [line("same"), line("same")])

            with self.assertRaisesRegex(UpdateDiffError, "Duplicate line ID"):
                compare_story_indexes(path, path)


if __name__ == "__main__":
    unittest.main()
