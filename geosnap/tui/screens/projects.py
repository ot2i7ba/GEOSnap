# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Finished projects: verify one like --verify and serve its map again."""

from __future__ import annotations

import logging
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Label, ListItem, ListView, Static
from textual.worker import Worker, WorkerState

from geosnap import APP_NAME, OUTPUT_DIR_NAME, __version__
from geosnap.audit_log import detach_log_handler
from geosnap.project.finished_projects import (
    FinishedProject,
    attach_reopen_log,
    discover_finished_projects,
    servable_map_path,
)
from geosnap.project.manifest import MANIFEST_FILE_NAME
from geosnap.project.map_server import MapServerPool
from geosnap.project.verify import VerificationReport, render_report, verify_project
from geosnap.project.workspace import REOPEN_LOG_NAME, SEARCH_AREAS_DIR_NAME
from geosnap.tui.map_opening import serve_and_open_map
from geosnap.tui.widgets import (
    CONTENT_SCROLL_BINDINGS,
    ContentArea,
    ContentScreen,
    StatusHeader,
)

logger = logging.getLogger(__name__)


def project_entry_text(number: int, project: FinishedProject) -> str:
    """One list row: directory name, start time from metadata.json, search area count."""
    parts = [
        f"{number}. {project.directory.name}",
        f"started {project.started_local or 'unknown'}",
        f"search areas: {project.search_area_count}",
    ]
    if not project.has_manifest:
        parts.append("no manifest")
    return "   ".join(parts)


