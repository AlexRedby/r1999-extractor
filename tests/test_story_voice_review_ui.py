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


class _Player:
    def __init__(self, _parent):
        self.errorOccurred = _Signal()
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

    def test_playback_keeps_decision_actions_enabled_and_uses_verified_buffer(self):
        candidate = self.dialog._selected_candidate()

        self.dialog.play_selected()

        self.assertIsNotNone(self.dialog._playback_buffer)
        self.assertEqual(
            hashlib.sha256(bytes(self.dialog._playback_buffer.data())).hexdigest(),
            candidate.reference_sha256,
        )
        self.assertTrue(self.dialog.accept.isEnabled())
        self.assertTrue(self.dialog.reject.isEnabled())
        self.assertTrue(self.dialog.uncertain.isEnabled())

    def test_ctrl_enter_accepts_without_starting_cell_edit(self):
        candidate = self.dialog._selected_candidate()

        QTest.keyClick(
            self.dialog.table,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()

        session = load_review_session(self.report)
        self.assertEqual(session.decisions[candidate.key]["decision"], "accept")
        self.assertTrue(self.dialog.reject.isEnabled())

    def test_pending_navigation_and_ab_slots_have_fixed_controls(self):
        self.dialog.recommended_only.setChecked(False)
        first = self.dialog._selected_candidate()
        self.dialog._set_ab("A")
        self.dialog._move_pending(1)
        second = self.dialog._selected_candidate()
        self.dialog._set_ab("B")

        self.assertNotEqual(first.key, second.key)
        self.assertIn(str(first.media_id), self.dialog.a_label.text())
        self.assertIn(str(second.media_id), self.dialog.b_label.text())
        self.assertTrue(self.dialog.play_a.isEnabled())
        self.assertTrue(self.dialog.play_b.isEnabled())

    def test_changed_reference_blocks_playback_and_decision(self):
        candidate = self.dialog._selected_candidate()
        candidate.reference.write_bytes(b"replacement")

        self.dialog.play_selected()
        self.dialog.save_decision("accept")

        self.assertIn("checksum changed", self.dialog.status.text())
        self.assertFalse(self.dialog.session.decisions.get(candidate.key))

    def test_changed_portrait_blocks_decision_after_display(self):
        candidate = self.dialog._selected_candidate()
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
