# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Dialog: assign the columns of a CSV source to roles, choose time format and zone."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from geosnap.extraction.coordinate_text import LAT_LON, LON_LAT, POSITION_ORDER_LABELS
from geosnap.extraction.csv_reader import (
    AUTOMATIC_TIME_FORMAT,
    COLUMN_ROLES,
    DATE_ORDER_FIELDS,
    TIME_FORMAT_LABELS,
    CsvMapping,
    CsvPreview,
    MappingSuggestion,
    canonical_zone_name,
    check_preview_rows,
    column_labels,
    mapping_problems,
    parse_timestamp,
    preview_problem,
    read_csv_preview,
    scaled_exponent,
    suggest_mapping_with_reasons,
    unconfirmed_assumptions,
)
from geosnap.extraction.timestamp_text import DATE_ORDER_LABELS, date_order_evidence
from geosnap.tui.widgets import ZoneField

PREVIEW_CELL_MAX_CHARS = 24
ROLE_MARK = " ↦ "
# Role, dialog label. Position and its order, and the end roles of a time span, are
# shown only when they apply (see CsvMappingScreen._show_applicable_fields).
ROLE_LABELS = (
    ("latitude", "Latitude"),
    ("longitude", "Longitude"),
    ("position", "Position (lat, lon)"),
    ("timestamp", "Date / timestamp"),
    ("time_of_day", "Time of day"),
    ("timestamp_end", "End date / timestamp"),
    ("time_of_day_end", "End time of day"),
    ("accuracy", "Accuracy (m)"),
    ("label", "Label"),
    ("note", "Note"),
    ("positioning_method", "Positioning method"),
)
TIME_SPAN_ROLES = ("timestamp_end", "time_of_day_end")
DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
# Two zones with different offsets: a time that reads the same in both carries its own.
ZONE_PROBES = ("UTC", "Etc/GMT-12")
# Where the suggested zone came from; recorded with the source in metadata.json.
ZONE_ORIGIN_EXAMINER = "examiner"
ZONE_ORIGIN_DISPLAY_SETTING = "display_setting"
ZONE_ORIGIN_HOST_SYSTEM = "host_system"
ZONE_ORIGIN_HEADER = "header"
ZONE_ORIGIN_NOTES = {
    ZONE_ORIGIN_HOST_SYSTEM: (
        "{zone}, suggested from the host system clock. Check it: the evidence device may use "
        "another zone."
    ),
    ZONE_ORIGIN_DISPLAY_SETTING: (
        "{zone}, the display zone in config.toml. Check it: the file may use another zone."
    ),
    ZONE_ORIGIN_HEADER: "{zone}, named in the header of the time column",
    ZONE_ORIGIN_EXAMINER: "{zone} chosen here",
}


# Reasons of the suggestion shown in the dialog: the roles and the time format; the zone
# has its own note under its field and the delimiter its column count in the select.
EXPLAINED_SUGGESTION_KEYS = (*COLUMN_ROLES, "time_format")


def suggest_columns(preview: CsvPreview, default_zone: str) -> MappingSuggestion:
    """The reader's suggestion for the preview with the evidence the dialog explains."""
    return suggest_mapping_with_reasons(preview, default_zone)


def date_marks_in_use(preview: CsvPreview, mapping: CsvMapping) -> frozenset[str]:
    """Separator marks of the sampled dates in the mapping's date columns whose
    day/month order matters under the automatic format (none for a fixed format, which
    settles the order itself); follows the columns as the examiner changes them."""
    if mapping.time_format != AUTOMATIC_TIME_FORMAT:
        return frozenset()
    marks: set[str] = set()
    for role in ("timestamp", "timestamp_end"):
        column = mapping_role(mapping, role)
        if column:
            marks.update(date_order_evidence(preview.column_samples(column)))
    return frozenset(marks)


