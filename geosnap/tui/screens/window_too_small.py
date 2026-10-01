# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Notice shown instead of a broken layout while the window is below the usable size."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.widgets import Label

from geosnap import APP_NAME

MIN_WINDOW_COLUMNS = 60
MIN_WINDOW_ROWS = 20


def window_is_too_small(size: Size) -> bool:
    return size.width < MIN_WINDOW_COLUMNS or size.height < MIN_WINDOW_ROWS


class WindowTooSmallScreen(ModalScreen[None]):
    """Covers the current screen and takes no keys; the app removes it once the window
    is large enough again. A running project keeps running underneath."""

    def compose(self) -> ComposeResult:
        with Vertical(id="too-small-notice"):
            yield Label("Window too small", classes="heading")
            yield Label("", id="too-small-size", markup=False)

    def show_size(self, size: Size) -> None:
        self.query_one("#too-small-size", Label).update(
            f"{APP_NAME} needs at least {MIN_WINDOW_COLUMNS}x{MIN_WINDOW_ROWS} characters. "
            f"This window has {size.width}x{size.height}. Enlarge it to continue. "
            "A running project keeps running."
        )
