# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""File-format help: what GEOSnap reads from each accepted input format (key H)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Label, Static

from geosnap.tui.format_help import FORMAT_HELP, UNRECOGNISED_JSON_HELP, InputFormatHelp
from geosnap.tui.widgets import (
    CONTENT_SCROLL_BINDINGS,
    ContentArea,
    ContentScreen,
    StatusHeader,
)

# Label of each part of an entry, in the order the screen shows them.
ENTRY_PART_LABELS = (
    ("Read", "reads"),
    ("Required", "required"),
    ("Refused", "refused"),
)


def format_entry_title(entry: InputFormatHelp) -> str:
    """Heading of one format: its suffixes, the format name, and how it is recognised."""
    title = f"{entry.suffix_text} — format {entry.name}"
    return f"{title} ({entry.recognised_by})" if entry.recognised_by else title


class FormatHelpScreen(ContentScreen[None]):
    """Scrolls through every accepted format; Escape returns to the file selection."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        *CONTENT_SCROLL_BINDINGS,
    ]

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            yield Label("Input formats", classes="heading")
            yield Label(
                "What GEOSnap reads from each format, what a file needs and what is refused. "
                "The file name sets the format; a .json file is recognised by its first 64 KB.",
                id="prompt",
                classes="instructions",
            )
            for entry in FORMAT_HELP:
                yield Label(
                    format_entry_title(entry), classes="section-heading heading", markup=False
                )
                for label, field_name in ENTRY_PART_LABELS:
                    yield Label(
                        f"{label}: {getattr(entry, field_name)}",
                        classes="format-help-part",
                        markup=False,
                    )
                yield Static("\n".join(entry.example), classes="format-help-example", markup=False)
            yield Label("Other .json files", classes="section-heading heading")
            yield Label(UNRECOGNISED_JSON_HELP, markup=False)
        yield Footer(show_command_palette=False)

    def action_back(self) -> None:
        self.dismiss(None)
