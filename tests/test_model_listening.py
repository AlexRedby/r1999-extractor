import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.bulk_generation import generation_state_codec
from r1999extractor.model_benchmark import benchmark_codec
from r1999extractor.model_listening import (
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session,
    create_listening_session_from_reports,
    listening_progress,
    load_listening_session,
    record_trial_preference,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt  # noqa: E402
    from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
    from PySide6.QtTest import QTest  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    from r1999extractor.model_listening_ui import ModelListeningDialog  # noqa: E402
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QPoint = None
    Qt = None
    QTest = None
    QMediaPlayer = None
    ModelListeningDialog = None


ui_unavailable = QApplication is None


def create_benchmark_fixture(root, *, item_count=2):
    queue = root / "benchmark-queue.jsonl"
    items = []
    for index in range(item_count):
        text = f"Shared listening line {index}."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        items.append(
            {
                "record_type": "generation_item",
                "queue_id": f"queue-{index}",
                "line_id": f"line-{index}",
                "text_sha256": text_hash,
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
    queue.write_text(
        "\n".join(json.dumps(row) for row in (metadata, *items)) + "\n",
        encoding="utf-8",
    )

    models = []
    for model_name, content in (("model-one", b"one"), ("model-two", b"two")):
        model_root = root / model_name
        audio_root = model_root / "audio"
        audio_root.mkdir(parents=True)
        state_items = {}
        for index, item in enumerate(items):
            audio = audio_root / f"line-{index}.wav"
            audio.write_bytes(content + str(index).encode())
            state_items[item["queue_id"]] = {
                "status": "generated",
                "path": audio.relative_to(model_root).as_posix(),
                "line_id": item["line_id"],
                "text_sha256": item["text_sha256"],
                "file_sha256": sha256_file(audio),
            }
        state_path = model_root / "generation-state.json"
        generation_state_codec.write(
            state_path,
            generation_state_codec.new(
                queue_sha256=sha256_file(queue),
                items=state_items,
            ),
            sort_keys=True,
        )
        models.append(
            {
                "provider": "synthetic",
                "model": model_name,
                "manifest": str(model_root / "manifest.json"),
                "state": str(state_path),
            }
        )

    benchmark = root / "benchmark-report.json"
    benchmark_codec.write(
        benchmark,
        benchmark_codec.new(
            generated_at="2026-01-01T00:00:00+00:00",
            source_queue=str(queue),
            sample_count=len(items),
            sample_queue=str(queue),
            manual_review_required=True,
            models=models,
        ),
        sort_keys=True,
    )
    return benchmark


class ModelListeningTest(unittest.TestCase):
    def test_creates_randomized_blind_same_text_trials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root)
            session_path = create_listening_session(benchmark, root / "session", seed=17)
            session = load_listening_session(session_path)
            public_document = session_path.read_text(encoding="utf-8")
            alias_contents = {
                (session_path.parent / path).read_bytes()
                for trial in session["trials"]
                for path in trial["audio"].values()
            }

        self.assertEqual(session["trial_count"], 2)
        self.assertEqual(session["completed_count"], 0)
        self.assertNotIn("model-one", public_document)
        self.assertNotIn("model-two", public_document)
        self.assertEqual(alias_contents, {b"one0", b"one1", b"two0", b"two1"})
        self.assertTrue(all(trial["rating"] is None for trial in session["trials"]))

    def test_imports_existing_per_model_reports_without_regeneration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_audio = root / "first.wav"
            second_audio = root / "second.wav"
            first_audio.write_bytes(b"first")
            second_audio.write_bytes(b"second")
            first_report = root / "first-report.json"
            second_report = root / "second-report.json"
            first_report.write_text(
                json.dumps(
                    {
                        "backend": "first-model",
                        "samples": [
                            {
                                "text": "The same sentence ... with an ellipsis.",
                                "audio": str(first_audio),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            second_report.write_text(
                json.dumps(
                    {
                        "backend": "second-model",
                        "samples": [
                            {
                                "text": "The same sentence … with an ellipsis.",
                                "audio": str(second_audio),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            session_path = create_listening_session_from_reports(
                (first_report, second_report),
                root / "session",
                seed=9,
            )
            session = load_listening_session(session_path)
            public_document = session_path.read_text(encoding="utf-8")

        self.assertEqual(session["trial_count"], 1)
        self.assertEqual(session["source_kind"], "model-reports")
        self.assertNotIn("first-model", public_document)
        self.assertNotIn("second-model", public_document)

    def test_resumes_scores_and_builds_unblinded_aggregate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root)
            session_path = create_listening_session(benchmark, root / "session", seed=5)
            key = json.loads(session_path.with_name(".blind-key.json").read_text(encoding="utf-8"))
            assignments = {item["trial_id"]: item for item in key["assignments"]}

            for trial in load_listening_session(session_path)["trials"]:
                assignment = assignments[trial["trial_id"]]
                a_is_winner = assignment["a"]["model_id"].endswith("model-one")
                record_trial_preference(
                    session_path,
                    trial["trial_id"],
                    "a" if a_is_winner else "b",
                )

            resumed = load_listening_session(session_path)
            report = aggregate_listening_report(session_path, root / "session" / "report.json")

        self.assertEqual(listening_progress(resumed), (2, 2))
        self.assertTrue(report["complete"])
        self.assertTrue(report["manual_selection_required"])
        self.assertEqual(report["models"][0]["model_id"], "synthetic/model-one")
        self.assertEqual(report["models"][0]["preference"]["wins"], 2)
        self.assertEqual(report["models"][0]["preference"]["rate"], 1.0)
        self.assertEqual(report["pairwise"][0]["trials"], 2)

    def test_rejects_invalid_or_duplicate_preferences(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root, item_count=1)
            session_path = create_listening_session(benchmark, root / "session")
            trial_id = load_listening_session(session_path)["trials"][0]["trial_id"]

            with self.assertRaisesRegex(ModelListeningError, "a, b, or tie"):
                record_trial_preference(session_path, trial_id, "maybe")
            record_trial_preference(session_path, trial_id, "tie")
            with self.assertRaisesRegex(ModelListeningError, "already rated"):
                record_trial_preference(session_path, trial_id, "tie")


@unittest.skipIf(ui_unavailable, "PySide6 is not installed")
class ModelListeningDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, ModelListeningDialog):
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def test_saves_rating_and_resumes_to_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root, item_count=1)
            session_path = create_listening_session(benchmark, root / "session")
            dialog = ModelListeningDialog(session_path, auto_play=False)
            dialog.player = Mock()

            dialog.play("a")
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertFalse(dialog.prefer_a.isEnabled())
            dialog.play("b")
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertTrue(dialog.prefer_a.isEnabled())
            dialog.save_preference("a")
            session = load_listening_session(session_path)

            self.assertEqual(session["completed_count"], 1)
            self.assertIsNone(dialog.current_trial)
            self.assertTrue(session_path.with_name("report.json").is_file())
            self.assertIn("complete", dialog.dialogue.toPlainText().lower())
            self.assertGreaterEqual(dialog.minimumHeight(), 430)
            self.assertTrue(dialog.dialogue.isReadOnly())
            self.assertEqual(dialog.player.play.call_count, 2)
            dialog.deleteLater()

    def test_autoplays_a_then_b_before_enabling_preferences(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root, item_count=1)
            session_path = create_listening_session(benchmark, root / "session")
            dialog = ModelListeningDialog(session_path, auto_play=False)
            dialog.player = Mock()

            dialog.start_auto_playback()
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(dialog.started_sides, {"a"})
            self.assertFalse(dialog.tie.isEnabled())

            dialog.media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.application.processEvents()
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)

            self.assertEqual(dialog.started_sides, {"a", "b"})
            self.assertTrue(dialog.tie.isEnabled())
            self.assertEqual(dialog.player.play.call_count, 2)
            dialog.deleteLater()

    def test_seeks_and_skips_within_the_active_sample(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root, item_count=1)
            session_path = create_listening_session(benchmark, root / "session")
            dialog = ModelListeningDialog(session_path, auto_play=False)
            dialog.player = Mock()
            dialog.player.position.return_value = 2_000
            dialog.player.duration.return_value = 120_000

            dialog.duration_changed(120_000)
            dialog.position_changed(65_000)
            dialog.seek_to(90_000)
            dialog.skip_by(5_000)

            self.assertEqual(dialog.seek.maximum(), 120_000)
            self.assertEqual(dialog.seek.value(), 65_000)
            self.assertEqual(dialog.time.text(), "0:07 / 2:00")
            self.assertEqual(
                dialog.player.setPosition.call_args_list,
                [unittest.mock.call(90_000), unittest.mock.call(7_000)],
            )
            dialog.deleteLater()

    def test_clicking_timeline_seeks_to_clicked_position(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark = create_benchmark_fixture(root, item_count=1)
            session_path = create_listening_session(benchmark, root / "session")
            dialog = ModelListeningDialog(session_path, auto_play=False)
            dialog.player = Mock()
            dialog.player.position.return_value = 0
            dialog.player.duration.return_value = 120_000
            dialog.duration_changed(120_000)
            dialog.show()
            self.application.processEvents()

            QTest.mouseClick(
                dialog.seek,
                Qt.MouseButton.LeftButton,
                pos=QPoint(dialog.seek.width() * 3 // 4, dialog.seek.height() // 2),
            )

            sought_position = dialog.player.setPosition.call_args.args[0]
            self.assertAlmostEqual(sought_position, 90_000, delta=2_000)
            self.assertEqual(dialog.seek.value(), sought_position)
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
