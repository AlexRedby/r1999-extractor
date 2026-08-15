import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
)

from r1999extractor.model_listening import (
    aggregate_listening_report,
    listening_progress,
    load_listening_session,
    next_pending_trial,
    record_trial_preference,
)


class SeekSlider(QSlider):
    seek_requested = Signal(int)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        if handle.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return

        slider_span = max(1, self.width() - handle.width())
        click_position = round(event.position().x() - handle.width() / 2)
        value = QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            click_position,
            slider_span,
            option.upsideDown,
        )
        self.setValue(value)
        self.seek_requested.emit(value)
        event.accept()


class ModelListeningDialog(QDialog):
    def __init__(self, session_path, parent=None, *, auto_play=True):
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.session = load_listening_session(self.session_path)
        self.current_trial = None
        self.auto_play = auto_play
        self.auto_play_pending_b = False
        self.active_side = None
        self.started_sides = set()
        self.setWindowTitle("Blind voice-model listening workbench")
        self.setMinimumSize(760, 430)
        self.resize(900, 520)

        self.progress = QLabel()
        self.dialogue = QPlainTextEdit()
        self.dialogue.setReadOnly(True)
        self.dialogue.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.dialogue.setMinimumHeight(160)
        self.play_a = QPushButton("Play A")
        self.play_b = QPushButton("Play B")
        self.stop = QPushButton("Stop")
        self.play_a.clicked.connect(lambda: self.play("a"))
        self.play_b.clicked.connect(lambda: self.play("b"))
        self.stop.clicked.connect(self.stop_audio)

        playback = QHBoxLayout()
        playback.addWidget(self.play_a)
        playback.addWidget(self.play_b)
        playback.addWidget(self.stop)

        self.skip_back = QPushButton("-5s")
        self.seek = SeekSlider(Qt.Orientation.Horizontal)
        self.skip_forward = QPushButton("+5s")
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(90)
        self.skip_back.clicked.connect(lambda: self.skip_by(-5_000))
        self.skip_forward.clicked.connect(lambda: self.skip_by(5_000))
        self.seek.sliderMoved.connect(self.seek_to)
        self.seek.seek_requested.connect(self.seek_to)
        seek_controls = QHBoxLayout()
        seek_controls.addWidget(self.skip_back)
        seek_controls.addWidget(self.seek, 1)
        seek_controls.addWidget(self.skip_forward)
        seek_controls.addWidget(self.time)

        self.prefer_a = QPushButton("A is better")
        self.tie = QPushButton("No preference")
        self.prefer_b = QPushButton("B is better")
        self.prefer_a.clicked.connect(lambda: self.save_preference("a"))
        self.tie.clicked.connect(lambda: self.save_preference("tie"))
        self.prefer_b.clicked.connect(lambda: self.save_preference("b"))
        decisions = QHBoxLayout()
        decisions.addWidget(self.prefer_a)
        decisions.addWidget(self.tie)
        decisions.addWidget(self.prefer_b)
        self.status = QLabel()
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.dialogue, 1)
        layout.addLayout(playback)
        layout.addLayout(seek_controls)
        layout.addLayout(decisions)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self.media_status_changed)
        self.player.playbackStateChanged.connect(self.playback_state_changed)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.positionChanged.connect(self.position_changed)
        self.load_next_trial()

    def set_preference_buttons_enabled(self, enabled):
        self.prefer_a.setEnabled(enabled)
        self.tie.setEnabled(enabled)
        self.prefer_b.setEnabled(enabled)

    def load_next_trial(self):
        self.session = load_listening_session(self.session_path)
        completed, total = listening_progress(self.session)
        self.progress.setText(f"Progress: {completed}/{total}")
        self.current_trial = next_pending_trial(self.session)
        self.auto_play_pending_b = False
        self.active_side = None
        self.started_sides.clear()
        self.seek.setRange(0, 0)
        self.seek.setValue(0)
        self.time.setText("0:00 / 0:00")
        self.set_preference_buttons_enabled(False)
        if self.current_trial is None:
            report_path = self.session_path.with_name("report.json")
            report = aggregate_listening_report(self.session_path, report_path)
            leader = report["models"][0]["model_id"] if report["models"] else "none"
            self.dialogue.setPlainText("Listening session complete.")
            self.status.setText(
                f"Unblinded aggregate report: {report_path}. Current leader: {leader}. "
                "Production selection still requires manual approval."
            )
            self.play_a.setEnabled(False)
            self.play_b.setEnabled(False)
            self.skip_back.setEnabled(False)
            self.seek.setEnabled(False)
            self.skip_forward.setEnabled(False)
            return
        self.play_a.setEnabled(True)
        self.play_b.setEnabled(True)
        self.skip_back.setEnabled(True)
        self.seek.setEnabled(True)
        self.skip_forward.setEnabled(True)
        self.dialogue.setPlainText(
            f"{self.current_trial.get('line_id') or self.current_trial['queue_id']}\n\n"
            f"{self.current_trial.get('text', '')}"
        )
        self.dialogue.verticalScrollBar().setValue(0)
        self.status.setText(
            "Starting A, then B automatically. Preference unlocks after both have started."
        )
        if self.auto_play:
            QTimer.singleShot(0, self.start_auto_playback)

    def start_auto_playback(self):
        if self.current_trial is None:
            return
        self.auto_play_pending_b = True
        self.play("a", automatic=True)

    def play(self, side, *, automatic=False):
        if self.current_trial is None:
            return
        if side == "b":
            self.auto_play_pending_b = False
        self.active_side = side
        relative = self.current_trial["audio"][side]
        path = self.session_path.parent / relative
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        mode = " automatically" if automatic else ""
        self.status.setText(f"Playing anonymous sample {side.upper()}{mode}.")

    def playback_state_changed(self, state):
        if state != QMediaPlayer.PlaybackState.PlayingState or self.active_side is None:
            return
        self.started_sides.add(self.active_side)
        if self.started_sides == {"a", "b"}:
            self.set_preference_buttons_enabled(True)
            self.status.setText("Both samples have started. Choose A, no preference, or B.")

    def media_status_changed(self, status):
        if (
            status == QMediaPlayer.MediaStatus.EndOfMedia
            and self.active_side == "a"
            and self.auto_play_pending_b
        ):
            self.auto_play_pending_b = False
            QTimer.singleShot(0, lambda: self.play("b", automatic=True))

    @staticmethod
    def format_time(milliseconds):
        total_seconds = max(0, int(milliseconds)) // 1000
        return f"{total_seconds // 60}:{total_seconds % 60:02d}"

    def update_time_label(self, position, duration):
        self.time.setText(f"{self.format_time(position)} / {self.format_time(duration)}")

    def duration_changed(self, duration):
        duration = max(0, int(duration))
        self.seek.setRange(0, duration)
        self.update_time_label(self.player.position(), duration)

    def position_changed(self, position):
        position = max(0, int(position))
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.update_time_label(position, self.player.duration())

    def seek_to(self, position):
        self.player.setPosition(position)
        self.update_time_label(position, self.player.duration())

    def skip_by(self, delta_milliseconds):
        duration = max(0, int(self.player.duration()))
        position = max(
            0,
            min(duration, int(self.player.position()) + delta_milliseconds),
        )
        self.player.setPosition(position)
        self.update_time_label(position, duration)

    def stop_audio(self):
        self.auto_play_pending_b = False
        self.player.stop()

    def save_preference(self, preference):
        if self.current_trial is None:
            return
        if self.started_sides != {"a", "b"}:
            self.status.setText("Start both samples before choosing a preference.")
            return
        try:
            record_trial_preference(
                self.session_path,
                self.current_trial["trial_id"],
                preference,
            )
            aggregate_listening_report(
                self.session_path,
                self.session_path.with_name("report.json"),
            )
        except Exception as error:
            self.status.setText(f"Unable to save listening score: {error}")
            return
        self.stop_audio()
        self.load_next_trial()


def launch_listening_workbench(session_path):
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = ModelListeningDialog(session_path)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open listening workbench", str(error))
        return 1
    dialog.exec()
    return 0
