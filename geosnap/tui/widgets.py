# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Widgets shared by every screen."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import ClassVar
from zoneinfo import available_timezones

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen, ScreenResultType
from textual.style import Style
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static

from geosnap import APP_NAME, AUTHOR_NAME, COPYRIGHT_YEAR, __version__

HEX_COLOUR_PATTERN = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})", re.IGNORECASE)


class StatusHeader(Widget):
    """One-line header: application name, version, author with key g (opens the author's
    page) and the current status. A narrow window gets a shorter form without the year."""

    # The key letter is coloured like the keys in the footer.
    COMPONENT_CLASSES = {"status-header--key"}
    DEFAULT_CSS = """
    StatusHeader > .status-header--key {
        color: $footer-key-foreground;
    }
    """

    status: reactive[str] = reactive("Idle")

    def render(self) -> Content:
        title = f"{APP_NAME} v{__version__}"
        status = f"Status: {self.status}"
        line = f"{title}   |   by {AUTHOR_NAME} (c) {COPYRIGHT_YEAR} [g]   |   {status}"
        if len(line) > self.content_size.width:
            line = f"{title} | by {AUTHOR_NAME} [g] | {status}"
        # Plain content: "[g]" would otherwise be read as markup.
        key_start = line.index("[g]") + 1
        key_style = Style.from_rich_style(
            self.get_component_rich_style("status-header--key", partial=True)
        )
        return Content(line).stylize(key_style, key_start, key_start + 1)


# Appended to the BINDINGS of a ContentScreen. PgUp/PgDn have priority because inputs and
# scroll containers bind them without a footer hint and would otherwise shadow this one.
CONTENT_SCROLL_BINDINGS = (
    Binding(
        "pagedown", "scroll_content(1, True)", "Scroll", key_display="PgUp/PgDn", priority=True
    ),
    Binding("pageup", "scroll_content(-1, True)", show=False, priority=True),
    Binding("down", "scroll_content(1, False)", show=False),
    Binding("up", "scroll_content(-1, False)", show=False),
)


class ContentArea(VerticalScroll, can_focus=False):
    """The scrolling text area of a ContentScreen; the screen's keys scroll it, so it
    stays out of the Tab order."""


class ContentScreen(Screen[ScreenResultType]):
    """A screen whose text may exceed the window: PgUp/PgDn (and the arrow keys, where the
    focused widget does not use them) scroll it. The first listed area that can still move
    in the direction scrolls; an area that is not on the screen at the moment is skipped."""

    SCROLL_AREA_SELECTORS: ClassVar[tuple[str, ...]] = ("#content",)

    def action_scroll_content(self, direction: int, by_page: bool) -> None:
        scroll_areas = [
            area for selector in self.SCROLL_AREA_SELECTORS for area in self.query(selector)
        ]
        for scroll_area in scroll_areas:
            can_move = (
                scroll_area.scroll_y < scroll_area.max_scroll_y
                if direction > 0
                else scroll_area.scroll_y > 0
            )
            if can_move:
                rows = scroll_area.scrollable_content_region.height if by_page else 1
                scroll_area.scroll_relative(y=direction * rows, animate=False)
                return


def resolve_colour_entry(entry: str, named_colours: Sequence[tuple[str, str]]) -> str | None:
    """Hex colour of a listed name (case-insensitive) or of #rgb/#rrggbb as lowercase #rrggbb."""
    wanted = entry.strip()
    for name, colour in named_colours:
        if name.casefold() == wanted.casefold():
            return colour
    if HEX_COLOUR_PATTERN.fullmatch(wanted) is None:
        return None
    digits = wanted[1:].lower()
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    return f"#{digits}"


class ChoiceList(OptionList):
    """Dropdown of a FilteredChoiceField; a click chooses without taking the focus from
    the entry."""

    FOCUS_ON_CLICK = False


