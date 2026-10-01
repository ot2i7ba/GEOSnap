# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Screen flow: file selection → project setup → processing → summary → back to start;
file selection → finished projects (verify, serve the map again) → back."""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.geometry import Size

from geosnap import APP_NAME, AUTHOR_PAGE_URL, OUTPUT_DIR_NAME
from geosnap.online.http_client import OnlineClient
from geosnap.project.map_server import MapServerPool
from geosnap.project.pipeline import ProjectRequest, ProjectResult
from geosnap.runtime_paths import bundled_resource
from geosnap.settings import Settings
from geosnap.tui.screens.file_select import FileSelectScreen
from geosnap.tui.screens.processing import ProcessingScreen
from geosnap.tui.screens.project_setup import ProjectSetupScreen
from geosnap.tui.screens.projects import ProjectsScreen
from geosnap.tui.screens.summary import SummaryScreen
from geosnap.tui.screens.window_too_small import WindowTooSmallScreen, window_is_too_small


class GeoSnapApp(App[None]):
    TITLE = APP_NAME
    CSS_PATH = str(bundled_resource("geosnap/tui/styles.tcss"))
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False),
        # Shown in the header as "[g]"; a focused text field keeps the letter.
        Binding("g", "open_author_page", show=False),
    ]
    # styles.tcss lays rows out side by side only on screens that carry -wide.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]

    def __init__(
        self, settings: Settings, app_root: Path, online_client: OnlineClient | None = None
    ) -> None:
        super().__init__()
        self.settings = settings
        self.app_root = app_root
        self.online_client = online_client
        self.map_servers = MapServerPool()
        # A Resize can arrive before on_mount; a notice pushed then would end up below the
        # file selection, so the notice waits for the first screen.
        self._first_screen_shown = False

    async def on_mount(self) -> None:
        self.show_file_selection()
        self._first_screen_shown = True
        await self._fit_size_notice(self.size)

    def on_unmount(self) -> None:
        self.map_servers.stop_all()

    async def on_resize(self, event: events.Resize) -> None:
        if self._first_screen_shown:
            await self._fit_size_notice(event.size)

    async def _fit_size_notice(self, size: Size) -> None:
        """Below the usable size a notice covers the screen; it goes once the window fits."""
        notice_shown = isinstance(self.screen, WindowTooSmallScreen)
        if window_is_too_small(size):
            if not notice_shown:
                await self.push_screen(WindowTooSmallScreen())
            notice = self.screen
            assert isinstance(notice, WindowTooSmallScreen)
            notice.show_size(size)
        elif notice_shown:
            self.pop_screen()

    def action_open_author_page(self) -> None:
        self.open_url(AUTHOR_PAGE_URL)

    async def action_quit(self) -> None:
        """Ctrl+Q; during a run the processing screen cancels it first, so the pipeline
        records its cancelled state before the application closes."""
        running = next(
            (screen for screen in self.screen_stack if isinstance(screen, ProcessingScreen)),
            None,
        )
        if running is None:
            self.exit()
        else:
            running.request_quit()

    def show_file_selection(self) -> None:
        self.push_screen(FileSelectScreen(self.app_root), self._after_file_chosen)

    def action_show_projects(self) -> None:
        """Key P of the file selection: finished projects below output/."""
        self.push_screen(ProjectsScreen(self.app_root / OUTPUT_DIR_NAME, self.map_servers))

    def _after_file_chosen(self, source_paths: list[Path] | None) -> None:
        if not source_paths:
            self.exit()
            return
        self.push_screen(
            ProjectSetupScreen(source_paths, self.settings), self._after_project_configured
        )

    def _after_project_configured(self, request: ProjectRequest | None) -> None:
        if request is None:
            self.show_file_selection()
            return
        self.push_screen(
            ProcessingScreen(
                request, self.settings, self.app_root, online_client=self.online_client
            ),
            self._after_processing,
        )

    def _after_processing(self, result: ProjectResult | None) -> None:
        if result is None:
            self.show_file_selection()
            return
        self.push_screen(SummaryScreen(result, self.map_servers), self._after_summary)

    def _after_summary(self, _: None) -> None:
        self.show_file_selection()
