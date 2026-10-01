# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Second screen: name the project, describe each source and confirm the run options."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Input, Label, Select, Static, Switch

from geosnap.analysis.accuracy import ACCURACY_LEVEL_TEXTS
from geosnap.extraction.csv_reader import (
    CsvMapping,
    CsvPreview,
    MappingSuggestion,
    read_csv_preview,
)
from geosnap.extraction.geojson_reader import UNRECOGNISED_JSON_REASON
from geosnap.extraction.models import SOURCE_KINDS, UNRECOGNISED_FORMAT, SourceDescriptor
from geosnap.mapping.tile_providers import tile_provider_by_key
from geosnap.project.case_place_input import CasePlaceInputError, reread_case_places
from geosnap.project.pipeline import (
    DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING,
    DISPLAY_ZONE_ORIGIN_EXAMINER,
    DISPLAY_ZONE_ORIGIN_HOST_SYSTEM,
    DISPLAY_ZONE_ORIGIN_HOST_SYSTEM_CONFIRMED,
    SOURCE_COLOUR_PALETTE,
    ProjectRequest,
    source_format_for,
)
from geosnap.project.workspace import (
    PROJECT_NAME_MAX_LENGTH,
    PROJECT_NAME_MIN_LENGTH,
    ProjectNameError,
    validate_project_name,
)
from geosnap.settings import MAX_MATRIX_TOLERANCE_M, AnalysisSettings, MapSettings, Settings
from geosnap.timezones import LOCAL_ZONE_SETTING, host_zone_name, resolve_display_timezone
from geosnap.tui.screens.case_places import (
    CasePlacesEntry,
    CasePlacesScreen,
    describe_case_places,
)
from geosnap.tui.screens.csv_mapping import (
    ZONE_ORIGIN_DISPLAY_SETTING,
    ZONE_ORIGIN_EXAMINER,
    ZONE_ORIGIN_HEADER,
    ZONE_ORIGIN_HOST_SYSTEM,
    CsvMappingScreen,
    csv_assumptions,
    csv_mapping_problem,
    describe_csv_mapping,
    suggest_columns,
)
from geosnap.tui.widgets import (
    CONTENT_SCROLL_BINDINGS,
    ColourField,
    ContentArea,
    ContentScreen,
    FilteredChoiceField,
    StatusHeader,
    ZoneField,
)

SOURCE_LABEL_MAX_LENGTH = 32
CASE_FIELD_MAX_LENGTH = 80
SOURCE_COLOUR_NAMES = ("blue", "orange", "green", "pink", "yellow", "light blue", "purple", "brown")
SOURCE_NAMED_COLOURS = tuple(zip(SOURCE_COLOUR_NAMES, SOURCE_COLOUR_PALETTE, strict=True))
# (shown text, SourceDescriptor.accuracy_level); the first is the default.
# The level wording is the one the report and the map use.
ACCURACY_LEVEL_OPTIONS = tuple(
    (ACCURACY_LEVEL_TEXTS[level], level) for level in ("unknown", "68", "95")
)


def describe_tile_source(map_settings: MapSettings, online_enabled: bool) -> str:
    if map_settings.tile_source == "online":
        if not online_enabled:
            return "none (online services are off)"
        provider = tile_provider_by_key(map_settings.tile_provider)
        return (
            f"online tiles from {provider.label} (the browser contacts "
            f"{', '.join(provider.origins)}; the map offers other styles)"
        )
    if map_settings.tile_source == "local":
        return (
            f"local tiles from '{map_settings.local_tiles_path}' "
            "(no base map if the directory is missing)"
        )
    return "none (offline, no base map)"


def describe_online_services(settings: Settings) -> str:
    online = settings.online
    if not online.enabled:
        return "off (no online tiles, Overpass or Nominatim)"
    parts = []
    if settings.map.tile_source == "online":
        parts.append(f"{tile_provider_by_key(settings.map.tile_provider).label} tiles")
    if online.overpass_categories:
        parts.append(f"Overpass places within {online.overpass_radius_m} m")
    if online.nominatim_enabled and online.nominatim_max_lookups > 0:
        parts.append(f"Nominatim addresses (max {online.nominatim_max_lookups})")
    elif online.nominatim_enabled:
        # Addresses of case places entered without coordinates are searched regardless.
        parts.append("Nominatim (case place address searches only)")
    return "on: " + ", ".join(parts) if parts else "on (nothing configured)"


