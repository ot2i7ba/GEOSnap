# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Modal confirmation shown when the user presses Escape or Ctrl+Q during processing."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label


class ConfirmCancelScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes, cancel"),
        Binding("n", "keep_running", "No, keep running"),
        Binding("escape", "keep_running", "No", show=False),
    ]

    def __init__(self, quitting: bool = False) -> None:
        """``quitting``: Ctrl+Q asked; the application closes after the cancellation."""
        super().__init__()
        self.quitting = quitting

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            if self.quitting:
                yield Label("Cancel processing and quit?", id="confirm-title")
                yield Label(
                    "Partial results stay in the project directory, and metadata.json "
                    "records the cancellation. GEOSnap closes once that is saved."
                )
                yield Label("Press Y to cancel and quit, or N to keep running.")
                return
            yield Label("Cancel processing?", id="confirm-title")
            yield Label(
                "Partial results stay in the project directory, and metadata.json "
                "records the cancellation."
            )
            yield Label("Press Y to cancel, or N to keep running.")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_keep_running(self) -> None:
        self.dismiss(False)
