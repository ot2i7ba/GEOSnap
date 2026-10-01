# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Entry of case places: validation of the dialog fields and of the strict CSV file.

Window times are wall-clock times of the project's display zone. A time that occurs twice
(clocks go back) is accepted only with its offset; a time that does not exist (clocks go
forward) is refused. Nothing is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

from geosnap.analysis.case_places import (
    CASE_PLACE_DEFAULT_RADIUS_M,
    CASE_PLACE_MAX_RADIUS_M,
    CASE_PLACE_MIN_RADIUS_M,
    LOCATION_ENTERED,
    LOCATION_NOT_LOCATED,
    CasePlace,
    CasePlaceWindow,
)
from geosnap.timezones import display_local_text, to_display_time

CASE_PLACES_CSV_HEADER = (
    "label",
    "latitude",
    "longitude",
    "radius_m",
    "from_local",
    "to_local",
    "address",
    "note",
)
CASE_PLACE_LABEL_MAX_LENGTH = 60
CASE_PLACE_TEXT_MAX_LENGTH = 200
MAX_CASE_PLACES = 200
MAX_CASE_PLACES_FILE_BYTES = 5_000_000
# The problems of a file are listed up to this many rows.
LISTED_PROBLEMS_LIMIT = 20
WINDOW_TIME_FORMAT_HINT = "YYYY-MM-DD HH:MM[:SS], optionally followed by an offset like +02:00"
_WINDOW_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*(?:([+-])(\d{2}):(\d{2})|(Z))?$"
)


_PLAIN_DECIMAL = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?", re.ASCII)


class CasePlaceInputError(ValueError):
    """A case place entry or file cannot be used; the message says what and where."""


@dataclass(frozen=True, slots=True)
class CasePlaceFields:
    """The texts of one case place as typed into the dialog or read from a CSV row."""

    label: str = ""
    latitude: str = ""
    longitude: str = ""
    radius_m: str = ""
    from_local: str = ""
    to_local: str = ""
    address: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class CasePlacesFile:
    """A loaded case places CSV: metadata.json records its name and SHA-256.
    ``entered_fields``: the texts of each place's row, in the order of ``places``."""

    name: str
    sha256: str
    places: tuple[CasePlace, ...]
    entered_fields: tuple[CasePlaceFields, ...] = ()


