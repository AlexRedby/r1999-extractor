import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.async_ui import LatestTaskRunner
from r1999extractor.reverse1999_audition import (
    BankCandidate,
    candidate_banks,
    default_bank_index,
    default_dialogue_index,
    filter_dialogue,
    load_audition_data,
    load_speaker_mappings,
    prepare_audition_clip,
    save_speaker_mapping,
    voice_coverage,
)
from r1999extractor.reverse1999_voice_import import (
    ImportedReference,
    default_output,
    update_manifest,
)
from r1999extractor.voice_reference_quality import (
    VoiceReferenceMetrics,
    analyze_voice_reference,
    record_clip_review,
    review_voice_reference,
    trim_and_normalize_voice_reference,
)


@dataclass(frozen=True)
class _PreparedClip:
    candidate: BankCandidate
    media_id: int
    output: Path
    metrics: VoiceReferenceMetrics
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class _ReviewedClip:
    clip: _PreparedClip
    review: VoiceReferenceMetrics
    speaker_name: str
    npc_id: str
    chapter: str


class _PreparationCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class _PreparationRequest:
    candidate: BankCandidate
    media_id: int
    cancelled: Event


class Reverse1999AuditionDialog(QDialog):
    voice_imported = Signal(str)

    def __init__(
        self,
        dialogue_index,
        bank_index,
        *,
        clip_preparer=prepare_audition_clip,
        mapping_saver=save_speaker_mapping,
        quality_analyzer=analyze_voice_reference,
        review_recorder=record_clip_review,
        mapping_loader=load_speaker_mappings,
        manifest_updater=update_manifest,
        voice_output=default_output,
        reference_processor=trim_and_normalize_voice_reference,
        thread_pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.dialogue_index = dialogue_index
        self.bank_index = bank_index
        self.clip_preparer = clip_preparer
        self.mapping_saver = mapping_saver
        self.quality_analyzer = quality_analyzer
        self.review_recorder = review_recorder
        self.mapping_loader = mapping_loader
        self.manifest_updater = manifest_updater
        self.voice_output = Path(voice_output).expanduser().resolve()
        self.reference_processor = reference_processor
        self.candidates = []
        self.current_clip = None
        self._reviewed_clip = None
        self._playing_clip = None
        self._heard_clip = None
        self._preparation_request = None
        self.setWindowTitle("Reverse: 1999 voice mapping manager")
        self.setMinimumSize(640, 480)
        self.resize(1000, 650)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Speaker name, NPC ID, or dialogue text")
        self.chapter = QComboBox()
        self.chapter.addItem("All chapters", None)
        chapters = sorted({str(row.get("chapter")) for row in dialogue_index.get("dialogue", [])})
        for chapter in chapters:
            self.chapter.addItem(chapter, chapter)
        self.search.textChanged.connect(self.refresh_dialogue)
        self.chapter.currentIndexChanged.connect(self.refresh_dialogue)

        filters = QGridLayout()
        filters.addWidget(QLabel("Find"), 0, 0)
        filters.addWidget(self.search, 0, 1)
        filters.addWidget(QLabel("Chapter"), 1, 0)
        filters.addWidget(self.chapter, 1, 1)

        self.coverage = QLabel()
        self.coverage.setWordWrap(True)

        self.dialogue = QTableWidget(0, 4)
        self.dialogue.setHorizontalHeaderLabels(
            ["Chapter", "Sequence", "Speaker", "Dialogue evidence"]
        )
        self.dialogue.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dialogue.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dialogue.horizontalHeader().setStretchLastSection(True)
        self.dialogue.itemSelectionChanged.connect(self.dialogue_selected)

        self.banks = QListWidget()
        self.banks.currentRowChanged.connect(self.bank_selected)
        self.media = QComboBox()
        self.play_button = QPushButton("Play selected clip")
        self.cancel_preparation_button = QPushButton("Cancel preparation")
        self.cancel_preparation_button.setEnabled(False)
        self.cancel_preparation_button.setAccessibleDescription(
            "Cancel the active clip conversion and analysis request"
        )
        self.stop_button = QPushButton("Stop playback")
        self.stop_button.setEnabled(False)
        self.play_button.clicked.connect(self.play_clip)
        self.cancel_preparation_button.clicked.connect(self.cancel_preparation)
        self.stop_button.clicked.connect(self.stop_clip)
        player_actions = QGridLayout()
        player_actions.addWidget(self.media, 0, 0, 1, 3)
        player_actions.addWidget(self.play_button, 1, 0)
        player_actions.addWidget(self.cancel_preparation_button, 1, 1)
        player_actions.addWidget(self.stop_button, 1, 2)

        self.quality = QLabel("Play a clip to calculate its technical score.")
        self.quality.setWordWrap(True)
        self.music_or_sfx = QComboBox()
        self.music_or_sfx.addItem("Not reviewed", None)
        self.music_or_sfx.addItem("No music or SFX", False)
        self.music_or_sfx.addItem("Contains music or SFX", True)
        self.multiple_speakers = QComboBox()
        self.multiple_speakers.addItem("Not reviewed", None)
        self.multiple_speakers.addItem("One speaker", False)
        self.multiple_speakers.addItem("Multiple speakers", True)
        self.matches_expected_speaker = QComboBox()
        self.matches_expected_speaker.addItem("Not reviewed", None)
        self.matches_expected_speaker.addItem("Matches expected speaker", True)
        self.matches_expected_speaker.addItem("Different / uncertain speaker", False)
        self.review_reason = QLabel()
        self.review_reason.setWordWrap(True)
        self.review_reason.setAccessibleName("Source audition review availability")
        self.save_review_button = QPushButton("Save clip review")
        self.save_review_button.setEnabled(False)
        self.save_review_button.clicked.connect(self.save_clip_review)
        review_form = QFormLayout()
        review_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        review_form.addRow("Technical quality", self.quality)
        review_form.addRow("Music / SFX", self.music_or_sfx)
        review_form.addRow("Speakers", self.multiple_speakers)
        review_form.addRow("Speaker identity", self.matches_expected_speaker)
        review_form.addRow("Review availability", self.review_reason)
        review_form.addRow("", self.save_review_button)
        self.import_button = QPushButton("Import reviewed clip as character voice")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.import_voice)
        review_form.addRow("", self.import_button)

        bank_panel = QWidget()
        bank_layout = QVBoxLayout(bank_panel)
        bank_layout.addWidget(QLabel("Chapter-aware voice-bank candidates"))
        bank_layout.addWidget(self.banks, 1)
        bank_layout.addLayout(player_actions)
        bank_layout.addLayout(review_form)

        evidence_panel = QWidget()
        evidence_layout = QVBoxLayout(evidence_panel)
        evidence_layout.addWidget(QLabel("Dialogue evidence"))
        evidence_layout.addWidget(self.dialogue)

        splitter = QSplitter()
        splitter.addWidget(evidence_panel)
        splitter.addWidget(bank_panel)
        splitter.setSizes([600, 400])

        self.speaker_name = QLineEdit()
        self.npc_id = QLineEdit()
        self.save_button = QPushButton("Save local speaker mapping")
        self.save_button.clicked.connect(self.save_mapping)
        mapping = QFormLayout()
        mapping.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        mapping.addRow("Speaker name", self.speaker_name)
        mapping.addRow("NPC ID", self.npc_id)
        mapping.addRow("", self.save_button)

        self.status = QLabel(
            "Select dialogue evidence, audition matching chapter banks, then save "
            "the confirmed speaker mapping."
        )
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        content_layout.addWidget(self.coverage)
        content_layout.addLayout(filters)
        content_layout.addWidget(splitter, 1)
        content_layout.addLayout(mapping)
        content_layout.addWidget(self.status)
        content_layout.addWidget(buttons)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll_area)

        controls = (
            (
                self.search,
                "Search dialogue evidence",
                "Filter by speaker, NPC ID, or dialogue text",
            ),
            (self.chapter, "Filter dialogue by chapter", "Show dialogue evidence from one chapter"),
            (
                self.dialogue,
                "Dialogue evidence",
                "Select one dialogue row to identify candidate banks",
            ),
            (self.banks, "Candidate voice banks", "Select a chapter-aware voice bank candidate"),
            (self.media, "Media clip", "Select one embedded media clip from the candidate bank"),
            (
                self.play_button,
                "Prepare and play selected clip",
                "Convert, analyze, and play the selected clip",
            ),
            (
                self.cancel_preparation_button,
                "Cancel clip preparation",
                "Cancel the active conversion and analysis request",
            ),
            (self.stop_button, "Stop clip playback", "Stop the currently playing audition clip"),
            (
                self.music_or_sfx,
                "Music or sound effects review",
                "Classify whether the clip contains music or sound effects",
            ),
            (
                self.multiple_speakers,
                "Speaker count review",
                "Classify whether the clip contains one or multiple speakers",
            ),
            (
                self.matches_expected_speaker,
                "Expected speaker review",
                "Classify whether the voice matches the expected speaker",
            ),
            (
                self.save_review_button,
                "Save clip review",
                "Save review answers after complete playback",
            ),
            (
                self.import_button,
                "Import reviewed character voice",
                "Import the exact approved reviewed clip bytes",
            ),
            (
                self.speaker_name,
                "Speaker name",
                "In-game display name for the selected speaker mapping",
            ),
            (self.npc_id, "NPC ID", "NPC identifier for the selected speaker mapping"),
            (
                self.save_button,
                "Save local speaker mapping",
                "Save the selected speaker, NPC, and bank mapping",
            ),
            (self.close_button, "Close voice mapping manager", "Close this voice mapping window"),
        )
        for widget, name, description in controls:
            widget.setAccessibleName(name)
            widget.setAccessibleDescription(description)
        focus_order = tuple(widget for widget, _name, _description in controls)
        for current, following in zip(focus_order, focus_order[1:]):
            self.setTabOrder(current, following)
        self.coverage.setAccessibleName("Voice mapping coverage")
        self.quality.setAccessibleName("Selected clip technical quality")
        self.status.setAccessibleName("Voice mapping operation status")

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._media_error)
        self.preparation_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preparation_runner.finished.connect(self._clip_prepared)
        self.current_review = None
        self.media.currentIndexChanged.connect(self._clip_dependency_changed)
        self.speaker_name.textChanged.connect(self._review_dependency_changed)
        self.npc_id.textChanged.connect(self._review_dependency_changed)
        self.music_or_sfx.currentIndexChanged.connect(self._review_dependency_changed)
        self.multiple_speakers.currentIndexChanged.connect(self._review_dependency_changed)
        self.matches_expected_speaker.currentIndexChanged.connect(self._review_dependency_changed)
        self.refresh_coverage()
        self.refresh_dialogue()

    def _clear_review(self):
        self.current_review = None
        self._reviewed_clip = None
        self.import_button.setEnabled(False)

    def _set_review_available(self, enabled):
        reason = (
            "Review ready: this clip played completely."
            if enabled
            else "Review locked: play this clip completely before saving a decision."
        )
        self.review_reason.setText(reason)
        self.save_review_button.setEnabled(enabled)
        self.save_review_button.setAccessibleDescription(
            "Save the review for the completely heard clip" if enabled else f"Unavailable. {reason}"
        )

    def _review_dependency_changed(self, *_arguments):
        self.player.stop()
        self.stop_button.setEnabled(False)
        self._playing_clip = None
        self._clear_review()

    def _clip_dependency_changed(self, *_arguments):
        self._cancel_preparation()
        self.player.stop()
        self.stop_button.setEnabled(False)
        self._playing_clip = None
        self._heard_clip = None
        self.current_clip = None
        self.quality.setText("Play a clip to calculate its technical score.")
        self._set_review_available(False)
        self._clear_review()

    def _set_preparation_busy(self, busy):
        for widget in (
            self.search,
            self.chapter,
            self.dialogue,
            self.banks,
            self.media,
            self.speaker_name,
            self.npc_id,
            self.music_or_sfx,
            self.multiple_speakers,
            self.matches_expected_speaker,
            self.play_button,
            self.save_button,
        ):
            widget.setEnabled(not busy)
        self.cancel_preparation_button.setEnabled(busy)

    def _cancel_preparation(self):
        request = self._preparation_request
        if request is not None:
            request.cancelled.set()
        cancelled = self.preparation_runner.cancel()
        self._preparation_request = None
        if cancelled:
            self._set_preparation_busy(False)
        return cancelled

    def cancel_preparation(self):
        if self._cancel_preparation():
            self.status.setText(
                "Clip preparation cancelled. Any late conversion result will be ignored."
            )

    def refresh_coverage(self):
        mappings = self.mapping_loader()
        coverage = voice_coverage(self.dialogue_index, mappings)
        mapped = sum(item["mapped"] for item in coverage)
        named = sum(bool(item["speaker_name"]) for item in coverage)
        unresolved = len(coverage) - mapped
        self.coverage.setText(
            f"Assisted mappings: {mapped}/{len(coverage)} detected speaker IDs; "
            f"{named} have names; {unresolved} still need review. Search by a name, "
            "NPC ID, or dialogue, then preview and import a clean clip."
        )

    def refresh_dialogue(self):
        self._clip_dependency_changed()
        self.speaker_name.clear()
        self.npc_id.clear()
        rows = filter_dialogue(
            self.dialogue_index.get("dialogue", []),
            query=self.search.text(),
            chapter=self.chapter.currentData(),
        )
        self.dialogue.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            speaker = row.get("speaker_name") or f"Unknown ({row.get('speaker_id')})"
            values = (
                row.get("chapter", ""),
                row.get("sequence", ""),
                speaker,
                row.get("text", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(256, row)
                self.dialogue.setItem(row_index, column, item)
        self.status.setText(f"Showing {len(rows)} dialogue evidence row(s).")
        self.banks.clear()
        self.media.clear()

    def dialogue_selected(self):
        self._clip_dependency_changed()
        selected = self.dialogue.selectedItems()
        if not selected:
            return
        row = selected[0].data(256)
        chapter = row.get("chapter")
        speaker_id = row.get("speaker_id")
        self.speaker_name.setText(str(row.get("speaker_name") or ""))
        self.npc_id.setText(str(speaker_id or ""))
        self.candidates = candidate_banks(self.bank_index, chapter=chapter, speaker_id=speaker_id)
        self.banks.clear()
        for candidate in self.candidates:
            npc = ", ".join(candidate.npc_ids) or "unknown NPC"
            self.banks.addItem(f"{candidate.filename}  [{npc}]")
        self.status.setText(
            f"Found {len(self.candidates)} candidate bank(s) for chapter {chapter}."
        )
        if self.candidates:
            self.banks.setCurrentRow(0)

    def bank_selected(self, index):
        self._clip_dependency_changed()
        self.media.clear()
        self.music_or_sfx.setCurrentIndex(0)
        self.multiple_speakers.setCurrentIndex(0)
        self.matches_expected_speaker.setCurrentIndex(0)
        if index < 0 or index >= len(self.candidates):
            return
        candidate = self.candidates[index]
        for media_id in candidate.media_ids:
            self.media.addItem(str(media_id), media_id)
        if len(candidate.npc_ids) == 1:
            self.npc_id.setText(candidate.npc_ids[0])

    def selected_bank(self):
        index = self.banks.currentRow()
        if index < 0 or index >= len(self.candidates):
            return None
        return self.candidates[index]

    def play_clip(self):
        candidate = self.selected_bank()
        media_id = self.media.currentData()
        if candidate is None or media_id is None:
            self.status.setText("Choose a bank and media clip first.")
            return
        self._clip_dependency_changed()
        root = Path(self.bank_index["game_audio_directory"])
        request = _PreparationRequest(candidate, int(media_id), Event())
        self._preparation_request = request
        self._set_preparation_busy(True)
        self.status.setText(f"Preparing {candidate.filename} / {media_id} in the background...")
        self.preparation_runner.start(
            self._prepare_clip,
            request,
            root / candidate.path,
            self.clip_preparer,
            self.quality_analyzer,
        )

    @staticmethod
    def _prepare_clip(request, bank_path, clip_preparer, quality_analyzer):
        if request.cancelled.is_set():
            raise _PreparationCancelled()
        try:
            output = Path(clip_preparer(bank_path, request.media_id)).resolve()
            if request.cancelled.is_set():
                raise _PreparationCancelled()
            metrics = quality_analyzer(output)
            if request.cancelled.is_set():
                raise _PreparationCancelled()
            if output != Path(metrics.path).resolve():
                raise ValueError("Prepared clip does not match its quality analysis")
            payload = output.read_bytes()
            if request.cancelled.is_set():
                raise _PreparationCancelled()
        except _PreparationCancelled:
            raise
        return request, _PreparedClip(
            request.candidate,
            request.media_id,
            output,
            metrics,
            payload,
            hashlib.sha256(payload).hexdigest(),
        )

    def _clip_prepared(self, result, error):
        request = self._preparation_request
        self._preparation_request = None
        self._set_preparation_busy(False)
        if error is not None:
            if not isinstance(error, _PreparationCancelled):
                self.status.setText(f"Unable to prepare clip: {error}")
            return
        completed_request, clip = result
        if (
            request is not completed_request
            or completed_request.cancelled.is_set()
            or self.selected_bank() != completed_request.candidate
            or self.media.currentData() != completed_request.media_id
        ):
            self.status.setText("Prepared clip ignored because the selection changed.")
            return
        self.current_clip = clip
        metrics = clip.metrics
        flags = ", ".join(metrics.technical_flags) or "no technical flags"
        self.quality.setText(
            f"{metrics.quality_score}/100; {metrics.duration_seconds:.1f}s; {flags}"
        )
        self.player.setSource(QUrl.fromLocalFile(str(clip.output)))
        self._playing_clip = self.current_clip
        self.stop_button.setEnabled(True)
        self.player.play()
        self.status.setText(f"Playing {clip.candidate.filename} / {clip.media_id}")

    def stop_clip(self):
        self.player.stop()
        self.stop_button.setEnabled(False)
        self._playing_clip = None
        self.status.setText("Playback stopped.")

    def _media_status_changed(self, status):
        if (
            status != QMediaPlayer.MediaStatus.EndOfMedia
            or self._playing_clip is None
            or self._playing_clip is not self.current_clip
        ):
            return
        self._heard_clip = self._playing_clip
        self._playing_clip = None
        self.stop_button.setEnabled(False)
        self._set_review_available(True)
        self.status.setText("Clip played completely. Review decisions are now available.")

    def _media_error(self, _error, message):
        self._playing_clip = None
        self.stop_button.setEnabled(False)
        self.status.setText(f"Playback failed: {message or self.player.errorString()}")

    def save_clip_review(self):
        if self.current_clip is None:
            self.status.setText("Play and listen to the selected clip first.")
            return
        if self._heard_clip is not self.current_clip:
            self.status.setText("Play this clip completely before saving its review decision.")
            return
        music_or_sfx = self.music_or_sfx.currentData()
        multiple_speakers = self.multiple_speakers.currentData()
        matches_expected_speaker = self.matches_expected_speaker.currentData()
        if music_or_sfx is None or multiple_speakers is None or matches_expected_speaker is None:
            self.status.setText("Review music/SFX, speaker count, and expected speaker first.")
            return
        clip = self.current_clip
        candidate, media_id, metrics = clip.candidate, clip.media_id, clip.metrics
        selected = self.dialogue.selectedItems()
        chapter = selected[0].data(256).get("chapter") if selected else ""
        speaker_name = self.speaker_name.text().strip()
        npc_id = self.npc_id.text().strip()
        try:
            reviewed = review_voice_reference(
                metrics,
                music_or_sfx=music_or_sfx,
                multiple_speakers=multiple_speakers,
                matches_expected_speaker=matches_expected_speaker,
            )
            path = self.review_recorder(
                reviewed,
                speaker_name=speaker_name,
                npc_id=npc_id,
                bank=candidate.filename,
                media_id=media_id,
                chapter=chapter,
            )
        except Exception as error:
            self.status.setText(f"Unable to save clip review: {error}")
            return
        decision = "approved" if reviewed.approved else "rejected"
        self.current_review = reviewed
        self._reviewed_clip = _ReviewedClip(
            clip,
            reviewed,
            speaker_name,
            npc_id,
            str(chapter),
        )
        self.import_button.setEnabled(reviewed.approved)
        self.status.setText(f"Clip {decision}; saved review to {path}")

    def import_voice(self):
        reviewed_clip = self._reviewed_clip
        if reviewed_clip is None:
            self.status.setText("Review and approve a clip before importing it.")
            return
        if not reviewed_clip.review.approved:
            self.status.setText("Only an approved clean single-speaker clip can be imported.")
            return
        character = reviewed_clip.speaker_name
        if not character:
            self.status.setText("Enter the in-game speaker name first.")
            return
        clip = reviewed_clip.clip
        destination = (
            self.voice_output
            / "references"
            / (f"{character.casefold().replace(' ', '-')}-{clip.media_id}.wav")
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="r1999-reviewed-import-") as temporary:
                source = Path(temporary) / clip.output.name
                source.write_bytes(clip.payload)
                self.reference_processor(source, destination)
            imported = ImportedReference(
                path=destination,
                media_id=clip.media_id,
                source_sha256=clip.sha256,
                reference_sha256=sha256_file(destination),
                bank=clip.candidate.filename,
            )
            manifest = self.manifest_updater(
                self.voice_output,
                character,
                [imported],
                Path(clip.candidate.filename),
            )
        except Exception as error:
            self.status.setText(f"Unable to import voice: {error}")
            return
        self.voice_imported.emit(str(manifest))
        self.status.setText(f"Imported {character} into {manifest}. Restart speech to load it.")

    def save_mapping(self):
        candidate = self.selected_bank()
        if candidate is None:
            self.status.setText("Choose the confirmed voice bank first.")
            return
        selected = self.dialogue.selectedItems()
        chapter = selected[0].data(256).get("chapter") if selected else ""
        try:
            path = self.mapping_saver(
                self.speaker_name.text(),
                self.npc_id.text(),
                candidate.filename,
                chapter,
            )
        except Exception as error:
            self.status.setText(f"Unable to save mapping: {error}")
            return
        self.status.setText(f"Saved local speaker mapping to {path}")
        self.refresh_coverage()

    def closeEvent(self, event):
        self._cancel_preparation()
        self.player.stop()
        self.stop_button.setEnabled(False)
        event.accept()


def create_parser():
    parser = argparse.ArgumentParser(
        description="Review and audition unresolved Reverse: 1999 NPC voices."
    )
    parser.add_argument("--dialogue-index", type=Path, default=default_dialogue_index)
    parser.add_argument("--bank-index", type=Path, default=default_bank_index)
    parser.add_argument("--search", default="")
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialogue_index, bank_index = load_audition_data(
            arguments.dialogue_index, arguments.bank_index
        )
    except Exception as error:
        QMessageBox.critical(None, "Unable to open speaker audition", str(error))
        return 1
    dialog = Reverse1999AuditionDialog(dialogue_index, bank_index)
    dialog.search.setText(arguments.search)
    dialog.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
