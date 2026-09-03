import hashlib
import json
import os
import struct
import unittest
import wave
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION
from r1999extractor.story_voice_evidence import analyze_story_voice_evidence

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt  # noqa: E402
    from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
    from PySide6.QtTest import QTest  # noqa: E402
    from PySide6.QtWidgets import QApplication, QTableWidget  # noqa: E402

    from r1999extractor.story_voice_review import load_review_session  # noqa: E402
    from r1999extractor.story_voice_review_ui import (  # noqa: E402
        StoryVoiceReviewDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    StoryVoiceReviewDialog = None


ui_unavailable = QApplication is None


class _Signal:
    def connect(self, callback):
        self.callback = callback

    def emit(self, value):
        self.callback(value)


class _Player:
    def __init__(self, _parent):
        self.errorOccurred = _Signal()
        self.mediaStatusChanged = _Signal()
        self.source_device = None

    def setAudioOutput(self, output):
        self.audio_output = output

    def setSourceDevice(self, source, _url):
        self.source_device = source

    def setSource(self, _url):
        self.source_device = None

    def play(self):
        pass

    def stop(self):
        pass

    def finish(self):
        self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)


class _AudioOutput:
    def __init__(self, _parent):
        pass


@unittest.skipIf(ui_unavailable, "install the ui extra to run Qt tests")
class StoryVoiceReviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.portraits = self.root / "portraits"
        self.portraits.mkdir()
        self._write_png(self.portraits / "534704.png", red=120)
        self.report = self._make_report()
        self.dialog = StoryVoiceReviewDialog(
            self.report,
            portrait_directory=self.portraits,
            player_factory=_Player,
            audio_output_factory=_AudioOutput,
        )
        self.dialog.show()
        self.application.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.application.processEvents()
        self.directory.cleanup()

    def _write_wav(self, path, frequency):
        path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 8000
        frames = [
            int(1000 * ((index * frequency) % sample_rate < sample_rate / 2))
            for index in range(800)
        ]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(struct.pack(f"<{len(frames)}h", *frames))

    def _write_png(self, path, *, red):
        def chunk(kind, payload):
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        payload = b"\x89PNG\r\n\x1a\n"
        payload += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        row = b"\x00" + bytes((red, 40, 20, 255)) * 2
        payload += chunk(b"IDAT", zlib.compress(row * 2))
        payload += chunk(b"IEND", b"")
        path.write_bytes(payload)

    def _make_report(self):
        candidates = []
        groups = []
        for index, (character, portrait, media_id, recommended) in enumerate(
            (
                ("Dobharchú", "534704.png", 10, True),
                ("Aderyn", "314601.png", 20, False),
            )
        ):
            reference = self.root / "references" / f"{media_id}.wav"
            self._write_wav(reference, index + 1)
            candidates.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": f"voice-{index}.bnk",
                    "media_id": media_id,
                    "candidate_origin": "story_line_route",
                    "source_event_ids": [1000 + media_id],
                    "reference": f"references/{media_id}.wav",
                    "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                    "technical_pass": recommended,
                    "transcript_conflict": not recommended,
                    "metrics": {
                        "duration_seconds": 0.1,
                        "quality_score": 100 if recommended else 65,
                        "technical_flags": [] if recommended else ["too-short"],
                    },
                    "source_lines": [
                        {
                            "line_id": f"reverse1999:test:{index}",
                            "text": f"Evidence for {character}",
                        }
                    ],
                }
            )
            groups.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": f"voice-{index}.bnk",
                    "recommended_media_ids_for_audition": [media_id] if recommended else [],
                }
            )
        report = self.root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "schema_version": REPORT_VERSION,
                    "groups": groups,
                    "candidates": candidates,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return report

    def test_first_pass_filter_and_details_are_truthful(self):
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertIn("Dobharchú", self.dialog.details.text())
        self.assertIn("reverse1999:test:0", self.dialog.details.text())
        self.assertFalse(self.dialog.portrait_image.pixmap().isNull())
        self.assertEqual(self.dialog.table.editTriggers(), QTableWidget.EditTrigger.NoEditTriggers)

        self.dialog.recommended_only.setChecked(False)

        self.assertEqual(self.dialog.table.rowCount(), 2)
        self.assertIn("too-short", self.dialog.table.item(1, 6).text())
        self.dialog.table.setCurrentCell(1, 0)
        self.assertEqual(self.dialog.portrait_image.text(), "Exact game portrait is not installed")

    def test_scaled_layout_accessibility_focus_order_and_stop_state(self):
        controls = (
            self.dialog.search,
            self.dialog.decision_filter,
            self.dialog.evidence_filter,
            self.dialog.recommended_only,
            self.dialog.table,
            self.dialog.notes,
            self.dialog.previous_pending,
            self.dialog.play,
            self.dialog.stop,
            self.dialog.accept,
            self.dialog.reject,
            self.dialog.uncertain,
            self.dialog.next_pending,
            self.dialog.set_a,
            self.dialog.play_a,
            self.dialog.set_b,
            self.dialog.play_b,
            self.dialog.clear_ab,
        )
        for control in controls:
            self.assertTrue(control.accessibleName(), type(control).__name__)
            self.assertTrue(control.accessibleDescription(), type(control).__name__)
        for current, following in zip(controls, controls[1:]):
            next_control = current.nextInFocusChain()
            while not next_control.focusPolicy() & Qt.FocusPolicy.TabFocus:
                next_control = next_control.nextInFocusChain()
            self.assertIs(next_control, following)

        self.assertEqual(self.dialog.stop.text(), "Stop playback")
        self.assertFalse(self.dialog.stop.isEnabled())
        self.dialog.play_selected()
        self.assertTrue(self.dialog.stop.isEnabled())
        self.dialog.player.finish()
        self.assertFalse(self.dialog.stop.isEnabled())

        base_font = self.dialog.font()
        for scale in (1.5, 2.0):
            with self.subTest(scale=scale):
                font = self.dialog.font()
                font.setPointSizeF(base_font.pointSizeF() * scale)
                self.dialog.setFont(font)
                self.dialog.resize(640, 480)
                self.application.processEvents()
                self.assertGreater(self.dialog.scroll_area.verticalScrollBar().maximum(), 0)
                self.dialog.scroll_area.ensureWidgetVisible(self.dialog.clear_ab)
                self.application.processEvents()
                self.assertGreater(self.dialog.scroll_area.verticalScrollBar().value(), 0)

    def test_decision_requires_complete_playback_and_uses_verified_buffer(self):
        candidate = self.dialog._selected_candidate()

        self.assertFalse(self.dialog.accept.isEnabled())
        self.assertIn("play this candidate completely", self.dialog.decision_reason.text())
        self.assertIn("Unavailable", self.dialog.accept.accessibleDescription())
        self.dialog.save_decision("accept")
        self.assertFalse(self.dialog.session.decisions.get(candidate.key))

        self.dialog.play_selected()

        self.assertIsNotNone(self.dialog._playback_buffer)
        self.assertEqual(
            hashlib.sha256(bytes(self.dialog._playback_buffer.data())).hexdigest(),
            candidate.reference_sha256,
        )
        self.assertFalse(self.dialog.accept.isEnabled())
        self.dialog.stop_playback()
        self.dialog.player.finish()
        self.assertFalse(self.dialog.accept.isEnabled())

        self.dialog.play_selected()
        self.dialog.player.finish()

        self.assertTrue(self.dialog.accept.isEnabled())
        self.assertTrue(self.dialog.reject.isEnabled())
        self.assertTrue(self.dialog.uncertain.isEnabled())
        self.assertIn("Decision ready", self.dialog.decision_reason.text())

        self.dialog.play_selected()
        self.assertTrue(self.dialog.accept.isEnabled())

        self.dialog.recommended_only.setChecked(False)
        self.dialog.table.setCurrentCell(1, 0)
        self.assertFalse(self.dialog.accept.isEnabled())
        self.dialog.table.setCurrentCell(0, 0)
        self.assertTrue(self.dialog.accept.isEnabled())

    def test_ctrl_enter_accepts_without_starting_cell_edit(self):
        candidate = self.dialog._selected_candidate()
        self.dialog.play_selected()
        self.dialog.player.finish()

        QTest.keyClick(
            self.dialog.table,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()

        session = load_review_session(self.report)
        self.assertEqual(session.decisions[candidate.key]["decision"], "accept")
        self.assertTrue(self.dialog.reject.isEnabled())

    def test_ab_slots_reject_duplicate_cross_character_pairs_and_clear(self):
        self.assertFalse(self.dialog.play_a.isEnabled())
        self.assertFalse(self.dialog.play_b.isEnabled())
        self.assertFalse(self.dialog.clear_ab.isEnabled())
        self.dialog.recommended_only.setChecked(False)
        first = self.dialog._selected_candidate()
        self.dialog._set_ab("A")

        self.assertTrue(self.dialog.play_a.isEnabled())
        self.assertFalse(self.dialog.play_b.isEnabled())
        self.assertTrue(self.dialog.clear_ab.isEnabled())
        self.dialog._set_ab("B")
        self.assertIsNone(self.dialog._ab_keys["B"])
        self.assertIn("two different candidates", self.dialog.status.text())

        self.dialog._move_pending(1)
        second = self.dialog._selected_candidate()
        self.dialog._set_ab("B")

        self.assertNotEqual(first.key, second.key)
        self.assertIn(str(first.media_id), self.dialog.a_label.text())
        self.assertEqual(self.dialog.b_label.text(), "B: not selected")
        self.assertFalse(self.dialog.play_b.isEnabled())
        self.assertIn("same character", self.dialog.status.text())

        self.dialog._ab_keys["B"] = second.key
        self.dialog._play_ab("A")
        self.assertIn("one character", self.dialog.status.text())

        self.dialog._clear_ab()
        self.assertEqual(self.dialog._ab_keys, {"A": None, "B": None})
        self.assertFalse(self.dialog.play_a.isEnabled())
        self.assertFalse(self.dialog.play_b.isEnabled())
        self.assertFalse(self.dialog.clear_ab.isEnabled())
        self.assertEqual(self.dialog.a_label.text(), "A: not selected")

    def test_changed_note_is_kept_per_candidate_until_saved(self):
        self.dialog.recommended_only.setChecked(False)
        first = self.dialog._selected_candidate()
        self.dialog.notes.setText("Keep this exact decision note")

        self.dialog.table.setCurrentCell(1, 0)

        self.assertEqual(self.dialog.notes.text(), "")
        self.assertIn("Draft note kept", self.dialog.status.text())
        self.dialog.table.setCurrentCell(0, 0)
        self.assertEqual(self.dialog.notes.text(), "Keep this exact decision note")

        self.dialog.play_selected()
        self.dialog.player.finish()
        self.dialog.save_decision("accept")

        saved = load_review_session(self.report)
        self.assertEqual(
            saved.decisions[first.key]["notes"],
            "Keep this exact decision note",
        )
        self.assertNotIn(first.key, self.dialog._note_drafts)

    def test_changed_reference_blocks_playback_and_decision(self):
        candidate = self.dialog._selected_candidate()
        candidate.reference.write_bytes(b"replacement")

        self.dialog.play_selected()

        self.assertIn("checksum changed", self.dialog.status.text())
        self.assertFalse(self.dialog.accept.isEnabled())
        self.dialog.save_decision("accept")
        self.assertFalse(self.dialog.session.decisions.get(candidate.key))

    def test_changed_portrait_blocks_decision_after_display(self):
        candidate = self.dialog._selected_candidate()
        self.dialog.play_selected()
        self.dialog.player.finish()
        self._write_png(self.portraits / "534704.png", red=210)

        self.dialog.save_decision("accept")

        self.assertIn("portrait changed after display", self.dialog.status.text())
        self.assertFalse(self.dialog.session.decisions.get(candidate.key))

    def test_bound_automatic_evidence_is_visible_and_filterable(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.application.processEvents()
        analyze_story_voice_evidence(
            self.report,
            self.root / "evidence.json",
            transcriber=lambda path: (
                "[noise]" if Path(path).stem == "10" else "Evidence for Aderyn"
            ),
        )
        self.dialog = StoryVoiceReviewDialog(
            self.report,
            player_factory=_Player,
            audio_output_factory=_AudioOutput,
        )
        self.dialog.recommended_only.setChecked(False)
        self.dialog.evidence_filter.setCurrentText("Obvious reject")

        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertIn("OBVIOUS REJECT", self.dialog.table.item(0, 6).text())
        self.dialog.evidence_filter.setCurrentText("ASR mismatch")
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertIn("Automatic advisory evidence", self.dialog.details.text())


if __name__ == "__main__":
    unittest.main()
