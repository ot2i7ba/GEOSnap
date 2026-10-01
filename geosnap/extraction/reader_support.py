# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Checks shared by the format readers: XML prolog, ISO 8601 times, positions, excerpts."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from geosnap.extraction.source_reader import (
    UTF8_BOM,
    UTF16_BE_BOM,
    UTF16_LE_BOM,
    SourceReadReport,
)

DEFAULT_XML_ENCODING = "utf-8"
GX_NAMESPACE = "http://www.google.com/kml/ext/2.2"
ORIGINAL_RECORD_MAX_CHARS = 2000
REASON_EXCERPT_MAX_CHARS = 100
NULL_ISLAND_LIMIT_DEGREES = 0.001
TRUNCATION_MARK = "…"
# Starts every note part GEOSnap writes itself; record_text keeps it out of file text.
TOOL_NAME = "GEOSnap"
TOOL_NOTE_MARK = f"[{TOOL_NAME}]"
FILE_TEXT_MARK_REPLACEMENT = "(GEOSnap)"
# Any coordinate, epoch or scaled integer fits; CPython refuses int() beyond 4300 digits.
MAX_INTEGER_DIGITS = 40
NUMBER_TOO_LONG_REASON = "number too long"

# Every timestamp any reader accepts lies in this window (UTC, end exclusive); outside it
# a value is far more likely a default (0001-01-01, 1970-01-01) or a unit error than data.
PLAUSIBLE_TIME_RANGE_UTC = (datetime(1990, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
IMPLAUSIBLE_TIME_REASON = "timestamp outside the plausible range 1990-01-01 to 2099-12-31"
# UTC offsets as real zones use them: at most 14 hours, minutes 00/15/30/45.
MAX_OFFSET_MINUTES = 14 * 60
OFFSET_MINUTE_STEPS = frozenset({0, 15, 30, 45})

ISO_TIMESTAMP_PATTERN = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?"
)


class XmlPrologInspector:
    """Refuses a DOCTYPE and notes the encoding, up to the start of the root element.

    Without a DOCTYPE there are no internal entity definitions to expand. As a second
    line of defence, pyexpat (expat >= 2.4.1 in the CPython 3.11+ builds we ship)
    limits entity amplification, and ElementTree never resolves external entities.
    A byte-order mark decides the reported encoding before the XML declaration does.
    The root element must have the local name ``root_name``. ``format_error`` builds the
    exception raised for a DOCTYPE (``doctype_reason``) or a wrong root element.
    """

    def __init__(
        self, format_error: Callable[[str], Exception], doctype_reason: str, root_name: str
    ) -> None:
        self._format_error = format_error
        self._doctype_reason = doctype_reason
        self._root_name = root_name
        self._parser = expat.ParserCreate()
        self._parser.XmlDeclHandler = self._on_xml_declaration
        self._parser.StartDoctypeDeclHandler = self._on_doctype
        self._parser.StartElementHandler = self._on_root_element
        self._bom_encoding: str | None = None
        self._declared_encoding: str | None = None
        self._first_chunk = True
        self.root_element_seen = False

    @property
    def encoding(self) -> str:
        return (self._bom_encoding or self._declared_encoding or DEFAULT_XML_ENCODING).lower()

    def inspect(self, chunk: bytes) -> None:
        if self._first_chunk:
            self._first_chunk = False
            if chunk.startswith(UTF8_BOM):
                self._bom_encoding = "utf-8-sig"
            elif chunk.startswith((UTF16_LE_BOM, UTF16_BE_BOM)):
                self._bom_encoding = "utf-16"
        # The prolog ends where the root element starts; the rest is the pull parser's.
        if not self.root_element_seen:
            self._parser.Parse(chunk, False)

    def finish(self) -> None:
        """End of file: an unfinished prolog raises expat's error ("no element found")."""
        if not self.root_element_seen:
            self._parser.Parse(b"", True)

    def _on_xml_declaration(self, version: str, encoding: str | None, standalone: int) -> None:
        self._declared_encoding = encoding

    def _on_doctype(self, *declaration: object) -> None:
        raise self._format_error(self._doctype_reason)

    def _on_root_element(self, name: str, *attributes: object) -> None:
        # The rest of the chunk holding the root start is parsed here too; only the
        # first element is the root.
        if self.root_element_seen:
            return
        self.root_element_seen = True
        found_name = name.rpartition(":")[2]
        if found_name != self._root_name:
            raise self._format_error(f"root element is <{found_name}>, not <{self._root_name}>")


def parse_iso_timestamp(timestamp_text: str) -> tuple[datetime, bool] | str | None:
    """ISO 8601 date and time with optional fraction and zone, as (UTC instant, zone given);
    the refusal reason for a time outside PLAUSIBLE_TIME_RANGE_UTC; None when the text is
    no valid ISO time (the caller words that reason).

    A missing zone is read as UTC; the caller decides whether to warn about it.
    """
    match = ISO_TIMESTAMP_PATTERN.fullmatch(timestamp_text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction_digits, zone_text = match.group(7), match.group(8)
    microsecond = int((fraction_digits or "").ljust(6, "0")[:6])
    try:
        if zone_text is None or zone_text == "Z":
            zone: timezone = UTC
        else:
            offset_hours, offset_minutes = int(zone_text[1:3]), int(zone_text[4:6])
            if not utc_offset_allowed(offset_hours, offset_minutes):
                return None
            offset_sign = -1 if zone_text[0] == "-" else 1
            offset = timedelta(hours=offset_hours, minutes=offset_minutes)
            zone = timezone(offset_sign * offset)
        local_time = datetime(year, month, day, hour, minute, second, microsecond, zone)
    except ValueError:
        return None
    # The wall time is checked first: near year 1 or 9999 the UTC instant lies outside
    # the datetime range.
    implausible = implausible_time_reason(local_time.replace(tzinfo=None), timestamp_text)
    if implausible is not None:
        return implausible
    instant = local_time.astimezone(UTC)
    return implausible_time_reason(instant, timestamp_text) or (instant, zone_text is not None)


def utc_offset_allowed(hours: int, minutes: int) -> bool:
    """True for an offset of at most MAX_OFFSET_MINUTES in OFFSET_MINUTE_STEPS."""
    return hours * 60 + minutes <= MAX_OFFSET_MINUTES and minutes in OFFSET_MINUTE_STEPS


def implausible_time_reason(moment: datetime, timestamp_text: str) -> str | None:
    """The refusal reason for a time outside PLAUSIBLE_TIME_RANGE_UTC, else None. A wall
    time without zone is compared as if it were UTC."""
    instant = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    earliest, latest = PLAUSIBLE_TIME_RANGE_UTC
    if earliest <= instant < latest:
        return None
    return implausible_time_text(timestamp_text)


def implausible_time_text(timestamp_text: str) -> str:
    """The reason a time outside PLAUSIBLE_TIME_RANGE_UTC is refused, for every reader."""
    return (
        f"{IMPLAUSIBLE_TIME_REASON} (a default value such as 0001-01-01 or 1970-01-01?):"
        f" {excerpt(timestamp_text)}"
    )


def bounded_int(text: str) -> int | None:
    """int(text) for a plain (signed) digit string of at most MAX_INTEGER_DIGITS digits;
    None for anything else, so that no reader ever raises on a digit string."""
    digits = text[1:] if text[:1] in "+-" else text
    if not digits.isascii() or not digits.isdigit() or len(digits) > MAX_INTEGER_DIGITS:
        return None
    return int(text)


class OversizedNumber(float):
    """A JSON integer beyond MAX_INTEGER_DIGITS: decoded as infinity so that the document
    stays readable; the readers refuse the record with NUMBER_TOO_LONG_REASON."""

    __slots__ = ()


def json_int(text: str) -> int | float:
    """``parse_int`` hook for json.loads and JSONDecoder: bounded, never a ValueError."""
    whole_number = bounded_int(text)
    return OversizedNumber("inf") if whole_number is None else whole_number


def checked_position(latitude: float, longitude: float) -> tuple[float, float] | str:
    """The position, or the rejection reason: non-finite, out of range, null island."""
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return "non-finite number in coordinates"
    if not -90.0 <= latitude <= 90.0:
        return f"latitude out of range: {latitude}"
    if not -180.0 <= longitude <= 180.0:
        return f"longitude out of range: {longitude}"
    if abs(latitude) < NULL_ISLAND_LIMIT_DEGREES and abs(longitude) < NULL_ISLAND_LIMIT_DEGREES:
        return "coordinates at null island"
    return latitude, longitude


def record_text(text: str | None) -> str | None:
    """Label or note text taken from a source file, the same for every reader: whitespace
    collapsed, control and format characters removed (categories Cc and Cf: NUL, ESC,
    bidi overrides, zero-width characters, BOM), and never GEOSnap's own note mark.
    The original record keeps the text as it was."""
    if text is None:
        return None
    visible = " ".join(text.split())
    # Printable ASCII, the bulk of all cells, holds neither category.
    if not (visible.isascii() and visible.isprintable()):
        visible = "".join(
            character
            for character in visible
            if unicodedata.category(character) not in ("Cc", "Cf")
        )
        visible = " ".join(visible.split())
    if TOOL_NOTE_MARK in visible:
        visible = visible.replace(TOOL_NOTE_MARK, FILE_TEXT_MARK_REPLACEMENT)
    return visible or None


def tool_note(text: str) -> str:
    """A note part written by GEOSnap itself, told apart from file text by its mark."""
    return f"{TOOL_NOTE_MARK} {text}"


def release_finished_element(
    element: ET.Element, open_elements: list[ET.Element], record_names: frozenset[str]
) -> None:
    """Free an element that ended outside any open record element, so documents with
    millions of non-record elements (styles, metadata) keep memory flat."""
    if any(local_name(open_element.tag) in record_names for open_element in open_elements):
        return
    element.clear()
    if open_elements:
        open_elements[-1].remove(element)


def whole_file_read_report(path: Path, chunk_bytes: int, encoding: str) -> SourceReadReport:
    """SHA-256 over every byte of a file that is refused before its records are read, so
    the failed source is still documented with a digest (no records counted)."""
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as source_file:
        while chunk := source_file.read(chunk_bytes):
            digest.update(chunk)
            bytes_read += len(chunk)
    return SourceReadReport(
        sha256_hex=digest.hexdigest(),
        size_bytes=bytes_read,
        bytes_read=bytes_read,
        encoding=encoding,
        line_count=0,
        decode_replacements=0,
    )


def truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + TRUNCATION_MARK


def excerpt(text: str) -> str:
    return truncate(" ".join(text.split()), REASON_EXCERPT_MAX_CHARS)


def compact_xml(element: ET.Element) -> str:
    """Element as one-line XML: prefix-free names (KML gx: kept), whitespace collapsed."""
    parts: list[str] = []
    pending: list[tuple[ET.Element, bool]] = [(element, False)]
    while pending:
        node, closing = pending.pop()
        name = qualified_name(node.tag)
        if closing:
            parts.append(f"</{name}>")
        else:
            attributes = "".join(
                f" {qualified_name(key)}={_quote_attribute(value)}"
                for key, value in node.attrib.items()
            )
            text = _escape_text(collapse_whitespace(node.text))
            children = list(node)
            if not children and not text:
                parts.append(f"<{name}{attributes}/>")
            else:
                parts.append(f"<{name}{attributes}>{text}")
                pending.append((node, True))
                pending.extend((child, False) for child in reversed(children))
                continue
        if node is not element:
            parts.append(_escape_text(collapse_whitespace(node.tail)))
    return "".join(parts)


def local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def qualified_name(tag: str) -> str:
    return f"gx:{local_name(tag)}" if tag.startswith(f"{{{GX_NAMESPACE}}}") else local_name(tag)


def first_child(parent: ET.Element, wanted_name: str) -> ET.Element | None:
    return next((child for child in parent if local_name(child.tag) == wanted_name), None)


def first_descendant(parent: ET.Element, wanted_name: str) -> ET.Element | None:
    return next((node for node in parent.iter() if local_name(node.tag) == wanted_name), None)


def collapse_whitespace(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote_attribute(value: str) -> str:
    return '"' + _escape_text(value).replace('"', "&quot;") + '"'


# "name: value" parts of a record's own fields (CSV columns without a role, KML
# ExtendedData, GeoJSON properties): one set of bounds for every reader, so a record with
# hundreds of fields cannot inflate the map. The original record stays the reference.
FIELD_PARTS_MAX_CHARS = 500
FIELD_VALUE_MAX_CHARS = 100
FIELD_NAME_MAX_CHARS = 40
NOTE_PART_SEPARATOR = " · "
# The separator's dot never occurs in field text, so a note splits back into its parts.
SEPARATOR_DOT, SEPARATOR_DOT_REPLACEMENT = "·", "-"


def field_text(text: str | None) -> str | None:
    """Field name or value from a file for a note: record_text, never the separator's dot,
    and a text that is just the tool's name (a column called "GEOSnap") is bracketed like
    the mark, so that no file field can pose as a part GEOSnap wrote."""
    cleaned = record_text(text)
    if cleaned is not None and SEPARATOR_DOT in cleaned:
        cleaned = cleaned.replace(SEPARATOR_DOT, SEPARATOR_DOT_REPLACEMENT)
    if cleaned is not None and cleaned.casefold() == TOOL_NAME.casefold():
        cleaned = FILE_TEXT_MARK_REPLACEMENT
    return cleaned


def field_part(name: str | None, value: str | None) -> str | None:
    """ "name: value" with the name cut to FIELD_NAME_MAX_CHARS and the value to
    FIELD_VALUE_MAX_CHARS; None when either is empty once cleaned."""
    cleaned_name, cleaned_value = field_text(name), field_text(value)
    if cleaned_name is None or cleaned_value is None:
        return None
    return (
        f"{truncate(cleaned_name, FIELD_NAME_MAX_CHARS)}: "
        f"{truncate(cleaned_value, FIELD_VALUE_MAX_CHARS)}"
    )


def bounded_field_parts(
    parts: list[str], fields_noun: str, full_record_reference: str
) -> str | None:
    """The parts joined by NOTE_PART_SEPARATOR. Only whole parts are kept within
    FIELD_PARTS_MAX_CHARS; when some do not fit, the text ends by saying how many and
    where the full record is: "… (+3 more columns; full row in the source file, line 12)"
    (fields_noun "columns", full_record_reference "row in the source file, line 12")."""
    if not parts:
        return None
    text = NOTE_PART_SEPARATOR.join(parts)
    if len(text) <= FIELD_PARTS_MAX_CHARS:
        return text
    for kept_count in range(len(parts) - 1, -1, -1):
        text = (
            NOTE_PART_SEPARATOR.join(parts[:kept_count])
            + f" … (+{len(parts) - kept_count} more {fields_noun}; full {full_record_reference})"
        )
        if len(text) <= FIELD_PARTS_MAX_CHARS:
            break
    return text.strip()


# Raw method texts (casefolded, without spaces, "-" and "_") by the method they name. Only
# exact words count: "fused" (Android's mix of methods), "passive" or "GPS (fused)" name
# none, so the record's method stays unknown and its raw text stays in the note.
POSITIONING_METHOD_WORDS: dict[str, str] = {
    **dict.fromkeys(
        ("gps", "gnss", "satellite", "sat", "glonass", "galileo", "2d", "3d", "dgps", "pps"),
        "gnss",
    ),
    **dict.fromkeys(("wifi", "wlan"), "wifi"),
    **dict.fromkeys(("cell", "cellid", "gsm", "umts", "lte", "5g", "tower", "cellular"), "cell"),
    "network": "network",
}
POSITIONING_METHOD_SEPARATORS = re.compile(r"[\s_-]+")


def positioning_method_from_text(raw_method: str | None) -> str | None:
    """ "gnss", "wifi", "cell" or "network" for a raw method text of any reader
    (POSITIONING_METHOD_WORDS); None when it names none of them or is empty."""
    if raw_method is None:
        return None
    folded = POSITIONING_METHOD_SEPARATORS.sub("", raw_method.casefold())
    return POSITIONING_METHOD_WORDS.get(folded)


def disagreeing_methods_note(named_methods: list[tuple[str, str]]) -> str:
    """Tool note for fields of one record that name different positioning methods, each
    method with the field it came from: "gnss (provider), cell (source)"."""
    methods_text = ", ".join(
        f"{method} ({truncate(field_text(field_name) or '', FIELD_NAME_MAX_CHARS)})"
        for method, field_name in named_methods
    )
    return tool_note(f"positioning methods disagree: {methods_text}")
