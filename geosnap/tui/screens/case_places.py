# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Dialog: enter the case places of a project by hand or load them from a CSV file."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import tzinfo
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from geosnap.analysis.case_places import CasePlace
from geosnap.project.case_place_input import (
    CASE_PLACES_CSV_HEADER,
    MAX_CASE_PLACES,
    CasePlaceFields,
    CasePlaceInputError,
    CasePlacesFile,
    case_place_fields,
    load_case_places_csv,
    parse_case_place,
)
from geosnap.settings import Settings
from geosnap.timezones import display_local_text

TABLE_CELL_MAX_CHARS = 28
# Field of CasePlaceFields, dialog label, placeholder; the order of the inputs.
FIELD_INPUTS = (
    ("label", "Label", "e.g. Kiosk Hauptstrasse"),
    ("radius_m", "Radius (m)", "100 (10-5000)"),
    ("latitude", "Latitude", "51.45561"),
    ("longitude", "Longitude", "7.01156"),
    ("from_local", "Window from", "YYYY-MM-DD HH:MM, optional"),
    ("to_local", "Window to", "YYYY-MM-DD HH:MM, optional"),
    ("address", "Address", "looked up if no coordinates"),
    ("note", "Note", "optional"),
)
INPUT_IDS = {
    "label": "case-place-label",
    "radius_m": "case-place-radius",
    "latitude": "case-place-latitude",
    "longitude": "case-place-longitude",
    "from_local": "case-place-from",
    "to_local": "case-place-to",
    "address": "case-place-address",
    "note": "case-place-note",
}


@dataclass(frozen=True, slots=True)
class CasePlacesEntry:
    """What the dialog returns: the places in order and the CSV they were loaded from,
    the texts each place was entered with and the display zone its window was read in
    (the setup reads the windows again when the display zone changes afterwards)."""

    places: tuple[CasePlace, ...] = ()
    file: CasePlacesFile | None = None
    entered_fields: tuple[CasePlaceFields, ...] = ()
    zone_name: str = ""


def describe_case_places(places: tuple[CasePlace, ...], settings: Settings) -> str:
    """One line for the project setup; names what an address-only place will cause."""
    if not places:
        return "none"
    count = len(places)
    parts = [f"{count} case place{'' if count == 1 else 's'}"]
    windows = sum(1 for place in places if place.window is not None)
    if windows:
        parts.append(f"{windows} with a window")
    unlocated = sum(1 for place in places if not place.located)
    if unlocated:
        if not settings.online.enabled:
            parts.append(f"{unlocated} not located (online services are off)")
        elif not settings.online.nominatim_enabled:
            parts.append(f"{unlocated} not located (Nominatim is disabled)")
        else:
            noun = "address goes" if unlocated == 1 else "addresses go"
            parts.append(f"{unlocated} {noun} to Nominatim")
    return " · ".join(parts)


