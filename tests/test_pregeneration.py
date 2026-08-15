import hashlib
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.bulk_generation import generation_state_codec, load_generation_queue
from r1999extractor.pregeneration import (
    create_pregeneration_job,
    discover_default_moss_model,
    discover_pregeneration_targets,
    generation_command,
    load_pregeneration_job,
    read_generation_progress,
    register_existing_job,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    from r1999extractor.pregeneration_ui import (  # noqa: E402
        PregenerationDialog,
        format_chapter_ids,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    Qt = None
    PregenerationDialog = None
    format_chapter_ids = None


def story_line(line_id, chapter, text, *, source_kind="story", title=None, voice="Matilda"):
    return {
        "record_type": "line",
        "line_id": line_id,
        "chapter": str(chapter),
        "sequence": 1,
        "speaker": voice,
        "voice_character": voice,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "kind": "dialogue",
        "audio_status": "no_audio",
        "source_kind": source_kind,
        "story_group": str(chapter),
        "story_title": title,
        "episode_title": f"Episode {chapter}",
    }


def write_story_index(path, records):
    metadata = {
        "record_type": "metadata",
        "schema": "vntts.story-index",
        "schema_version": 1,
        "line_count": len(records),
    }
    path.write_text(
        "\n".join(json.dumps(row) for row in (metadata, *records)) + "\n",
        encoding="utf-8",
    )


def write_voice_manifest(path):
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Matilda",
                        "references": ["matilda.wav"],
                        "aliases": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class PregenerationTest(unittest.TestCase):
    def test_prefers_complete_local_moss_model(self):
        with TemporaryDirectory() as directory:
            cache_root = Path(directory) / "cache"
            model = cache_root / "models" / "moss-tts-local-v1.5-mlx-int8"
            model.mkdir(parents=True)
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "model.safetensors").write_bytes(b"model")

            discovered = discover_default_moss_model(
                environment={}, cache_root=cache_root, data_root=Path(directory) / "data"
            )

        self.assertEqual(discovered, str(model.resolve()))

    def test_discovers_main_story_chapters_and_whole_anecdotes(self):
        records = [
            story_line("main-1", 101301, "Main one."),
            story_line("main-2", 101302, "Main two."),
            story_line(
                "hero-1",
                315401,
                "Hero one.",
                source_kind="hero_story_plot",
                title="The Eaglet Takes Wing",
            ),
            story_line(
                "hero-2",
                315402,
                "Hero two.",
                source_kind="hero_story_plot",
                title="The Eaglet Takes Wing",
            ),
        ]

        targets = discover_pregeneration_targets(records)

        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].target_id, "main-story:13")
        self.assertEqual(targets[0].title, "Chapter 13")
        self.assertEqual(targets[0].chapters, ("101301", "101302"))
        self.assertEqual(targets[0].line_count, 2)
        self.assertEqual(targets[1].category, "Anecdotes")
        self.assertEqual(targets[1].title, "The Eaglet Takes Wing")
        self.assertEqual(targets[1].line_count, 2)

    def test_discovers_named_activity_character_story(self):
        records = [
            story_line(
                "rhiannon-1",
                314601,
                "Character story line.",
                source_kind="activity_story",
                title="The You That's Meant To Be",
            )
        ]
        records[0]["story_group"] = "13710"

        targets = discover_pregeneration_targets(records)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_id, "activity-story:13710")
        self.assertEqual(targets[0].category, "Character stories")
        self.assertEqual(targets[0].title, "The You That's Meant To Be")

    def test_separates_player_episodes_and_needed_voices_from_full_cast(self):
        records = [
            story_line("part-1", 101201, "Narration one.", voice="Narrator"),
            story_line("part-2", 101202, "Narration two.", voice="Narrator"),
            story_line("voiced", 101201, "Installed dialogue.", voice="Vertin"),
        ]
        records[0]["episode_title"] = "Episode 1"
        records[1]["episode_title"] = "Episode 1"
        records[2]["episode_title"] = "Episode 1"
        records[2]["audio_status"] = "installed"

        target = discover_pregeneration_targets(records)[0]

        self.assertEqual(target.episode_count, 1)
        self.assertEqual(target.line_count, 2)
        self.assertEqual(target.voice_count, 1)
        self.assertEqual(target.cast_count, 2)

    def test_creates_exact_resumable_job_for_selected_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_index = root / "story-index.jsonl"
            voice_manifest = root / "voices.json"
            records = [
                story_line("chapter-12", 101201, "Older chapter."),
                story_line("chapter-13", 101301, "Current chapter."),
            ]
            write_story_index(story_index, records)
            write_voice_manifest(voice_manifest)
            targets = discover_pregeneration_targets(records)
            selected = next(target for target in targets if target.title == "Chapter 13")

            job_directory = create_pregeneration_job(
                story_index,
                targets,
                (selected.target_id,),
                root / "jobs",
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
                now=datetime(2026, 8, 15, 12, 0, 0),
            )
            job = load_pregeneration_job(job_directory)
            metadata, items = load_generation_queue(job["queue"])
            command = generation_command(job_directory)

        self.assertEqual(job_directory.name, "20260815-120000-chapter-13")
        self.assertEqual(job["title"], "Chapter 13")
        self.assertEqual(metadata["item_count"], 1)
        self.assertEqual(items[0]["line_id"], "chapter-13")
        self.assertEqual(job["model"], discover_default_moss_model())
        self.assertEqual(command[-2:], ("--model", job["model"]))

    def test_progress_excludes_missing_voices_and_sound_effects(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_index = root / "story-index.jsonl"
            voice_manifest = root / "voices.json"
            records = [
                story_line("ready", 101301, "A spoken line."),
                story_line("effect", 101301, "*bang*"),
                story_line("missing", 101301, "Missing voice.", voice="Unknown Guard"),
            ]
            write_story_index(story_index, records)
            write_voice_manifest(voice_manifest)
            targets = discover_pregeneration_targets(records)
            job_directory = create_pregeneration_job(
                story_index,
                targets,
                (targets[0].target_id,),
                root / "jobs",
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
            )
            job = load_pregeneration_job(job_directory)
            _metadata, items = load_generation_queue(job["queue"])
            ready = next(item for item in items if item["line_id"] == "ready")
            output = Path(job["output"])
            output.mkdir(parents=True)
            generation_state_codec.write(
                output / "generation-state.json",
                generation_state_codec.new(
                    queue_sha256=sha256_file(job["queue"]),
                    items={
                        ready["queue_id"]: {
                            "status": "generated",
                            "line_id": ready["line_id"],
                            "updated_at": "2026-08-15T12:00:00+00:00",
                        }
                    },
                ),
                sort_keys=True,
            )

            progress = read_generation_progress(job_directory)

        self.assertEqual(progress.status, "complete")
        self.assertEqual(progress.generated, 1)
        self.assertEqual(progress.eligible, 1)
        self.assertEqual(progress.skipped_missing_voice, 1)
        self.assertEqual(progress.skipped_sound_effects, 1)
        self.assertEqual(progress.missing_voice_names, ("Unknown Guard",))
        self.assertEqual(progress.latest_text, "A spoken line.")

    def test_registers_an_existing_cli_generation_as_a_visible_job(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_index = root / "story-index.jsonl"
            voice_manifest = root / "voices.json"
            records = [story_line("ready", 101301, "A spoken line.")]
            write_story_index(story_index, records)
            write_voice_manifest(voice_manifest)
            targets = discover_pregeneration_targets(records)
            original = create_pregeneration_job(
                story_index,
                targets,
                (targets[0].target_id,),
                root / "original-jobs",
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
            )
            original_job = load_pregeneration_job(original)

            registered = register_existing_job(
                original_job["queue"],
                original_job["output"],
                root / "visible-jobs",
                title="Patch 3.7",
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
            )
            repeated = register_existing_job(
                original_job["queue"],
                original_job["output"],
                root / "visible-jobs",
                title="Patch 3.7",
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
            )
            job = load_pregeneration_job(registered)

        self.assertEqual(registered, repeated)
        self.assertEqual(job["title"], "Patch 3.7")
        self.assertTrue(job["registered_existing_job"])
        self.assertEqual(job["queue"], original_job["queue"])


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class PregenerationDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, PregenerationDialog):
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def test_lists_targets_and_enables_generation_after_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_index = root / "story-index.jsonl"
            voice_manifest = root / "voices.json"
            write_story_index(
                story_index,
                [
                    story_line("main", 101301, "Main line."),
                    story_line(
                        "hero",
                        315401,
                        "Hero line.",
                        source_kind="hero_story_plot",
                        title="The Eaglet Takes Wing",
                    ),
                ],
            )
            write_voice_manifest(voice_manifest)
            dialog = PregenerationDialog(
                story_index=story_index,
                voice_manifest=voice_manifest,
                vntts_python=Path("/test/vntts/python"),
                jobs_root=root / "jobs",
            )

            self.assertEqual(len(dialog.targets), 2)
            self.assertFalse(dialog.start_button.isEnabled())
            item = dialog.target_items["main-story:13"]
            item.setCheckState(0, Qt.CheckState.Checked)

            self.assertTrue(dialog.start_button.isEnabled())
            self.assertIn("1 stories", dialog.selection_summary.text())
            self.assertIn("1 voiceless lines", dialog.selection_summary.text())
            dialog.deleteLater()

    def test_wraps_long_chapter_id_lists_in_tooltips(self):
        self.assertEqual(
            format_chapter_ids(("101201", "101202", "101203", "101204"), per_line=3),
            "101201, 101202, 101203\n101204",
        )


if __name__ == "__main__":
    unittest.main()
