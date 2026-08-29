import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import StoryIndexDocument, voice_generation_action
from vntts_artifacts.game_pack import load_game_pack
from vntts_artifacts.story_index import load_story_index
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from r1999extractor.source_pack import export_source_game_pack
from r1999extractor.story_index import parse_story_document, write_story_index


def payload(speaker, text, *, voice=""):
    values = [None] * 18
    values[1] = [1.0, 1.0, 2.5]
    values[11] = ["", "", speaker]
    values[13] = ""
    values[14] = voice
    values[15] = ["", "", text]
    return values


class VnttsArtifactsCompatibilityTest(unittest.TestCase):
    def test_source_artifacts_round_trip_and_bind_into_portable_pack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_audio = root / "source-audio" / "voice-7.wav"
            source_audio.parent.mkdir()
            source_audio.write_bytes(b"synthetic source audio")

            story_path = root / "story-index.jsonl"
            line = replace(
                parse_story_document(
                    ["title", "", [[1, "step", payload("Matilda", "A synthetic line.")]]],
                    "json_story_step_101301",
                )[0],
                audio_status="installed",
                source_voice_id="voice-7",
                source_bank="synthetic.bnk",
                source_media_ids=(7,),
                available_media_ids=(7,),
                story_group="main:13",
                story_title="Chapter 13",
                previous_text="Earlier synthetic context.",
                next_text="Later synthetic context.",
                audio_reason="exact_synthetic_match",
                collection_id="reverse1999:main-story:13",
                collection_title="Chapter 13",
                collection_kind="main_story",
                collection_order=13,
            )
            write_story_index([line], story_path)

            manifest_path = root / "voice-manifest.json"
            write_voice_manifest(
                manifest_path,
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Matilda",
                            "speaker": "matilda-v1",
                            "aliases": [],
                            "references": ["source-audio/voice-7.wav"],
                        }
                    ],
                },
            )

            story_metadata, story_lines = load_story_index(story_path)
            _manifest, voices = load_voice_manifest(manifest_path, allow_legacy=False)
            raw_line = json.loads(story_path.read_text(encoding="utf-8").splitlines()[1])
            pack = export_source_game_pack(
                root / "delivery",
                story_index=story_path,
                voice_manifest=manifest_path,
                game_version="3.7.0-synthetic",
                created_at="2026-08-16T00:00:00+00:00",
            )
            delivery_document = json.loads(pack.manifest_path.read_text(encoding="utf-8"))
            moved = root / "moved-delivery"
            pack.manifest_path.parent.rename(moved)
            moved_pack = load_game_pack(moved / "game-pack.json")
            moved_story = StoryIndexDocument.load(moved_pack.story_index.path)
            moved_record = moved_story.find(line.line_id)
            moved_collection = moved_story.collections[0]

        self.assertEqual(story_metadata["collections"][0]["collection_id"], line.collection_id)
        self.assertEqual(story_lines[0].line_id, line.line_id)
        self.assertEqual(raw_line["source_audio_status"], "available")
        self.assertEqual(raw_line["source_audio_id"], "voice-7")
        self.assertEqual(raw_line["source_bank"], "synthetic.bnk")
        self.assertEqual(raw_line["source_media_ids"], [7])
        self.assertEqual(raw_line["collection_id"], line.collection_id)
        self.assertTrue(
            {
                "action",
                "delivery",
                "emotion",
                "model",
                "prompt_adapters",
                "provider",
                "retries",
                "seed",
            }.isdisjoint(raw_line)
        )
        self.assertEqual(voices[0].references, ("source-audio/voice-7.wav",))
        self.assertEqual(delivery_document["schema"], "vntts.game-pack")
        self.assertEqual(delivery_document["schema_version"], 2)
        self.assertNotIn("generated_audio", delivery_document["components"])
        self.assertEqual(moved_pack.game_id, "reverse1999")
        self.assertEqual(moved_pack.game_version, "3.7.0-synthetic")
        self.assertEqual(moved_pack.producers[0].name, "reverse1999-extractor")
        self.assertEqual(len(moved_pack.voice_wavs), 1)
        self.assertEqual(moved_pack.voice_wavs[0].path.name, "voice-7.wav")
        self.assertEqual(moved_story.game, "Reverse: 1999")
        self.assertEqual(moved_story.language, "en")
        self.assertEqual(moved_collection.collection_id, line.collection_id)
        self.assertEqual(moved_collection.title, "Chapter 13")
        self.assertEqual(moved_collection.kind, "main_story")
        self.assertEqual(moved_collection.order, 13)
        self.assertEqual(moved_collection.to_record(), story_metadata["collections"][0])
        self.assertIsNotNone(moved_record)
        self.assertEqual(moved_record.to_record(), raw_line)
        self.assertEqual(moved_record.voice_character, "Matilda")
        self.assertEqual(moved_record.previous_text, "Earlier synthetic context.")
        self.assertEqual(moved_record.next_text, "Later synthetic context.")
        self.assertEqual(moved_record.source_audio_reason, "exact_synthetic_match")
        self.assertEqual(moved_record.producer_fields["source_bank"], "synthetic.bnk")
        self.assertEqual(moved_record.producer_fields["source_media_ids"], [7])
        self.assertEqual(
            moved_story.records_for_collection(line.collection_id, speakable_only=True),
            (moved_record,),
        )
        self.assertIsNone(voice_generation_action(moved_record.source_audio_status))
        self.assertEqual(voice_generation_action("absent"), "generate")
        self.assertEqual(voice_generation_action("unavailable"), "prefer_source_audio")
        self.assertEqual(
            voice_generation_action("unknown", unknown_action="resolve_audio"),
            "resolve_audio",
        )
        for binding in moved_pack.artifacts:
            self.assertEqual(len(binding.sha256), 64)


if __name__ == "__main__":
    unittest.main()
