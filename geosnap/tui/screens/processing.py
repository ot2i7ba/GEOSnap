# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Third screen: run the pipeline in a worker thread and show live progress."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, Label, LoadingIndicator, ProgressBar
from textual.worker import Worker, WorkerState, get_current_worker

from geosnap.extraction.extractor import ExtractionProgress
from geosnap.online.http_client import OnlineClient
from geosnap.project.pipeline import (
    PipelinePhase,
    ProjectRequest,
    ProjectResult,
    ProjectStatus,
    run_project,
)
from geosnap.settings import Settings
from geosnap.tui.formatting import format_file_size
from geosnap.tui.screens.confirm_cancel import ConfirmCancelScreen
from geosnap.tui.widgets import (
    CONTENT_SCROLL_BINDINGS,
    ContentArea,
    ContentScreen,
    StatusHeader,
)

logger = logging.getLogger(__name__)


def describe_request_sources(request: ProjectRequest) -> str:
    if len(request.sources) == 1:
        return request.source_path.name
    return f"{len(request.sources)} sources"


def describe_reading_source(progress: ExtractionProgress) -> str | None:
    """'reading 2 of 3: Laptop' when the progress names its source, else None."""
    if not progress.source_label:
        return None
    return f"reading {progress.source_index} of {progress.source_count}: {progress.source_label}"


class PhaseChanged(Message):
    def __init__(self, phase: PipelinePhase) -> None:
        super().__init__()
        self.phase = phase


class ProgressUpdated(Message):
    def __init__(self, progress: ExtractionProgress) -> None:
        super().__init__()
        self.progress = progress


class OnlineProgress(Message):
    def __init__(self, completed: int, total: int, label: str) -> None:
        super().__init__()
        self.completed = completed
        self.total = total
        self.label = label


class _WorkerObserver:
    """Forwards pipeline events from the worker thread to the screen's message queue."""

    def __init__(self, screen: ProcessingScreen) -> None:
        self._screen = screen

    def on_phase(self, phase: PipelinePhase) -> None:
        self._screen.post_message(PhaseChanged(phase))

    def on_progress(self, progress: ExtractionProgress) -> None:
        self._screen.post_message(ProgressUpdated(progress))

    def on_online_progress(self, completed: int, total: int, label: str) -> None:
        self._screen.post_message(OnlineProgress(completed, total, label))