def describe_accuracy_settings(analysis: AnalysisSettings) -> str:
    if analysis.accuracy_confidence == "reported":
        confidence = "as reported"
    else:
        confidence = "95 % (unknown levels treated as 68 %)"
    methods = analysis.excluded_positioning_methods
    if methods:
        left_out = f"{' and '.join(methods)} records left out of speed, stays and encounters"
    else:
        left_out = "all positioning methods analysed"
    return f"Accuracy: {confidence} · {left_out}"


def default_source_label(path: Path) -> str:
    return path.stem[:SOURCE_LABEL_MAX_LENGTH] or "source"


def default_source_colour_name(position: int) -> str:
    """Palette colour name for the source at the given 0-based position, cycling when exhausted."""
    return SOURCE_COLOUR_NAMES[position % len(SOURCE_COLOUR_NAMES)]


class CaseFieldError(ValueError):
    """A case reference or examiner entry is too long or holds non-printable characters."""


def validate_case_field(raw_value: str, field_name: str) -> str:
    """Trimmed entry: empty, or 1-80 printable characters."""
    value = raw_value.strip()
    if len(value) > CASE_FIELD_MAX_LENGTH:
        raise CaseFieldError(
            f"{field_name} must not exceed {CASE_FIELD_MAX_LENGTH} characters "
            f"({len(value)} characters)"
        )
    if not value.isprintable():
        raise CaseFieldError(f"{field_name} may only hold printable characters")
    return value


def default_csv_zone(settings: Settings) -> tuple[str, str]:
    """(zone, origin) suggested for naive CSV times: the display zone of config.toml when
    it names one, else the host system's zone, else nothing (the examiner enters one)."""
    display = settings.timezone.display
    if display != LOCAL_ZONE_SETTING:
        return display, ZONE_ORIGIN_DISPLAY_SETTING
    host_zone = host_zone_name()
    if host_zone:
        return host_zone, ZONE_ORIGIN_HOST_SYSTEM
    return "", ZONE_ORIGIN_EXAMINER


def default_display_zone(settings: Settings) -> tuple[str, str, str]:
    """(zone, origin, note) for the display zone field: config.toml's zone, or the host
    system's when config.toml says "local"; "" when neither names one (the system zone is
    used, unnamed)."""
    display = settings.timezone.display
    if display != LOCAL_ZONE_SETTING:
        return display, DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING, "from config.toml"
    host_zone = host_zone_name()
    if host_zone:
        return (
            host_zone,
            DISPLAY_ZONE_ORIGIN_HOST_SYSTEM,
            'suggested from the host system clock (config.toml says "local")',
        )
    return (
        "",
        DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING,
        'not named: config.toml says "local" and the host zone is unknown',
    )


class MatrixToleranceError(ValueError):
    """The matrix tolerance entry is not a whole number of metres within its bounds."""


def validate_matrix_tolerance(raw_value: str) -> int:
    """The tolerance for this run: a whole number of metres from 0 to MAX_MATRIX_TOLERANCE_M."""
    entry = raw_value.strip()
    if not entry.isdecimal() or not 0 <= int(entry) <= MAX_MATRIX_TOLERANCE_M:
        raise MatrixToleranceError(
            f"Matrix tolerance must be a whole number of metres from 0 to {MAX_MATRIX_TOLERANCE_M}"
        )
    return int(entry)


class DisplayZoneError(ValueError):
    """The display zone entry is not a zone from the list, or a host zone that would read
    GeoJSON times without offset has not been confirmed."""


def validate_display_zone(field: ZoneField) -> str:
    """The canonical zone name of the entry, "" for an empty entry."""
    if not field.value.strip():
        return ""
    zone_name = field.zone_name
    if zone_name is None:
        raise DisplayZoneError("Display time zone must be a zone from the list (or empty)")
    return zone_name


class CasePlacesZoneError(ValueError):
    """Case place windows entered in one display zone do not fit the zone of the run."""


class SourceLabelError(ValueError):
    """A source label is empty, too long or clashes with another source."""


class SourceColourError(ValueError):
    """A source colour is neither a palette name nor a hex value."""


class SourceFormatSelectionError(ValueError):
    """A source cannot be read as selected: unrecognised JSON or no usable CSV mapping."""


