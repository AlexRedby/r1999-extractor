import hashlib
import json
import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.generated_audio import load_generated_audio_manifest

from r1999extractor.bulk_generation import review_item, run_bulk_generation


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


if __name__ == "__main__":
    unittest.main()