class ProcessingScreen(ContentScreen[ProjectResult]):
    BINDINGS = [Binding("escape", "request_cancel", "Cancel"), *CONTENT_SCROLL_BINDINGS]

    def __init__(
        self,
        request: ProjectRequest,
        settings: Settings,
        app_root: Path,
        online_client: OnlineClient | None = None,
    ) -> None:
        super().__init__()
        self.request = request
        self.settings = settings
        self.app_root = app_root
        self.online_client = online_client
        self._cancel_requested = False
        # Ctrl+Q during the run: close the application once the pipeline has returned.
        self._quit_requested = False
        self._started_at = 0.0
        self._pending_result: ProjectResult | None = None

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            yield Label("Processing", classes="heading")
            yield Label(
                f"Processing {describe_request_sources(self.request)} "
                f"into project '{self.request.project_name}'",
                id="processing-title",
                markup=False,
            )
            yield Label("Phase: starting", id="phase", markup=False)
            yield ProgressBar(id="progress", show_eta=True)
            yield LoadingIndicator(id="spinner")
            yield Label("", id="counters", markup=False)
            yield Label("Elapsed: 00:00", id="timing", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one(StatusHeader).status = "Processing"
        self.query_one("#spinner", LoadingIndicator).display = False
        self._started_at = time.monotonic()
        self.set_interval(1.0, self._refresh_timing)
        self.run_pipeline()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def run_pipeline(self) -> ProjectResult:
        # An application exit cancels the worker, not the thread: the pipeline has to see
        # that as well, or it would go on writing the project and asking online services.
        worker = get_current_worker()
        return run_project(
            self.request,
            self.settings,
            self.app_root,
            _WorkerObserver(self),
            lambda: self._cancel_requested or worker.is_cancelled,
            online_client=self.online_client,
        )

    def on_phase_changed(self, message: PhaseChanged) -> None:
        self.query_one("#phase", Label).update(f"Phase: {message.phase.value}")
        reading = message.phase is PipelinePhase.READING
        self.query_one("#progress", ProgressBar).display = reading
        self.query_one("#spinner", LoadingIndicator).display = not reading

    def on_progress_updated(self, message: ProgressUpdated) -> None:
        progress = message.progress
        reading_line = describe_reading_source(progress)
        if reading_line is not None and not self._cancel_requested:
            self.query_one("#phase", Label).update(f"Phase: {reading_line}")
        self.query_one("#progress", ProgressBar).update(
            total=max(progress.size_bytes, 1), progress=progress.bytes_read
        )
        self.query_one("#counters", Label).update(
            f"Accepted: {progress.accepted}   Invalid: {progress.invalid}   "
            f"Unrelated: {progress.unrelated}   Read: {format_file_size(progress.bytes_read)} "
            f"of {format_file_size(progress.size_bytes)}"
        )

    def on_online_progress(self, message: OnlineProgress) -> None:
        self.query_one("#counters", Label).update(
            f"Online {message.label}: {message.completed} of {message.total}"
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, ProjectResult):
            self._deliver(event.worker.result)
        elif event.state is WorkerState.ERROR:
            logger.error("Processing worker failed: %s", event.worker.error)
            self._deliver(
                ProjectResult(status=ProjectStatus.FAILED, error_message=str(event.worker.error))
            )

    def on_screen_resume(self) -> None:
        """The cancel dialog closed above us; hand over a result that arrived meanwhile,
        or close the application when the quit was confirmed in that dialog."""
        finished = self._pending_result
        if finished is not None:
            self._pending_result = None
            if self._quit_requested:
                logger.info("Run ended while the quit was confirmed; closing the application")
                self.app.exit()
                return
            self.dismiss(finished)

    def _deliver(self, result: ProjectResult) -> None:
        """Dismiss with the result, or park it while the cancel dialog is on top.

        Dismissing while the dialog is above us would pop the dialog instead of this
        screen and leave the processing screen behind on the stack. After Ctrl+Q the
        cancelled state is written now, and the application closes.
        """
        if self._quit_requested:
            logger.info("Run ended after the quit request; closing the application")
            self.app.exit()
            return
        if self.is_active:
            self.dismiss(result)
            return
        logger.info("Run finished while the cancel dialog was open; result is held back")
        self._pending_result = result

    def action_request_cancel(self) -> None:
        if self._cancel_requested:
            return
        self.app.push_screen(ConfirmCancelScreen(), self._apply_cancel_decision)

    def request_quit(self) -> None:
        """Ctrl+Q during the run: confirm, cancel, and close once the pipeline returns.

        Only while no dialog or notice is on top: a second dialog above it would be
        confusing, and the pipeline goes on until the one on top is answered."""
        if self._quit_requested or not self.is_active:
            self.app.bell()
            return
        if self._cancel_requested:
            self._apply_quit_decision(True)
            return
        self.app.push_screen(ConfirmCancelScreen(quitting=True), self._apply_quit_decision)

    def _apply_cancel_decision(self, confirmed: bool | None) -> None:
        if confirmed:
            self._cancel_requested = True
            self.query_one("#phase", Label).update("Phase: cancelling after the current block")

    def _apply_quit_decision(self, confirmed: bool | None) -> None:
        if confirmed:
            self._cancel_requested = True
            self._quit_requested = True
            self.query_one("#phase", Label).update(
                "Phase: cancelling after the current block, then GEOSnap closes"
            )

    def _refresh_timing(self) -> None:
        elapsed = int(time.monotonic() - self._started_at)
        self.query_one("#timing", Label).update(f"Elapsed: {elapsed // 60:02d}:{elapsed % 60:02d}")
