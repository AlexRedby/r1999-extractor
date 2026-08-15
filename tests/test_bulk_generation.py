import hashlib
import json
import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.generated_audio import load_generated_audio_manifest

from r1999extractor.bulk_generation import (
    generation_state_codec,
    review_item,
    run_bulk_generation,
)


class SyntheticProvider:
    provider = "synthetic"
    model = "synthetic-v1"

    def __init__(self):
        self.calls = 0

    def generate(self, _item, output, *, seed):
        self.calls += 1
        write_test_wav(output, frequency=220 + seed)


def write_test_wav(path, *, frequency):
    sample_rate = 16000
    samples = [
        int(math.sin(2 * math.pi * frequency * index / sample_rate) * 4000)
        for index in range(sample_rate // 4)
    ]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", value) for value in samples))


class BulkGenerationTest(unittest.TestCase):
    def test_generates_resumably_and_publishes_exact_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            text = "A synthetic line."
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            metadata = {
                "record_type": "metadata",
                "schema": "vntts.voice-generation-queue",
                "schema_version": 1,
                "game": "Synthetic Game",
                "language": "en",
                "item_count": 1,
            }
            item = {
                "record_type": "generation_item",
                "queue_id": "synthetic:1:" + text_hash[:16],
                "line_id": "synthetic:1",
                "text_sha256": text_hash,
                "speaker": "Test Hero",
                "voice_character": "Test Hero",
                "text": text,
                "action": "generate",
                "prompt_adapters": {"generic": "Speak naturally."},
            }
            queue.write_text(json.dumps(metadata) + "\n" + json.dumps(item) + "\n")
            provider = SyntheticProvider()

            first = run_bulk_generation(queue, root / "output", provider, seed=7)
            second = run_bulk_generation(queue, root / "output", provider, seed=7)
            pending_document, pending_entries = load_generated_audio_manifest(first["manifest"])
            review_item(first["state"], item["queue_id"], "approved")
            document, entries = load_generated_audio_manifest(first["manifest"])
            raw_manifest = json.loads(first["manifest"].read_text(encoding="utf-8"))

        self.assertEqual(first["generated"], 1)
        self.assertEqual(second["generated"], 0)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(pending_document["entry_count"], 0)
        self.assertEqual(pending_entries, ())
        self.assertEqual(document["entry_count"], 1)
        self.assertEqual(entries[0].line_id, "synthetic:1")
        self.assertEqual(entries[0].text_sha256, text_hash)
        self.assertEqual(raw_manifest["entries"][0]["review_status"], "approved")

    def test_filters_generation_by_voice_character(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            items = []
            for character in ("Ready", "Missing"):
                text = f"A line for {character}."
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                items.append(
                    {
                        "record_type": "generation_item",
                        "queue_id": f"synthetic:{character}:{text_hash[:16]}",
                        "line_id": f"synthetic:{character}",
                        "text_sha256": text_hash,
                        "speaker": character,
                        "voice_character": character,
                        "text": text,
                        "action": "generate",
                    }
                )
            metadata = {
                "record_type": "metadata",
                "schema": "vntts.voice-generation-queue",
                "schema_version": 1,
                "game": "Synthetic Game",
                "language": "en",
                "item_count": len(items),
            }
            queue.write_text("\n".join(json.dumps(row) for row in (metadata, *items)) + "\n")
            provider = SyntheticProvider()

            result = run_bulk_generation(
                queue,
                root / "output",
                provider,
                include_characters={"Ready"},
            )

        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["skipped_characters"], 1)
        self.assertEqual(result["skipped_items"], 0)
        self.assertEqual(provider.calls, 1)

    def test_retries_a_previously_failed_item_on_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            text = "Retry this line."
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            metadata = {
                "record_type": "metadata",
                "schema": "vntts.voice-generation-queue",
                "schema_version": 1,
                "game": "Synthetic Game",
                "language": "en",
                "item_count": 1,
            }
            item = {
                "record_type": "generation_item",
                "queue_id": "retry:1:" + text_hash[:16],
                "line_id": "retry:1",
                "text_sha256": text_hash,
                "speaker": "Test Hero",
                "voice_character": "Test Hero",
                "text": text,
                "action": "generate",
            }
            queue.write_text(json.dumps(metadata) + "\n" + json.dumps(item) + "\n")
            output = root / "output"
            output.mkdir()
            generation_state_codec.write(
                output / "generation-state.json",
                generation_state_codec.new(
                    queue_sha256=hashlib.sha256(queue.read_bytes()).hexdigest(),
                    items={
                        item["queue_id"]: {
                            "status": "failed",
                            "attempts": 3,
                            "last_error": "Earlier failure",
                        }
                    },
                ),
                sort_keys=True,
            )
            provider = SyntheticProvider()

            result = run_bulk_generation(queue, output, provider, retries=0)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