def zone_assumption(zone: str, zone_origin: str) -> str | None:
    """What OK confirms about a zone the examiner did not choose, or None."""
    if not zone or zone_origin not in (ZONE_ORIGIN_HOST_SYSTEM, ZONE_ORIGIN_DISPLAY_SETTING):
        return None
    source = (
        "the host system clock"
        if zone_origin == ZONE_ORIGIN_HOST_SYSTEM
        else "the display zone in config.toml"
    )
    return f"time zone {zone} was taken from {source}; the evidence device may use another zone"


def zone_applies_to_samples(preview: CsvPreview, mapping: CsvMapping) -> bool:
    """False only when every sampled time of the mapping's time columns reads the same
    instant whatever the zone (offset carried, or a plain number): the zone never
    applies. Unreadable or missing samples count as "may apply"."""
    if not mapping.timestamp:
        return False  # no time column: the rows are read undated, the zone never applies
    if mapping.time_format not in TIME_FORMAT_LABELS:
        return True
    if mapping.time_of_day or mapping_role(mapping, "time_of_day_end"):
        return True  # a bare date with a separate time of day never carries an offset
    for role in ("timestamp", "timestamp_end"):
        column = mapping_role(mapping, role)
        samples = preview.column_samples(column) if column else []
        if column and not samples:
            return True
        for sample in samples:
            readings = [
                parse_timestamp(sample, mapping.time_format, probe_zone)
                for probe_zone in ZONE_PROBES
            ]
            if any(isinstance(reading, str) for reading in readings) or readings[0] != readings[1]:
                return True
    return False


def csv_assumptions(preview: CsvPreview, mapping: CsvMapping, zone_origin: str) -> list[str]:
    """Everything the mapping takes for granted, the origin of its zone included (only
    where the zone applies to the sampled times)."""
    assumptions = unconfirmed_assumptions(preview, mapping)
    zone_note = zone_assumption(mapping.zone, zone_origin)
    if zone_note and zone_applies_to_samples(preview, mapping):
        return [*assumptions, zone_note]
    return assumptions


def csv_mapping_problem(preview: CsvPreview, mapping: CsvMapping) -> str | None:
    """The first reason the mapping cannot be used, or None: structural problems first,
    then a preview in which no row can be read."""
    problems = mapping_problems(preview.header, mapping)
    if problems:
        return problems[0]
    return preview_problem(preview, mapping)


def describe_csv_mapping(mapping: CsvMapping) -> str:
    """One line for the source row, such as "lat=Breite, lon=Länge, time=Zeit (ISO 8601)"."""
    if mapping.latitude and mapping.longitude:
        columns = [
            f"{name}={column}" + (f" (E{exponent} integers)" if exponent else "")
            for name, column in (("lat", mapping.latitude), ("lon", mapping.longitude))
            for exponent in (scaled_exponent(column),)
        ]
    elif mapping.position:
        order = "lon, lat" if mapping.position_order == LON_LAT else "lat, lon"
        columns = [f"position={mapping.position} ({order})"]
    else:
        columns = [f"lat={mapping.latitude or '?'}", f"lon={mapping.longitude or '?'}"]
    time_format = TIME_FORMAT_LABELS.get(mapping.time_format, "no time format")
    zone = f", zone {mapping.zone}" if mapping.zone else ""
    date_orders = ", ".join(
        f"'{mark}' {date_order.replace('_', ' ')}"
        for mark, date_order in mapping.date_orders.items()
    )
    if date_orders:
        zone = f", {date_orders}{zone}"
    if mapping.timestamp:
        time_columns = " + ".join(filter(None, (mapping.timestamp, mapping.time_of_day)))
        columns.append(f"time={time_columns} ({time_format}{zone})")
    else:
        columns.append("time=none (records without timestamps)")
    end_columns = " + ".join(
        filter(None, (mapping_role(mapping, role) for role in TIME_SPAN_ROLES))
    )
    if end_columns:
        columns.append(f"until={end_columns}")
    columns.extend(
        f"{name}={getattr(mapping, role)}"
        for role, name in (
            ("accuracy", "accuracy"),
            ("label", "label"),
            ("note", "note"),
            ("positioning_method", "method"),
        )
        if getattr(mapping, role)
    )
    return ", ".join(columns)