class ProjectsScreen(ContentScreen[None]):
    """Enter opens the highlighted project (verification in a worker); O serves its map."""

    # The list is focused by hand: the automatic focus scrolls the still unsized content.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("enter", "open_project", "Verify", priority=True),
        Binding("o", "open_map", "Map"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "back", "Back"),
        *CONTENT_SCROLL_BINDINGS,
    ]

    def __init__(self, output_root: Path, map_servers: MapServerPool) -> None:
        super().__init__()
        self.output_root = output_root
        self.map_servers = map_servers
        self.projects = discover_finished_projects(output_root)
        self.opened_project: FinishedProject | None = None
        # None while the verification runs and for a project without manifest.
        self.opened_report: VerificationReport | None = None
        self._verification_running = False
        self._reopen_log: logging.Handler | None = None

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            yield Label("Finished projects", classes="heading")
            yield Label(
                f"Projects in the {OUTPUT_DIR_NAME}/ directory, newest first. Enter verifies "
                "a project as --verify does, then O serves its map to record more search "
                "areas. Nothing is analysed again or changed.",
                id="prompt",
                classes="instructions",
            )
            if self.projects:
                yield ListView(
                    *[
                        ListItem(Static(project_entry_text(number, project), markup=False))
                        for number, project in enumerate(self.projects, start=1)
                    ],
                    id="project-list",
                )
            else:
                yield Label(
                    f"No finished projects in {OUTPUT_DIR_NAME}/. Press R to refresh.",
                    id="empty-hint",
                )
            yield Label("Verification", id="verification-title", classes="heading")
            yield Static(
                "No project verified yet." if self.projects else "",
                id="verification-report",
                markup=False,
            )
            yield Static("", id="map-status", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one(StatusHeader).status = "Projects"
        self._focus_project_list()

    def on_unmount(self) -> None:
        self._detach_reopen_log()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            event.item.scroll_visible(animate=False)

    def action_back(self) -> None:
        if self._verification_running:
            # The worker reports to this screen; leave once it has finished.
            self._show_map_status("Verification is still running.")
            return
        self.dismiss(None)

    async def action_refresh(self) -> None:
        if self._verification_running:
            return
        self._close_opened_project()
        self.projects = discover_finished_projects(self.output_root)
        await self.recompose()
        self._focus_project_list()

    def action_open_project(self) -> None:
        project = self._highlighted_project()
        if project is None or self._verification_running:
            return
        self._close_opened_project()
        self.opened_project = project
        self._show_map_status("")
        try:
            self._reopen_log = attach_reopen_log(project.directory)
        except OSError as error:
            logger.warning("Cannot write %s of %s: %s", REOPEN_LOG_NAME, project.directory, error)
            self._show_map_status(f"Cannot write {REOPEN_LOG_NAME}: {error}")
        logger.info("Project reopened with %s %s: %s", APP_NAME, __version__, project.directory)
        if not project.has_manifest:
            logger.warning(
                "Verification: %s has no %s and cannot be verified",
                project.directory,
                MANIFEST_FILE_NAME,
            )
            self._show_report(
                [
                    f"{project.directory}",
                    f"This project cannot be verified: {MANIFEST_FILE_NAME} is missing or not "
                    "a regular file (older version, or the run did not finish). O serves its "
                    "map unverified.",
                ]
            )
            return
        self._verification_running = True
        self.query_one(StatusHeader).status = "Verifying"
        self._show_report([f"Verifying {project.directory} ..."])
        self.verify_opened_project(project.directory)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def verify_opened_project(self, project_dir: Path) -> VerificationReport:
        return verify_project(project_dir)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        self._verification_running = False
        self.query_one(StatusHeader).status = "Projects"
        report = event.worker.result
        if event.state is WorkerState.ERROR or not isinstance(report, VerificationReport):
            logger.error("Verification failed: %s", event.worker.error)
            self._close_opened_project()
            self._show_report([f"Error: the project could not be verified: {event.worker.error}"])
            return
        self.opened_report = report
        if report.deviation_count:
            logger.warning(
                "Verification: %d deviation(s) in %s", report.deviation_count, report.project_dir
            )
        else:
            logger.info(
                "Verification: everything matches, manifest sha256=%s", report.manifest_sha256
            )
        self._show_report(render_report(report))

    def action_open_map(self) -> None:
        """Serve the opened project's own map file, as the summary screen does."""
        project = self.opened_project
        if self._verification_running:
            self._show_map_status("Verification is still running.")
            return
        if project is None or project is not self._highlighted_project():
            if self.projects:
                self._show_map_status(
                    "Press Enter first to verify the project, then O serves its map."
                )
            return
        map_html_path = servable_map_path(project)
        if map_html_path is None:
            self._show_map_status(
                "No map to serve: the map file is missing, or it or "
                f"{SEARCH_AREAS_DIR_NAME}/ is a link."
            )
            return
        verification_note = ""
        if self.opened_report is None:
            verification_note = f"Not verified (no {MANIFEST_FILE_NAME}). "
        elif self.opened_report.deviation_count:
            deviations = self.opened_report.deviation_count
            logger.warning(
                "Serving the map of %s despite %d verification deviation(s)",
                project.directory,
                deviations,
            )
            verification_note = (
                f"Verification found {deviations} deviation(s). The map is served as it is. "
            )
        served_text = serve_and_open_map(
            self.map_servers,
            map_html_path,
            local_tiles_used=project.tile_source_used == "local",
        )
        self._show_map_status(verification_note + served_text)

    def _highlighted_project(self) -> FinishedProject | None:
        if not self.projects:
            return None
        index = self.query_one("#project-list", ListView).index
        return None if index is None else self.projects[index]

    def _close_opened_project(self) -> None:
        self.opened_project = None
        self.opened_report = None
        self._detach_reopen_log()

    def _detach_reopen_log(self) -> None:
        if self._reopen_log is not None:
            detach_log_handler(self._reopen_log)
            self._reopen_log = None

    def _show_report(self, lines: list[str]) -> None:
        self.query_one("#verification-report", Static).update("\n".join(lines))
        # The report starts at the top of the window; the list comes back with PgUp or
        # with the next move of its highlight.
        self.query_one("#verification-title", Label).scroll_visible(animate=False, top=True)

    def _show_map_status(self, text: str) -> None:
        self.query_one("#map-status", Static).update(text)

    def _focus_project_list(self) -> None:
        if self.projects:
            self.query_one("#project-list", ListView).focus(scroll_visible=False)
