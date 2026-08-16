import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.game_pack import GamePackError, load_game_pack
from vntts_artifacts.voice_manifest import write_voice_manifest

from r1999extractor.source_pack import SourceGamePackError, export_source_game_pack, main
from r1999extractor.story_index import parse_story_document, write_story_index


def payload(speaker="Matilda", text="A source-only synthetic line."):
    values = [None] * 18
    values[1] = [1.0, 1.0, 2.5]
    values[11] = ["", "", speaker]
    values[13] = ""
    values[14] = ""
    values[15] = ["", "", text]
    return values


def create_sources(root, *, reference="references/matilda.wav"):
    story = root / "input" / "story-index.jsonl"
    manifest = root / "input" / "manifest.json"
    reference_path = manifest.parent / reference
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_bytes(b"synthetic wav")
    line = parse_story_document(
        ["title", "", [[1, "step", payload()]]],
        "json_story_step_101301",
    )[0]
    write_story_index([line], story)
    write_voice_manifest(
        manifest,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Matilda",
                    "speaker": "reverse-1999-matilda-game-v1",
                    "aliases": [],
                    "references": [reference],
                }
            ],
        },
    )
    return story, manifest


class SourceGamePackTest(unittest.TestCase):
    def test_exports_source_only_pack_with_provenance_and_checksum_bindings(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = create_sources(root)
            pack = export_source_game_pack(
                root / "pack",
                story_index=story,
                voice_manifest=manifest,
                game_version="3.7",
                created_at="2026-08-16T12:00:00+00:00",
            )
            document = json.loads(pack.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(pack.game_id, "reverse1999")
            self.assertEqual(pack.game_version, "3.7")
            self.assertEqual(pack.producers[0].name, "reverse1999-extractor")
            self.assertEqual(pack.producers[0].version, "0.1.0")
            self.assertEqual(pack.created_at, "2026-08-16T12:00:00+00:00")
            self.assertNotIn("generated_audio", document["components"])
            self.assertEqual(document["components"]["story_index"]["path"], "story-index.jsonl")
            self.assertEqual(
                document["components"]["voice_manifest"]["path"],
                "voice/manifest.json",
            )
            self.assertEqual(
                document["components"]["voice_wavs"][0]["path"],
                "voice/references/matilda.wav",
            )
            self.assertIsNone(pack.generated_audio)
            self.assertEqual(pack.generated_wavs, ())

    def test_checksum_validation_rejects_mutated_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = create_sources(root)
            pack = export_source_game_pack(
                root / "pack",
                story_index=story,
                voice_manifest=manifest,
                game_version="3.7",
            )
            pack.voice_wavs[0].path.write_bytes(b"mutated")

            with self.assertRaisesRegex(GamePackError, "checksum does not match"):
                load_game_pack(pack.manifest_path)

    def test_refuses_unsafe_reference_without_creating_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = create_sources(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["voices"][0]["references"] = ["../outside.wav"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            output = root / "pack"

            with self.assertRaisesRegex(SourceGamePackError, "safe POSIX-relative"):
                export_source_game_pack(
                    output,
                    story_index=story,
                    voice_manifest=manifest,
                    game_version="3.7",
                )

            self.assertFalse(output.exists())

    def test_refuses_to_replace_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = create_sources(root)
            output = root / "pack"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(SourceGamePackError, "already exists"):
                export_source_game_pack(
                    output,
                    story_index=story,
                    voice_manifest=manifest,
                    game_version="3.7",
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_cli_prints_machine_readable_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = create_sources(root)
            with patch("builtins.print") as output:
                exit_code = main(
                    [
                        "--story-index",
                        str(story),
                        "--voice-manifest",
                        str(manifest),
                        "--game-version",
                        "3.7",
                        "--output",
                        str(root / "pack"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            summary = json.loads(output.call_args.args[0])
            self.assertEqual(summary["game"], {"id": "reverse1999", "version": "3.7"})
            self.assertEqual(summary["voice_reference_count"], 1)


if __name__ == "__main__":
    unittest.main()
