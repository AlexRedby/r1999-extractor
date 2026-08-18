"""Qt interface for checksum-bound Character Story reference review."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QUrl
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from r1999extractor.story_voice_evidence import (
    StoryVoiceEvidenceError,
    load_story_voice_evidence,
)
from r1999extractor.story_voice_review import (
    ReviewCandidate,
    StoryVoiceReviewError,
    load_review_session,
    record_review_decision,
)


class StoryVoiceReviewDialog(QDialog):
    """Review one immutable candidate report without changing its sources."""

    def __init__(
        self,
        report_path,
        review_path=None,
        evidence_path=None,
        *,
        session_loader=load_review_session,
        decision_recorder=record_review_decision,
        evidence_loader=load_story_voice_evidence,
        player_factory=QMediaPlayer,
        audio_output_factory=QAudioOutput,
        parent=None,
    ):
        super().__init__(parent)
        self.report_path = Path(report_path).expanduser().resolve()
        self.review_path = None if review_path is None else Path(review_path).expanduser().resolve()
        self._session_loader = session_loader
        self._decision_recorder = decision_recorder
        self.session = self._session_loader(self.report_path, self.review_path)
        self.evidence_path, self.evidence = evidence_loader(self.report_path, evidence_path)
        self._visible_candidates: tuple[ReviewCandidate, ...] = ()
        self._playback_buffer = None
        self._ab_keys = {"A": None, "B": None}

        self.setWindowTitle("Character Story voice reference review")
        self.setMinimumSize(1180, 720)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Character, portrait, bank, media ID, or transcript")
        self.decision_filter = QComboBox()
        self.decision_filter.addItems(["All", "Pending", "Accepted", "Rejected", "Uncertain"])
        self.recommended_only = QCheckBox("Recommended first pass only")
        self.recommended_only.setChecked(True)
        self.evidence_filter = QComboBox()
        self.evidence_filter.addItems(
            ["All evidence", "Obvious reject", "Speaker outlier", "ASR mismatch", "Not analyzed"]
        )

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Find"))
        filters.addWidget(self.search, 3)
        filters.addWidget(QLabel("Decision"))
        filters.addWidget(self.decision_filter, 1)
        filters.addWidget(self.evidence_filter, 1)
        filters.addWidget(self.recommended_only)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Decision",
                "Character",
                "Portrait",
                "Bank",
                "Media",
                "Duration",
                "Score / flags",
                "Transcript evidence",
            ]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)

        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Optional decision note")

        self.previous_pending = QPushButton("Previous pending")
        self.play = QPushButton("Play")
        self.stop = QPushButton("Stop")
        self.accept = QPushButton("Accept")
        self.reject = QPushButton("Reject")
        self.uncertain = QPushButton("Uncertain")
        self.next_pending = QPushButton("Next pending")
        actions = QHBoxLayout()
        for button in (
            self.previous_pending,
            self.play,
            self.stop,
            self.accept,
            self.reject,
            self.uncertain,
            self.next_pending,
        ):
            actions.addWidget(button)

        self.set_a = QPushButton("Set current as A")
        self.play_a = QPushButton("Play A")
        self.a_label = QLabel("A: not selected")
        self.set_b = QPushButton("Set current as B")
        self.play_b = QPushButton("Play B")
        self.b_label = QLabel("B: not selected")
        compare = QHBoxLayout()
        for widget in (
            self.set_a,
            self.play_a,
            self.a_label,
            self.set_b,
            self.play_b,
            self.b_label,
        ):
            compare.addWidget(widget)

        self.status = QLabel()
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.details)
        layout.addWidget(self.notes)
        layout.addLayout(actions)
        layout.addLayout(compare)
        layout.addWidget(self.status)

        self.audio_output = audio_output_factory(self)
        self.player = player_factory(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.errorOccurred.connect(self._media_error)

        self.search.textChanged.connect(self.refresh)
        self.decision_filter.currentIndexChanged.connect(self.refresh)
        self.evidence_filter.currentIndexChanged.connect(self.refresh)
        self.recommended_only.toggled.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.previous_pending.clicked.connect(lambda: self._move_pending(-1))
        self.next_pending.clicked.connect(lambda: self._move_pending(1))
        self.play.clicked.connect(self.play_selected)
        self.stop.clicked.connect(self.stop_playback)
        self.accept.clicked.connect(lambda: self.save_decision("accept"))
        self.reject.clicked.connect(lambda: self.save_decision("reject"))
        self.uncertain.clicked.connect(lambda: self.save_decision("uncertain"))
        self.set_a.clicked.connect(lambda: self._set_ab("A"))
        self.set_b.clicked.connect(lambda: self._set_ab("B"))
        self.play_a.clicked.connect(lambda: self._play_ab("A"))
        self.play_b.clicked.connect(lambda: self._play_ab("B"))

        self._shortcuts = []
        self._add_shortcut("Space", self.play_selected)
        self._add_shortcut("Ctrl+Return", lambda: self.save_decision("accept"))
        self._add_shortcut("Ctrl+Backspace", lambda: self.save_decision("reject"))
        self._add_shortcut("Ctrl+Shift+Return", lambda: self.save_decision("uncertain"))
        self._add_shortcut("Alt+Left", lambda: self._move_pending(-1))
        self._add_shortcut("Alt+Right", lambda: self._move_pending(1))

        self.refresh()

    def _add_shortcut(self, sequence, callback):
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self._shortcuts.append(shortcut)

    def _candidate_decision(self, candidate):
        return self.session.decisions.get(candidate.key, {}).get("decision")

    def _candidate_by_key(self, key):
        return next((item for item in self.session.candidates if item.key == key), None)

    def _candidate_evidence(self, candidate):
        return self.evidence.get(candidate.key, {})

    def _evidence_summary(self, candidate):
        evidence = self._candidate_evidence(candidate)
        if not evidence:
            return "not analyzed"
        content = evidence.get("content", {})
        speaker = evidence.get("speaker", {})
        asr = evidence.get("asr", {})
        values = [str(content.get("classification") or "uncertain")]
        if content.get("obvious_rejection_candidate") is True:
            values.append("OBVIOUS REJECT")
        group = speaker.get("group_similarity", {})
        if group.get("outlier_risk") is True:
            values.append("SPEAKER OUTLIER")
        if asr.get("status") == "complete":
            values.append(f"ASR {float(asr.get('best_similarity', 0.0)):.2f}")
        return "; ".join(values)

    def _selected_candidate(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._visible_candidates):
            return None
        return self._visible_candidates[row]

    def refresh(self, *_arguments, selected_key=None):
        if selected_key is None:
            selected = self._selected_candidate()
            selected_key = None if selected is None else selected.key
        query = self.search.text().strip().casefold()
        decision_filter = self.decision_filter.currentText()
        evidence_filter = self.evidence_filter.currentText()

        def included(candidate):
            decision = self._candidate_decision(candidate)
            if self.recommended_only.isChecked() and not candidate.recommended:
                return False
            if decision_filter == "Pending" and decision is not None:
                return False
            if decision_filter == "Accepted" and decision != "accept":
                return False
            if decision_filter == "Rejected" and decision != "reject":
                return False
            if decision_filter == "Uncertain" and decision != "uncertain":
                return False
            evidence = self._candidate_evidence(candidate)
            content = evidence.get("content", {})
            speaker = evidence.get("speaker", {})
            asr = evidence.get("asr", {})
            if (
                evidence_filter == "Obvious reject"
                and content.get("obvious_rejection_candidate") is not True
            ):
                return False
            if (
                evidence_filter == "Speaker outlier"
                and speaker.get("group_similarity", {}).get("outlier_risk") is not True
            ):
                return False
            if evidence_filter == "ASR mismatch" and not (
                asr.get("status") == "complete" and float(asr.get("best_similarity", 0.0)) < 0.58
            ):
                return False
            if evidence_filter == "Not analyzed" and evidence:
                return False
            searchable = " ".join(
                (
                    candidate.character,
                    candidate.portrait or "",
                    candidate.source_bank,
                    str(candidate.media_id),
                    *candidate.transcripts,
                )
            ).casefold()
            return not query or query in searchable

        def priority(candidate):
            evidence = self._candidate_evidence(candidate)
            obvious = evidence.get("content", {}).get("obvious_rejection_candidate") is True
            outlier = (
                evidence.get("speaker", {}).get("group_similarity", {}).get("outlier_risk") is True
            )
            return (not obvious, not outlier, not candidate.recommended)

        self._visible_candidates = tuple(
            sorted(
                (candidate for candidate in self.session.candidates if included(candidate)),
                key=priority,
            )
        )
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._visible_candidates))
            selected_row = -1
            for row, candidate in enumerate(self._visible_candidates):
                decision = self._candidate_decision(candidate) or "pending"
                flags = ", ".join(candidate.technical_flags) or "pass"
                values = (
                    decision,
                    candidate.character,
                    candidate.portrait or "-",
                    candidate.source_bank,
                    str(candidate.media_id),
                    (
                        "-"
                        if candidate.duration_seconds is None
                        else f"{candidate.duration_seconds:.2f}s"
                    ),
                    (
                        f"{candidate.quality_score if candidate.quality_score is not None else '-'} / "
                        f"{flags}; {self._evidence_summary(candidate)}"
                    ),
                    " | ".join(candidate.transcripts),
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, candidate.key)
                    self.table.setItem(row, column, item)
                if candidate.key == selected_key:
                    selected_row = row
            if selected_row < 0 and self._visible_candidates:
                selected_row = 0
            if selected_row >= 0:
                self.table.setCurrentCell(selected_row, 0)
        finally:
            self.table.blockSignals(False)
        self.summary.setText(
            f"Candidates: {len(self.session.candidates)} | Decisions: "
            f"{len(self.session.decisions)} | Pending: {self.session.pending_count} | "
            f"Invalidated evidence: {len(self.session.invalidated_decisions)} | "
            f"Automatic evidence: {len(self.evidence)} | "
            f"Visible: {len(self._visible_candidates)}"
        )
        self._selection_changed()

    def _selection_changed(self):
        candidate = self._selected_candidate()
        enabled = candidate is not None
        for button in (
            self.play,
            self.accept,
            self.reject,
            self.uncertain,
            self.set_a,
            self.set_b,
        ):
            button.setEnabled(enabled)
        self.previous_pending.setEnabled(self.session.pending_count > 0)
        self.next_pending.setEnabled(self.session.pending_count > 0)
        if candidate is None:
            self.details.setText("No candidate matches the active filter.")
            self.notes.clear()
            return
        decision = self.session.decisions.get(candidate.key, {})
        self.notes.setText(decision.get("notes", ""))
        lines = ", ".join(value for value in candidate.line_ids if value) or "not recorded"
        context_parts = []
        for title, (previous, following) in zip(
            candidate.collection_titles, candidate.contexts, strict=True
        ):
            context_parts.append(
                f"{title or 'Untitled story'}: before={previous or '-'}; after={following or '-'}"
            )
        coverage = (
            "not recorded in this report"
            if candidate.affected_character_line_count is None
            else (
                f"{candidate.affected_character_line_count} missing-source lines for the character; "
                f"{candidate.affected_portrait_line_count} for this exact portrait"
            )
        )
        evidence = self._candidate_evidence(candidate)
        automatic = "not analyzed"
        if evidence:
            asr = evidence["asr"]
            content = evidence["content"]
            speaker = evidence["speaker"]
            automatic = (
                f"classification={content.get('classification')}; "
                f"reasons={', '.join(content.get('reasons', [])) or 'none'}; "
                f"ASR={asr.get('transcript', asr.get('status'))!r}; "
                f"WER={asr.get('best_word_error_rate', '-')}; "
                f"speaker_count={speaker.get('speaker_count_estimate', speaker.get('status'))}; "
                f"group={speaker.get('group_similarity', {}) or 'not available'}"
            )
        self.details.setText(
            f"Character: {candidate.character} | Portrait: {candidate.portrait or '-'} | "
            f"Bank: {candidate.source_bank} | Media: {candidate.media_id} | "
            f"Source evidence lines: {len(candidate.transcripts)} ({lines})\n"
            f"Transcript: {' | '.join(candidate.transcripts)}\n"
            f"Context: {' | '.join(context_parts) or 'not recorded in this report'}\n"
            f"Potential coverage: {coverage}\n"
            f"Automatic advisory evidence: {automatic}\n"
            f"Technical: {'pass' if candidate.technical_pass else 'needs attention'}; "
            f"flags: {', '.join(candidate.technical_flags) or 'none'}; "
            f"transcript conflict: {'yes' if candidate.transcript_conflict else 'no'}; "
            f"recommended: {'yes' if candidate.recommended else 'no'}"
        )

    def _move_pending(self, offset):
        pending = [
            candidate
            for candidate in self._visible_candidates
            if self._candidate_decision(candidate) is None
        ]
        if not pending:
            self.status.setText("No pending candidate matches the active filter.")
            return
        current = self._selected_candidate()
        try:
            index = pending.index(current)
        except ValueError:
            index = -1 if offset > 0 else 0
        target = pending[(index + offset) % len(pending)]
        row = self._visible_candidates.index(target)
        self.table.setCurrentCell(row, 0)
        self.table.scrollToItem(self.table.item(row, 0))

    def _read_verified_audio(self, candidate):
        try:
            audio_bytes = candidate.reference.read_bytes()
        except OSError as error:
            raise StoryVoiceReviewError(
                f"Unable to read candidate reference {candidate.reference}: {error}"
            ) from error
        if hashlib.sha256(audio_bytes).hexdigest() != candidate.reference_sha256:
            raise StoryVoiceReviewError("Candidate reference checksum changed before playback")
        return audio_bytes

    def _play_candidate(self, candidate):
        try:
            audio_bytes = self._read_verified_audio(candidate)
        except StoryVoiceReviewError as error:
            self.status.setText(f"PLAYBACK BLOCKED: {error}")
            return
        self.stop_playback(clear_status=False)
        playback = QBuffer(self)
        playback.setData(QByteArray(audio_bytes))
        if not playback.open(QIODevice.OpenModeFlag.ReadOnly):
            playback.deleteLater()
            self.status.setText("PLAYBACK BLOCKED: unable to open immutable audio buffer")
            return
        self._playback_buffer = playback
        self.player.setSourceDevice(playback, QUrl("r1999-story-reference.wav"))
        self.player.play()
        self.status.setText(
            f"PLAYING {candidate.character} / media {candidate.media_id} / "
            f"SHA-256 {candidate.reference_sha256[:12]}..."
        )
        self._selection_changed()

    def play_selected(self):
        candidate = self._selected_candidate()
        if candidate is not None:
            self._play_candidate(candidate)

    def stop_playback(self, *, clear_status=True):
        self.player.stop()
        playback = self._playback_buffer
        self._playback_buffer = None
        if playback is not None:
            self.player.setSource(QUrl())
            playback.close()
            playback.deleteLater()
        if clear_status:
            self.status.setText("Playback stopped.")

    def save_decision(self, decision):
        candidate = self._selected_candidate()
        if candidate is None:
            return
        try:
            self.session = self._decision_recorder(
                self.report_path,
                candidate.key,
                decision,
                notes=self.notes.text(),
                review_path=self.review_path,
            )
        except StoryVoiceReviewError as error:
            self.status.setText(f"DECISION NOT SAVED: {error}")
            return
        self.status.setText(
            f"SAVED {decision.upper()}: {candidate.character} / media {candidate.media_id}."
        )
        self.refresh(selected_key=candidate.key)

    def _set_ab(self, slot):
        candidate = self._selected_candidate()
        if candidate is None:
            return
        self._ab_keys[slot] = candidate.key
        label = self.a_label if slot == "A" else self.b_label
        label.setText(f"{slot}: {candidate.character} / {candidate.media_id}")
        (self.play_a if slot == "A" else self.play_b).setEnabled(True)

    def _play_ab(self, slot):
        candidate = self._candidate_by_key(self._ab_keys[slot])
        if candidate is None:
            self.status.setText(f"A/B slot {slot} has no candidate.")
            return
        self._play_candidate(candidate)

    def _media_error(self, _error, message):
        self.status.setText(f"PLAYBACK ERROR: {message}")

    def closeEvent(self, event):
        self.stop_playback(clear_status=False)
        event.accept()


def create_parser():
    parser = argparse.ArgumentParser(
        description="Review checksum-bound Character Story voice candidates in Qt"
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        dialog = StoryVoiceReviewDialog(options.report, options.review, options.evidence)
    except (StoryVoiceEvidenceError, StoryVoiceReviewError) as error:
        QMessageBox.critical(None, "Character Story voice review", str(error))
        return 1
    dialog.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