def validate_source_labels(raw_labels: list[str]) -> list[str]:
    """Trimmed labels, 1-32 characters each and unique; names the first offending row."""
    labels: list[str] = []
    for position, raw_label in enumerate(raw_labels, start=1):
        label = raw_label.strip()
        if not label:
            raise SourceLabelError(
                f"Source {position} needs a name (1-{SOURCE_LABEL_MAX_LENGTH} characters)"
            )
        if len(label) > SOURCE_LABEL_MAX_LENGTH:
            raise SourceLabelError(
                f"Source {position}: name must not exceed {SOURCE_LABEL_MAX_LENGTH} characters "
                f"({len(label)} characters)"
            )
        if label in labels:
            raise SourceLabelError(f"Source {position}: name '{label}' is already used")
        labels.append(label)
    return labels


class ProjectSetupScreen(ContentScreen[ProjectRequest | None]):
    # on_mount focuses the project name; the automatic focus would scroll the content
    # area before it has a size and hide the heading.
    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("enter", "start", "Start", priority=True),
        Binding("escape", "go_back", "Back"),
        *CONTENT_SCROLL_BINDINGS,
    ]

    def __init__(self, source_paths: list[Path], settings: Settings) -> None:
        super().__init__()
        if not source_paths:
            raise ValueError("A project needs at least one source file")
        self.source_paths = list(source_paths)
        self.settings = settings
        self.source_formats = [source_format_for(path) for path in self.source_paths]
        # Per CSV row number: the preview (or why it cannot be read), the mapping, the
        # suggestion it started from and where its zone came from.
        self.csv_previews: dict[int, CsvPreview | str] = {}
        self.csv_mappings: dict[int, CsvMapping | None] = {}
        self.csv_suggestions: dict[int, MappingSuggestion] = {}
        self.csv_zone_origins: dict[int, str] = {}
        self.suggested_display_zone, self.suggested_display_zone_origin, self.display_zone_note = (
            default_display_zone(settings)
        )
        # Rows whose mapping the examiner has accepted in the Columns… dialog.
        self.confirmed_csv_rows: set[int] = set()
        for row_number, (path, source_format) in enumerate(
            zip(self.source_paths, self.source_formats, strict=True), start=1
        ):
            if source_format == "csv":
                self._load_csv_preview(row_number, path)
        self.case_places_entry = CasePlacesEntry()

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            yield Label("Project setup", classes="heading")
            yield Label(
                "Tab moves between fields, Enter starts processing, Escape goes back to the "
                "file list.",
                id="hint",
                classes="instructions",
            )
            yield Label(
                f"Project name ({PROJECT_NAME_MIN_LENGTH}-{PROJECT_NAME_MAX_LENGTH} characters "
                "from A-Z a-z 0-9 _ -):"
            )
            yield Input(placeholder="e.g. Case_2026_09", id="project-name")
            with Container(id="case-row"):
                with Horizontal(classes="case-field"):
                    yield Label("Case reference:", classes="case-label")
                    yield Input(
                        placeholder="optional",
                        max_length=CASE_FIELD_MAX_LENGTH,
                        id="case-reference",
                        classes="case-input",
                    )
                with Horizontal(classes="case-field"):
                    yield Label("Examiner:", classes="case-label")
                    yield Input(
                        placeholder="optional",
                        max_length=CASE_FIELD_MAX_LENGTH,
                        id="examiner",
                        classes="case-input",
                    )
            with Container(id="options-row"):
                with Horizontal(id="duplicates-row"):
                    yield Label("Remove duplicates from the map (Space toggles): ")
                    yield Switch(
                        value=self.settings.extraction.remove_duplicates, id="remove-duplicates"
                    )
                with Horizontal(id="case-places-row"):
                    yield Button("Case places…", id="case-places-button")
                    yield Static(
                        describe_case_places((), self.settings),
                        id="case-places-summary",
                        markup=False,
                    )
            yield Label(
                f"Sources ({len(self.source_paths)}): file · format, then name · type · colour, "
                "accuracy level",
                id="sources-title",
                classes="heading",
            )
            with Vertical(id="source-rows"):
                for position, path in enumerate(self.source_paths):
                    yield self._source_block(position, path)
            yield Label("Run settings (config.toml)", id="settings-title", classes="heading")
            with Horizontal(id="display-zone-row"):
                yield Label("Display time zone:", classes="case-label")
                yield ZoneField(self.suggested_display_zone, id="display-zone")
            yield Label(
                f"Display zone {self.display_zone_note}. Times are shown in it, and GeoJSON "
                "times without offset are read in it. Applies to this run only.",
                id="display-zone-note",
                classes="instructions",
                markup=False,
            )
            # Shown only while a GeoJSON source would take a zone that merely comes from
            # the host clock: the examiner confirms it here, like a CSV assumption.
            with Horizontal(id="display-zone-confirm-row"):
                yield Label("", id="display-zone-confirm-label", markup=False)
                yield Switch(value=False, id="display-zone-confirmed")
            base_map_description = describe_tile_source(
                self.settings.map, self.settings.online.enabled
            )
            yield Label(f"Base map: {base_map_description}", markup=False)
            analysis = self.settings.analysis
            yield Label(
                f"Analysis: stays within {analysis.stop_radius_m} m for "
                f"{analysis.stop_min_minutes}+ min, gaps from {analysis.gap_min_minutes} min, "
                f"accuracy limit {analysis.max_accuracy_m} m, "
                f"implausible above {analysis.implausible_speed_kmh} km/h",
                markup=False,
            )
            yield Label(describe_accuracy_settings(analysis), id="accuracy-settings", markup=False)
            with Horizontal(id="matrix-tolerance-row"):
                yield Label("Matrix tolerance (m):", classes="case-label")
                yield Input(
                    str(analysis.matrix_tolerance_m),
                    placeholder=f"0-{MAX_MATRIX_TOLERANCE_M}",
                    max_length=len(str(MAX_MATRIX_TOLERANCE_M)) + 2,
                    id="matrix-tolerance",
                )
            yield Label(
                "Stays whose centres lie within this distance share one row of the presence "
                "matrix. The report and CSV use this value; the map can try others.",
                classes="instructions",
                markup=False,
            )
            yield Label(f"Online services: {describe_online_services(self.settings)}", markup=False)
            # Docked below the scrolling fields: a refusal is in view wherever the focus is.
            yield Static("", id="name-feedback", markup=False)
        yield Footer(show_command_palette=False)

    def _source_block(self, position: int, path: Path) -> Vertical:
        """File name and format on a line of their own (they wrap), the entries below."""
        row_number = position + 1
        block = [
            Label(
                f"Source {row_number}: {path.name} · {self.source_formats[position]}",
                id=f"source-file-{row_number}",
                classes="source-file",
                markup=False,
            ),
            self._source_row(position, path),
            self._accuracy_level_row(row_number),
        ]
        if self.source_formats[position] == "csv":
            block.append(self._csv_mapping_row(row_number))
        return Vertical(*block, classes="source-block")

    def _source_row(self, position: int, path: Path) -> Horizontal:
        row_number = position + 1
        return Horizontal(
            Input(
                value=default_source_label(path),
                max_length=SOURCE_LABEL_MAX_LENGTH,
                id=f"source-label-{row_number}",
                classes="source-label",
            ),
            Select(
                [(kind, kind) for kind in SOURCE_KINDS],
                value=SOURCE_KINDS[0],
                allow_blank=False,
                id=f"source-kind-{row_number}",
                classes="source-kind",
            ),
            ColourField(
                SOURCE_NAMED_COLOURS,
                default_source_colour_name(position),
                id=f"source-colour-{row_number}",
                classes="source-colour",
            ),
            classes="source-row",
        )

    def _accuracy_level_row(self, row_number: int) -> Horizontal:
        return Horizontal(
            Label("Accuracy level:", classes="accuracy-level-label"),
            Select(
                ACCURACY_LEVEL_OPTIONS,
                value=ACCURACY_LEVEL_OPTIONS[0][1],
                allow_blank=False,
                id=f"source-accuracy-{row_number}",
                classes="source-accuracy",
            ),
            classes="accuracy-level-row",
        )

    def _csv_mapping_row(self, row_number: int) -> Horizontal:
        return Horizontal(
            Button("Columns…", id=f"source-columns-{row_number}", classes="source-columns"),
            Static(
                self._csv_mapping_text(row_number),
                id=f"source-mapping-{row_number}",
                classes="source-mapping",
                markup=False,
            ),
            classes="csv-mapping-row",
        )

    def _load_csv_preview(self, row_number: int, path: Path) -> None:
        try:
            preview = read_csv_preview(path, self.settings.extraction.fallback_encoding)
        except OSError as error:
            self.csv_previews[row_number] = f"cannot be read: {error}"
            self.csv_mappings[row_number] = None
            return
        self.csv_previews[row_number] = preview
        default_zone, zone_origin = default_csv_zone(self.settings)
        suggestion = suggest_columns(preview, default_zone)
        self.csv_suggestions[row_number] = suggestion
        self.csv_mappings[row_number] = suggestion.mapping
        if suggestion.mapping.zone != default_zone:
            zone_origin = ZONE_ORIGIN_HEADER if suggestion.mapping.zone else ZONE_ORIGIN_EXAMINER
        self.csv_zone_origins[row_number] = zone_origin
        self.confirmed_csv_rows.discard(row_number)

    def _csv_problem(self, row_number: int) -> str | None:
        preview = self.csv_previews[row_number]
        if isinstance(preview, str):
            return preview
        mapping = self.csv_mappings[row_number]
        if mapping is None:
            return "no column mapping"
        problem = csv_mapping_problem(preview, mapping)
        if problem is None and row_number not in self.confirmed_csv_rows:
            assumptions = csv_assumptions(preview, mapping, self.csv_zone_origins[row_number])
            if assumptions:
                return f"confirm in Columns…: {assumptions[0]}"
        return problem

    def _csv_mapping_text(self, row_number: int) -> str:
        problem = self._csv_problem(row_number)
        mapping = self.csv_mappings[row_number]
        if problem is not None:
            return f"Columns not usable: {problem}"
        assert mapping is not None
        return describe_csv_mapping(mapping)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "case-places-button":
            event.stop()
            self._open_case_places()
            return
        if not button_id.startswith("source-columns-"):
            return
        event.stop()
        row_number = int(button_id.removeprefix("source-columns-"))
        if isinstance(self.csv_previews[row_number], str):
            self._show_validation()
            return
        self._open_csv_mapping(row_number, chain_to_next=False)

    def _rows_needing_the_columns_dialog(self, after_row: int = 0) -> list[int]:
        """CSV rows after ``after_row`` whose mapping has a problem or rests on an
        assumption the examiner has not confirmed."""
        return [
            row_number
            for row_number, preview in sorted(self.csv_previews.items())
            if row_number > after_row
            and not isinstance(preview, str)
            and self._csv_problem(row_number) is not None
        ]

    def _open_csv_mapping(self, row_number: int, *, chain_to_next: bool) -> None:
        """The Columns… dialog for a CSV row; with ``chain_to_next``, OK goes on to the
        next row that needs it (Cancel leaves the row unusable and stops the chain)."""
        preview = self.csv_previews[row_number]
        assert not isinstance(preview, str)
        mapping = self.csv_mappings[row_number] or self.csv_suggestions[row_number].mapping

        def keep_mapping(chosen: CsvMapping | None) -> None:
            if chosen is not None:
                if chosen.zone != mapping.zone:
                    self.csv_zone_origins[row_number] = ZONE_ORIGIN_EXAMINER
                # The dialog re-reads the preview when the delimiter changes; the mapping
                # is checked against that preview, not the one sniffed at first.
                self.csv_previews[row_number] = mapping_dialog.preview
                self.csv_mappings[row_number] = chosen
                self.confirmed_csv_rows.add(row_number)
                self.query_one(f"#source-mapping-{row_number}", Static).update(
                    self._csv_mapping_text(row_number)
                )
            self._show_validation()
            if chosen is not None and chain_to_next:
                next_rows = self._rows_needing_the_columns_dialog(after_row=row_number)
                if next_rows:
                    self._open_csv_mapping(next_rows[0], chain_to_next=True)

        mapping_dialog = CsvMappingScreen(
            self.source_paths[row_number - 1],
            self.settings.extraction.fallback_encoding,
            preview,
            mapping,
            self.csv_suggestions[row_number],
            self.csv_zone_origins[row_number],
        )
        self.app.push_screen(mapping_dialog, keep_mapping)

    def _open_case_places(self) -> None:
        def keep_case_places(entry: CasePlacesEntry | None) -> None:
            if entry is not None:
                self.case_places_entry = entry
                self.query_one("#case-places-summary", Static).update(
                    describe_case_places(entry.places, self.settings)
                )

        # CSV files are offered from the directory of the sources (the input directory).
        display_zone = self._display_zone_of_the_run()
        # The dialog shows and edits the windows in the zone of the run.
        try:
            case_places_entry = self._case_places_in_zone(display_zone)
        except CasePlacesZoneError as error:
            feedback = self.query_one("#name-feedback", Static)
            feedback.remove_class("valid")
            feedback.update(str(error))
            return
        self.app.push_screen(
            CasePlacesScreen(
                case_places_entry,
                self.source_paths[0].parent,
                self.source_paths,
                resolve_display_timezone(display_zone),
                display_zone,
            ),
            keep_case_places,
        )

    def _case_places_in_zone(self, display_zone: str) -> CasePlacesEntry:
        """The case places with their windows read in ``display_zone``: read again from
        the entered texts when the display zone changed after their entry."""
        entry = self.case_places_entry
        if entry.zone_name == display_zone or all(place.window is None for place in entry.places):
            return entry
        try:
            places = reread_case_places(
                entry.places,
                entry.entered_fields,
                resolve_display_timezone(display_zone),
                display_zone,
            )
        except CasePlaceInputError as error:
            raise CasePlacesZoneError(
                f"Case place {error}: its window was entered in {entry.zone_name}, but the "
                f"display zone is now {display_zone}. Set {entry.zone_name} again to edit it "
                "in Case places…"
            ) from error
        return replace(entry, places=places, zone_name=display_zone)

    def _display_zone_of_the_run(self) -> str:
        """The display zone field when it names a zone, else config.toml's setting."""
        return (
            self.query_one("#display-zone", ZoneField).zone_name or self.settings.timezone.display
        )

    def _display_zone_origin(self) -> str:
        """Where the run's display zone comes from: the suggestion's origin while the field
        still holds it (host_system_confirmed once the switch is on), the examiner when
        it was changed, config.toml when it is empty."""
        zone_name = self.query_one("#display-zone", ZoneField).zone_name
        if not zone_name:
            return DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING
        if zone_name != self.suggested_display_zone:
            return DISPLAY_ZONE_ORIGIN_EXAMINER
        if self.suggested_display_zone_origin == DISPLAY_ZONE_ORIGIN_HOST_SYSTEM and (
            self.query_one("#display-zone-confirmed", Switch).value
        ):
            return DISPLAY_ZONE_ORIGIN_HOST_SYSTEM_CONFIRMED
        return self.suggested_display_zone_origin

    def _host_zone_would_read_geojson(self) -> bool:
        return "geojson" in self.source_formats and self._display_zone_origin() in (
            DISPLAY_ZONE_ORIGIN_HOST_SYSTEM,
            DISPLAY_ZONE_ORIGIN_HOST_SYSTEM_CONFIRMED,
        )

    def _show_display_zone_confirmation(self) -> None:
        confirm_row = self.query_one("#display-zone-confirm-row")
        confirm_row.display = self._host_zone_would_read_geojson()
        if confirm_row.display:
            self.query_one("#display-zone-confirm-label", Label).update(
                f"Read GeoJSON times without offset in {self.suggested_display_zone}, the "
                "host system's zone (Space confirms):"
            )

    def _validate_display_zone(self) -> str:
        display_zone = validate_display_zone(self.query_one("#display-zone", ZoneField))
        if self._display_zone_origin() == DISPLAY_ZONE_ORIGIN_HOST_SYSTEM and (
            "geojson" in self.source_formats
        ):
            raise DisplayZoneError(
                f"Confirm the display time zone: {display_zone} comes from the host system "
                "clock and would be used for GeoJSON times without offset. Press Space on the "
                "switch."
            )
        return display_zone

    def on_mount(self) -> None:
        self.query_one(StatusHeader).status = "Idle"
        self._show_display_zone_confirmation()
        self.query_one("#project-name", Input).focus(scroll_visible=False)
        # A CSV that cannot be used as suggested, or only on an assumption, opens its
        # Columns… dialog at once; cleanly recognised files do not.
        rows_to_confirm = self._rows_needing_the_columns_dialog()
        if rows_to_confirm:
            self._open_csv_mapping(rows_to_confirm[0], chain_to_next=True)

    def on_input_changed(self, _: Input.Changed) -> None:
        self._show_display_zone_confirmation()
        self._show_validation()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "display-zone-confirmed":
            self._show_validation()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Leave Enter to an open kind/colour list so it confirms the option, not the run,
        and to a focused button so it presses the button."""
        if action == "start":
            focused = self.focused
            if isinstance(focused, Button):
                return False
            if focused is not None and any(
                (isinstance(ancestor, Select) and ancestor.expanded)
                or (isinstance(ancestor, FilteredChoiceField) and ancestor.choices_open)
                for ancestor in focused.ancestors_with_self
            ):
                return False
        return True

    def action_start(self) -> None:
        request = self._show_validation()
        if request is not None:
            self.dismiss(request)

    def action_go_back(self) -> None:
        self.dismiss(None)

    def _show_validation(self) -> ProjectRequest | None:
        """Validate every field; show the first problem or the resulting project directory."""
        feedback = self.query_one("#name-feedback", Static)
        try:
            project_name = validate_project_name(self.query_one("#project-name", Input).value)
            labels = validate_source_labels(
                [
                    self.query_one(f"#source-label-{row_number}", Input).value
                    for row_number in range(1, len(self.source_paths) + 1)
                ]
            )
            colours = self._source_colours()
            self._check_source_formats()
            case_reference = validate_case_field(
                self.query_one("#case-reference", Input).value, "Case reference"
            )
            examiner = validate_case_field(self.query_one("#examiner", Input).value, "Examiner")
            display_zone = self._validate_display_zone()
            case_places_entry = self._case_places_in_zone(self._display_zone_of_the_run())
            matrix_tolerance_m = validate_matrix_tolerance(
                self.query_one("#matrix-tolerance", Input).value
            )
        except (
            ProjectNameError,
            SourceLabelError,
            SourceColourError,
            SourceFormatSelectionError,
            CaseFieldError,
            DisplayZoneError,
            CasePlacesZoneError,
            MatrixToleranceError,
        ) as error:
            feedback.remove_class("valid")
            feedback.update(str(error))
            return None
        feedback.add_class("valid")
        feedback.update(f"Project directory: <timestamp>_{project_name}")
        return ProjectRequest(
            tuple(
                self._source_descriptor(position, label, colour)
                for position, (label, colour) in enumerate(zip(labels, colours, strict=True))
            ),
            project_name,
            self.query_one("#remove-duplicates", Switch).value,
            case_reference=case_reference,
            examiner=examiner,
            case_places=case_places_entry.places,
            case_places_file=case_places_entry.file,
            display_zone=display_zone,
            display_zone_origin=self._display_zone_origin(),
            matrix_tolerance_m=matrix_tolerance_m,
        )

    def _check_source_formats(self) -> None:
        """Names the first source that cannot be read as selected."""
        for row_number, (path, source_format) in enumerate(
            zip(self.source_paths, self.source_formats, strict=True), start=1
        ):
            if source_format == UNRECOGNISED_FORMAT:
                raise SourceFormatSelectionError(
                    f"Source {row_number}: {path.name} is {UNRECOGNISED_JSON_REASON}"
                )
            if source_format == "csv":
                problem = self._csv_problem(row_number)
                if problem is not None:
                    raise SourceFormatSelectionError(
                        f"Source {row_number}: {path.name} needs a usable column mapping "
                        f"(Columns…): {problem}"
                    )

    def _source_colours(self) -> list[str]:
        """Hex colour per source row; names the first row whose entry is not a colour."""
        colours: list[str] = []
        for row_number in range(1, len(self.source_paths) + 1):
            colour = self.query_one(f"#source-colour-{row_number}", ColourField).colour
            if colour is None:
                raise SourceColourError(
                    f"Source {row_number}: colour must be a colour name from the list "
                    "or a hex value like #ff8800"
                )
            colours.append(colour)
        return colours

    def _source_descriptor(self, position: int, label: str, colour: str) -> SourceDescriptor:
        row_number = position + 1
        path = self.source_paths[position]
        return SourceDescriptor(
            identifier=row_number,
            label=label,
            kind=str(self.query_one(f"#source-kind-{row_number}", Select).value),
            colour=colour,
            path=path,
            format=self.source_formats[position],
            csv_mapping=self._csv_mapping_document(row_number),
            accuracy_level=str(self.query_one(f"#source-accuracy-{row_number}", Select).value),
            zone_origin=self._csv_zone_origin(row_number),
        )

    def _csv_mapping_document(self, row_number: int) -> dict[str, str] | None:
        mapping = self.csv_mappings.get(row_number)
        return None if mapping is None else mapping.to_document()

    def _csv_zone_origin(self, row_number: int) -> str | None:
        mapping = self.csv_mappings.get(row_number)
        if mapping is None or not mapping.zone:
            return None
        return self.csv_zone_origins[row_number]