class CasePlacesScreen(ModalScreen[CasePlacesEntry | None]):
    """Table of the entered places, one input per field, Add/Update/Remove, and "Load file"
    for a CSV from the input directory. OK returns the rows, Cancel changes nothing."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        entry: CasePlacesEntry,
        input_directory: Path,
        excluded_files: list[Path],
        display_tz: tzinfo | None,
        zone_name: str,
    ) -> None:
        super().__init__()
        self.places = list(entry.places)
        # The texts of each place, parallel to self.places.
        self.entered_fields = list(entry.entered_fields)
        self.loaded_file = entry.file
        self.input_directory = input_directory
        self.excluded_names = {path.name for path in excluded_files}
        self.display_tz = display_tz
        self.zone_name = zone_name
        # Table row whose values are in the inputs (Update and Remove act on it).
        self.edited_row: int | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="case-places-dialog"):
            yield Label(
                f"Case places from the case file (times in {self.zone_name})",
                id="case-places-title",
                markup=False,
            )
            yield DataTable(id="case-places-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="case-places-detail", markup=False)
            with Grid(id="case-place-grid"):
                for field_name, field_label, placeholder in FIELD_INPUTS:
                    yield Label(f"{field_label}:", classes="case-place-field-label")
                    yield Input(
                        placeholder=placeholder,
                        id=INPUT_IDS[field_name],
                        classes="case-place-input",
                    )
            with Horizontal(id="case-places-file-row"):
                yield Select(
                    [(Text(name), name) for name in self._csv_file_names()],
                    prompt="CSV file in the input directory",
                    id="case-places-file",
                )
                yield Button("Load file", id="case-places-load")
            # Docked below the scrolling body: a long refusal scrolls in its own area
            # instead of pushing the buttons away.
            with Vertical(id="case-places-footer"):
                with VerticalScroll(id="case-places-feedback-area"):
                    yield Static("", id="case-places-feedback", markup=False)
                with Horizontal(id="case-places-buttons"):
                    yield Button("Add", id="case-place-add")
                    yield Button("Update", id="case-place-update", disabled=True)
                    yield Button("Remove", id="case-place-remove", disabled=True)
                    yield Button("OK", variant="primary", id="case-places-ok")
                    yield Button("Cancel", id="case-places-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#case-places-table", DataTable)
        for column in ("#", "label", "position", "radius", "origin", "window"):
            table.add_column(column)
        self._fill_table()
        self._show_feedback(
            "Fill in a place and press Add. Select a row (Enter or click) to change or "
            "remove it. An address is looked up online only without coordinates. "
            f"CSV header: {','.join(CASE_PLACES_CSV_HEADER)}",
            valid=True,
        )
        self.query_one("#case-place-label", Input).focus()

    def _csv_file_names(self) -> list[str]:
        try:
            candidates = sorted(self.input_directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return []
        return [
            path.name
            for path in candidates
            if path.is_file()
            and path.suffix.lower() == ".csv"
            and path.name not in self.excluded_names
        ]

    def _entered_fields(self) -> CasePlaceFields:
        return CasePlaceFields(
            **{
                field.name: self.query_one(f"#{INPUT_IDS[field.name]}", Input).value
                for field in fields(CasePlaceFields)
            }
        )

    def _set_fields(self, entry: CasePlaceFields) -> None:
        for field in fields(CasePlaceFields):
            self.query_one(f"#{INPUT_IDS[field.name]}", Input).value = getattr(entry, field.name)

    def _set_edited_row(self, row: int | None) -> None:
        self.edited_row = row
        self.query_one("#case-place-update", Button).disabled = row is None
        self.query_one("#case-place-remove", Button).disabled = row is None

    def _show_feedback(self, message: str, valid: bool) -> None:
        feedback = self.query_one("#case-places-feedback", Static)
        feedback.set_class(valid, "valid")
        feedback.update(message)
        self.query_one("#case-places-feedback-area", VerticalScroll).scroll_home(animate=False)

    def _fill_table(self) -> None:
        table = self.query_one("#case-places-table", DataTable)
        table.clear()
        for number, place in enumerate(self.places, start=1):
            window = place.window
            table.add_row(
                str(number),
                _plain(place.label),
                _plain(
                    f"{place.latitude:.5f}, {place.longitude:.5f}"
                    if place.latitude is not None and place.longitude is not None
                    else f"address: {place.address}"
                ),
                f"{place.radius_m:g} m",
                "entered" if place.file_row is None else f"file row {place.file_row}",
                _plain(
                    "-"
                    if window is None
                    else f"{display_local_text(window.from_utc, self.display_tz)} to "
                    f"{display_local_text(window.to_utc, self.display_tz)}",
                    max_chars=56,
                ),
            )
        self._show_detail(table.cursor_row)

    def _show_detail(self, row: int) -> None:
        """The highlighted place with every field in full; the table shortens long cells."""
        detail = self.query_one("#case-places-detail", Static)
        if not 0 <= row < len(self.places):
            detail.update("")
            return
        entry = case_place_fields(self.places[row], self.display_tz)
        parts = [
            f"{field_label}: {getattr(entry, field_name)}"
            for field_name, field_label, _ in FIELD_INPUTS
            if getattr(entry, field_name)
        ]
        detail.update(f"Row {row + 1} in full: " + " · ".join(parts))

    def _parsed_entry(self, replaced_row: int | None) -> CasePlace | None:
        """The inputs as a case place, or None after showing why they cannot be used."""
        try:
            place = parse_case_place(self._entered_fields(), 0, self.display_tz, self.zone_name)
        except CasePlaceInputError as error:
            self._show_feedback(f"Not usable: {error}", valid=False)
            return None
        for row, other in enumerate(self.places):
            if row != replaced_row and other.label == place.label:
                self._show_feedback(
                    f"Not usable: label '{place.label}' is already used in row {row + 1}",
                    valid=False,
                )
                return None
        return place

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if 0 <= row < len(self.places):
            self._set_fields(case_place_fields(self.places[row], self.display_tz))
            self._set_edited_row(row)
            self._show_feedback(
                f"Row {row + 1} is in the fields: press Update or Remove", valid=True
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "case-places-cancel":
            self.dismiss(None)
        elif button_id == "case-places-ok":
            self._confirm()
        elif button_id == "case-place-add":
            self._add()
        elif button_id == "case-place-update":
            self._update()
        elif button_id == "case-place-remove":
            self._remove()
        elif button_id == "case-places-load":
            self._load_file()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _add(self) -> None:
        if len(self.places) >= MAX_CASE_PLACES:
            self._show_feedback(f"No more than {MAX_CASE_PLACES} case places", False)
            return
        place = self._parsed_entry(replaced_row=None)
        if place is None:
            return
        self.places.append(place)
        self.entered_fields.append(self._entered_fields())
        self._after_change(f"Added '{place.label}'")

    def _update(self) -> None:
        row = self.edited_row
        if row is None:
            return
        place = self._parsed_entry(replaced_row=row)
        if place is None:
            return
        # An edited row is no longer what the file said.
        self.places[row] = place
        self.entered_fields[row] = self._entered_fields()
        self._after_change(f"Updated row {row + 1}")

    def _remove(self) -> None:
        row = self.edited_row
        if row is None:
            return
        removed = self.places.pop(row)
        del self.entered_fields[row]
        self._after_change(f"Removed '{removed.label}'")

    def _after_change(self, message: str) -> None:
        self._fill_table()
        self._set_fields(CasePlaceFields())
        self._set_edited_row(None)
        self._show_feedback(message, valid=True)

    def _load_file(self) -> None:
        chosen = self.query_one("#case-places-file", Select).value
        if not isinstance(chosen, str):
            self._show_feedback("Choose a CSV file from the input directory first", valid=False)
            return
        try:
            loaded = load_case_places_csv(
                self.input_directory / chosen, self.display_tz, self.zone_name
            )
        except CasePlaceInputError as error:
            self._show_feedback(str(error), valid=False)
            return
        replaced = len(self.places)
        self.places = list(loaded.places)
        self.entered_fields = list(loaded.entered_fields)
        self.loaded_file = loaded
        self._after_change(
            f"Loaded {len(loaded.places)} case place(s) from {loaded.name} "
            f"(SHA-256 {loaded.sha256[:16]}…)"
            + (f", replacing {replaced} earlier row(s)" if replaced else "")
        )

    def _confirm(self) -> None:
        pending = self._entered_fields()
        if any(getattr(pending, field.name).strip() for field in fields(CasePlaceFields)):
            self._show_feedback(
                "The fields hold an entry that is not in the table. Press Add or Update, or "
                "clear the fields, then OK.",
                valid=False,
            )
            return
        numbered = tuple(
            replace(place, identifier=number) for number, place in enumerate(self.places, start=1)
        )
        # A loaded file stays on record; rows changed afterwards no longer name a file row.
        self.dismiss(
            CasePlacesEntry(
                numbered,
                self.loaded_file if numbered else None,
                tuple(self.entered_fields),
                self.zone_name,
            )
        )


def _plain(text: str, max_chars: int = TABLE_CELL_MAX_CHARS) -> Text:
    """Cell text shortened and shown verbatim (never read as markup)."""
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return Text(text)
