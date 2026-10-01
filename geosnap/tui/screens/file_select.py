# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""First screen: choose one or more of the input files in the input/ directory."""

from __future__ import annotations

import logging
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Label, ListItem, ListView, Static

from geosnap import INPUT_DIR_NAME
from geosnap.tui.formatting import format_file_size
from geosnap.tui.screens.format_help import FormatHelpScreen
from geosnap.tui.widgets import ContentArea, ContentScreen, StatusHeader

logger = logging.getLogger(__name__)

INPUT_SUFFIXES = (".txt", ".log", ".kml", ".kmz", ".gpx", ".csv", ".json", ".geojson")
INPUT_SUFFIX_TEXT = ".txt, .log, .kml, .kmz, .gpx, .csv, .json or .geojson"
SINGLE_DIGIT_LIMIT = 9
# Window width from which the footer has room for the "h Formats" hint: the five other hints
# fill 53 of 60 columns and this one needs 11 more. One column less and the footer cuts it
# (measured in tests/test_tui.py::test_the_footer_shows_the_format_key_from_its_own_width);
# on a narrower window only the prompt names H, while the key itself keeps working.
FORMAT_HELP_FOOTER_MIN_COLUMNS = 64
MARKED_PREFIX = "[x]"
UNMARKED_PREFIX = "[ ]"


def discover_input_files(app_root: Path) -> list[Path]:
    """Files with an input suffix directly in <app root>/input/, sorted by name.

    A missing input/ directory yields an empty list.
    """
    input_dir = app_root / INPUT_DIR_NAME
    if not input_dir.is_dir():
        return []
    candidates = [
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in INPUT_SUFFIXES
    ]
    return sorted(candidates, key=lambda path: path.name.lower())


