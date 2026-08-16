import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from r1999extractor.generation_queue import GenerationQueueError, load_story_records
from r1999extractor.pregeneration import (
    PregenerationError,
    create_pregeneration_job,
    default_jobs_root,
    discover_default_moss_model,
    discover_default_story_index,
    discover_default_vntts_python,
    discover_default_voice_manifest,
    discover_jobs,
    discover_pregeneration_targets,
    generation_command,
    load_narrator_voice_choices,
    load_pregeneration_job,
    read_generation_progress,
    resolve_job_runtime_status,
    update_job_status,
)

status_labels = {
    "ready": "Ready",
    "running": "Generating",
    "running_here": "Generating here",
    "running_external": "Generating externally",
    "interrupted": "Interrupted",
    "paused": "Ready to resume",
    "stopped": "Stopped",
    "failed": "Needs attention",
    "complete": "Complete",
    "incomplete": "Incomplete - missing voices",
}


def format_duration(seconds):
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def format_elapsed(timestamp, *, now=None):
    if not timestamp:
        return None
    try:
        started = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = max(0, round((now - started).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_chapter_ids(chapters, per_line=5):
    return "\n".join(
        ", ".join(chapters[index : index + per_line]) for index in range(0, len(chapters), per_line)
    )


class PregenerationDialog(QDialog):
    def __init__(
        self,
        *,
        story_index=None,
        voice_manifest=None,
        vntts_python=None,
        model=None,
        jobs_root=default_jobs_root,
        narrator_character="Matilda",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Reverse: 1999 voice pregeneration")
        self.setMinimumSize(1180, 720)
        self.resize(1380, 820)
        self.targets = ()
        self.target_items = {}
        self.current_job = None
        self.process = None
        self.jobs_root = Path(jobs_root).expanduser().resolve()

        self.story_index = QLineEdit(str(story_index or discover_default_story_index()))
        self.voice_manifest = QLineEdit(str(voice_manifest or discover_default_voice_manifest()))
        self.voice_manifest.editingFinished.connect(self.reload_narrator_voices)
        self.vntts_python = QLineEdit(str(vntts_python or discover_default_vntts_python()))
        self.model = QLineEdit(str(model or discover_default_moss_model()))
        self.narrator = QComboBox()
        self.narrator.setEditable(True)
        self.narrator.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.narrator.setMinimumContentsLength(24)
        self.narrator.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.narrator.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.narrator_voice_status = QLabel()
        self.narrator_voice_status.setWordWrap(True)
        self.narrator_voice_status.setStyleSheet("color: #6b7280;")
        self.preview_narrator_button = QPushButton("Play reference")
        self.preview_narrator_button.clicked.connect(self.preview_narrator_voice)
        self.narrator.currentIndexChanged.connect(self.narrator_voice_changed)
        self.narrator_player = QMediaPlayer(self)
        self.narrator_audio_output = QAudioOutput(self)
        self.narrator_player.setAudioOutput(self.narrator_audio_output)
        self.story_index.setToolTip("Latest extracted story index used to discover chapters")
        self.voice_manifest.setToolTip("VNTTS character voice manifest")
        self.vntts_python.setToolTip("Python from the VNTTS environment with MOSS installed")
        self.model.setToolTip("Local MOSS model directory (recommended) or Hugging Face model ID")

        settings = QGroupBox("Sources")
        settings_grid = QGridLayout(settings)
        settings_grid.addWidget(QLabel("Story index"), 0, 0)
        settings_grid.addWidget(self.story_index, 0, 1)
        index_browse = QPushButton("Browse")
        index_browse.clicked.connect(self.browse_story_index)
        settings_grid.addWidget(index_browse, 0, 2)
        settings_grid.addWidget(QLabel("Voice manifest"), 1, 0)
        settings_grid.addWidget(self.voice_manifest, 1, 1)
        manifest_browse = QPushButton("Browse")
        manifest_browse.clicked.connect(self.browse_voice_manifest)
        settings_grid.addWidget(manifest_browse, 1, 2)
        settings_grid.addWidget(QLabel("VNTTS Python"), 2, 0)
        settings_grid.addWidget(self.vntts_python, 2, 1)
        python_browse = QPushButton("Browse")
        python_browse.clicked.connect(self.browse_vntts_python)
        settings_grid.addWidget(python_browse, 2, 2)
        settings_grid.addWidget(QLabel("MOSS model"), 3, 0)
        settings_grid.addWidget(self.model, 3, 1)
        model_browse = QPushButton("Browse")
        model_browse.clicked.connect(self.browse_model)
        settings_grid.addWidget(model_browse, 3, 2)
        settings_grid.addWidget(QLabel("Narrator voice"), 4, 0)
        narrator_row = QHBoxLayout()
        narrator_row.addWidget(self.narrator, 1)
        narrator_row.addWidget(self.preview_narrator_button)
        settings_grid.addLayout(narrator_row, 4, 1)
        reload_button = QPushButton("Reload stories")
        reload_button.clicked.connect(self.reload_targets)
        settings_grid.addWidget(reload_button, 4, 2)
        settings_grid.addWidget(self.narrator_voice_status, 5, 1)
        settings_grid.setColumnStretch(1, 1)

        selection_panel = QWidget()
        selection_layout = QVBoxLayout(selection_panel)
        selection_header = QHBoxLayout()
        selection_header.addWidget(QLabel("Choose chapters or anecdotes"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or chapter ID")
        self.search.textChanged.connect(self.filter_targets)
        selection_header.addWidget(self.search, 1)
        select_visible = QPushButton("Select visible")
        select_visible.clicked.connect(self.select_visible_targets)
        selection_header.addWidget(select_visible)
        clear_selection = QPushButton("Clear")
        clear_selection.clicked.connect(self.clear_target_selection)
        selection_header.addWidget(clear_selection)
        selection_layout.addLayout(selection_header)

        self.target_tree = QTreeWidget()
        self.target_tree.setColumnCount(5)
        self.target_tree.setHeaderLabels(
            ["Story", "Episodes", "Lines to generate", "Voices needed", "Full cast"]
        )
        self.target_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.target_tree.header().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.target_tree.itemChanged.connect(self.target_selection_changed)
        selection_layout.addWidget(self.target_tree, 1)
        self.selection_summary = QLabel("No stories selected.")
        selection_layout.addWidget(self.selection_summary)
        self.start_button = QPushButton("Generate selected stories")
        self.start_button.setMinimumHeight(42)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_selected)
        selection_layout.addWidget(self.start_button)

        status_panel = QWidget()
        status_layout = QVBoxLayout(status_panel)
        status_layout.addWidget(QLabel("Generation status"))
        self.current_title = QLabel("Select a previous job or start a new one.")
        self.current_title.setWordWrap(True)
        self.current_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        status_layout.addWidget(self.current_title)
        self.status_badge = QLabel("IDLE")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_badge.setMinimumHeight(34)
        status_layout.addWidget(self.status_badge)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m generated (%p%)")
        status_layout.addWidget(self.progress)
        self.progress_details = QLabel("No generation job selected.")
        self.progress_details.setWordWrap(True)
        status_layout.addWidget(self.progress_details)
        self.latest_line = QLabel("")
        self.latest_line.setWordWrap(True)
        self.latest_line.setMinimumHeight(54)
        status_layout.addWidget(self.latest_line)

        actions = QHBoxLayout()
        self.resume_button = QPushButton("Resume selected job")
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.resume_selected_job)
        actions.addWidget(self.resume_button)
        self.stop_button = QPushButton("Stop generation")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_generation)
        actions.addWidget(self.stop_button)
        refresh_jobs = QPushButton("Refresh")
        refresh_jobs.clicked.connect(self.refresh_jobs)
        actions.addWidget(refresh_jobs)
        status_layout.addLayout(actions)

        self.job_table = QTableWidget(0, 5)
        self.job_table.setHorizontalHeaderLabels(
            ["Started", "Stories", "Narrator", "Progress", "Status"]
        )
        self.job_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.job_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.job_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.job_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.job_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 5):
            self.job_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.job_table.itemSelectionChanged.connect(self.job_selected)
        status_layout.addWidget(QLabel("Previous jobs"))
        status_layout.addWidget(self.job_table, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("MOSS process messages appear here.")
        self.log.setMaximumHeight(125)
        status_layout.addWidget(self.log)

        splitter = QSplitter()
        splitter.addWidget(selection_panel)
        splitter.addWidget(status_panel)
        splitter.setSizes([680, 700])

        layout = QVBoxLayout(self)
        layout.addWidget(settings)
        layout.addWidget(splitter, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.poll_status)
        self.timer.start()
        self.reload_narrator_voices(preferred=narrator_character)
        self.reload_targets()
        self.refresh_jobs()

    def browse_story_index(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select story index", self.story_index.text(), "JSON Lines (*.jsonl)"
        )
        if path:
            self.story_index.setText(path)
            self.reload_targets()

    def browse_voice_manifest(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select voice manifest", self.voice_manifest.text(), "JSON (*.json)"
        )
        if path:
            self.voice_manifest.setText(path)
            self.reload_narrator_voices()

    def browse_vntts_python(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select VNTTS Python", str(Path(self.vntts_python.text()).parent)
        )
        if path:
            self.vntts_python.setText(path)

    def browse_model(self):
        path = QFileDialog.getExistingDirectory(self, "Select local MOSS model", self.model.text())
        if path:
            self.model.setText(path)

    def reload_narrator_voices(self, *, preferred=None):
        preferred = preferred or self.selected_narrator_character()
        try:
            voices = load_narrator_voice_choices(self.voice_manifest.text())
        except PregenerationError as error:
            self.narrator.clear()
            self.narrator_voice_status.setText(str(error))
            self.preview_narrator_button.setEnabled(False)
            return

        self.narrator.blockSignals(True)
        self.narrator.clear()
        selected_index = -1
        for index, voice in enumerate(voices):
            self.narrator.addItem(voice.character, voice)
            reference_label = "reference" if len(voice.references) == 1 else "references"
            self.narrator.setItemData(
                index,
                f"{voice.speaker}; {len(voice.references)} {reference_label}",
                Qt.ItemDataRole.ToolTipRole,
            )
            if preferred and voice.character.casefold() == preferred.casefold():
                selected_index = index
        if selected_index < 0 and voices and not preferred:
            selected_index = 0
        self.narrator.setCurrentIndex(selected_index)
        if selected_index < 0 and preferred:
            self.narrator.setEditText(preferred)
        self.narrator.blockSignals(False)
        self.narrator_voice_changed()

    def selected_narrator_character(self):
        voice = self.narrator.currentData()
        if voice is not None:
            return voice.character
        return self.narrator.currentText().strip()

    def narrator_voice_changed(self, *_args):
        voice = self.narrator.currentData()
        if voice is None:
            self.narrator_voice_status.setText("Choose a voice from the manifest list.")
            self.preview_narrator_button.setEnabled(False)
            return
        prompt = voice.references[0]
        prompt_status = "available" if prompt.is_file() else "missing"
        total = len(voice.references)
        self.narrator_voice_status.setText(
            f"Generation prompt: {prompt.name} ({prompt_status}); {total} reference clips. "
            "This voice will be saved in the job."
        )
        self.preview_narrator_button.setEnabled(prompt.is_file())

    def preview_narrator_voice(self):
        voice = self.narrator.currentData()
        if voice is None:
            self.show_error("Narrator voice", "Choose a voice from the manifest list first.")
            return
        reference = voice.references[0]
        if not reference.is_file():
            self.show_error(
                "Narrator voice",
                f"No local reference clips are available for {voice.character}.",
            )
            return
        self.narrator_player.stop()
        self.narrator_player.setSource(QUrl.fromLocalFile(str(reference)))
        self.narrator_player.play()
        self.narrator_voice_status.setText(f"Playing {voice.character}: {reference.name}")

    def reload_targets(self):
        try:
            _metadata, records = load_story_records(self.story_index.text())
            self.targets = discover_pregeneration_targets(records)
        except (GenerationQueueError, OSError) as error:
            self.targets = ()
            self.target_tree.clear()
            self.show_error("Unable to load stories", str(error))
            return
        self.populate_target_tree()

    def populate_target_tree(self):
        self.target_tree.blockSignals(True)
        self.target_tree.clear()
        self.target_items = {}
        categories = {}
        for target in self.targets:
            parent = categories.get(target.category)
            if parent is None:
                parent = QTreeWidgetItem([target.category])
                parent.setFirstColumnSpanned(True)
                parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                categories[target.category] = parent
                self.target_tree.addTopLevelItem(parent)
            child = QTreeWidgetItem(
                [
                    target.title,
                    str(target.episode_count),
                    str(target.line_count),
                    str(target.voice_count),
                    str(target.cast_count),
                ]
            )
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, Qt.CheckState.Unchecked)
            child.setData(0, Qt.ItemDataRole.UserRole, target.target_id)
            tooltip = "Chapter IDs:\n" + format_chapter_ids(target.chapters)
            tooltip += (
                f"\nVoices needed for no-audio lines: {target.voice_count}"
                f"\nFull story cast: {target.cast_count}"
            )
            if target.episode_titles:
                tooltip += "\n\nEpisodes:\n" + "\n".join(target.episode_titles)
            child.setToolTip(0, tooltip)
            parent.addChild(child)
            self.target_items[target.target_id] = child
        for parent in categories.values():
            parent.setExpanded(parent.text(0) in {"Main story", "Character stories", "Anecdotes"})
        self.target_tree.blockSignals(False)
        self.target_selection_changed()

    def filter_targets(self, text):
        query = text.strip().casefold()
        for target in self.targets:
            item = self.target_items[target.target_id]
            haystack = " ".join(
                (target.title, target.category, *target.chapters, *target.episode_titles)
            ).casefold()
            item.setHidden(bool(query) and query not in haystack)
        for index in range(self.target_tree.topLevelItemCount()):
            parent = self.target_tree.topLevelItem(index)
            all_hidden = all(parent.child(row).isHidden() for row in range(parent.childCount()))
            parent.setHidden(all_hidden)
            if query and not all_hidden:
                parent.setExpanded(True)

    def selected_target_ids(self):
        return tuple(
            target_id
            for target_id, item in self.target_items.items()
            if item.checkState(0) == Qt.CheckState.Checked
        )

    def select_visible_targets(self):
        self.target_tree.blockSignals(True)
        for item in self.target_items.values():
            if not item.isHidden():
                item.setCheckState(0, Qt.CheckState.Checked)
        self.target_tree.blockSignals(False)
        self.target_selection_changed()

    def clear_target_selection(self):
        self.target_tree.blockSignals(True)
        for item in self.target_items.values():
            item.setCheckState(0, Qt.CheckState.Unchecked)
        self.target_tree.blockSignals(False)
        self.target_selection_changed()

    def target_selection_changed(self, *_args):
        selected = set(self.selected_target_ids())
        selected_targets = [target for target in self.targets if target.target_id in selected]
        lines = sum(target.line_count for target in selected_targets)
        if selected_targets:
            self.selection_summary.setText(
                f"Selected {len(selected_targets)} stories with {lines:,} voiceless lines."
            )
        else:
            self.selection_summary.setText("No stories selected.")
        self.start_button.setEnabled(bool(selected_targets) and not self.process_is_running())

    def validate_runtime_paths(self):
        checks = (
            (self.story_index.text(), "Story index"),
            (self.voice_manifest.text(), "Voice manifest"),
            (self.vntts_python.text(), "VNTTS Python"),
        )
        missing = [label for value, label in checks if not Path(value).expanduser().is_file()]
        if missing:
            raise PregenerationError("Missing required file(s): " + ", ".join(missing))
        narrator = self.narrator.currentData()
        if narrator is None:
            raise PregenerationError("Choose a narrator voice from the manifest list")
        if not narrator.references[0].is_file():
            raise PregenerationError(
                f"Narrator voice {narrator.character!r} has no local reference recording"
            )
        model = self.model.text().strip()
        if not model:
            raise PregenerationError("MOSS model cannot be empty")
        if Path(model).expanduser().is_absolute() and not Path(model).expanduser().is_dir():
            raise PregenerationError(f"Local MOSS model directory does not exist: {model}")

    def start_selected(self):
        try:
            self.validate_runtime_paths()
            job_directory = create_pregeneration_job(
                self.story_index.text(),
                self.targets,
                self.selected_target_ids(),
                self.jobs_root,
                voice_manifest=self.voice_manifest.text(),
                vntts_python=self.vntts_python.text(),
                model=self.model.text().strip(),
                narrator_character=self.selected_narrator_character(),
            )
            self.refresh_jobs(select_job=job_directory)
            self.launch_job(job_directory)
        except (PregenerationError, GenerationQueueError, OSError) as error:
            self.show_error("Unable to start generation", str(error))

    def launch_job(self, job_directory):
        if self.process_is_running():
            self.show_error("Generation already running", "Stop the active job first.")
            return
        job = load_pregeneration_job(job_directory)
        if not job.get("model"):
            update_job_status(
                job_directory,
                job.get("status", "ready"),
                model=discover_default_moss_model(),
            )
        command = generation_command(job_directory)
        executable = Path(command[0])
        if not executable.is_file():
            self.show_error("VNTTS Python not found", str(executable))
            return
        self.current_job = Path(job_directory).resolve()
        self.log.clear()
        self.log.appendPlainText("Starting MOSS generation ...")
        self.process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        self.process.setProgram(command[0])
        self.process.setArguments(list(command[1:]))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_process_output)
        self.process.started.connect(self.process_started)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start()
        self.update_controls()

    def process_started(self):
        if self.current_job is not None:
            update_job_status(
                self.current_job,
                "running",
                pid=int(self.process.processId()),
                exit_code=None,
            )
        self.update_controls()
        self.poll_status()

    def read_process_output(self):
        if self.process is None:
            return
        output = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if output:
            self.log.moveCursor(QTextCursor.MoveOperation.End)
            self.log.insertPlainText(output)

    def process_finished(self, exit_code, _exit_status):
        self.read_process_output()
        if self.current_job is not None:
            try:
                progress = read_generation_progress(self.current_job)
                if progress.pending == 0 and progress.failed == 0:
                    final_status = "incomplete" if progress.skipped_missing_voice else "complete"
                else:
                    final_status = "stopped"
                if exit_code != 0:
                    final_status = "failed"
                update_job_status(
                    self.current_job, final_status, exit_code=int(exit_code), pid=None
                )
            except PregenerationError as error:
                self.log.appendPlainText(str(error))
        self.process = None
        self.update_controls()
        self.refresh_jobs(select_job=self.current_job)
        self.poll_status()

    def process_error(self, error):
        self.log.appendPlainText(f"Process error: {error}")

    def process_is_running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def stop_generation(self):
        if not self.process_is_running():
            return
        self.log.appendPlainText("Stopping after the current process interruption ...")
        self.process.terminate()
        QTimer.singleShot(5000, self.kill_process_if_needed)

    def kill_process_if_needed(self):
        if self.process_is_running():
            self.process.kill()

    def refresh_jobs(self, *, select_job=None):
        selected = Path(select_job).resolve() if select_job else self.selected_job_directory()
        jobs = discover_jobs(self.jobs_root)
        self.job_table.blockSignals(True)
        self.job_table.setRowCount(len(jobs))
        selected_row = None
        for row, directory in enumerate(jobs):
            try:
                job = load_pregeneration_job(directory)
                progress = read_generation_progress(directory)
                local_running = (
                    self.process_is_running()
                    and self.current_job is not None
                    and directory.resolve() == self.current_job.resolve()
                )
                runtime_status = resolve_job_runtime_status(
                    job,
                    local_running=local_running,
                )
                values = (
                    directory.name[:15],
                    job.get("title", directory.name),
                    job.get("narrator_character", "Matilda"),
                    f"{progress.generated}/{progress.eligible}",
                    status_labels.get(runtime_status, runtime_status.title()),
                )
            except PregenerationError as error:
                values = (directory.name[:15], directory.name, "-", "-", "Invalid")
                self.log.appendPlainText(str(error))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, str(directory))
                self.job_table.setItem(row, column, item)
            if selected is not None and directory.resolve() == selected:
                selected_row = row
        self.job_table.blockSignals(False)
        if selected_row is not None:
            self.job_table.selectRow(selected_row)
        elif self.current_job is None and jobs:
            self.job_table.selectRow(0)
        self.job_selected()

    def selected_job_directory(self):
        selected = self.job_table.selectedItems()
        if not selected:
            return None
        value = selected[0].data(Qt.ItemDataRole.UserRole)
        return None if not value else Path(value).resolve()

    def job_selected(self):
        selected = self.selected_job_directory()
        if selected is not None and not self.process_is_running():
            self.current_job = selected
        self.update_controls()
        self.poll_status()

    def resume_selected_job(self):
        selected = self.selected_job_directory()
        if selected is not None:
            self.launch_job(selected)

    def poll_status(self):
        if self.current_job is None:
            return
        try:
            job = load_pregeneration_job(self.current_job)
            progress = read_generation_progress(self.current_job)
        except PregenerationError as error:
            self.progress_details.setText(str(error))
            return
        title = job.get("title", self.current_job.name)
        narrator = job.get("narrator_character", "Matilda")
        self.current_title.setText(f"{title} - Narrator: {narrator}")
        runtime_status = resolve_job_runtime_status(
            job,
            local_running=self.process_is_running(),
        )
        maximum = max(1, progress.eligible)
        self.progress.setRange(0, maximum)
        processed = progress.generated + progress.failed
        self.progress.setValue(processed)
        if progress.eligible == 0:
            self.progress.setFormat("No lines can be generated")
        else:
            self.progress.setFormat(
                f"%v / %m processed - {progress.generated:,} generated, "
                f"{progress.failed:,} failed"
            )
        label = status_labels.get(runtime_status, runtime_status.title()).upper()
        if runtime_status in {"running_here", "running_external"} and progress.active_phase:
            activity = progress.active_phase.replace("_", " ").upper()
            if progress.active_attempt and progress.active_attempt_limit:
                activity += f" - ATTEMPT {progress.active_attempt}/{progress.active_attempt_limit}"
            elapsed = format_elapsed(progress.active_started_at)
            if elapsed:
                activity += f" - {elapsed}"
            label += f" - {activity}"
        self.status_badge.setText(label)
        color = {
            "running": "#2563eb",
            "running_here": "#2563eb",
            "running_external": "#0369a1",
            "interrupted": "#b91c1c",
            "complete": "#15803d",
            "incomplete": "#a16207",
            "failed": "#b91c1c",
            "paused": "#a16207",
            "stopped": "#a16207",
        }.get(runtime_status, "#52525b")
        self.status_badge.setStyleSheet(
            f"background: {color}; color: white; border-radius: 6px; font-weight: 700;"
        )
        details = [f"{progress.pending:,} pending", f"{progress.failed:,} failed"]
        if runtime_status in {"running_here", "running_external"} and progress.generated == 0:
            model = str(job.get("model") or "")
            phase = (
                "Loading the local MOSS model"
                if Path(model).expanduser().is_absolute()
                else "Downloading or loading the MOSS model"
            )
            details.insert(0, phase)
        if progress.skipped_missing_voice:
            details.append(f"{progress.skipped_missing_voice:,} skipped without voice references")
        if progress.skipped_sound_effects:
            details.append(f"{progress.skipped_sound_effects:,} sound effects skipped")
        if progress.rate_per_minute is not None:
            details.append(f"{progress.rate_per_minute:g} lines/min")
        if progress.eta_seconds is not None:
            details.append(f"ETA {format_duration(progress.eta_seconds)}")
        self.progress_details.setText(" | ".join(details))
        if progress.missing_voice_names:
            self.progress_details.setToolTip(
                "Missing voices: " + ", ".join(progress.missing_voice_names)
            )
        else:
            self.progress_details.setToolTip("")
        if progress.active_line:
            current = f"Current: {progress.active_line}"
            if progress.active_speaker:
                current += f" - {progress.active_speaker}"
            if progress.active_text:
                current += f"\n{progress.active_text}"
            if progress.active_last_error and progress.active_phase == "retrying":
                current += f"\nRetry reason: {progress.active_last_error}"
            self.latest_line.setText(current)
        elif progress.latest_line:
            latest = f"Latest: {progress.latest_line}"
            if progress.latest_text:
                latest += f"\n{progress.latest_text}"
            self.latest_line.setText(latest)
        else:
            model = str(job.get("model") or "")
            if (
                runtime_status in {"running_here", "running_external"}
                and Path(model).expanduser().is_absolute()
            ):
                self.latest_line.setText(f"Loading local model:\n{model}")
            elif runtime_status in {"running_here", "running_external"}:
                self.latest_line.setText(
                    "The model may be downloading. Generation starts after it is loaded."
                )
            else:
                self.latest_line.setText("Waiting for the first generated line.")
        self.update_job_table_row(self.current_job, progress)
        self.update_controls(progress)

    def update_job_table_row(self, job_directory, progress):
        if job_directory is None:
            return
        wanted = str(Path(job_directory).resolve())
        for row in range(self.job_table.rowCount()):
            item = self.job_table.item(row, 0)
            if item is None or item.data(Qt.ItemDataRole.UserRole) != wanted:
                continue
            job = load_pregeneration_job(job_directory)
            runtime_status = resolve_job_runtime_status(
                job,
                local_running=(
                    self.process_is_running()
                    and self.current_job is not None
                    and Path(job_directory).resolve() == self.current_job.resolve()
                ),
            )
            self.job_table.item(row, 3).setText(f"{progress.generated}/{progress.eligible}")
            self.job_table.item(row, 4).setText(
                status_labels.get(runtime_status, runtime_status.title())
            )
            break

    def update_controls(self, progress=None):
        running = self.process_is_running()
        self.stop_button.setEnabled(running)
        self.start_button.setEnabled(bool(self.selected_target_ids()) and not running)
        if progress is None and self.current_job is not None:
            try:
                progress = read_generation_progress(self.current_job)
            except PregenerationError:
                progress = None
        runtime_status = None
        if self.current_job is not None:
            try:
                runtime_status = resolve_job_runtime_status(
                    load_pregeneration_job(self.current_job),
                    local_running=running,
                )
            except PregenerationError:
                runtime_status = None
        can_resume = (
            self.selected_job_directory() is not None
            and not running
            and progress is not None
            and runtime_status not in {"running_here", "running_external"}
            and (progress.pending > 0 or progress.failed > 0)
        )
        self.resume_button.setEnabled(can_resume)

    def show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event):
        if not self.process_is_running():
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "Generation is running",
            "Closing this window will stop generation. Close anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.process.kill()
            event.accept()
        else:
            event.ignore()


def create_parser():
    parser = argparse.ArgumentParser(
        description="Open the Reverse: 1999 chapter and anecdote voice pregeneration UI."
    )
    parser.add_argument("--story-index", type=Path)
    parser.add_argument("--voice-manifest", type=Path)
    parser.add_argument("--vntts-python", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--resume-job", type=Path)
    parser.add_argument("--jobs-root", type=Path, default=default_jobs_root)
    parser.add_argument("--narrator-character", default="Matilda")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    application = QApplication.instance() or QApplication(sys.argv)
    dialog = PregenerationDialog(
        story_index=options.story_index,
        voice_manifest=options.voice_manifest,
        vntts_python=options.vntts_python,
        model=options.model,
        jobs_root=options.jobs_root,
        narrator_character=options.narrator_character,
    )
    dialog.show()
    if options.resume_job:
        job_directory = options.resume_job.expanduser().resolve()
        dialog.refresh_jobs(select_job=job_directory)
        QTimer.singleShot(0, lambda: dialog.launch_job(job_directory))
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