def mapping_role(mapping: CsvMapping, role: str) -> str:
    """The column of a role, "" for a role this mapping does not know."""
    return str(getattr(mapping, role, ""))


def dialog_roles() -> tuple[tuple[str, str], ...]:
    """The roles the dialog offers: those the reader knows, in dialog order."""
    return tuple((role, label) for role, label in ROLE_LABELS if role in COLUMN_ROLES)


def delimiter_options(delimiter_counts: dict[str, int], detected: str) -> list[tuple[str, str]]:
    """Select options per delimiter with its column count, the sniffed one marked."""
    options = []
    for delimiter, name in DELIMITER_NAMES.items():
        notes = []
        if delimiter in delimiter_counts:
            count = delimiter_counts[delimiter]
            notes.append(f"{count} column{'s' if count != 1 else ''}")
        if delimiter == detected:
            notes.append("detected")
        options.append((f"{name} ({', '.join(notes)})" if notes else name, delimiter))
    return options


class CsvMappingScreen(ModalScreen[CsvMapping | None]):
    """Preview of the first rows with their roles, one select per role, time format, zone
    with its origin, day/month orders for the marks in use, delimiter, and a live check of
    the preview rows. OK returns the mapping only when it is usable."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        path: Path,
        fallback_encoding: str,
        preview: CsvPreview,
        mapping: CsvMapping,
        suggestion: MappingSuggestion | None = None,
        zone_origin: str = ZONE_ORIGIN_EXAMINER,
    ) -> None:
        super().__init__()
        self.path = path
        self.fallback_encoding = fallback_encoding
        self.preview = preview
        self.initial_mapping = mapping
        self.suggestion = suggestion or suggest_columns(preview, mapping.zone)
        # The suggestion was made for the sniffed delimiter; the preview may already be
        # read with one the examiner chose earlier, whose columns need their own reasons.
        self.detected_delimiter = self.suggestion.mapping.delimiter
        if preview.delimiter != self.detected_delimiter:
            self.suggestion = suggest_columns(preview, default_zone="")
        self.initial_zone_origin = zone_origin

    def compose(self) -> ComposeResult:
        # The footer is a sibling after the scrolling body, not docked inside it: the
        # focus order follows the tree, so Tab reaches OK only after every field.
        with Vertical(id="csv-mapping-dialog"):
            with VerticalScroll(id="csv-mapping-body"):
                yield Label(
                    f"Columns of {self.path.name} (encoding {self.preview.encoding}); first rows:",
                    id="csv-mapping-title",
                    markup=False,
                )
                yield DataTable(id="csv-preview", cursor_type="row", zebra_stripes=True)
                yield Static("", id="csv-preview-detail", markup=False)
                # One grid per group: a hidden field (position order, an unused day/month
                # mark) shifts only the cells of its own group, never a spanning field.
                with Grid(id="csv-position-grid", classes="csv-role-grid"):
                    yield from self._compose_role_selects(("latitude", "longitude", "position"))
                    yield Label(
                        "Position order:", classes="csv-role-label", id="csv-label-position-order"
                    )
                    yield Select(
                        [(label, key) for key, label in POSITION_ORDER_LABELS.items()],
                        value=self.initial_mapping.position_order
                        if self.initial_mapping.position_order in POSITION_ORDER_LABELS
                        else LAT_LON,
                        allow_blank=False,
                        id="csv-position-order",
                        classes="csv-role-select",
                    )
                with Grid(id="csv-time-grid", classes="csv-role-grid"):
                    yield from self._compose_role_selects(("timestamp", "time_of_day"))
                    if all(role in COLUMN_ROLES for role in TIME_SPAN_ROLES):
                        yield Label(
                            "Time span (optional): end of each record",
                            id="csv-time-span-title",
                            classes="csv-role-group",
                        )
                        yield from self._compose_role_selects(TIME_SPAN_ROLES)
                    yield from self._compose_time_fields()
                with Grid(id="csv-other-grid", classes="csv-role-grid"):
                    yield Label("Delimiter:", classes="csv-role-label")
                    yield Select(
                        delimiter_options(
                            self.suggestion.delimiter_counts, self.detected_delimiter
                        ),
                        value=self.preview.delimiter,
                        allow_blank=False,
                        id="csv-delimiter",
                        classes="csv-role-select",
                    )
                    yield from self._compose_role_selects(
                        ("accuracy", "label", "note", "positioning_method")
                    )
                # Below the fields, where it does not get in the way.
                yield Static("", id="csv-reasons", markup=False)
            # Below the scrolling body: the check and the buttons stay in view.
            with Vertical(id="csv-mapping-footer"):
                with VerticalScroll(id="csv-check-area"):
                    yield Static("", id="csv-check", markup=False)
                with Horizontal(id="csv-mapping-buttons"):
                    yield Button("OK", variant="primary", id="csv-mapping-ok")
                    yield Button("Cancel", id="csv-mapping-cancel")

    def _compose_role_selects(self, roles: tuple[str, ...]) -> ComposeResult:
        labels = column_labels(self.preview.header)
        role_labels = dict(dialog_roles())
        for role in roles:
            if role not in role_labels:
                continue
            yield Label(f"{role_labels[role]}:", classes="csv-role-label", id=f"csv-label-{role}")
            current = mapping_role(self.initial_mapping, role)
            yield Select(
                _column_options(labels),
                prompt="(unused)",
                value=current if current in labels else Select.NULL,
                id=f"csv-role-{role}",
                classes="csv-role-select",
            )

    def _compose_time_fields(self) -> ComposeResult:
        yield Label("Time format:", classes="csv-role-label")
        yield Select(
            [(label, key) for key, label in TIME_FORMAT_LABELS.items()],
            prompt="(choose)",
            value=self.initial_mapping.time_format
            if self.initial_mapping.time_format in TIME_FORMAT_LABELS
            else Select.NULL,
            id="csv-time-format",
            classes="csv-role-select",
        )
        yield Label("Time zone:", classes="csv-role-label")
        yield ZoneField(self.initial_mapping.zone, id="csv-zone", classes="csv-role-select")
        yield Static("", id="csv-zone-origin", classes="csv-role-note", markup=False)
        for mark, field_name in DATE_ORDER_FIELDS.items():
            yield Label(
                f"Day/month with '{mark}':",
                classes="csv-role-label",
                id=f"csv-label-{field_name.replace('_', '-')}",
            )
            current_order = getattr(self.initial_mapping, field_name)
            yield Select(
                [(label, key) for key, label in DATE_ORDER_LABELS.items()],
                prompt="(not chosen)",
                value=current_order if current_order in DATE_ORDER_LABELS else Select.NULL,
                id=f"csv-{field_name.replace('_', '-')}",
                classes="csv-role-select",
            )

    def on_mount(self) -> None:
        self._fill_preview_table()
        self._show_reasons()
        self._show_applicable_fields()
        self._show_check()

    def current_mapping(self) -> CsvMapping:
        chosen: dict[str, str] = {}
        for role, _ in dialog_roles():
            value = self.query_one(f"#csv-role-{role}", Select).value
            chosen[role] = value if isinstance(value, str) else ""
        time_format = self.query_one("#csv-time-format", Select).value
        date_orders = {}
        for field_name in DATE_ORDER_FIELDS.values():
            date_order = self.query_one(f"#csv-{field_name.replace('_', '-')}", Select).value
            date_orders[field_name] = date_order if isinstance(date_order, str) else ""
        position_order = self.query_one("#csv-position-order", Select).value
        zone_field = self.query_one("#csv-zone", ZoneField)
        zone_entry = zone_field.value.strip()
        return CsvMapping(
            delimiter=self.preview.delimiter,
            time_format=time_format if isinstance(time_format, str) else "",
            position_order=position_order if isinstance(position_order, str) else LAT_LON,
            **date_orders,
            zone=zone_field.zone_name or canonical_zone_name(zone_entry) or zone_entry,
            **chosen,
        )

    def zone_origin(self) -> str:
        """Where the zone in the current mapping came from: the origin of the suggestion
        while it is unchanged, the examiner once it differs."""
        if self.current_mapping().zone == self.initial_mapping.zone:
            return self.initial_zone_origin
        return ZONE_ORIGIN_EXAMINER

    def on_select_changed(self, event: Select.Changed) -> None:
        if (
            event.select.id == "csv-delimiter"
            and isinstance(event.value, str)
            and event.value != self.preview.delimiter
        ):
            self._reload_preview(event.value)
        elif event.select.id != "csv-delimiter":
            self._fill_preview_table()
        self._show_applicable_fields()
        self._show_check()

    def on_input_changed(self, _: Input.Changed) -> None:
        self._show_check()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_preview_detail(event.cursor_row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "csv-mapping-cancel":
            self.dismiss(None)
            return
        mapping = self.current_mapping()
        if csv_mapping_problem(self.preview, mapping) is None:
            self.dismiss(mapping)
        else:
            self._show_check()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _reload_preview(self, delimiter: str) -> None:
        """Read the preview again with the chosen delimiter; roles whose column label
        still exists keep it, the others take the suggestion for the new columns."""
        try:
            self.preview = read_csv_preview(self.path, self.fallback_encoding, delimiter=delimiter)
        except OSError as error:
            self.query_one("#csv-check", Static).update(f"Cannot read the file: {error}")
            return
        labels = column_labels(self.preview.header)
        self.suggestion = suggest_columns(self.preview, default_zone="")
        suggestion = self.suggestion.mapping
        for role, _ in dialog_roles():
            role_select = self.query_one(f"#csv-role-{role}", Select)
            kept_value = role_select.value
            role_select.set_options(_column_options(labels))
            if isinstance(kept_value, str) and kept_value in labels:
                role_select.value = kept_value
            elif mapping_role(suggestion, role):
                role_select.value = mapping_role(suggestion, role)
        time_format_select = self.query_one("#csv-time-format", Select)
        if not isinstance(time_format_select.value, str) and suggestion.time_format:
            time_format_select.value = suggestion.time_format
        # New rows: the day/month orders follow their evidence again.
        for field_name in DATE_ORDER_FIELDS.values():
            date_order_select = self.query_one(f"#csv-{field_name.replace('_', '-')}", Select)
            suggested_order = getattr(suggestion, field_name)
            if suggested_order:
                date_order_select.value = suggested_order
            else:
                date_order_select.clear()
        if suggestion.position:
            self.query_one("#csv-position-order", Select).value = suggestion.position_order
        self._fill_preview_table()
        self._show_reasons()

    def _fill_preview_table(self) -> None:
        """The first rows; a column chosen for a role carries the role in its heading."""
        table = self.query_one("#csv-preview", DataTable)
        highlighted_row = table.cursor_row if table.row_count else 0
        table.clear(columns=True)
        table.add_column("line")
        roles_by_column = self._roles_by_column()
        for label in column_labels(self.preview.header):
            heading = _plain(label)
            if label in roles_by_column:
                heading.append(f"{ROLE_MARK}{roles_by_column[label]}")
            table.add_column(heading)
        for row in self.preview.rows:
            cells = [_plain(cell) for cell in row.cells[: len(self.preview.header)]]
            cells += [Text("")] * (len(self.preview.header) - len(cells))
            table.add_row(str(row.line_number), *cells)
        self._show_preview_detail(highlighted_row)

    def _roles_by_column(self) -> dict[str, str]:
        """Column label -> role name, or "role, role" for a label chosen twice."""
        roles: dict[str, list[str]] = {}
        for role, _ in dialog_roles():
            value = self.query_one(f"#csv-role-{role}", Select).value
            column = value if isinstance(value, str) else ""
            if column:
                roles.setdefault(column, []).append(role.replace("_", " "))
        return {column: ", ".join(names) for column, names in roles.items()}

    def _show_preview_detail(self, row_index: int) -> None:
        """The highlighted preview row with every cell in full; the table shortens cells."""
        detail = self.query_one("#csv-preview-detail", Static)
        if not 0 <= row_index < len(self.preview.rows):
            detail.update("")
            return
        row = self.preview.rows[row_index]
        cells = " · ".join(
            f"{label}={cell}"
            for label, cell in zip(column_labels(self.preview.header), row.cells, strict=False)
        )
        detail.update(f"Line {row.line_number} in full (arrow keys choose the line): {cells}")

    def _show_reasons(self) -> None:
        """Why the suggestion chose its columns, and which pairs it left unused."""
        reasons = self.suggestion.reasons
        lines = [
            f"{key.replace('_', ' ')}: {reasons[key]}"
            for key in EXPLAINED_SUGGESTION_KEYS
            if key in reasons
        ]
        lines.extend(
            f"{first} and {second} look like projected coordinates without a zone; not used"
            for first, second in self.suggestion.projected_pairs
        )
        reasons_block = self.query_one("#csv-reasons", Static)
        reasons_block.update("\n".join(["Why the suggestion chose these columns:", *lines]))
        reasons_block.display = bool(lines)

    def _show_applicable_fields(self) -> None:
        """Hide what does not apply: the position order without a position column, the
        day/month orders for marks the sampled dates do not use."""
        mapping = self.current_mapping()
        self._set_field_display("position-order", bool(mapping.position))
        marks_in_use = date_marks_in_use(self.preview, mapping)
        for mark, field_name in DATE_ORDER_FIELDS.items():
            self._set_field_display(field_name.replace("_", "-"), mark in marks_in_use)
        self._show_zone_origin(mapping)

    def _set_field_display(self, field_id: str, shown: bool) -> None:
        for widget in (
            self.query_one(f"#csv-label-{field_id}"),
            self.query_one(f"#csv-{field_id}"),
        ):
            widget.display = shown

    def on_filtered_choice_field_choices_toggled(self, _: ZoneField.ChoicesToggled) -> None:
        self._show_zone_origin(self.current_mapping())

    def _show_zone_origin(self, mapping: CsvMapping) -> None:
        origin_note = self.query_one("#csv-zone-origin", Static)
        if self.query_one("#csv-zone", ZoneField).choices_open:
            origin_note.update("")  # the open list lies over this line
        elif not mapping.zone:
            origin_note.update("applies to times without offset; type part of a name or country")
        elif self.query_one("#csv-zone", ZoneField).zone_name is None:
            origin_note.update(f"'{mapping.zone}' is not a zone: pick one from the list")
        else:
            note = ZONE_ORIGIN_NOTES[self.zone_origin()].format(zone=mapping.zone)
            if not zone_applies_to_samples(self.preview, mapping):
                note += " (not used: every sampled time carries its offset)"
            origin_note.update(note)

    def _show_check(self) -> None:
        mapping = self.current_mapping()
        self._show_zone_origin(mapping)
        problems = mapping_problems(self.preview.header, mapping)
        check = self.query_one("#csv-check", Static)
        if problems:
            check.remove_class("valid")
            check.update("Not usable yet: " + "; ".join(problems))
            return
        lines = check_preview_rows(self.preview, mapping)
        problem = csv_mapping_problem(self.preview, mapping)
        check.set_class(problem is None, "valid")
        heading = "Check of the first rows:" if problem is None else f"Not usable: {problem}"
        assumed = [
            f"Assumed, confirmed by OK: {assumption}"
            for assumption in csv_assumptions(self.preview, mapping, self.zone_origin())
        ]
        check.update("\n".join([*assumed, heading, *lines]))


def _column_options(labels: list[str]) -> list[tuple[Text, str]]:
    return [(Text(label), label) for label in labels]


def _plain(text: str) -> Text:
    """Cell text shortened and shown verbatim (never read as markup)."""
    if len(text) > PREVIEW_CELL_MAX_CHARS:
        text = text[: PREVIEW_CELL_MAX_CHARS - 1] + "…"
    return Text(text)