class FileSelectScreen(ContentScreen[list[Path] | None]):
    """Dismisses with the marked files in list order, or the highlighted file alone."""

    BINDINGS = [
        Binding("enter", "choose", "Open", priority=True),
        Binding("space", "toggle_mark", "Mark", priority=True),
        Binding("backspace", "clear_digits", "Clear number", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "app.show_projects", "Projects"),
        Binding("escape", "quit_app", "Quit"),
        # In the footer only from FORMAT_HELP_FOOTER_MIN_COLUMNS on (see check_action); the
        # key itself works at every width and the prompt names it.
        Binding("h", "show_format_help", "Formats"),
        # Not in the footer: its five keys fill 60 columns, and the arrow keys scroll the list.
        Binding("pagedown", "scroll_content(1, True)", show=False, priority=True),
        Binding("pageup", "scroll_content(-1, True)", show=False, priority=True),
    ]
    # The list is focused by hand: the automatic focus scrolls the still unsized content.
    AUTO_FOCUS = ""

    def __init__(self, app_root: Path) -> None:
        super().__init__()
        self.app_root = app_root
        self.input_files = self._reload_input_files()
        self.marked_files: set[Path] = set()
        self._typed_digits = ""

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            yield Label("Input files", classes="heading")
            yield Label(
                f"Files ({INPUT_SUFFIX_TEXT}) in the {INPUT_DIR_NAME}/ directory next to the "
                "application. Enter or a click opens the highlighted file, or the marked ones. "
                "Space marks files to combine in one project. A number opens that file; once "
                "files are marked, it toggles its mark. P shows finished projects, H the input "
                "formats.",
                id="prompt",
                classes="instructions",
            )
            if self.input_files:
                yield ListView(
                    *[
                        ListItem(
                            Label(self._entry_text(index), markup=False),
                            id=f"file-{index}",
                        )
                        for index in range(1, len(self.input_files) + 1)
                    ],
                    id="file-list",
                )
            else:
                yield Label(
                    f"No input files in {INPUT_DIR_NAME}/. Copy files there and press R.",
                    id="empty-hint",
                )
            yield Static("", id="digit-buffer", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self._show_selection_status()
        self._focus_file_list()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Leaves the Formats hint out while the footer is too narrow for six keys.

        False is what hides a binding in Textual; None would only grey it out and keep its
        room. Both also stop the action itself (App.run_action checks this too), which is why
        on_key handles H before the bindings.
        """
        if action == "show_format_help":
            return self.size.width >= FORMAT_HELP_FOOTER_MIN_COLUMNS
        return True

    def on_resize(self, _: events.Resize) -> None:
        """A resize can cross the width at which the Formats hint fits."""
        self.refresh_bindings()

    def on_key(self, event: events.Key) -> None:
        # H before the bindings: check_action hides its footer hint on a narrow window, and
        # that would disable the binding's action as well (see check_action). Stopping the
        # event also keeps the enabled binding from opening the screen a second time.
        if event.key == "h":
            event.stop()
            self.action_show_format_help()
            return
        # ASCII digits only: isdigit() alone also accepts '²' (AltGr+2), which int() refuses.
        character = event.character
        if (
            not self.input_files
            or character is None
            or not (character.isascii() and character.isdigit())
        ):
            return
        event.stop()
        if len(self.input_files) <= SINGLE_DIGIT_LIMIT:
            self._choose_number(int(character))
            return
        self._typed_digits += character
        self._show_digit_buffer(
            f"Selection: {self._typed_digits}   (Enter to apply, Backspace to clear)"
        )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """The list is as tall as its files; the content area follows the highlight."""
        if event.item is not None:
            event.item.scroll_visible(animate=False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.index is not None:
            self._dismiss_with_selection(event.list_view.index)

    def action_choose(self) -> None:
        if not self.input_files:
            return
        if self._typed_digits:
            self._choose_number(int(self._typed_digits))
            return
        list_view = self.query_one(ListView)
        if list_view.index is not None:
            self._dismiss_with_selection(list_view.index)

    def action_toggle_mark(self) -> None:
        if not self.input_files:
            return
        list_view = self.query_one(ListView)
        if list_view.index is not None:
            self._toggle_mark(list_view.index)

    def action_clear_digits(self) -> None:
        self._typed_digits = ""
        self._show_digit_buffer("")

    async def action_refresh(self) -> None:
        """Re-read the input/ directory and rebuild the list in place, keeping marks."""
        self.input_files = self._reload_input_files()
        self.marked_files &= set(self.input_files)
        self._typed_digits = ""
        await self.recompose()
        self._show_selection_status()
        self._focus_file_list()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_show_format_help(self) -> None:
        self.app.push_screen(FormatHelpScreen())

    def _choose_number(self, number: int) -> None:
        """A number opens that file directly, or toggles its mark while files are marked."""
        if not 1 <= number <= len(self.input_files):
            self._typed_digits = ""
            self._show_digit_buffer(
                f"No file with number {number}; choose 1-{len(self.input_files)}"
            )
            return
        if self.marked_files:
            self._typed_digits = ""
            self._show_digit_buffer("")
            self.query_one(ListView).index = number - 1
            self._toggle_mark(number - 1)
            return
        self.dismiss([self.input_files[number - 1]])

    def _dismiss_with_selection(self, highlighted_index: int) -> None:
        marked_in_list_order = [path for path in self.input_files if path in self.marked_files]
        self.dismiss(marked_in_list_order or [self.input_files[highlighted_index]])

    def _toggle_mark(self, index: int) -> None:
        path = self.input_files[index]
        if path in self.marked_files:
            self.marked_files.remove(path)
        else:
            self.marked_files.add(path)
        entry_label = self.query_one(f"#file-{index + 1}", ListItem).query_one(Label)
        entry_label.update(self._entry_text(index + 1))
        self._show_selection_status()

    def _entry_text(self, number: int) -> str:
        path = self.input_files[number - 1]
        prefix = MARKED_PREFIX if path in self.marked_files else UNMARKED_PREFIX
        try:
            size_text = format_file_size(path.stat().st_size)
        except OSError:
            # Removed or unreadable since the list was built; R refreshes the list.
            size_text = "missing"
        return f"{prefix} {number}. {path.name}  ({size_text})"

    def _show_selection_status(self) -> None:
        marked_count = len(self.marked_files)
        self.query_one(StatusHeader).status = f"{marked_count} selected" if marked_count else "Idle"

    def _show_digit_buffer(self, text: str) -> None:
        self.query_one("#digit-buffer", Static).update(text)

    def _reload_input_files(self) -> list[Path]:
        input_files = discover_input_files(self.app_root)
        logger.info(
            "Found %d input file(s) in %s", len(input_files), self.app_root / INPUT_DIR_NAME
        )
        return input_files

    def _focus_file_list(self) -> None:
        if self.input_files:
            self.query_one(ListView).focus(scroll_visible=False)
