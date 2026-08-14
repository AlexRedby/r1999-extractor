import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.generation_queue import (
    GenerationQueueError,
    build_generation_queue,
    load_story_records,
    write_generation_queue,
)


def story_line(line_id, speaker, text, status, **fields):
    return {
        "record_type": "line",
        "line_id": line_id,
        "chapter": fields.pop("chapter", "1001"),
        "sequence": fields.pop("sequence", 1),
        "speaker": speaker,
        "voice_character": fields.pop("voice_character", speaker),
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "kind": "dialogue",
        "audio_status": status,
        **fields,
    }


class GenerationQueueTest(unittest.TestCase):
    def test_includes_every_noninstalled_line_with_separate_actions(self):
        records = [
            story_line("installed", "B", "Already voiced.", "installed"),
            story_line("blank", "B", "Generate me.", "no_audio"),
            story_line("optional", "A", "Prefer original.", "configured_unavailable"),
            story_line("broken", "A", "Review me.", "unresolved"),
        ]

        queue = build_generation_queue(records)

        self.assertEqual([item["line_id"] for item in queue], ["broken", "optional", "blank"])
        self.assertEqual(
            [item["action"] for item in queue],
            ["manual_review", "prefer_source_audio", "generate"],
        )
        self.assertTrue(all(item["state"] == "pending" for item in queue))
        self.assertTrue(all("emotion" in item for item in queue))
        self.assertTrue(all("prompt_adapters" in item for item in queue))

    def test_groups_alias_normalized_character_then_story_order(self):
        records = [
            story_line(
                "later",
                "Slouch Hat",
                "Later.",
                "no_audio",
                voice_character="Brimley",
                story_order=2,
            ),
            story_line(
                "earlier",
                "Brimley",
                "Earlier.",
                "no_audio",
                voice_character="Brimley",
                story_order=1,
            ),
        ]
        queue = build_generation_queue(records)
        self.assertEqual([item["line_id"] for item in queue], ["earlier", "later"])

    def test_rejects_hash_drift(self):
        record = story_line("changed", "A", "Current text.", "no_audio")
        record["text_sha256"] = "0" * 64
        with self.assertRaisesRegex(GenerationQueueError, "Text hash"):
            build_generation_queue([record])

    def test_writes_versioned_queue_with_source_checksum(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story_index = root / "story.jsonl"
            story_index.write_text(
                json.dumps(
                    {
                        "record_type": "metadata",
                        "schema": "vntts.story-index",
                        "schema_version": 1,
                        "line_count": 1,
                    }
                )
                + "\n"
                + json.dumps(story_line("line", "A", "Text.", "no_audio"))
                + "\n",
                encoding="utf-8",
            )
            _metadata, records = load_story_records(story_index)
            output, metadata = write_generation_queue(
                build_generation_queue(records), story_index, root / "queue.jsonl"
            )
            rows = [json.loads(row) for row in output.read_text().splitlines()]

        self.assertEqual(rows[0]["schema"], "vntts.voice-generation-queue")
        self.assertEqual(rows[0]["schema_version"], 1)
        self.assertEqual(rows[0]["item_count"], 1)
        self.assertEqual(metadata["source_audio_status_counts"], {"no_audio": 1})
        self.assertEqual(rows[1]["record_type"], "generation_item")


if __name__ == "__main__":
    unittest.main()