def _offset_text(offset: timedelta) -> str:
    minutes = round(offset.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _occurrences(wall: datetime, display_tz: tzinfo | None) -> list[datetime]:
    """The UTC instants at which the display zone shows this wall-clock time (0, 1 or 2)."""
    instants: list[datetime] = []
    for fold in (0, 1):
        # A naive datetime converts through the system zone, which display_tz None means.
        candidate = wall.replace(fold=fold)
        if display_tz is not None:
            candidate = candidate.replace(tzinfo=display_tz)
        instant = candidate.astimezone(UTC)
        shown = to_display_time(instant, display_tz).replace(tzinfo=None)
        if shown == wall and instant not in instants:
            instants.append(instant)
    return sorted(instants)


def parse_window_time(text: str, display_tz: tzinfo | None, zone_name: str) -> datetime:
    """UTC instant of a window bound entered as wall-clock time of the display zone."""
    match = _WINDOW_TIME.match(text.strip())
    if match is None:
        raise CasePlaceInputError(f"'{text.strip()}' is not a time as {WINDOW_TIME_FORMAT_HINT}")
    date_text, clock_text, sign, offset_hours, offset_minutes, zulu = match.groups()
    try:
        wall = datetime.fromisoformat(f"{date_text}T{clock_text}")
    except ValueError as error:
        raise CasePlaceInputError(f"'{text.strip()}' is not a valid date and time") from error
    shown = f"{wall:%Y-%m-%d %H:%M:%S}"
    entered_offset: timedelta | None = None
    if zulu is not None:
        entered_offset = timedelta(0)
    elif sign is not None:
        if int(offset_hours) > 23 or int(offset_minutes) > 59:
            raise CasePlaceInputError(
                f"offset {sign}{offset_hours}:{offset_minutes} is not an offset (hours up to "
                "23, minutes up to 59)"
            )
        entered_offset = timedelta(hours=int(offset_hours), minutes=int(offset_minutes))
        if sign == "-":
            entered_offset = -entered_offset
    try:
        return _window_instant(wall, shown, entered_offset, display_tz, zone_name)
    except CasePlaceInputError:
        raise
    except (OverflowError, ValueError, OSError) as error:
        # Dates at the edges of the calendar cannot be converted between zones.
        raise CasePlaceInputError(
            f"{shown} cannot be converted to UTC in {zone_name} ({error})"
        ) from error


def _window_instant(
    wall: datetime,
    shown: str,
    entered_offset: timedelta | None,
    display_tz: tzinfo | None,
    zone_name: str,
) -> datetime:
    occurrences = _occurrences(wall, display_tz)
    if not occurrences:
        raise CasePlaceInputError(
            f"{shown} does not exist in {zone_name} (clocks go forward over it)"
        )
    if entered_offset is None:
        if len(occurrences) > 1:
            offsets = [
                _offset_text(to_display_time(instant, display_tz).utcoffset() or timedelta(0))
                for instant in occurrences
            ]
            raise CasePlaceInputError(
                f"{shown} occurs twice in {zone_name} (clocks go back): add the offset, "
                f"{offsets[0]} for the first or {offsets[1]} for the second occurrence"
            )
        return occurrences[0]
    instant = wall.replace(tzinfo=timezone(entered_offset)).astimezone(UTC)
    if instant not in occurrences:
        valid = " or ".join(
            _offset_text(to_display_time(occurrence, display_tz).utcoffset() or timedelta(0))
            for occurrence in occurrences
        )
        raise CasePlaceInputError(
            f"offset {_offset_text(entered_offset)} does not match {zone_name} at {shown} ({valid})"
        )
    return instant


def _checked_text(value: str, name: str, max_length: int) -> str:
    text = value.strip()
    if len(text) > max_length:
        raise CasePlaceInputError(
            f"{name} must not exceed {max_length} characters ({len(text)} characters)"
        )
    if not text.isprintable():
        raise CasePlaceInputError(f"{name} may only hold printable characters")
    return text


def _plain_decimal(value: str) -> float:
    """float() of an ASCII decimal only: no underscores, exponents, other digits, inf or nan
    (float() alone would accept "5_2.5" and full-width digits)."""
    if _PLAIN_DECIMAL.fullmatch(value) is None:
        return math.nan
    return float(value)


def _coordinate(value: str, name: str, limit: float) -> float:
    number = _plain_decimal(value)
    if not math.isfinite(number) or abs(number) > limit:
        raise CasePlaceInputError(
            f"{name} must be a decimal number between -{limit:g} and {limit:g} "
            f"(ASCII digits, decimal point), got '{value}'"
        )
    return number


def _radius(value: str) -> float:
    if not value:
        return CASE_PLACE_DEFAULT_RADIUS_M
    number = _plain_decimal(value)
    if not CASE_PLACE_MIN_RADIUS_M <= number <= CASE_PLACE_MAX_RADIUS_M:
        raise CasePlaceInputError(
            f"radius_m must be a number between {CASE_PLACE_MIN_RADIUS_M} and "
            f"{CASE_PLACE_MAX_RADIUS_M} (ASCII digits, decimal point), got '{value}'"
        )
    return int(number) if number == int(number) else number


def parse_case_place(
    entry: CasePlaceFields,
    identifier: int,
    display_tz: tzinfo | None,
    zone_name: str,
    file_row: int | None = None,
) -> CasePlace:
    """The validated case place, or CasePlaceInputError naming the first bad field."""
    label = _checked_text(entry.label, "label", CASE_PLACE_LABEL_MAX_LENGTH)
    if not label:
        raise CasePlaceInputError(f"label is needed (1-{CASE_PLACE_LABEL_MAX_LENGTH} characters)")
    address = _checked_text(entry.address, "address", CASE_PLACE_TEXT_MAX_LENGTH)
    note = _checked_text(entry.note, "note", CASE_PLACE_TEXT_MAX_LENGTH)
    latitude_text, longitude_text = entry.latitude.strip(), entry.longitude.strip()
    latitude: float | None = None
    longitude: float | None = None
    if latitude_text or longitude_text:
        if not (latitude_text and longitude_text):
            raise CasePlaceInputError("latitude and longitude must both be given or both be empty")
        latitude = _coordinate(latitude_text, "latitude", 90.0)
        longitude = _coordinate(longitude_text, "longitude", 180.0)
    elif not address:
        raise CasePlaceInputError("a place needs coordinates or an address")
    radius_m = _radius(entry.radius_m.strip())
    from_text, to_text = entry.from_local.strip(), entry.to_local.strip()
    window: CasePlaceWindow | None = None
    if from_text or to_text:
        if not (from_text and to_text):
            raise CasePlaceInputError("from_local and to_local must both be given or both be empty")
        try:
            from_utc = parse_window_time(from_text, display_tz, zone_name)
        except CasePlaceInputError as error:
            raise CasePlaceInputError(f"from_local: {error}") from error
        try:
            to_utc = parse_window_time(to_text, display_tz, zone_name)
        except CasePlaceInputError as error:
            raise CasePlaceInputError(f"to_local: {error}") from error
        if from_utc > to_utc:
            raise CasePlaceInputError("from_local is after to_local")
        window = CasePlaceWindow(from_utc, to_utc)
    return CasePlace(
        identifier=identifier,
        label=label,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        window=window,
        address=address,
        note=note,
        location=LOCATION_ENTERED if latitude is not None else LOCATION_NOT_LOCATED,
        file_row=file_row,
    )


def reread_case_places(
    places: tuple[CasePlace, ...],
    entered_fields: tuple[CasePlaceFields, ...],
    display_tz: tzinfo | None,
    zone_name: str,
) -> tuple[CasePlace, ...]:
    """The places with their windows read again from the entered texts in another display
    zone; everything else stays. CasePlaceInputError names the first place that does not
    fit the zone (a time that does not exist or occurs twice there, or an entered offset
    the zone does not have)."""
    reread: list[CasePlace] = []
    for place, entry in zip(places, entered_fields, strict=True):
        try:
            window = parse_case_place(entry, place.identifier, display_tz, zone_name).window
        except CasePlaceInputError as error:
            raise CasePlaceInputError(f"'{place.label}': {error}") from error
        reread.append(replace(place, window=window))
    return tuple(reread)


def case_place_fields(place: CasePlace, display_tz: tzinfo | None) -> CasePlaceFields:
    """The place as editable texts; window times carry their offset, so they parse back to
    the same instants even in a repeated hour."""
    window = place.window
    return CasePlaceFields(
        label=place.label,
        latitude="" if place.latitude is None else repr(place.latitude),
        longitude="" if place.longitude is None else repr(place.longitude),
        radius_m=f"{place.radius_m:g}",
        from_local="" if window is None else display_local_text(window.from_utc, display_tz),
        to_local="" if window is None else display_local_text(window.to_utc, display_tz),
        address=place.address,
        note=place.note,
    )


def load_case_places_csv(path: Path, display_tz: tzinfo | None, zone_name: str) -> CasePlacesFile:
    """Read the strict case places CSV (UTF-8, comma or semicolon, header as documented).

    Every problem is reported with its row number (header = row 1); one bad row refuses the
    whole file, so a case place never goes missing unnoticed.
    """
    try:
        with path.open("rb") as file:
            content = file.read(MAX_CASE_PLACES_FILE_BYTES + 1)
    except OSError as error:
        raise CasePlaceInputError(f"{path.name} cannot be read: {error}") from error
    if len(content) > MAX_CASE_PLACES_FILE_BYTES:
        raise CasePlaceInputError(f"{path.name} is larger than {MAX_CASE_PLACES_FILE_BYTES} bytes")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CasePlaceInputError(f"{path.name} is not UTF-8 text: {error}") from error
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if ";" in first_line and "," not in first_line else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    expected_header = ",".join(CASE_PLACES_CSV_HEADER)
    places: list[CasePlace] = []
    entered_fields: list[CasePlaceFields] = []
    problems: list[str] = []
    label_rows: dict[str, int] = {}
    try:
        header = next(reader, None)
        if header is None or [cell.strip().lower() for cell in header] != list(
            CASE_PLACES_CSV_HEADER
        ):
            raise CasePlaceInputError(
                f"{path.name}: the header must be exactly {expected_header} "
                "(comma or semicolon separated)"
            )
        for cells in reader:
            row_number = reader.line_num
            if not any(cell.strip() for cell in cells):
                continue
            if len(cells) != len(CASE_PLACES_CSV_HEADER):
                problems.append(
                    f"row {row_number}: {len(cells)} columns instead of "
                    f"{len(CASE_PLACES_CSV_HEADER)}"
                )
                continue
            entry = CasePlaceFields(
                **dict(zip((field.name for field in fields(CasePlaceFields)), cells, strict=True))
            )
            try:
                place = parse_case_place(
                    entry, len(places) + 1, display_tz, zone_name, file_row=row_number
                )
            except CasePlaceInputError as error:
                problems.append(f"row {row_number}: {error}")
                continue
            if place.label in label_rows:
                problems.append(
                    f"row {row_number}: label '{place.label}' is already used in row "
                    f"{label_rows[place.label]}"
                )
                continue
            label_rows[place.label] = row_number
            places.append(place)
            entered_fields.append(entry)
    except csv.Error as error:
        problems.append(f"row {reader.line_num}: not readable as CSV ({error})")
    if len(places) > MAX_CASE_PLACES:
        problems.append(f"{len(places)} case places; at most {MAX_CASE_PLACES} are accepted")
    if problems:
        listed = problems[:LISTED_PROBLEMS_LIMIT]
        if len(problems) > len(listed):
            listed.append(f"... and {len(problems) - len(listed)} more")
        raise CasePlaceInputError(f"{path.name} refused: " + "; ".join(listed))
    if not places:
        raise CasePlaceInputError(f"{path.name} holds no case place")
    return CasePlacesFile(
        path.name, hashlib.sha256(content).hexdigest(), tuple(places), tuple(entered_fields)
    )
