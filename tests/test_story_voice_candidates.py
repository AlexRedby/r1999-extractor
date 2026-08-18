import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.reverse1999_voice_import import ImportedReference
from r1999extractor.story_audio import wwise_event_id
from r1999extractor.story_voice_candidates import (
    BankSnapshot,
    StoryVoiceCandidateError,
    build_story_voice_candidates,
    collect_story_voice_lines,
)
from r1999extractor.voice_reference_quality import VoiceReferenceMetrics


def story_line(number, text, *, portrait="hero.png", media_id=10):
    return {
        "record_type": "line",
        "line_id": f"reverse1999:story:{number}",
        "speaker": "Hero",
        "voice_character": "Hero",
        "portrait": portrait,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_audio_id": str(1000 + number),
        "source_audio_status": "available",
        "source_event": "play_hero_line",
        "source_bank": "hero_story.bnk",
        "source_media_ids": [media_id],
    }


def write_story(path, lines):
    documents = [
        {
            "record_type": "metadata",
            "schema": "vntts.story-index",
            "schema_version": 1,
            "game": "Synthetic",
            "language": "en",
            "generated_at": "2026-08-18T00:00:00+00:00",
            "line_count": len(lines),
        },
        *lines,
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in documents),
        encoding="utf-8",
    )
    return path


def write_bank_index(path, audio_root):
    bank = audio_root / "hero_story.bnk"
    bank.write_bytes(b"synthetic bank")
    stat = bank.stat()
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "game_audio_directory": str(audio_root),
                "bank_count": 1,
                "banks": [
                    {
                        "filename": bank.name,
                        "path": bank.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "embedded_media_ids": [10, 20],
                        "events": [
                            {
                                "event_id": wwise_event_id("play_hero_line"),
                                "media_ids": [10],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path, bank


def clean_metrics(path):
    return VoiceReferenceMetrics(
        path=str(path),
        duration_seconds=3.2,
        peak_dbfs=-3.0,
        rms_dbfs=-18.0,
        silence_ratio=0.1,
        leading_silence_seconds=0.08,
        trailing_silence_seconds=0.08,
        clipping_ratio=0.0,
        quality_score=100,
        technical_flags=(),
    )


class StoryVoiceCandidateTest(unittest.TestCase):
    def test_collects_only_exact_installed_role_and_validates_text_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    story_line(1, "Exact hero line."),
                    {**story_line(2, "Unavailable."), "source_audio_status": "absent"},
                    {**story_line(3, "Other."), "voice_character": "Other"},
                ],
            )
            expected_digest = hashlib.sha256(story.read_bytes()).hexdigest()

            lines, digest = collect_story_voice_lines(story, ["Hero"])

        self.assertEqual([line.line_id for line in lines], ["reverse1999:story:1"])
        self.assertEqual(digest, expected_digest)

    def test_missing_exact_role_fails_instead_of_borrowing_another_voice(self):
        with TemporaryDirectory() as directory:
            story = write_story(Path(directory) / "story.jsonl", [story_line(1, "Hero")])

            with self.assertRaisesRegex(StoryVoiceCandidateError, "Missing"):
                collect_story_voice_lines(story, ["Missing"])

    def test_normalized_quoted_display_variant_keeps_requested_role_identity(self):
        with TemporaryDirectory() as directory:
            line = story_line(1, "Exact role line.")
            line["speaker"] = '"Mrs. Owen"'
            line["voice_character"] = '"Mrs. Owen"'
            story = write_story(Path(directory) / "story.jsonl", [line])

            lines, _digest = collect_story_voice_lines(story, ["Mrs. Owen"])

        self.assertEqual(lines[0].character, "Mrs. Owen")
        self.assertEqual(lines[0].speaker, '"Mrs. Owen"')

    def test_builds_grouped_checksum_bound_audition_without_manifest_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    story_line(1, "First transcript.", portrait="adult.png"),
                    story_line(2, "Second transcript.", portrait="adult.png"),
                    story_line(3, "Young transcript.", portrait="young.png"),
                ],
            )
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"exact media"},
                routes={wwise_event_id("play_hero_line"): (10,)},
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"normalized wav")
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(output.read_bytes()).hexdigest(),
                    bank,
                )

            report_path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
            )
            reference_digest = hashlib.sha256(
                (report_path.parent / report["candidates"][0]["reference"]).read_bytes()
            ).hexdigest()
            manifest_exists = (report_path.parent / "manifest.json").exists()

        self.assertEqual(report["source_line_count"], 3)
        self.assertEqual(report["group_count"], 2)
        self.assertEqual(report["candidate_count"], 2)
        self.assertTrue(report["candidates"][0]["transcript_conflict"])
        self.assertTrue(report["candidates"][0]["manual_content_review_required"])
        self.assertEqual(reference_digest, report["candidates"][0]["reference_sha256"])
        self.assertFalse(manifest_exists)

    def test_refuses_to_replace_an_existing_candidate_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "candidates"
            output.mkdir()
            story = write_story(root / "story.jsonl", [story_line(1, "Hero")])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, _bank = write_bank_index(root / "banks.json", audio_root)

            with self.assertRaisesRegex(StoryVoiceCandidateError, "already exists"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    output,
                    decoder="true",
                )


if __name__ == "__main__":
    unittest.main()
