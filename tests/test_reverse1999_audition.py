import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock

from r1999extractor.reverse1999_audition import (
    candidate_banks,
    chapter_tokens,
    filter_dialogue,
    save_speaker_mapping,
    voice_coverage,
)
from r1999extractor.voice_reference_quality import VoiceReferenceMetrics

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt  # noqa: E402
    from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    from r1999extractor.reverse1999_audition_ui import (  # noqa: E402
        Reverse1999AuditionDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    Reverse1999AuditionDialog = None


ui_unavailable = QApplication is None


class _ImmediateThreadPool:
    def start(self, task):
        task.run()


class _ManualThreadPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


class Reverse1999AuditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = None if ui_unavailable else QApplication.instance() or QApplication([])

    def setUp(self):
        if self.application is not None:
            self.application.processEvents()

    def tearDown(self):
        if self.application is None:
            return
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, Reverse1999AuditionDialog):
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    @staticmethod
    def _dependency_data():
        dialogue_index = {
            "dialogue": [
                {
                    "chapter": "24006",
                    "sequence": 1,
                    "speaker_id": "100",
                    "speaker_name": "Reviewed",
                    "text": "First line.",
                },
                {
                    "chapter": "24006",
                    "sequence": 2,
                    "speaker_id": "200",
                    "speaker_name": None,
                    "text": "Second line.",
                },
            ]
        }
        bank_index = {
            "game_audio_directory": "/game",
            "banks": [
                {
                    "path": "reviewed.bnk",
                    "filename": "reviewed-npc100.bnk",
                    "npc_ids": ["100"],
                    "events": [{"media_ids": [42, 43]}],
                },
                {
                    "path": "chapter.bnk",
                    "filename": "activityvoc_npc300_2_4_part01.bnk",
                    "npc_ids": ["300"],
                    "events": [{"media_ids": [44]}],
                },
            ],
        }
        return dialogue_index, bank_index

    def _approve_selected_clip(self, dialog):
        dialog.play_clip()
        self.assertIsNotNone(dialog.current_clip)
        dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        dialog.music_or_sfx.setCurrentIndex(1)
        dialog.multiple_speakers.setCurrentIndex(1)
        dialog.matches_expected_speaker.setCurrentIndex(1)
        dialog.save_clip_review()
        self.assertIsNotNone(dialog.current_review)
        self.assertTrue(dialog.import_button.isEnabled())

    def _assert_review_invalidated(self, dialog, *, prepared_clip=None):
        self.assertIsNone(dialog.current_review)
        self.assertIsNone(dialog._reviewed_clip)
        self.assertFalse(dialog.import_button.isEnabled())
        if prepared_clip is not None:
            self.assertIs(dialog.current_clip, prepared_clip)
        dialog.player.stop.assert_called()

    def test_filters_dialogue_by_chapter_and_selone_mention(self):
        dialogue = [
            {"chapter": "24006", "speaker_id": "310918", "text": "Selone!"},
            {"chapter": "24007", "speaker_id": "520301", "text": "Paddle out."},
        ]

        result = filter_dialogue(dialogue, query="selone", chapter="24006")

        self.assertEqual(result, [dialogue[0]])
        self.assertEqual(chapter_tokens("24006"), ("2_4", "2-4", "plot24"))

    def test_ranks_exact_npc_before_chapter_candidates(self):
        index = {
            "banks": [
                {
                    "path": "chapter.bnk",
                    "filename": "activityvoc_npc624901_2_4_part01.bnk",
                    "npc_ids": ["624901"],
                    "events": [{"media_ids": [12, 11]}],
                },
                {
                    "path": "exact.bnk",
                    "filename": "npc520301_other.bnk",
                    "npc_ids": ["520301"],
                    "events": [{"media_ids": [20]}],
                },
                {
                    "path": "unrelated.bnk",
                    "filename": "activityvoc_npc999999_3_1_part01.bnk",
                    "npc_ids": ["999999"],
                    "events": [{"media_ids": [30]}],
                },
            ]
        }

        result = candidate_banks(index, chapter="24006", speaker_id="520301")

        self.assertEqual(result[0].filename, "npc520301_other.bnk")
        self.assertEqual(result[1].media_ids, (11, 12))
        self.assertEqual(len(result), 2)

    def test_saves_mapping_atomically_and_replaces_same_speaker(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "mappings.json"
            save_speaker_mapping("Selone", "624901", "part01.bnk", "24006", path=output)
            save_speaker_mapping("Selone", "624901", "part02.bnk", "24007", path=output)
            document = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            document["mappings"],
            [
                {
                    "display_name": "Selone",
                    "npc_id": "624901",
                    "bank": "part02.bnk",
                    "chapter": "24007",
                }
            ],
        )

    def test_voice_coverage_prioritizes_unmapped_speakers(self):
        index = {
            "dialogue": [
                {"speaker_id": "1", "speaker_name": "Kamuta"},
                {"speaker_id": "1", "speaker_name": "Kamuta"},
                {"speaker_id": "2", "speaker_name": "Selone"},
            ]
        }

        coverage = voice_coverage(
            index,
            [{"display_name": "Kamuta", "npc_id": "1"}],
        )

        self.assertEqual(coverage[0]["speaker_name"], "Selone")
        self.assertFalse(coverage[0]["mapped"])
        self.assertEqual(coverage[1]["dialogue_count"], 2)

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_dialog_selects_chapter_candidates_and_prefills_npc_id(self):
        dialogue_index = {
            "dialogue": [
                {
                    "chapter": "24006",
                    "sequence": 3,
                    "speaker_id": "310918",
                    "speaker_name": "Fatutu",
                    "text": "Selone! Take this.",
                }
            ]
        }
        bank_index = {
            "game_audio_directory": "/game",
            "banks": [
                {
                    "path": "selone.bnk",
                    "filename": "activityvoc_npc624901_2_4_part01.bnk",
                    "npc_ids": ["624901"],
                    "events": [{"media_ids": [42]}],
                }
            ],
        }
        dialog = Reverse1999AuditionDialog(dialogue_index, bank_index)
        dialog.search.setText("Selone")
        dialog.speaker_name.setText("Selone")
        dialog.npc_id.setText("624901")
        dialog.candidates = candidate_banks(bank_index, chapter="24006", speaker_id="310918")
        for candidate in dialog.candidates:
            dialog.banks.addItem(candidate.filename)
        dialog.banks.setCurrentRow(0)
        dialog.bank_selected(0)

        self.assertEqual(dialog.banks.count(), 1)
        self.assertEqual(dialog.npc_id.text(), "624901")
        self.assertEqual(dialog.media.currentData(), 42)
        dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_scaled_layout_accessibility_focus_order_and_stop_state(self):
        dialogue_index, bank_index = self._dependency_data()
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "clip.wav"
            source.write_bytes(b"voice")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=lambda _bank, _media_id: source,
                quality_analyzer=lambda _path: metrics,
                thread_pool=_ImmediateThreadPool(),
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)
            dialog.show()
            self.application.processEvents()
            controls = (
                dialog.search,
                dialog.chapter,
                dialog.dialogue,
                dialog.banks,
                dialog.media,
                dialog.play_button,
                dialog.cancel_preparation_button,
                dialog.stop_button,
                dialog.music_or_sfx,
                dialog.multiple_speakers,
                dialog.matches_expected_speaker,
                dialog.save_review_button,
                dialog.import_button,
                dialog.speaker_name,
                dialog.npc_id,
                dialog.save_button,
                dialog.close_button,
            )
            for control in controls:
                self.assertTrue(control.accessibleName(), type(control).__name__)
                self.assertTrue(control.accessibleDescription(), type(control).__name__)
            for current, following in zip(controls, controls[1:]):
                next_control = current.nextInFocusChain()
                while not next_control.focusPolicy() & Qt.FocusPolicy.TabFocus:
                    next_control = next_control.nextInFocusChain()
                self.assertIs(next_control, following)

            self.assertEqual(dialog.stop_button.text(), "Stop playback")
            self.assertFalse(dialog.stop_button.isEnabled())
            dialog.play_clip()
            self.assertTrue(dialog.stop_button.isEnabled())
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.stop_button.isEnabled())

            base_font = dialog.font()
            for scale in (1.5, 2.0):
                with self.subTest(scale=scale):
                    font = dialog.font()
                    font.setPointSizeF(base_font.pointSizeF() * scale)
                    dialog.setFont(font)
                    dialog.resize(640, 480)
                    self.application.processEvents()
                    self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)
                    dialog.scroll_area.ensureWidgetVisible(dialog.close_button)
                    self.application.processEvents()
                    self.assertGreater(dialog.scroll_area.verticalScrollBar().value(), 0)
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_dialog_records_reviewed_clip(self):
        dialogue_index = {
            "dialogue": [
                {
                    "chapter": "24006",
                    "sequence": 3,
                    "speaker_id": "521001",
                    "speaker_name": "Selone",
                    "text": "Here, I'll give you a hand!",
                }
            ]
        }
        bank_index = {
            "game_audio_directory": "/game",
            "banks": [
                {
                    "path": "selone.bnk",
                    "filename": "activityvoc_npc521001_2_4.bnk",
                    "npc_ids": ["521001"],
                    "events": [{"media_ids": [42]}],
                }
            ],
        }
        recorded = []
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "42.wav"
            source.write_bytes(b"voice")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=lambda _bank, _media_id: source,
                quality_analyzer=lambda _path: metrics,
                review_recorder=lambda reviewed, **metadata: (
                    recorded.append((reviewed, metadata)) or Path("/reviews.json")
                ),
                thread_pool=_ImmediateThreadPool(),
            )
            dialog.speaker_name.setText("Selone")
            dialog.npc_id.setText("521001")
            dialog.candidates = candidate_banks(bank_index, chapter="24006", speaker_id="521001")
            for candidate in dialog.candidates:
                dialog.banks.addItem(candidate.filename)
            dialog.banks.setCurrentRow(0)
            dialog.bank_selected(0)
            dialog.player = Mock()
            dialog.play_clip()
            self.assertFalse(dialog.save_review_button.isEnabled())
            self.assertIn("play this clip completely", dialog.review_reason.text())
            self.assertIn("Unavailable", dialog.save_review_button.accessibleDescription())

            dialog.save_clip_review()
            self.assertEqual(recorded, [])
            dialog.stop_clip()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.save_review_button.isEnabled())

            dialog.play_clip()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertTrue(dialog.save_review_button.isEnabled())
            self.assertIn("Review ready", dialog.review_reason.text())
            dialog.music_or_sfx.setCurrentIndex(1)
            dialog.multiple_speakers.setCurrentIndex(1)
            dialog.matches_expected_speaker.setCurrentIndex(1)

            dialog.save_clip_review()

            self.assertEqual(len(recorded), 1)
            self.assertTrue(recorded[0][0].approved)
            self.assertEqual(recorded[0][1]["media_id"], 42)
            self.assertIn("approved", dialog.status.text())
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_clip_preparation_runs_asynchronously_with_bounded_busy_state(self):
        dialogue_index, bank_index = self._dependency_data()
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "clip.wav"
            source.write_bytes(b"voice")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            pool = _ManualThreadPool()
            preparer = Mock(return_value=source)
            analyzer = Mock(return_value=metrics)
            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=preparer,
                quality_analyzer=analyzer,
                thread_pool=pool,
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)

            dialog.play_clip()

            self.assertEqual(len(pool.tasks), 1)
            preparer.assert_not_called()
            self.assertTrue(dialog.preparation_runner.active)
            self.assertTrue(dialog.cancel_preparation_button.isEnabled())
            for widget in (
                dialog.search,
                dialog.chapter,
                dialog.dialogue,
                dialog.banks,
                dialog.media,
                dialog.speaker_name,
                dialog.play_button,
            ):
                self.assertFalse(widget.isEnabled(), type(widget).__name__)

            pool.tasks.pop().run()

            preparer.assert_called_once()
            analyzer.assert_called_once_with(source.resolve())
            self.assertIsNotNone(dialog.current_clip)
            self.assertFalse(dialog.preparation_runner.active)
            self.assertFalse(dialog.cancel_preparation_button.isEnabled())
            self.assertTrue(dialog.search.isEnabled())
            dialog.player.play.assert_called_once()
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_preparation_cancel_and_changed_selection_reject_late_results(self):
        dialogue_index, bank_index = self._dependency_data()
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "clip.wav"
            source.write_bytes(b"voice")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            pool = _ManualThreadPool()
            started = Event()
            release = Event()
            analyzer = Mock(return_value=metrics)

            def slow_prepare(_bank, _media_id):
                started.set()
                release.wait(2)
                return source

            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=slow_prepare,
                quality_analyzer=analyzer,
                thread_pool=pool,
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)
            dialog.play_clip()
            cancelled_task = pool.tasks.pop()
            worker = Thread(target=cancelled_task.run)
            worker.start()
            self.assertTrue(started.wait(1))

            dialog.cancel_preparation()
            release.set()
            worker.join(2)
            self.application.processEvents()

            self.assertFalse(worker.is_alive())
            self.assertIsNone(dialog.current_clip)
            analyzer.assert_not_called()
            self.assertTrue(dialog.search.isEnabled())
            self.assertIn("cancelled", dialog.status.text())

            dialog.clip_preparer = lambda _bank, _media_id: source
            dialog.play_clip()
            dialog.media.blockSignals(True)
            dialog.media.setCurrentIndex(1)
            dialog.media.blockSignals(False)
            pool.tasks.pop().run()

            self.assertIsNone(dialog.current_clip)
            self.assertIn("selection changed", dialog.status.text())
            dialog.player.play.assert_not_called()
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_dialogue_bank_and_media_changes_invalidate_prepared_clip(self):
        dialogue_index, bank_index = self._dependency_data()
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "clip.wav"
            source.write_bytes(b"reviewed bytes")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=lambda _bank, _media_id: source,
                quality_analyzer=lambda _path: metrics,
                review_recorder=lambda _reviewed, **_metadata: Path("/reviews.json"),
                thread_pool=_ImmediateThreadPool(),
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)

            self._approve_selected_clip(dialog)
            dialog.player.reset_mock()
            dialog.media.setCurrentIndex(1)
            self._assert_review_invalidated(dialog)
            self.assertIsNone(dialog.current_clip)

            self._approve_selected_clip(dialog)
            dialog.player.reset_mock()
            dialog.banks.setCurrentRow(1)
            self._assert_review_invalidated(dialog)
            self.assertIsNone(dialog.current_clip)

            self._approve_selected_clip(dialog)
            dialog.player.reset_mock()
            dialog.dialogue.clearSelection()
            dialog.dialogue.selectRow(1)
            self._assert_review_invalidated(dialog)
            self.assertIsNone(dialog.current_clip)
            self.assertEqual(dialog.speaker_name.text(), "")
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_speaker_and_review_changes_invalidate_approval_and_stop_playback(self):
        dialogue_index, bank_index = self._dependency_data()
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "clip.wav"
            source.write_bytes(b"reviewed bytes")
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )
            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=lambda _bank, _media_id: source,
                quality_analyzer=lambda _path: metrics,
                review_recorder=lambda _reviewed, **_metadata: Path("/reviews.json"),
                thread_pool=_ImmediateThreadPool(),
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)

            changes = (
                ("speaker name", lambda: dialog.speaker_name.setText("Changed")),
                ("NPC ID", lambda: dialog.npc_id.setText("999")),
                ("music / SFX review", lambda: dialog.music_or_sfx.setCurrentIndex(2)),
                ("speaker count review", lambda: dialog.multiple_speakers.setCurrentIndex(2)),
                (
                    "speaker identity review",
                    lambda: dialog.matches_expected_speaker.setCurrentIndex(2),
                ),
            )
            for label, change in changes:
                with self.subTest(dependency=label):
                    self._approve_selected_clip(dialog)
                    prepared_clip = dialog.current_clip
                    dialog.player.reset_mock()

                    change()

                    self._assert_review_invalidated(dialog, prepared_clip=prepared_clip)
                    dialog.speaker_name.setText("Reviewed")
                    dialog.npc_id.setText("100")
                    dialog.music_or_sfx.setCurrentIndex(1)
                    dialog.multiple_speakers.setCurrentIndex(1)
                    dialog.matches_expected_speaker.setCurrentIndex(1)
            dialog.deleteLater()

    @unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
    def test_import_uses_exact_reviewed_manifest_identity_and_source_bytes(self):
        dialogue_index, bank_index = self._dependency_data()
        manifest_updates = []
        processed_payloads = []
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "42.wav"
            reviewed_payload = b"reviewed voice bytes"
            source.write_bytes(reviewed_payload)
            metrics = VoiceReferenceMetrics(
                path=str(source),
                duration_seconds=5.0,
                peak_dbfs=-2.0,
                rms_dbfs=-18.0,
                silence_ratio=0.1,
                leading_silence_seconds=0.1,
                trailing_silence_seconds=0.1,
                clipping_ratio=0.0,
                quality_score=100,
                technical_flags=(),
            )

            def process_reference(input_path, output_path):
                payload = input_path.read_bytes()
                processed_payloads.append(payload)
                output_path.write_bytes(payload)

            dialog = Reverse1999AuditionDialog(
                dialogue_index,
                bank_index,
                clip_preparer=lambda _bank, _media_id: source,
                quality_analyzer=lambda _path: metrics,
                review_recorder=lambda reviewed, **metadata: (
                    Path(temporary_directory) / "reviews.json"
                ),
                mapping_loader=lambda: (),
                voice_output=Path(temporary_directory) / "voice-pack",
                reference_processor=process_reference,
                manifest_updater=lambda directory, character, references, bank: (
                    manifest_updates.append((directory, character, references, bank))
                    or directory / "manifest.json"
                ),
                thread_pool=_ImmediateThreadPool(),
            )
            dialog.player = Mock()
            dialog.dialogue.selectRow(0)
            self._approve_selected_clip(dialog)

            source.write_bytes(b"mutated cache bytes")
            dialog.speaker_name.blockSignals(True)
            dialog.speaker_name.setText("Displayed later")
            dialog.speaker_name.blockSignals(False)
            dialog.media.blockSignals(True)
            dialog.media.setCurrentIndex(1)
            dialog.media.blockSignals(False)
            dialog.banks.blockSignals(True)
            dialog.banks.setCurrentRow(1)
            dialog.banks.blockSignals(False)

            dialog.import_voice()

            self.assertEqual(processed_payloads, [reviewed_payload])
            self.assertEqual(len(manifest_updates), 1)
            directory, character, references, bank = manifest_updates[0]
            self.assertEqual(character, "Reviewed")
            self.assertEqual(bank, Path("reviewed-npc100.bnk"))
            self.assertEqual(len(references), 1)
            reference = references[0]
            self.assertEqual(reference.media_id, 42)
            self.assertEqual(reference.bank, "reviewed-npc100.bnk")
            self.assertEqual(
                reference.source_sha256,
                hashlib.sha256(reviewed_payload).hexdigest(),
            )
            destination = directory / "references" / "reviewed-42.wav"
            self.assertEqual(destination.read_bytes(), reviewed_payload)
            self.assertIn("Imported Reviewed", dialog.status.text())
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