class FilteredChoiceField(Widget):
    """Entry with a dropdown of named choices that typing filters.

    Down opens the full list, typing filters it, Enter or a click chooses, Escape closes
    it. The focus stays in the entry throughout. Subclasses provide the choice names, the
    filter and the labels shown in the list.
    """

    DEFAULT_CSS = """
    FilteredChoiceField {
        height: auto;
    }
    FilteredChoiceField > Horizontal {
        height: auto;
    }
    FilteredChoiceField Input {
        width: 1fr;
    }
    FilteredChoiceField > ChoiceList {
        display: none;
        width: 1fr;
        height: auto;
        max-height: 10;
        overlay: screen;
        constrain: none inside;
        border: tall $border-blurred;
        background: $surface;
    }
    FilteredChoiceField.-open > ChoiceList {
        display: block;
    }
    """

    BINDINGS = [
        Binding("down", "choice_down", "Choice list", show=False),
        Binding("up", "choice_up", show=False),
        Binding("escape", "close_choices", show=False),
        Binding("tab", "leave_forward", show=False),
    ]

    def __init__(
        self,
        value: str,
        *,
        placeholder: str = "",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._initial_entry = value
        self._placeholder = placeholder
        # Names behind the listed options, in list order.
        self._shown_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Input(value=self._initial_entry, placeholder=self._placeholder)
            yield from self.compose_beside_entry()
        yield ChoiceList()

    def compose_beside_entry(self) -> ComposeResult:
        """Widgets to the right of the entry (none by default)."""
        yield from ()

    def choice_names(self) -> list[str]:
        """Every name the list offers, in list order."""
        raise NotImplementedError

    def matching_names(self, typed: str) -> list[str]:
        """Names the list shows for the folded, stripped entry text (a prefix match)."""
        return [name for name in self.choice_names() if name.casefold().startswith(typed)]

    def choice_label(self, name: str) -> str:
        """The text shown in the list for a name."""
        return name

    @property
    def value(self) -> str:
        """The entry text as typed or chosen."""
        return self.query_one(Input).value

    class ChoicesToggled(Message):
        """The list opened or closed; it lies over whatever is below the field."""

        def __init__(self, field: FilteredChoiceField) -> None:
            super().__init__()
            self.field = field

        @property
        def control(self) -> FilteredChoiceField:
            return self.field

    @property
    def choices_open(self) -> bool:
        return self.has_class("-open")

    def _set_choices_open(self, open_list: bool) -> None:
        if open_list != self.choices_open:
            self.set_class(open_list, "-open")
            self.post_message(self.ChoicesToggled(self))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Down always serves the list; the other keys only while it is open."""
        if action in ("choice_up", "close_choices", "leave_forward"):
            return self.choices_open
        return True

    def action_choice_down(self) -> None:
        if self.choices_open:
            self.query_one(ChoiceList).action_cursor_down()
        else:
            self._open_choices(self.choice_names())

    def action_choice_up(self) -> None:
        self.query_one(ChoiceList).action_cursor_up()

    def action_close_choices(self) -> None:
        self._set_choices_open(False)

    def action_leave_forward(self) -> None:
        """Tab leaves the field for the next one instead of entering the open list."""
        self._set_choices_open(False)
        self.screen.focus_next()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not event.input.has_focus:
            return
        typed = event.value.strip().casefold()
        if typed in (name.casefold() for name in self.choice_names()):
            self._set_choices_open(False)
            return
        matching = self.matching_names(typed)
        if matching:
            self._open_choices(matching)
        else:
            self._set_choices_open(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter reaches the entry only while the list is open (the screen yields it)."""
        choices = self.query_one(ChoiceList)
        if self.choices_open and choices.highlighted is not None:
            event.stop()
            self._choose(self._shown_names[choices.highlighted])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(self._shown_names[event.option_index])

    def on_descendant_blur(self, _: events.DescendantBlur) -> None:
        self._set_choices_open(False)

    def _open_choices(self, names: list[str]) -> None:
        self._shown_names = names
        choices = self.query_one(ChoiceList)
        choices.set_options([self.choice_label(name) for name in names])
        folded_names = [name.casefold() for name in names]
        current = self.value.strip().casefold()
        choices.highlighted = folded_names.index(current) if current in folded_names else 0
        self._set_choices_open(True)

    def _choose(self, name: str) -> None:
        entry = self.query_one(Input)
        entry.value = name
        entry.cursor_position = len(name)
        self._set_choices_open(False)
        entry.focus()


class ColourField(FilteredChoiceField):
    """Colour entry taking a listed name or a hex value, with a swatch and a name dropdown."""

    DEFAULT_CSS = """
    ColourField .colour-swatch {
        width: 3;
        height: 1;
        margin: 1 0 1 1;
        content-align: center middle;
    }
    """

    def __init__(
        self,
        named_colours: Sequence[tuple[str, str]],
        value: str,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(value, id=id, classes=classes)
        self.named_colours = tuple(named_colours)

    def compose_beside_entry(self) -> ComposeResult:
        yield Static("", classes="colour-swatch")

    def on_mount(self) -> None:
        self._show_swatch()

    def choice_names(self) -> list[str]:
        return [name for name, _ in self.named_colours]

    @property
    def colour(self) -> str | None:
        """The effective hex colour, or None when the entry is neither a listed name nor hex."""
        return resolve_colour_entry(self.value, self.named_colours)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._show_swatch()
        super().on_input_changed(event)

    def _show_swatch(self) -> None:
        swatch = self.query_one(".colour-swatch", Static)
        colour = self.colour
        swatch.styles.background = colour
        swatch.update("" if colour else "?")


# Names in zoneinfo that are not zones a record could be attributed to.
NON_ZONE_NAMES = frozenset({"localtime", "posixrules", "Factory"})
ZONE_HINT_SEPARATOR = " · "


@dataclass(frozen=True, slots=True)
class ZoneChoice:
    """An IANA zone with the text a user may know it by: countries and the zone1970.tab
    comment ("Germany, Denmark, ... · most of Germany"); empty for backward-compatible
    link names such as "CET", which the table does not list."""

    name: str
    hint: str

    @property
    def label(self) -> str:
        return f"{self.name}{ZONE_HINT_SEPARATOR}{self.hint}" if self.hint else self.name


def _zoneinfo_table(file_name: str) -> list[list[str]]:
    """Tab-separated rows of a tzdata table (comments skipped)."""
    text = (resources.files("tzdata") / "zoneinfo" / file_name).read_text(encoding="utf-8")
    return [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]


@cache
def zone_choices() -> tuple[ZoneChoice, ...]:
    """Every zone with a table entry, sorted by name, then the link names sorted by name."""
    country_names = {code: country for code, country in _zoneinfo_table("iso3166.tab")}
    hints: dict[str, str] = {}
    for codes, _coordinates, zone_name, *comment in _zoneinfo_table("zone1970.tab"):
        countries = ", ".join(country_names.get(code, code) for code in codes.split(","))
        hints[zone_name] = countries + (ZONE_HINT_SEPARATOR + comment[0] if comment else "")
    zone_names = sorted(available_timezones() - NON_ZONE_NAMES)
    return tuple(
        ZoneChoice(name, hints.get(name, ""))
        for name in sorted(zone_names, key=lambda name: (name not in hints, name))
    )


class ZoneField(FilteredChoiceField):
    """Entry for an IANA zone name with a dropdown that a typed part of the name, a
    country or a region filters ("berl", "germany", "new_y")."""

    DEFAULT_CSS = """
    ZoneField > ChoiceList {
        min-width: 60;
        constrain: inside inside;
    }
    """

    def __init__(
        self,
        value: str,
        *,
        placeholder: str = "e.g. berl",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(value, placeholder=placeholder, id=id, classes=classes)
        self._choices_by_name = {choice.name: choice for choice in zone_choices()}

    def choice_names(self) -> list[str]:
        return list(self._choices_by_name)

    def matching_names(self, typed: str) -> list[str]:
        """Substring matches in the name or the hint; zones whose city part starts with
        the text come first, then the table order."""
        matching = [
            choice.name
            for choice in self._choices_by_name.values()
            if typed in choice.name.casefold() or typed in choice.hint.casefold()
        ]
        return sorted(
            matching,
            key=lambda name: (not name.rsplit("/", 1)[-1].casefold().startswith(typed),),
        )

    def choice_label(self, name: str) -> str:
        return self._choices_by_name[name].label

    @property
    def zone_name(self) -> str | None:
        """The canonical zone name for the entry (case-insensitive), or None."""
        typed = self.value.strip().casefold()
        for name in self._choices_by_name:
            if name.casefold() == typed:
                return name
        return None
