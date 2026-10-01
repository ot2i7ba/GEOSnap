# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Read a CSV file through a column mapping: roles, time format and zone chosen per source.

The first row is the header. Delimiter by csv.Sniffer (candidates , ; tab |), encoding as
the text reader (BOM, UTF-8, configured fallback), decided once on the preview sample and
used for the whole file. Required roles: latitude and longitude (or one position column
holding both) and a timestamp; a separate time-of-day column, an end date and end time
(``timestamp_end``, ``time_of_day_end``), accuracy in metres, label, note and the
positioning method are optional. A time without offset is read in the mapping's zone,
which is never assumed silently: without one such a row is rejected.

Date and time in two columns are joined per row: an empty date or an empty time refuses
the row, a date cell that already carries a time must agree with the time column, and
"9.30" in the time column is read as 9:30 (timestamp_text.time_of_day_text). The end
instant is not a point of its own: it goes into the note as "[GEOSnap] end: <local time
with offset>", with "[GEOSnap] end before start" when it lies before the start and
"[GEOSnap] end time refused: <reason>" when it cannot be read; the examiner judges.

The positioning method column is read per row by reader_support.positioning_method_from_text
(an unrecognised value leaves the method unknown) and, unlike the other roles, stays in
the note as "header: value", so the raw text is always shown. Of several columns named as
a method, the one whose sampled cells name most known methods is suggested; when another
names a different method in a sampled row, that is an assumption to confirm, and every
row where they differ says so in the note. A header naming a radius or range used as
accuracy is an assumption to confirm: a cell sector radius is not an accuracy.

Coordinates are read per cell by coordinate_text, so one column may mix notations
(decimal, degrees and minutes, UTM, MGRS, ...). A column whose header ends in E7 or E6
holds scaled integers; nothing is scaled by its magnitude. Every coordinate that was not
a plain decimal leaves its notation and the text read in the point's note. A position
column has one order (``position_order``); hemisphere letters must agree with it. The
time format "automatic" reads each cell by timestamp_text; the order of day and month is
one choice per file and separator mark (``date_order_dot``, ``_slash``, ``_dash``),
suggested only from evidence in the sample. What a suggestion only assumes
(unconfirmed_assumptions) has to be confirmed by the examiner; without one the file is
refused.

Notes: parts GEOSnap writes start with "[GEOSnap]" and come first; text from the file
passes record_text and can neither carry that mark nor the part separator.

Every non-empty cell without a role follows in the note as "header: value", values cut
to REMAINING_VALUE_MAX_CHARS and the whole to REMAINING_COLUMNS_MAX_CHARS, so that a file
with hundreds of columns cannot inflate the map; the original row stays in the record.

The suggested mapping (suggest_mapping_with_reasons) comes from header synonyms in
English and German, from the words of a header (x/y, east/north, easting/northing,
Rechtswert/Hochwert with a date or time word and start/end qualifiers) proven by the
sampled cells, and from cell content alone; every choice carries its reason. A pair of
columns whose values lie far beyond 180 is reported as projected coordinates (UTM or
Gauß-Krüger, zone not stated) and never suggested. That the header vocabulary matches
the exports of mobile forensic tools has not been checked against sample files.

Blank lines before the header are skipped. Columns are identified by their label: the
header name, or "name (column n)" when a name occurs more than once (or is empty). Rows
are parsed strictly: a quoted field must close within MAX_RECORD_LINES physical lines
and a field may hold at most CSV_FIELD_SIZE_LIMIT characters; a row breaking either rule
is rejected on its own and reading continues with the next line. Setting the field size
limit changes the process-wide limit of the csv module.
"""

from __future__ import annotations

import codecs
import csv
import functools
import itertools
import logging
import math
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from geosnap.extraction.coordinate_text import (
    DECIMAL_DEGREES,
    LAT_LON,
    LATITUDE,
    LON_LAT,
    LONGITUDE,
    POSITION_ORDER_LABELS,
    ParsedAngle,
    parse_latitude,
    parse_longitude,
    parse_position,
    parse_scaled_integer,
)
from geosnap.extraction.extractor import ExtractionProgress
from geosnap.extraction.models import (
    ExtractionCounters,
    GeoPoint,
    ReferencePlace,
    RejectedLine,
    SourceExtractionOutcome,
    SourceFormatError,
)
from geosnap.extraction.reader_support import (
    FIELD_NAME_MAX_CHARS,
    FIELD_PARTS_MAX_CHARS,
    FIELD_VALUE_MAX_CHARS,
    IMPLAUSIBLE_TIME_REASON,
    NOTE_PART_SEPARATOR,
    ORIGINAL_RECORD_MAX_CHARS,
    bounded_field_parts,
    bounded_int,
    checked_position,
    disagreeing_methods_note,
    excerpt,
    field_text,
    implausible_time_reason,
    implausible_time_text,
    parse_iso_timestamp,
    positioning_method_from_text,
    tool_note,
    truncate,
    whole_file_read_report,
)
from geosnap.extraction.source_reader import SourceReader, detect_encoding
from geosnap.extraction.timestamp_text import (
    DATE_MARKS,
    DATE_ORDER_LABELS,
    DATE_ORDER_NOT_CHOSEN_REASON,
    PLAUSIBLE_UNIX_SECONDS,
    date_order_evidence,
    excel_serial_wall_time,
    needs_date_order,
    parse_time_of_day,
    recognise_timestamp,
    time_of_day_text,
)
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

CANCEL_CHECK_INTERVAL_ROWS = 1000
PREVIEW_SAMPLE_BYTES = 65_536
PREVIEW_ROW_COUNT = 5
# Rows of the 64 KB sample the suggestion looks at (notations, day/month order).
EVIDENCE_ROW_COUNT = 500
SNIFF_LINE_COUNT = 20
DELIMITER_CANDIDATES = ",;\t|"
TEXT_FIELD_MAX_CHARS = 500
# The bounds every reader applies to "name: value" parts (reader_support).
REMAINING_COLUMNS_MAX_CHARS = FIELD_PARTS_MAX_CHARS
REMAINING_VALUE_MAX_CHARS = FIELD_VALUE_MAX_CHARS
REMAINING_HEADER_MAX_CHARS = FIELD_NAME_MAX_CHARS
AUTOMATIC_TIME_FORMAT = "automatic"
EXCEL_SERIAL_TIME_FORMAT = "excel_serial"
TIME_FORMAT_LABELS = {
    AUTOMATIC_TIME_FORMAT: "Automatic (per row, unambiguous forms only)",
    "iso": "ISO 8601",
    "unix_seconds": "Unix seconds",
    "unix_milliseconds": "Unix milliseconds",
    "day_month_year": "DD.MM.YYYY HH:MM[:SS]",
    "month_day_year": "MM/DD/YYYY HH:MM[:SS] [AM|PM]",
    "year_month_day": "YYYY-MM-DD HH:MM[:SS]",
    EXCEL_SERIAL_TIME_FORMAT: "Excel serial date (1900 system)",
}
# Formats that never carry an offset and so always need the mapping's zone.
NAIVE_TIME_FORMATS = frozenset(
    {"day_month_year", "month_day_year", "year_month_day", EXCEL_SERIAL_TIME_FORMAT}
)
# Formats of a single number: a separate time-of-day column cannot belong to them.
NUMBER_TIME_FORMATS = frozenset({"unix_seconds", "unix_milliseconds", EXCEL_SERIAL_TIME_FORMAT})
COLUMN_ROLES = (
    "latitude",
    "longitude",
    "position",
    "timestamp",
    "time_of_day",
    "timestamp_end",
    "time_of_day_end",
    "accuracy",
    "label",
    "note",
    "positioning_method",
)

# Normalised header (casefolded, bracketed units removed, letters and digits only).
HEADER_SYNONYMS: dict[str, frozenset[str]] = {
    "latitude": frozenset(
        {
            "lat",
            "latitude",
            "breite",
            "breitengrad",
            "geobreite",
            "geographischebreite",
            "geografischebreite",
        }
    ),
    "longitude": frozenset(
        {
            "lon",
            "lng",
            "long",
            "longitude",
            "länge",
            "laenge",
            "längengrad",
            "laengengrad",
            "geolänge",
            "geographischelänge",
            "geografischelänge",
        }
    ),
    "position": frozenset(
        {
            "position",
            "location",
            "coordinates",
            "coordinate",
            "coords",
            "koordinaten",
            "koordinate",
            "gpskoordinaten",
            "latlon",
            "latlng",
            "latlong",
            "standort",
            "geo",
            "gps",
            "wkt",
            "utm",
            "mgrs",
            "utmref",
        }
    ),
    "timestamp": frozenset(
        {
            "time",
            "timestamp",
            "date",
            "datetime",
            "datum",
            "zeit",
            "zeitpunkt",
            "zeitstempel",
            "datumzeit",
            "datumuhrzeit",
        }
    ),
    "accuracy": frozenset({"accuracy", "horizontalaccuracy", "genauigkeit", "radius", "accuracym"}),
    "label": frozenset({"name", "label", "title", "bezeichnung", "titel"}),
    "note": frozenset(
        {"note", "notes", "notiz", "description", "beschreibung", "comment", "kommentar"}
    ),
    # Suggested only when a sampled cell names a known method (_best_method_column).
    "positioning_method": frozenset(
        {
            "source",
            "provider",
            "method",
            "positioning",
            "positioningmethod",
            "fix",
            "fixtype",
            "ortungsart",
            "quelle",
            "locationsource",
        }
    ),
}

TIME_OF_DAY_HEADERS = frozenset({"time", "zeit", "uhrzeit", "timeofday", "tageszeit"})
# Header words (header_tokens) that name an axis, a date, a time or the start/end of a
# span; a role from them is suggested only when the sampled cells prove it.
X_AXIS_WORDS: dict[str, str] = {
    "x": "the x axis",
    "ost": "the east value",
    "east": "the east value",
    "easting": "the east value",
    "rechtswert": "the east value",
    "lon": "the longitude",
    "lng": "the longitude",
    "long": "the longitude",
    "longitude": "the longitude",
    "länge": "the longitude",
    "laenge": "the longitude",
    "längengrad": "the longitude",
    "laengengrad": "the longitude",
}
Y_AXIS_WORDS: dict[str, str] = {
    "y": "the y axis",
    "nord": "the north value",
    "north": "the north value",
    "northing": "the north value",
    "hochwert": "the north value",
    "lat": "the latitude",
    "latitude": "the latitude",
    "breite": "the latitude",
    "breitengrad": "the latitude",
}
# x and y say which axis by convention only: such a pair has to be confirmed.
AXIS_CONVENTION_WORDS = frozenset({"x", "y"})
COORDINATE_WORDS = frozenset({"koordinate", "koordinaten", "coordinate", "coordinates", "coord"})
DATE_WORDS = frozenset({"datum", "date"})
TIME_WORDS = frozenset({"uhrzeit", "zeit", "time", "timestamp", "zeitstempel", "zeitpunkt"})
START_WORDS = frozenset({"anfang", "start", "beginn", "begin", "von", "from"})
END_WORDS = frozenset({"ende", "end", "bis", "to"})
HEADER_VOCABULARY = (
    frozenset(X_AXIS_WORDS)
    | frozenset(Y_AXIS_WORDS)
    | COORDINATE_WORDS
    | DATE_WORDS
    | TIME_WORDS
    | START_WORDS
    | END_WORDS
)
# A projected easting or northing (metres) lies far beyond any angle in degrees.
PROJECTED_MINIMUM_VALUE = 180.0
EXCEL_HEADER_MARK = "excel"
# "utc" as a word of a time header ("Zeit (UTC)"), or an offset the header names
# ("Zeit (UTC+2)", "GMT-5:30"): both are hints the examiner confirms, never a default.
UTC_HEADER_TOKEN = re.compile(r"(?<![a-z0-9])utc(?![a-z0-9+\-])", re.IGNORECASE)
OFFSET_HEADER_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?(?![0-9])", re.IGNORECASE
)
TIME_COLUMN_ROLES = ("timestamp", "time_of_day", "timestamp_end", "time_of_day_end")
# Rows of a date column paired with a time-of-day column.
DATE_MISSING_REASON = "date missing"
TIME_OF_DAY_MISSING_REASON = "time of day missing"

# "latitudeE7", "lat_e7", "Latitude (E7)": E7/E6 set off from a name that is itself a
# latitude or longitude synonym; "lat_phone7" or "Breite6" are ordinary names.
SCALED_HEADER_PATTERN = re.compile(
    r"(?P<name>.*?)(?:[_\s]+[Ee]|\s*[(\[]\s*[Ee]|(?<=[a-zäöü])E)(?P<exponent>[67])\s*[)\]]?\s*"
)
# Axis words in the raw header of a position column; the first named comes first.
LATITUDE_WORD_PATTERN = re.compile(r"lat|breite|(?<![a-zäöü])y(?![a-zäöü])")
LONGITUDE_WORD_PATTERN = re.compile(r"lon|lng|länge|laenge|(?<![a-zäöü])x(?![a-zäöü])")
DATE_ORDER_FIELDS = {".": "date_order_dot", "/": "date_order_slash", "-": "date_order_dash"}
# Header words: split at anything but letters and digits and where a small letter meets
# a capital ("StartDate").
HEADER_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])")
LONE_DECIMAL_COMMA_PATTERN = re.compile(r"[+-]?\d+,\d+", re.ASCII)
COMMA_PAIR_PATTERN = re.compile(r"[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?", re.ASCII)
HEADER_UNIT_PATTERN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
DECIMAL_PATTERN = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)", re.ASCII)
DECIMAL_COMMA_PATTERN = re.compile(r"[+-]?\d+,\d+", re.ASCII)
UNIX_TIME_PATTERN = re.compile(r"\d+(?:\.\d+)?", re.ASCII)
DAY_MONTH_YEAR_PATTERN = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", re.ASCII
)
MONTH_DAY_YEAR_PATTERN = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AaPp][Mm]))?", re.ASCII
)
YEAR_MONTH_DAY_PATTERN = re.compile(
    r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?", re.ASCII
)
ISO_NAIVE_PATTERN = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?", re.ASCII
)
ISO_WITH_ZONE_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:?\d{2})", re.ASCII
)
MISSING_ZONE_REASON = "time without offset needs a time zone"
NO_TIME_COLUMN_ASSUMPTION = (
    "no time column: records are read without timestamps; time-based features will be unavailable"
)
# Accuracy header words that may name a cell sector radius instead.
RADIUS_ACCURACY_WORDS = ("radius", "range", "reichweite")
RADIUS_ACCURACY_ASSUMPTION = (
    "column '{column}' used as accuracy: confirm it is a location accuracy, not a cell sector"
    " radius"
)
NO_TIMESTAMP_NOTE = tool_note("no timestamp in the record")
AMBIGUOUS_TIME_NOTE = tool_note("ambiguous local time, earlier occurrence used")
NOTE_SEPARATOR = NOTE_PART_SEPARATOR
# A quoted field still open after this many physical lines is taken as unterminated.
MAX_RECORD_LINES = 50
CSV_FIELD_SIZE_LIMIT = 16 * 1_048_576
# Present in some zoneinfo directories but not zones one could record.
NON_ZONE_NAMES = frozenset({"localtime", "posixrules"})
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CsvFormatError(SourceFormatError):
    """The CSV cannot be read with its mapping: missing columns, no mapping, broken CSV."""


@dataclass(frozen=True, slots=True)
class CsvMapping:
    """Column labels per role ("" = unused), time format key, zone for naive times."""

    delimiter: str
    latitude: str = ""
    longitude: str = ""
    position: str = ""
    timestamp: str = ""
    accuracy: str = ""
    label: str = ""
    note: str = ""
    time_format: str = ""
    zone: str = ""
    time_of_day: str = ""
    # End of a span (optional): the instant goes into the note, not into the point.
    timestamp_end: str = ""
    time_of_day_end: str = ""
    # "day_first" or "month_first" for dates like 03/04/2026 under the automatic format,
    # one per separator mark: 21.09.2026 says nothing about 09/05/2026.
    date_order_dot: str = ""
    date_order_slash: str = ""
    date_order_dash: str = ""
    # Order of two plain angles in the position column: "lat_lon" or "lon_lat".
    position_order: str = LAT_LON
    positioning_method: str = ""

    @property
    def date_orders(self) -> dict[str, str]:
        """Chosen day/month order by separator mark."""
        return {
            mark: getattr(self, field_name)
            for mark, field_name in DATE_ORDER_FIELDS.items()
            if getattr(self, field_name)
        }

    def to_document(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, str]) -> CsvMapping:
        """Raises ValueError for unknown keys, non-text values or a bad delimiter. A
        single ``date_order`` key of an older mapping document is applied to every mark."""
        document = dict(document)
        legacy_date_order = document.pop("date_order", None)
        if isinstance(legacy_date_order, str):
            for field_name in DATE_ORDER_FIELDS.values():
                document.setdefault(field_name, legacy_date_order)
        known_keys = {mapping_field.name for mapping_field in fields(cls)}
        unknown_keys = sorted(set(document) - known_keys)
        if unknown_keys:
            raise ValueError(f"unknown CSV mapping key(s): {', '.join(unknown_keys)}")
        for key, value in document.items():
            if not isinstance(value, str):
                raise ValueError(f"CSV mapping value for '{key}' is not text")
        delimiter = document.get("delimiter")
        if delimiter is None or len(delimiter) != 1 or delimiter not in DELIMITER_CANDIDATES:
            raise ValueError(f"unsupported CSV delimiter: {delimiter!r}")
        return cls(**document)


@dataclass(frozen=True, slots=True)
class CsvPreviewRow:
    line_number: int
    cells: list[str]
    original_text: str


@dataclass(frozen=True, slots=True)
class CsvPreview:
    """``rows`` are shown in the dialog; ``evidence_rows`` (the same and those after
    them in the sample) back the suggested mapping."""

    encoding: str
    delimiter: str
    header: list[str]
    rows: list[CsvPreviewRow]
    evidence_rows: list[CsvPreviewRow] = field(default_factory=list)
    # The header line as read (for the column count per delimiter candidate).
    header_text: str = ""

    def column_samples(self, column_label: str) -> list[str]:
        """Non-empty cells of a column in the evidence rows."""
        labels = column_labels(self.header)
        if column_label not in labels:
            return []
        column_index = labels.index(column_label)
        return [
            row.cells[column_index].strip()
            for row in self.evidence_rows or self.rows
            if column_index < len(row.cells) and row.cells[column_index].strip()
        ]


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    latitude: float
    longitude: float
    # None: an undated record (no time column mapped, or the mapped cells are empty).
    timestamp_utc: datetime | None
    accuracy_m: float | None
    label: str | None
    note: str | None
    naive_time: bool
    time_ambiguity_seconds: float = 0.0
    positioning_method: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedTimestamp:
    """A read time: the UTC instant, whether the text carried no offset (``naive``, read
    in the mapping's zone) and whether that wall time occurred twice (``ambiguous``,
    earlier occurrence used)."""

    instant: datetime
    naive: bool
    ambiguous: bool
    # How much later the second occurrence of an ambiguous wall time lies (0.0 otherwise).
    ambiguity_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class HeaderZoneHint:
    """What a time header says about the zone: "UTC" as a word, or an offset such as
    UTC+2 (``offset_text``), with the fixed IANA zone that has it (``zone``: "UTC",
    "Etc/GMT-2" for UTC+2, "" when no fixed zone exists, as for UTC-5:30)."""

    header: str
    offset_text: str
    zone: str


@dataclass(frozen=True, slots=True)
class _CsvRecord:
    """One logical row: its cells, or the reason it could not be split into cells."""

    line_number: int
    cells: list[str] | None
    problem: str | None
    original_text: str


def read_csv_preview(
    path: Path,
    fallback_encoding: str,
    row_limit: int = PREVIEW_ROW_COUNT,
    delimiter: str | None = None,
) -> CsvPreview:
    """Header and the first non-empty data rows from the first 64 KB of the file; the
    delimiter is sniffed unless one is given."""
    with path.open("rb") as source_file:
        sample = source_file.read(PREVIEW_SAMPLE_BYTES)
        whole_file = not source_file.read(1)
    encoding = detect_encoding(sample, fallback_encoding)
    text = codecs.getincrementaldecoder(encoding)("replace").decode(sample, final=whole_file)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not whole_file and len(lines) > 1:
        lines.pop()  # possibly cut in the middle
    if delimiter is None:
        delimiter = sniff_delimiter(lines[:SNIFF_LINE_COUNT])
    header: list[str] = []
    header_text = ""
    evidence_rows: list[CsvPreviewRow] = []
    for record in _csv_records(iter(lines), delimiter):
        if record.cells is None or _is_blank(record.cells):
            continue
        if not header:
            header = [cell.strip() for cell in record.cells]
            header_text = record.original_text
            continue
        evidence_rows.append(CsvPreviewRow(record.line_number, record.cells, record.original_text))
        if len(evidence_rows) >= max(row_limit, EVIDENCE_ROW_COUNT):
            break
    return CsvPreview(
        encoding, delimiter, header, evidence_rows[:row_limit], evidence_rows, header_text
    )


def sniff_delimiter(lines: list[str]) -> str:
    """csv.Sniffer over the sample; the most frequent candidate in the header otherwise."""
    sample = "\n".join(line for line in lines if line.strip())
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITER_CANDIDATES).delimiter
    except csv.Error:
        header_line = next((line for line in lines if line.strip()), "")
        counts = {candidate: header_line.count(candidate) for candidate in DELIMITER_CANDIDATES}
        best = max(counts, key=lambda candidate: counts[candidate])
        return best if counts[best] else ","


def normalised_header(header_cell: str) -> str:
    """Casefolded letters and digits without bracketed units, in Unicode NFC (a header
    written in NFD, as macOS does, matches the same synonym)."""
    composed = unicodedata.normalize("NFC", header_cell)
    stripped = HEADER_UNIT_PATTERN.sub("", composed).casefold()
    return "".join(character for character in stripped if character.isalnum())


def header_tokens(header_cell: str) -> frozenset[str]:
    """The words of a header, casefolded and in NFC: "X coordinate" gives x and
    coordinate, "StartDate" start and date. A word made of two vocabulary words, joined
    directly or by a German linking s ("Enddatum", "Anfangszeit"), gives both."""
    composed = unicodedata.normalize("NFC", header_cell)
    spaced = CAMEL_CASE_BOUNDARY.sub(" ", composed)
    tokens: set[str] = set()
    for word in HEADER_WORD_PATTERN.findall(spaced):
        folded = word.casefold()
        tokens.add(folded)
        if folded not in HEADER_VOCABULARY:
            tokens.update(_compound_parts(folded))
    return frozenset(tokens)


def _compound_parts(word: str) -> tuple[str, ...]:
    for split_at in range(1, len(word)):
        head, tail = word[:split_at], word[split_at:]
        if head not in HEADER_VOCABULARY and head.endswith("s"):
            head = head[:-1]
        if head in HEADER_VOCABULARY and tail in HEADER_VOCABULARY:
            return head, tail
    return ()


def column_labels(header: list[str]) -> list[str]:
    """Unique column labels: the name, or "name (column n)" for repeated or empty names."""
    name_counts = Counter(header)
    labels = []
    for column_number, name in enumerate(header, start=1):
        if not name:
            labels.append(f"(column {column_number})")
        elif name_counts[name] > 1:
            labels.append(f"{name} (column {column_number})")
        else:
            labels.append(name)
    return labels


def canonical_zone_name(zone_name: str) -> str | None:
    """The IANA zone name matching ``zone_name`` exactly or case-insensitively, or None."""
    return _zone_names_by_folded_name().get(zone_name.strip().casefold())


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    """A suggested mapping with its evidence. ``reasons``: one sentence per chosen
    CsvMapping field (roles, time_format, zone, delimiter, date orders), built from the
    header text and the sample counts. ``column_reasons``: every column label, with a
    short reason for the columns the suggestion used or recognised ("" otherwise).
    ``delimiter_counts``: columns the header line has under each candidate delimiter
    that occurs in it.
    ``date_marks_in_use``: separator marks of sampled dates that need a day/month order.
    ``projected_pairs``: column pairs holding projected coordinates, never suggested."""

    mapping: CsvMapping
    reasons: dict[str, str]
    column_reasons: dict[str, str]
    delimiter_counts: dict[str, int]
    date_marks_in_use: frozenset[str]
    projected_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ColumnRoles:
    """Roles found by one detection step, the reason per role and the short reason per
    column; ``projected_pairs`` names column pairs that hold metres, not degrees."""

    roles: dict[str, str]
    reasons: dict[str, str]
    column_reasons: dict[str, str]
    projected_pairs: tuple[tuple[str, str], ...] = ()


def suggest_mapping(preview: CsvPreview, default_zone: str) -> CsvMapping:
    """The mapping of suggest_mapping_with_reasons."""
    return suggest_mapping_with_reasons(preview, default_zone).mapping


def suggest_mapping_with_reasons(preview: CsvPreview, default_zone: str) -> MappingSuggestion:
    """Roles from header synonyms (never a repeated name), then from header words
    proven by the sampled cells (axis words with degrees, date and time words with dates
    and times of day, start and end qualifiers), then from cells that prove what they
    are; the time format and the day/month order from the sampled values; the zone: UTC
    when the time header says so (an assumption to confirm), else ``default_zone``
    (may be empty). Every choice comes with its reason."""
    name_counts = Counter(preview.header)
    unique_headers = [name for name in preview.header if name and name_counts[name] == 1]
    chosen: dict[str, str] = {}
    reasons: dict[str, str] = {}
    column_reasons = dict.fromkeys(column_labels(preview.header), "")

    def adopt(found: ColumnRoles) -> None:
        chosen.update(found.roles)
        reasons.update(found.reasons)
        column_reasons.update(found.column_reasons)

    adopt(_roles_by_synonym(unique_headers))
    first_method_column = chosen.pop("positioning_method", None)
    if first_method_column:
        reasons.pop("positioning_method")
        column_reasons[first_method_column] = ""
    method_column = _best_method_column(preview, unique_headers)
    if method_column:
        chosen["positioning_method"] = method_column
        reasons["positioning_method"] = (
            f"header '{method_column}' names the positioning method and"
            f" {_known_method_count(preview, method_column)} of"
            f" {len(preview.column_samples(method_column))} sampled cells name a known one"
        )
        column_reasons[method_column] = "positioning method (header)"
    if "latitude" in chosen and "longitude" in chosen:
        chosen.pop("position", None)
        reasons.pop("position", None)
    coordinates = coordinate_columns_by_header_and_content(preview, unique_headers)
    if not ("position" in chosen or ("latitude" in chosen and "longitude" in chosen)):
        adopt(coordinates)
        if not ("latitude" in chosen and "longitude" in chosen):
            adopt(_coordinate_roles_by_content(preview, unique_headers, chosen))
    for (x_column, y_column), reason in zip(
        coordinates.projected_pairs, coordinates.projected_reasons(), strict=True
    ):
        for column in (x_column, y_column):
            if column not in chosen.values():
                column_reasons[column] = "projected metres, zone not stated"
        reasons.setdefault("projected", reason)
    times = time_columns_by_header_and_content(preview, unique_headers)
    if "timestamp" in times.roles:
        for role in ("timestamp", "time_of_day"):
            chosen.pop(role, None)
        adopt(times)
    elif "timestamp" in chosen and (
        time_category(preview.column_samples(chosen["timestamp"])) == TIME_CATEGORY_TIME
    ):
        # A synonym such as "Zeit" over cells that are times of day alone is not a timestamp.
        column_reasons[chosen.pop("timestamp")] = ""
        reasons.pop("timestamp")
    if "timestamp" not in chosen:
        adopt(_time_roles_by_content(preview, unique_headers, chosen))
    timestamp_column = chosen.get("timestamp", "")
    time_format = ""
    date_order_fields: dict[str, str] = {}
    date_marks_in_use: set[str] = set()
    if timestamp_column:
        samples = _timestamp_samples(preview, timestamp_column, chosen.get("time_of_day", ""))
        if EXCEL_HEADER_MARK in normalised_header(timestamp_column):
            time_format = EXCEL_SERIAL_TIME_FORMAT
            reasons["time_format"] = f"header '{timestamp_column}' says Excel"
        else:
            time_format = detect_time_format(samples)
            if time_format:
                reasons["time_format"] = _time_format_reason(samples)
        if time_format == AUTOMATIC_TIME_FORMAT:
            for mark, evidence in date_order_evidence(samples).items():
                date_marks_in_use.add(mark)
                if evidence.order:
                    date_order_fields[DATE_ORDER_FIELDS[mark]] = evidence.order
                    reasons[DATE_ORDER_FIELDS[mark]] = (
                        f"{evidence.order.replace('_', ' ')} for dates written with '{mark}':"
                        f" a number above 12 stands in that place in"
                        f" {evidence.rows_with_evidence} of {evidence.rows_needing_order}"
                        " sampled rows and never in the other"
                    )
            end_samples = _timestamp_samples(
                preview, chosen.get("timestamp_end", ""), chosen.get("time_of_day_end", "")
            )
            date_marks_in_use.update(date_order_evidence(end_samples))
    zone_hint = header_zone_hint(chosen.get(role, "") for role in TIME_COLUMN_ROLES)
    if zone_hint is None:
        zone = default_zone
        if default_zone:
            reasons["zone"] = f"{default_zone} is the default zone, not taken from the file"
    elif zone_hint.offset_text == "UTC":
        zone = "UTC"
        reasons["zone"] = f"UTC named in the header '{zone_hint.header}'"
    elif zone_hint.zone:
        zone = zone_hint.zone
        reasons["zone"] = (
            f"the header '{zone_hint.header}' names the offset {zone_hint.offset_text}: the"
            f" fixed zone {zone_hint.zone}"
        )
    else:
        # A default zone would apply another offset silently: none is suggested.
        zone = ""
        reasons["zone"] = (
            f"the header '{zone_hint.header}' names the offset {zone_hint.offset_text}, which"
            " has no fixed zone; none suggested"
        )
    delimiter_counts = _delimiter_counts(preview)
    other_counts = ", ".join(
        f"{DELIMITER_NAMES[candidate]} {count}"
        for candidate, count in delimiter_counts.items()
        if candidate != preview.delimiter
    )
    reasons["delimiter"] = (
        f"{DELIMITER_NAMES.get(preview.delimiter, repr(preview.delimiter))} splits the header"
        f" line into {delimiter_counts.get(preview.delimiter, 1)} column(s)"
        + (f"; other candidates: {other_counts}" if other_counts else "")
    )
    mapping = CsvMapping(
        delimiter=preview.delimiter,
        time_format=time_format,
        zone=zone,
        position_order=_header_position_order(chosen.get("position", "")) or LAT_LON,
        **date_order_fields,
        **chosen,
    )
    return MappingSuggestion(
        mapping,
        reasons,
        column_reasons,
        delimiter_counts,
        frozenset(date_marks_in_use),
        coordinates.projected_pairs,
    )


DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


def header_zone_hint(time_columns: Iterable[str]) -> HeaderZoneHint | None:
    """The first time header that names UTC as a word or an offset (UTC+2, GMT-5:30),
    or None. Pure."""
    for header in time_columns:
        if not header:
            continue
        offset = OFFSET_HEADER_PATTERN.search(header)
        if offset is not None:
            sign, hours, minutes = offset[1], int(offset[2]), int(offset[3] or 0)
            offset_text = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
            return HeaderZoneHint(header, offset_text, _fixed_offset_zone(sign, hours, minutes))
        if UTC_HEADER_TOKEN.search(header):
            return HeaderZoneHint(header, "UTC", "UTC")
    return None


def _fixed_offset_zone(sign: str, hours: int, minutes: int) -> str:
    """ "UTC" for no offset, the Etc/GMT zone with that whole-hour offset (POSIX inverts
    the sign: Etc/GMT-2 is UTC+2), "" when no such zone exists."""
    if hours == 0 and minutes == 0:
        return "UTC"
    if minutes:
        return ""
    candidate = f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
    return candidate if candidate in available_timezones() else ""


def _delimiter_counts(preview: CsvPreview) -> dict[str, int]:
    """Columns the header line has under each candidate delimiter that occurs in it
    (quotes ignored)."""
    header_text = preview.header_text or preview.delimiter.join(preview.header)
    return {
        candidate: header_text.count(candidate) + 1
        for candidate in DELIMITER_CANDIDATES
        if candidate in header_text
    }


def _time_format_reason(samples: list[str]) -> str:
    first_read = next(
        (
            sample
            for sample in samples
            if sample.strip() and not _refused_outright(recognise_timestamp(sample, {}))
        ),
        "",
    )
    return f"per-cell recognition reads sampled values such as '{excerpt(first_read)}'"


def _refused_outright(recognised: object) -> bool:
    """A refusal other than a date waiting for its day/month order or a time outside the
    plausible window (the column still holds times; its rows are refused one by one)."""
    return isinstance(recognised, str) and not recognised.startswith(
        (DATE_ORDER_NOT_CHOSEN_REASON, IMPLAUSIBLE_TIME_REASON)
    )


def _synonym_header(header_cell: str) -> str:
    """The header as compared with the synonyms: normalised, without the E7/E6 mark and
    the Excel mark ("Source_Excel" reads "source")."""
    return normalised_header(_scaled_header_name(header_cell)).replace(EXCEL_HEADER_MARK, "")


def _roles_by_synonym(unique_headers: list[str]) -> ColumnRoles:
    roles: dict[str, str] = {}
    reasons: dict[str, str] = {}
    column_reasons: dict[str, str] = {}
    for header_cell in unique_headers:
        normalised = _synonym_header(header_cell)
        for role, synonyms in HEADER_SYNONYMS.items():
            if role not in roles and normalised in synonyms:
                roles[role] = header_cell
                reasons[role] = f"header '{header_cell}' names the {role}"
                column_reasons[header_cell] = f"{role} (header)"
                break
    return ColumnRoles(roles, reasons, column_reasons)


def method_columns(header: list[str]) -> list[str]:
    """Labels of the columns whose header is a positioning method synonym, in file order."""
    return [
        label
        for name, label in zip(header, column_labels(header), strict=True)
        if _synonym_header(name) in HEADER_SYNONYMS["positioning_method"]
    ]


def _known_method_count(preview: CsvPreview, column: str) -> int:
    return sum(
        positioning_method_from_text(sample) is not None
        for sample in preview.column_samples(column)
    )


def _best_method_column(preview: CsvPreview, unique_headers: list[str]) -> str | None:
    """The method-named column with most sampled cells naming a known method (the first
    on a tie); None when no sampled cell does ("source" often names the file or app)."""
    known_counts = {
        column: _known_method_count(preview, column)
        for column in method_columns(preview.header)
        if column in unique_headers
    }
    best_column = max(known_counts, key=known_counts.__getitem__, default=None)
    return best_column if best_column is not None and known_counts[best_column] else None


def _methods_disagree(preview: CsvPreview, first_column: str, second_column: str) -> bool:
    """True when a sampled row names a known method in both columns and they differ."""
    labels = column_labels(preview.header)
    first_index, second_index = labels.index(first_column), labels.index(second_column)
    for row in preview.evidence_rows or preview.rows:
        if max(first_index, second_index) >= len(row.cells):
            continue
        first_method = positioning_method_from_text(row.cells[first_index].strip())
        second_method = positioning_method_from_text(row.cells[second_index].strip())
        if first_method and second_method and first_method != second_method:
            return True
    return False


def scaled_exponent(header_cell: str) -> int | None:
    """7 or 6 when the header marks integers scaled by 1e7 or 1e6, else None."""
    match = SCALED_HEADER_PATTERN.fullmatch(header_cell.strip())
    if match is None:
        return None
    name = normalised_header(match["name"])
    if name in HEADER_SYNONYMS["latitude"] or name in HEADER_SYNONYMS["longitude"]:
        return int(match["exponent"])
    return None


def _scaled_header_name(header_cell: str) -> str:
    """The header without its E7/E6 mark."""
    match = SCALED_HEADER_PATTERN.fullmatch(header_cell.strip())
    return match["name"] if match is not None and scaled_exponent(header_cell) else header_cell


def _header_position_order(header_cell: str) -> str | None:
    """LON_LAT or LAT_LON when the raw header (bracketed parts included) names both axes,
    by the one named first; None when it does not say."""
    folded = header_cell.casefold()
    latitude_word = LATITUDE_WORD_PATTERN.search(folded)
    longitude_word = LONGITUDE_WORD_PATTERN.search(folded)
    if latitude_word is None or longitude_word is None:
        return None
    return LAT_LON if latitude_word.start() < longitude_word.start() else LON_LAT


def unconfirmed_assumptions(preview: CsvPreview, mapping: CsvMapping) -> list[str]:
    """What this mapping takes for granted without proof in the file. A suggestion with
    such assumptions is not applied on its own: the examiner confirms it in the dialog,
    and without one the file is refused."""
    assumptions = []
    labels = column_labels(preview.header)
    if not mapping.timestamp:
        assumptions.append(NO_TIME_COLUMN_ASSUMPTION)
    if mapping.position in labels and not (mapping.latitude and mapping.longitude):
        header_cell = preview.header[labels.index(mapping.position)]
        samples = preview.column_samples(mapping.position)
        if _header_position_order(header_cell) is None and not all(
            _order_is_in_the_cell(sample) for sample in samples
        ):
            assumption = (
                f"position column '{mapping.position}' is read as"
                f" {POSITION_ORDER_LABELS.get(mapping.position_order, mapping.position_order)}:"
                " neither its header nor hemisphere letters in every row prove that order"
            )
            if _found_by_comma_pairs(preview, mapping.position):
                assumption += (
                    "; it was taken as a position column because every sampled cell splits"
                    " at a comma"
                )
            assumptions.append(assumption)
    if mapping.latitude and mapping.longitude:
        axis_words = {
            _axis_word(mapping.longitude, X_AXIS_WORDS),
            _axis_word(mapping.latitude, Y_AXIS_WORDS),
        }
        if axis_words == AXIS_CONVENTION_WORDS:
            assumptions.append(
                f"columns '{mapping.longitude}' and '{mapping.latitude}' are read as longitude"
                " and latitude by convention only"
            )
    if mapping.timestamp and mapping.time_of_day:
        named = [
            column
            for column in (mapping.timestamp, mapping.time_of_day)
            if header_tokens(column) & (DATE_WORDS | TIME_WORDS)
        ]
        if not named:
            assumptions.append(
                f"columns '{mapping.timestamp}' and '{mapping.time_of_day}' are read as date"
                " and time of day by their content alone: neither header says so"
            )
    zone_hint = header_zone_hint(getattr(mapping, role) for role in TIME_COLUMN_ROLES)
    if zone_hint is not None:
        chosen_zone = canonical_zone_name(mapping.zone) if mapping.zone else ""
        if zone_hint.zone and chosen_zone == zone_hint.zone:
            zone_named = (
                "UTC"
                if zone_hint.offset_text == "UTC"
                else f"{zone_hint.zone} (the fixed offset {zone_hint.offset_text})"
            )
            assumptions.append(
                f"time zone {zone_named} is taken from the header '{zone_hint.header}': the cells"
                " carry no offset that proves it"
            )
        elif chosen_zone:
            assumptions.append(
                f"the header '{zone_hint.header}' names the offset {zone_hint.offset_text};"
                f" the chosen zone {chosen_zone} has another offset on some dates"
            )
        else:
            assumptions.append(
                f"the header '{zone_hint.header}' names the offset {zone_hint.offset_text};"
                " no zone is chosen"
            )
    if mapping.accuracy in labels:
        accuracy_header = preview.header[labels.index(mapping.accuracy)]
        if any(word in normalised_header(accuracy_header) for word in RADIUS_ACCURACY_WORDS):
            assumptions.append(RADIUS_ACCURACY_ASSUMPTION.format(column=mapping.accuracy))
    if mapping.positioning_method in labels:
        for other_column in method_columns(preview.header):
            if other_column != mapping.positioning_method and _methods_disagree(
                preview, mapping.positioning_method, other_column
            ):
                assumptions.append(
                    f"columns '{mapping.positioning_method}' and '{other_column}' name different"
                    f" positioning methods; '{mapping.positioning_method}' is used"
                )
    if mapping.time_format == AUTOMATIC_TIME_FORMAT:
        samples = _timestamp_samples(preview, mapping.timestamp, mapping.time_of_day)
        for mark, evidence in date_order_evidence(samples).items():
            if evidence.weak and mapping.date_orders.get(mark) == evidence.order:
                assumptions.append(
                    f"{evidence.order.replace('_', ' ')} for dates written with '{mark}' rests"
                    f" on {evidence.rows_with_evidence} of {evidence.rows_needing_order}"
                    " sampled rows: a single mistyped date could have caused it"
                )
    return assumptions


def _order_is_in_the_cell(position_text: str) -> bool:
    """True when the cell reads the same whatever the column's order: hemisphere
    letters, or a notation with an order of its own (WKT, geo URI, UTM, MGRS, URL)."""
    readings = [parse_position(position_text, order) for order in (LAT_LON, LON_LAT)]
    refusals = [reading for reading in readings if isinstance(reading, str)]
    if any("hemisphere letters give the order" in refusal for refusal in refusals):
        return True
    # A cell refused under both orders is refused at reading as well: nothing is assumed.
    return len(refusals) == 2 or readings[0] == readings[1]


def _found_by_comma_pairs(preview: CsvPreview, column: str) -> bool:
    """A column no header names as a position, whose sampled cells are all plain pairs
    split at a comma ("51.4361,6.9087")."""
    header_names_position = normalised_header(column) in HEADER_SYNONYMS["position"] or (
        _header_position_order(column) is not None
    )
    samples = preview.column_samples(column)
    return (
        not header_names_position
        and bool(samples)
        and all(COMMA_PAIR_PATTERN.fullmatch(sample) for sample in samples)
    )


def _axis_word(header_cell: str, axis_words: Mapping[str, str]) -> str | None:
    """The one word of the header naming this axis, None when it names none or both."""
    tokens = header_tokens(header_cell)
    own = tokens & frozenset(axis_words)
    other = tokens & (frozenset(X_AXIS_WORDS) | frozenset(Y_AXIS_WORDS)) - own
    if len(own) != 1 or other:
        return None
    return next(iter(own))


def _all_read(preview: CsvPreview, column: str, parse: Callable[[str], object]) -> bool:
    samples = preview.column_samples(column)
    return bool(samples) and not any(isinstance(parse(sample), str) for sample in samples)


def _all_projected(preview: CsvPreview, column: str) -> bool:
    """Every sampled cell is a number far beyond the range of degrees (metres)."""
    samples = preview.column_samples(column)
    if not samples:
        return False
    for sample in samples:
        number = _decimal(sample)
        if number is None or not math.isfinite(number) or abs(number) < PROJECTED_MINIMUM_VALUE:
            return False
    return True


@dataclass(frozen=True, slots=True)
class CoordinateColumns(ColumnRoles):
    """ColumnRoles of the axis-word step, with the projected pairs' reasons."""

    def projected_reasons(self) -> list[str]:
        return [
            f"'{x_column}' and '{y_column}' hold projected coordinates (UTM or Gauß-Krüger"
            " metres, zone not stated): every sampled value lies far beyond 180"
            for x_column, y_column in self.projected_pairs
        ]


def coordinate_columns_by_header_and_content(
    preview: CsvPreview, unique_headers: list[str]
) -> CoordinateColumns:
    """Latitude and longitude from pairs of headers naming the axes (x/y, east/north,
    easting/northing, Rechtswert/Hochwert, lat/lon words), paired by their other words
    ("X coordinate" with "Y coordinate"). The first pair whose sampled cells all read as
    degrees is suggested; pairs whose values are metres are reported as projected
    coordinates without a stated zone. Pure: reads the preview only."""
    x_candidates = [
        (name, word) for name in unique_headers if (word := _axis_word(name, X_AXIS_WORDS))
    ]
    y_candidates = [
        (name, word) for name in unique_headers if (word := _axis_word(name, Y_AXIS_WORDS))
    ]
    pairs: list[tuple[str, str, str, str]] = []
    if len(x_candidates) == 1 and len(y_candidates) == 1:
        pairs.append((*x_candidates[0], *y_candidates[0]))
    else:
        for x_name, x_word in x_candidates:
            x_rest = header_tokens(x_name) - {x_word}
            matching = [
                (y_name, y_word)
                for y_name, y_word in y_candidates
                if header_tokens(y_name) - {y_word} == x_rest
            ]
            if len(matching) == 1:
                pairs.append((x_name, x_word, *matching[0]))
    roles: dict[str, str] = {}
    reasons: dict[str, str] = {}
    column_reasons: dict[str, str] = {}
    projected_pairs: list[tuple[str, str]] = []
    for x_name, x_word, y_name, y_word in pairs:
        if (
            not roles
            and _all_read(preview, x_name, parse_longitude)
            and _all_read(preview, y_name, parse_latitude)
        ):
            roles = {"latitude": y_name, "longitude": x_name}
            # x and y by convention only, and both columns readable on both axes: the
            # cells cannot tell which is which.
            both_ways = (
                {x_word, y_word} == AXIS_CONVENTION_WORDS
                and _all_read(preview, x_name, parse_latitude)
                and _all_read(preview, y_name, parse_longitude)
            )
            for role, name, word, meaning in (
                ("latitude", y_name, y_word, Y_AXIS_WORDS[y_word]),
                ("longitude", x_name, x_word, X_AXIS_WORDS[x_word]),
            ):
                count = len(preview.column_samples(name))
                reasons[role] = (
                    f"header '{name}' names {meaning} and all {count} sampled cells read as {role}s"
                    + (" (the values would also read the other way round)" if both_ways else "")
                )
                column_reasons[name] = f"{role} (header word '{word}', cells in degrees)"
        elif _all_projected(preview, x_name) and _all_projected(preview, y_name):
            projected_pairs.append((x_name, y_name))
    return CoordinateColumns(roles, reasons, column_reasons, tuple(projected_pairs))


def _coordinate_roles_by_content(
    preview: CsvPreview, unique_headers: list[str], chosen: dict[str, str]
) -> ColumnRoles:
    """Coordinate columns no header named: two columns whose hemisphere letters name
    their axis, or one column of positions."""

    def proves_axis(column: str, axis: str) -> bool:
        """Every cell reads on this axis and is refused on the other for its letter."""
        own, other = (
            (parse_latitude, parse_longitude)
            if axis == LATITUDE
            else (parse_longitude, parse_latitude)
        )
        samples = preview.column_samples(column)
        return _all_read(preview, column, own) and all(
            "letter" in str(other(sample)) for sample in samples
        )

    free_columns = [name for name in unique_headers if name not in chosen.values()]
    latitude_column = chosen.get("latitude") or next(
        (name for name in free_columns if proves_axis(name, LATITUDE)), None
    )
    longitude_column = chosen.get("longitude") or next(
        (name for name in free_columns if proves_axis(name, LONGITUDE)), None
    )
    if latitude_column and longitude_column:
        roles = {"latitude": latitude_column, "longitude": longitude_column}
        reasons = {
            role: (
                f"all {len(preview.column_samples(column))} sampled cells of '{column}' read as"
                f" {role}s with a hemisphere letter"
            )
            for role, column in roles.items()
            if role not in chosen
        }
        return ColumnRoles(
            roles,
            reasons,
            {column: f"{role} (hemisphere letters)" for role, column in roles.items()},
        )
    for name in free_columns:
        samples = preview.column_samples(name)
        # "12,5" alone is a decimal number, not the position 12° N 5° E.
        if any(LONE_DECIMAL_COMMA_PATTERN.fullmatch(sample) for sample in samples):
            continue
        if _all_read(preview, name, parse_position):
            reason = f"all {len(samples)} sampled cells of '{name}' read as positions"
            if _found_by_comma_pairs(preview, name):
                reason += " because every sampled cell splits at a comma"
            return ColumnRoles({"position": name}, {"position": reason}, {name: "position (cells)"})
    return ColumnRoles({}, {}, {})


TIME_CATEGORY_DATE, TIME_CATEGORY_TIME, TIME_CATEGORY_DATETIME = "date", "time", "datetime"
TIME_CATEGORY_TEXT = {
    TIME_CATEGORY_DATE: "dates without a time of day",
    TIME_CATEGORY_TIME: "times of day",
    TIME_CATEGORY_DATETIME: "timestamps",
}


def time_category(samples: list[str]) -> str | None:
    """What the sampled cells of a column are: "date" when every one is a date without
    a time of day, "time" when every one is a time of day, "datetime" when at least one
    reads as a timestamp (a date waiting for its day/month order counts) and none is a
    lone time of day; None otherwise or without samples. Pure."""
    if not samples:
        return None
    if all(not isinstance(parse_time_of_day(sample), str) for sample in samples):
        return TIME_CATEGORY_TIME
    readings = [recognise_timestamp(sample, {}) for sample in samples]
    if all(
        isinstance(reading, str) and reading.startswith("timestamp with no time of day")
        for reading in readings
    ):
        return TIME_CATEGORY_DATE
    if any(not _refused_outright(reading) for reading in readings) and not any(
        not isinstance(parse_time_of_day(sample), str) for sample in samples
    ):
        return TIME_CATEGORY_DATETIME
    return None


def time_columns_by_header_and_content(
    preview: CsvPreview, unique_headers: list[str]
) -> ColumnRoles:
    """Timestamp, time-of-day and end roles from headers with a date or time word
    (datum/date, uhrzeit/zeit/time/...) and a start or end qualifier (anfang/start/
    beginn/von/from, ende/end/bis/to), each proven by the sampled cells: a column of
    dates without time is the timestamp (or timestamp_end), a column of times of day
    the time_of_day (or time_of_day_end), a column of timestamps the timestamp. Of
    several candidates for a role the one qualified as start wins; two of the same rank
    leave the role open. A time of day is kept only next to a column of plain dates.
    Pure: reads the preview only."""
    candidates: dict[str, list[tuple[bool, str, str, str]]] = {}
    for name in unique_headers:
        tokens = header_tokens(name)
        names_date, names_time = bool(tokens & DATE_WORDS), bool(tokens & TIME_WORDS)
        if not (names_date or names_time):
            continue
        starts, ends = bool(tokens & START_WORDS), bool(tokens & END_WORDS)
        if starts and ends:
            continue
        samples = preview.column_samples(name)
        category = time_category(samples)
        if category is None:
            continue
        role = "time_of_day" if category == TIME_CATEGORY_TIME else "timestamp"
        role += "_end" if ends else ""
        qualifier = "an end" if ends else "a start" if starts else "a"
        named = "date and time" if names_date and names_time else "date" if names_date else "time"
        reason = (
            f"header '{name}' names {qualifier} {named} and the {len(samples)} sampled cells"
            f" are {TIME_CATEGORY_TEXT[category]}"
        )
        short = f"{role} (header words, cells are {TIME_CATEGORY_TEXT[category]})"
        candidates.setdefault(role, []).append((starts or ends, name, reason, short))
    roles: dict[str, str] = {}
    reasons: dict[str, str] = {}
    column_reasons: dict[str, str] = {}
    for role, found in candidates.items():
        qualified = [entry for entry in found if entry[0]]
        ranked = qualified or found
        if len(ranked) != 1:
            continue
        _, name, reason, short = ranked[0]
        roles[role] = name
        reasons[role] = reason
        column_reasons[name] = short
    for date_role, time_role in (
        ("timestamp", "time_of_day"),
        ("timestamp_end", "time_of_day_end"),
    ):
        date_column = roles.get(date_role)
        if time_role in roles and (
            date_column is None
            or time_category(preview.column_samples(date_column)) != TIME_CATEGORY_DATE
        ):
            time_column = roles.pop(time_role)
            if date_column is None:
                column_reasons.pop(time_column, None)
                reasons.pop(time_role)
            else:
                reasons[time_role] = (
                    f"column '{time_column}' holds times of day but the date column already"
                    " carries a time; not combined"
                )
                column_reasons[time_column] = "times of day, not combined"
    return ColumnRoles(roles, reasons, column_reasons)


def _time_roles_by_content(
    preview: CsvPreview, unique_headers: list[str], chosen: dict[str, str]
) -> ColumnRoles:
    """Date and time-of-day columns no header names: exactly one column of dates without
    time and exactly one of times of day among the free columns (an assumption)."""
    free_columns = [name for name in unique_headers if name not in chosen.values()]
    by_category: dict[str, list[str]] = {}
    for name in free_columns:
        category = time_category(preview.column_samples(name))
        if category in (TIME_CATEGORY_DATE, TIME_CATEGORY_TIME):
            by_category.setdefault(category, []).append(name)
    dates, times = by_category.get(TIME_CATEGORY_DATE, []), by_category.get(TIME_CATEGORY_TIME, [])
    if len(dates) != 1 or len(times) != 1:
        return ColumnRoles({}, {}, {})
    roles = {"timestamp": dates[0], "time_of_day": times[0]}
    reasons = {
        role: (
            f"no header names it; all {len(preview.column_samples(column))} sampled cells of"
            f" '{column}' are {TIME_CATEGORY_TEXT[category]}"
        )
        for (role, column), category in zip(
            roles.items(), (TIME_CATEGORY_DATE, TIME_CATEGORY_TIME), strict=True
        )
    }
    return ColumnRoles(
        roles, reasons, {column: f"{role} (cells)" for role, column in roles.items()}
    )


def _timestamp_samples(
    preview: CsvPreview, timestamp_column: str, time_of_day_column: str
) -> list[str]:
    labels = column_labels(preview.header)
    indexes = [
        labels.index(column)
        for column in (timestamp_column, time_of_day_column)
        if column in labels
    ]
    samples = []
    for row in preview.evidence_rows or preview.rows:
        cells = [row.cells[index].strip() for index in indexes if index < len(row.cells)]
        if time_of_day_column in labels and len(cells) == 2:
            cells[1] = time_of_day_text(cells[1])
        samples.append(" ".join(cells))
    return samples


def detect_time_format(samples: list[str]) -> str:
    """ "automatic" when the per-cell recognition reads a sample (a date waiting for the
    day/month order counts as read), else "": every fixed format is a subset of it, and
    plain numbers outside the plausible Unix range or Excel serial dates are never guessed."""
    for sample in samples:
        if not sample.strip():
            continue
        if not _refused_outright(recognise_timestamp(sample, {})):
            return AUTOMATIC_TIME_FORMAT
    return ""


def undecided_date_marks(preview: CsvPreview, mapping: CsvMapping) -> list[str]:
    """Separator marks of sampled dates like 03/04/2026 for which the automatic format
    has no day/month order in the mapping."""
    if mapping.time_format != AUTOMATIC_TIME_FORMAT:
        return []
    samples = _timestamp_samples(preview, mapping.timestamp, mapping.time_of_day)
    samples += _timestamp_samples(preview, mapping.timestamp_end, mapping.time_of_day_end)
    marks_needing_order = {needs_date_order(sample) for sample in samples}
    return [
        mark
        for mark in DATE_MARKS
        if mark in marks_needing_order and mark not in mapping.date_orders
    ]


def mapping_problems(header: list[str], mapping: CsvMapping) -> list[str]:
    """Why the mapping cannot be used with this header; empty when it can."""
    problems: list[str] = []
    has_pair = bool(mapping.latitude and mapping.longitude)
    if not has_pair and not mapping.position:
        problems.append("no latitude/longitude or position column chosen")
    # No timestamp column: every row is read undated; the other time roles, the time
    # format and the zone only matter with one.
    if not mapping.timestamp:
        for role in TIME_COLUMN_ROLES[1:]:
            column = getattr(mapping, role)
            if column:
                problems.append(f"column '{column}' ({role}) needs a timestamp column")
    elif not mapping.time_format:
        problems.append("no time format chosen")
    elif mapping.time_format not in TIME_FORMAT_LABELS:
        problems.append(f"unknown time format: {mapping.time_format}")
    elif (mapping.time_of_day or mapping.time_of_day_end) and (
        mapping.time_format in NUMBER_TIME_FORMATS
    ):
        problems.append(
            "a separate time-of-day column needs a date format, not "
            + TIME_FORMAT_LABELS[mapping.time_format]
        )
    for date_order in mapping.date_orders.values():
        if date_order not in DATE_ORDER_LABELS:
            problems.append(f"unknown day/month order: {date_order}")
    if mapping.position_order not in POSITION_ORDER_LABELS:
        problems.append(f"unknown position order: {mapping.position_order}")
    labels = column_labels(header)
    for role in COLUMN_ROLES:
        column = getattr(mapping, role)
        if not column or column in labels:
            continue
        if column in header:
            choices = " or ".join(
                f"'{label}'" for label, name in zip(labels, header, strict=True) if name == column
            )
            problems.append(
                f"column name '{column}' ({role}) occurs more than once; choose {choices}"
            )
        else:
            problems.append(f"column '{column}' ({role}) is not in the header")
    if mapping.zone:
        if canonical_zone_name(mapping.zone) is None:
            problems.append(f"unknown time zone: {mapping.zone}")
    elif mapping.timestamp and mapping.time_format in NAIVE_TIME_FORMATS:
        problems.append("times without offset need a time zone (e.g. Europe/Berlin or UTC)")
    if mapping.delimiter not in DELIMITER_CANDIDATES or len(mapping.delimiter) != 1:
        problems.append(f"unsupported delimiter: {mapping.delimiter!r}")
    return problems


def check_preview_rows(preview: CsvPreview, mapping: CsvMapping) -> list[str]:
    """One line per preview row: the parsed record or the rejection reason."""
    layout = _column_layout(preview.header, mapping)
    zone = _zone(mapping.zone)
    lines = []
    for row in preview.rows:
        parsed = _parse_row(row.cells, layout, mapping, zone, row.line_number)
        if isinstance(parsed, str):
            lines.append(f"line {row.line_number}: rejected ({parsed})")
            continue
        time_text = (
            "no timestamp"
            if parsed.timestamp_utc is None
            else "at " + parsed.timestamp_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        accuracy_text = "" if parsed.accuracy_m is None else f" ±{parsed.accuracy_m:g} m"
        lines.append(
            f"line {row.line_number}: {parsed.latitude}, {parsed.longitude}"
            f"{',' if parsed.timestamp_utc is None else ''} {time_text}{accuracy_text}"
        )
    return lines


def preview_problem(preview: CsvPreview, mapping: CsvMapping) -> str | None:
    """Why a structurally sound mapping still cannot be used with this file, or None: an
    open day/month order, or a preview in which no row can be read."""
    undecided_marks = undecided_date_marks(preview, mapping)
    if undecided_marks:
        marks_text = " and ".join(f"'{mark}'" for mark in undecided_marks)
        return (
            f"day/month order not chosen for dates written with {marks_text}: dates like"
            " 03/04/2026 read both ways and the sample does not settle it"
        )
    check_lines = check_preview_rows(preview, mapping)
    if check_lines and all(": rejected (" in line for line in check_lines):
        if not mapping.zone and any(MISSING_ZONE_REASON in line for line in check_lines):
            return "times without offset need a time zone (e.g. Europe/Berlin or UTC)"
        return "no preview row can be read with this mapping"
    return None


def parse_timestamp(text: str, time_format: str, zone_name: str) -> datetime | str:
    """The UTC instant of ``text`` in ``time_format``, or a rejection reason. Naive times
    are read in ``zone_name`` (earlier occurrence when ambiguous, refused when the local
    time does not exist)."""
    parsed = parse_timestamp_details(text, time_format, zone_name)
    return parsed if isinstance(parsed, str) else parsed.instant


def parse_timestamp_details(text: str, time_format: str, zone_name: str) -> ParsedTimestamp | str:
    """As parse_timestamp, with the naive and ambiguous flags of the reading (for a
    caller that notes an ambiguous local time as the CSV reader does)."""
    return _parse_timestamp(text.strip(), time_format, _zone(zone_name), {})


def extract_csv(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
    mapping_document: dict[str, str] | None = None,
) -> SourceExtractionOutcome:
    """Read every row once with the mapping (the suggested one when none is given).

    The record number is the line where the row starts. Blank rows count as unrelated.
    Raises CsvFormatError when there is no usable mapping or the header lacks a mapped
    column; the error then carries a digest of the whole file.
    """
    preview = read_csv_preview(source_path, settings.fallback_encoding)

    def refuse(message: str) -> CsvFormatError:
        read_report = whole_file_read_report(
            source_path, settings.read_chunk_bytes, preview.encoding
        )
        return CsvFormatError(
            message,
            SourceExtractionOutcome([], [], ExtractionCounters(), read_report, False),
        )

    if mapping_document is None:
        mapping = suggest_mapping(preview, default_zone="")
        problems = mapping_problems(preview.header, mapping)
        if problems:
            raise refuse(f"{source_path.name}: no usable column mapping ({'; '.join(problems)})")
        assumptions = unconfirmed_assumptions(preview, mapping)
        if assumptions:
            raise refuse(
                f"{source_path.name}: the suggested column mapping needs confirmation"
                f" ({'; '.join(assumptions)})"
            )
    else:
        try:
            mapping = CsvMapping.from_document(mapping_document)
        except ValueError as error:
            raise refuse(f"{source_path.name}: {error}") from error
        if mapping.delimiter != preview.delimiter:
            preview = read_csv_preview(
                source_path, settings.fallback_encoding, delimiter=mapping.delimiter
            )
        problems = mapping_problems(preview.header, mapping)
        if problems:
            raise refuse(f"{source_path.name}: mapping does not fit ({'; '.join(problems)})")
    zone = _zone(mapping.zone)
    # The encoding the preview decided (and the dialog showed) reads the whole file.
    reader = SourceReader(
        source_path, settings.read_chunk_bytes, settings.fallback_encoding, preview.encoding
    )
    points: list[GeoPoint] = []
    undated_records: list[ReferencePlace] = []
    counters = ExtractionCounters()
    cancelled = False
    last_reported_bytes = -1
    logger.info(
        "CSV extraction started: %s (%d bytes), mapping %s",
        source_path.name,
        reader.size_bytes,
        mapping.to_document(),
    )

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(
                ExtractionProgress(
                    bytes_read=reader.bytes_read,
                    size_bytes=reader.size_bytes,
                    accepted=counters.accepted,
                    invalid=counters.invalid,
                    unrelated=counters.unrelated,
                )
            )

    def outcome() -> SourceExtractionOutcome:
        counters.decode_replacements = reader.decode_replacements
        return SourceExtractionOutcome(
            points, undated_records, counters, reader.report(), cancelled
        )

    def reject(line_number: int, reason: str, original_text: str) -> None:
        counters.invalid += 1
        on_rejected(RejectedLine(line_number, reason, original_text))
        logger.warning("CSV line %d rejected: %s", line_number, reason)

    text_lines = (source_line.text for source_line in reader.lines())
    layout: _ColumnLayout | None = None
    for record in _csv_records(text_lines, mapping.delimiter):
        if record.cells is None:
            reject(record.line_number, record.problem or "malformed CSV row", record.original_text)
        elif _is_blank(record.cells):
            counters.unrelated += 1
        elif layout is None:
            header = [cell.strip() for cell in record.cells]
            problems = mapping_problems(header, mapping)
            if problems:
                raise CsvFormatError(
                    f"{source_path.name}: mapping does not fit ({'; '.join(problems)})",
                    outcome(),
                )
            layout = _column_layout(header, mapping)
            continue
        else:
            parsed = _parse_row(record.cells, layout, mapping, zone, record.line_number)
            if isinstance(parsed, str):
                reject(record.line_number, parsed, record.original_text)
            elif parsed.timestamp_utc is None:
                undated_records.append(
                    _undated_record(parsed, record.line_number, record.original_text, source_id)
                )
                counters.undated += 1
            else:
                points.append(
                    _geo_point(parsed, record.line_number, record.original_text, source_id)
                )
                counters.accepted += 1
                counters.naive_timestamps += parsed.naive_time
        chunk_boundary_crossed = reader.bytes_read != last_reported_bytes
        if chunk_boundary_crossed:
            last_reported_bytes = reader.bytes_read
            report_progress()
        if (
            is_cancelled is not None
            and (chunk_boundary_crossed or record.line_number % CANCEL_CHECK_INTERVAL_ROWS == 0)
            and is_cancelled()
        ):
            cancelled = True
            logger.warning("CSV extraction cancelled after line %d", record.line_number)
            break
    if layout is None and not cancelled:
        raise CsvFormatError(f"{source_path.name}: no header row", outcome())
    finished = outcome()
    report_progress()
    logger.info(
        "CSV extraction finished: accepted=%d undated=%d invalid=%d unrelated=%d lines=%d"
        " cancelled=%s",
        counters.accepted,
        counters.undated,
        counters.invalid,
        counters.unrelated,
        finished.read_report.line_count,
        cancelled,
    )
    return finished


class _RecordTooLongError(Exception):
    """A quoted field did not close within MAX_RECORD_LINES physical lines."""


class _PhysicalLines:
    """Feeds csv.reader line by line, remembers the lines of the current record, limits
    it to MAX_RECORD_LINES and can hand lines back for a fresh start after a bad row."""

    def __init__(self, lines: Iterator[str]) -> None:
        self._lines = lines
        self._pending: deque[tuple[int, str]] = deque()
        self._last_line_number = 0
        self.consumed: list[tuple[int, str]] = []

    def __iter__(self) -> _PhysicalLines:
        return self

    def __next__(self) -> str:
        if len(self.consumed) >= MAX_RECORD_LINES:
            raise _RecordTooLongError
        if self._pending:
            line_number, line = self._pending.popleft()
        else:
            line = next(self._lines)
            self._last_line_number += 1
            line_number = self._last_line_number
        self.consumed.append((line_number, line))
        # The line end lets a quoted field keep its line break.
        return line + "\n"

    def finish_record(self) -> tuple[int, str]:
        """Start line and text of the record just read."""
        line_number = self.consumed[0][0]
        text = " ".join(line for _, line in self.consumed)
        self.consumed.clear()
        return line_number, truncate(text, ORIGINAL_RECORD_MAX_CHARS)

    def drop_first_line(self) -> tuple[int, str]:
        """Give up the record's first line; the lines after it are read again."""
        line_number, line = self.consumed[0]
        self._pending.extendleft(reversed(self.consumed[1:]))
        self.consumed.clear()
        return line_number, truncate(line, ORIGINAL_RECORD_MAX_CHARS)


def _csv_records(lines: Iterator[str], delimiter: str) -> Iterator[_CsvRecord]:
    """Logical rows with the line where each starts; a row that cannot be split (quote
    not closed within MAX_RECORD_LINES lines, field too large, quoting error) comes back
    with its problem, and reading resumes at the line after its first line."""
    csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    physical_lines = _PhysicalLines(lines)
    row_reader = csv.reader(physical_lines, delimiter=delimiter, strict=True)
    while True:
        try:
            cells = next(row_reader)
        except StopIteration:
            return
        except _RecordTooLongError:
            line_number, text = physical_lines.drop_first_line()
            yield _CsvRecord(
                line_number, None, f"quoted field not closed within {MAX_RECORD_LINES} lines", text
            )
            continue
        except csv.Error as error:
            if not physical_lines.consumed:
                return
            line_number, text = physical_lines.drop_first_line()
            yield _CsvRecord(line_number, None, f"malformed CSV row: {error}", text)
            continue
        line_number, text = physical_lines.finish_record()
        yield _CsvRecord(line_number, cells, None, text)


def _is_blank(cells: list[str]) -> bool:
    return not any(cell.strip() for cell in cells)


@dataclass(frozen=True, slots=True)
class _ColumnLayout:
    """Where the mapped roles sit in a row, which coordinate columns hold scaled integers
    (exponent by role) and which columns have no role (index and label)."""

    header_length: int
    role_indexes: dict[str, int]
    scaled_exponents: dict[str, int]
    remaining_columns: tuple[tuple[int, str], ...]
    date_orders: dict[str, str]
    # Other columns named as a method (index and label), checked against the mapped one.
    other_method_columns: tuple[tuple[int, str], ...] = ()


def _column_layout(header: list[str], mapping: CsvMapping) -> _ColumnLayout:
    labels = column_labels(header)
    role_indexes = {
        role: labels.index(getattr(mapping, role))
        for role in COLUMN_ROLES
        if getattr(mapping, role) in labels
    }
    scaled_exponents = {}
    for role in (LATITUDE, LONGITUDE):
        exponent = scaled_exponent(getattr(mapping, role))
        if exponent is not None:
            scaled_exponents[role] = exponent
    # The method column keeps its raw text in the note.
    mapped_indexes = {index for role, index in role_indexes.items() if role != "positioning_method"}
    remaining_columns = tuple(
        (index, truncate(field_text(label) or "", REMAINING_HEADER_MAX_CHARS))
        for index, label in enumerate(labels)
        if index not in mapped_indexes
    )
    other_method_columns = (
        tuple(
            (labels.index(label), label)
            for label in method_columns(header)
            if label != mapping.positioning_method
        )
        if "positioning_method" in role_indexes
        else ()
    )
    return _ColumnLayout(
        len(header),
        role_indexes,
        scaled_exponents,
        remaining_columns,
        mapping.date_orders,
        other_method_columns,
    )


def _parse_row(
    cells: list[str],
    layout: _ColumnLayout,
    mapping: CsvMapping,
    zone: tzinfo | None,
    line_number: int,
) -> _ParsedRow | str:
    role_indexes = layout.role_indexes
    if any(index >= len(cells) for index in role_indexes.values()):
        return f"row has {len(cells)} field(s), the header has {layout.header_length}"

    def cell(role: str) -> str:
        index = role_indexes.get(role)
        return "" if index is None else cells[index].strip()

    located = _located(cell, mapping, layout.scaled_exponents)
    if isinstance(located, str):
        return located
    latitude, longitude, coordinate_note = located
    checked = checked_position(latitude, longitude)
    if isinstance(checked, str):
        return checked
    parsed_time: ParsedTimestamp | None = None
    if mapping.timestamp:
        row_time = _row_time(
            cell("timestamp"),
            cell("time_of_day"),
            bool(mapping.time_of_day),
            mapping,
            zone,
            layout,
        )
        if isinstance(row_time, str):
            return row_time
        parsed_time = row_time
    accuracy_text = cell("accuracy")
    accuracy_m: float | None = None
    if accuracy_text:
        accuracy_m = _decimal(accuracy_text)
        if accuracy_m is None or not math.isfinite(accuracy_m) or accuracy_m < 0:
            return f"invalid accuracy: {excerpt(accuracy_text)}"
    positioning_method = positioning_method_from_text(cell("positioning_method"))
    # What GEOSnap says comes first and marked; text from the file can carry no mark.
    note_parts = (
        # An empty cell in a mapped time column (missing, not wrong) is said in the note;
        # a mapping without time column says it once for the whole source.
        NO_TIMESTAMP_NOTE if parsed_time is None and mapping.timestamp else None,
        AMBIGUOUS_TIME_NOTE if parsed_time is not None and parsed_time.ambiguous else None,
        _disagreeing_methods_text(cells, layout, mapping, positioning_method),
        coordinate_note,
        *_end_notes(
            cell, mapping, zone, layout, None if parsed_time is None else parsed_time.instant
        ),
        _text_field(cell("note")),
        _remaining_columns_text(cells, layout, line_number),
    )
    return _ParsedRow(
        latitude=checked[0],
        longitude=checked[1],
        timestamp_utc=None if parsed_time is None else parsed_time.instant,
        accuracy_m=accuracy_m,
        label=_text_field(cell("label")),
        note=NOTE_SEPARATOR.join(part for part in note_parts if part) or None,
        naive_time=parsed_time is not None and parsed_time.naive,
        time_ambiguity_seconds=0.0 if parsed_time is None else parsed_time.ambiguity_seconds,
        positioning_method=positioning_method,
    )


def _disagreeing_methods_text(
    cells: list[str], layout: _ColumnLayout, mapping: CsvMapping, positioning_method: str | None
) -> str | None:
    """Tool note when another method-named column of the row names a different method."""
    if positioning_method is None:
        return None
    named_methods = [(positioning_method, mapping.positioning_method)]
    for index, label in layout.other_method_columns:
        if index < len(cells) and (method := positioning_method_from_text(cells[index].strip())):
            named_methods.append((method, label))
    if all(method == positioning_method for method, _ in named_methods):
        return None
    return disagreeing_methods_note(named_methods)


def _row_time(
    date_text: str,
    time_text: str,
    time_column_mapped: bool,
    mapping: CsvMapping,
    zone: tzinfo | None,
    layout: _ColumnLayout,
    empty_cell_is_undated: bool = True,
) -> ParsedTimestamp | str | None:
    """The instant of a row from its date cell alone, or joined with the time-of-day cell
    when that column is mapped; a date cell that already carries a time of day must agree
    with the time cell.

    None means the record has no timestamp: one of the mapped time cells is empty. Missing
    is not wrong and the position is real, so the row becomes an undated record, not a
    rejection. Only text that is there but unreadable refuses a row. The end of a span
    passes ``empty_cell_is_undated=False``: a half-given end is named in the tool note,
    because the record keeps its start either way."""
    if not date_text and not time_text:
        return None
    if not time_column_mapped:
        return _parse_timestamp(date_text, mapping.time_format, zone, layout.date_orders)
    if not date_text:
        return None if empty_cell_is_undated else DATE_MISSING_REASON
    if not time_text:
        return None if empty_cell_is_undated else TIME_OF_DAY_MISSING_REASON
    clock_text = time_of_day_text(time_text)
    composed = _parse_timestamp(
        f"{date_text} {clock_text}", mapping.time_format, zone, layout.date_orders
    )
    if not isinstance(composed, str):
        return composed
    date_alone = _parse_timestamp(date_text, mapping.time_format, zone, layout.date_orders)
    if isinstance(date_alone, str):
        return composed
    clock = parse_time_of_day(clock_text)
    own_clock = date_alone.instant.astimezone(zone or UTC).time()
    if isinstance(clock, str) or clock != own_clock:
        return (
            f"date column already holds a time ({excerpt(date_text)}) that contradicts the"
            f" time-of-day column ({excerpt(time_text)})"
        )
    return date_alone


def _end_notes(
    cell: Callable[[str], str],
    mapping: CsvMapping,
    zone: tzinfo | None,
    layout: _ColumnLayout,
    start: datetime | None,
) -> list[str]:
    """Tool notes for the end of a span: its local time with offset (UTC without a
    zone), "end before start" when it precedes the start, or why it was not read. No
    note when the end cells are empty. Without an end date column the end time is read
    on the date of the start cell."""
    if not (mapping.timestamp_end or mapping.time_of_day_end):
        return []
    date_text = cell("timestamp_end") if mapping.timestamp_end else cell("timestamp")
    time_text = cell("time_of_day_end")
    if not cell("timestamp_end") and not time_text:
        return []
    parsed = _row_time(
        date_text,
        time_text,
        bool(mapping.time_of_day_end),
        mapping,
        zone,
        layout,
        empty_cell_is_undated=False,
    )
    if parsed is None:
        return []  # the end cells hold no time at all: nothing to say
    if isinstance(parsed, str):
        return [tool_note(f"end time refused: {parsed}")]
    if zone is not None:
        local_text = parsed.instant.astimezone(zone).isoformat(timespec="seconds")
    else:
        local_text = parsed.instant.strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = [tool_note(f"end: {local_text}")]
    if parsed.ambiguous:
        notes.append(tool_note("end time ambiguous, earlier occurrence used"))
    if start is not None and parsed.instant < start:
        notes.append(tool_note("end before start"))
    return notes


def _located(
    cell: Callable[[str], str], mapping: CsvMapping, scaled_exponents: dict[str, int]
) -> tuple[float, float, str | None] | str:
    """Latitude, longitude and, unless the cells were plain decimals, the note naming
    the notation and the text that was read; or the rejection reason."""
    if not (mapping.latitude and mapping.longitude):
        position_text = cell("position")
        position = parse_position(position_text, mapping.position_order)
        if isinstance(position, str):
            return position
        notation = position.notation + (f" ({position.remark})" if position.remark else "")
        return position.latitude, position.longitude, _coordinate_note(notation, position_text)
    latitude_text, longitude_text = cell(LATITUDE), cell(LONGITUDE)
    if scaled_exponents:
        latitude = _scaled_or_plain(latitude_text, LATITUDE, scaled_exponents)
        longitude = _scaled_or_plain(longitude_text, LONGITUDE, scaled_exponents)
    else:
        latitude, longitude = parse_latitude(latitude_text), parse_longitude(longitude_text)
    if isinstance(latitude, str):
        return latitude
    if isinstance(longitude, str):
        return longitude
    if latitude.notation == longitude.notation:
        notation = latitude.notation
    else:
        notation = f"{latitude.notation} / {longitude.notation}"
    note = _coordinate_note(notation, f"{latitude_text} | {longitude_text}")
    return latitude.degrees, longitude.degrees, note


def _scaled_or_plain(text: str, axis: str, scaled_exponents: dict[str, int]) -> ParsedAngle | str:
    exponent = scaled_exponents.get(axis)
    if exponent is not None:
        return parse_scaled_integer(text, axis, exponent)
    return parse_latitude(text) if axis == LATITUDE else parse_longitude(text)


def _coordinate_note(notation: str, text_read: str) -> str | None:
    """Traceability of a conversion; plain decimal degrees need none."""
    if notation == DECIMAL_DEGREES:
        return None
    text_shown = field_text(excerpt(text_read)) or ""
    return tool_note(f"coordinates read as {notation} from {text_shown}")


def _remaining_columns_text(
    cells: list[str], layout: _ColumnLayout, line_number: int
) -> str | None:
    """ "header: value" of every non-empty cell without a role (cells beyond the header
    as "(column n)"). Only whole parts are kept within REMAINING_COLUMNS_MAX_CHARS; when
    some do not fit, the text ends by saying how many and where the full row is."""
    cell_count = len(cells)
    remaining_columns: Iterable[tuple[int, str]] = layout.remaining_columns
    if cell_count > layout.header_length:
        remaining_columns = itertools.chain(
            remaining_columns,
            ((index, f"(column {index + 1})") for index in range(layout.header_length, cell_count)),
        )
    parts: list[str] = []
    for index, label in remaining_columns:
        if index >= cell_count:
            break
        value = field_text(cells[index])
        if value is None:
            continue
        parts.append(f"{label}: {truncate(value, REMAINING_VALUE_MAX_CHARS)}")
    return bounded_field_parts(parts, "columns", f"row in the source file, line {line_number}")


def _undated_record(
    parsed: _ParsedRow, line_number: int, original_text: str, source_id: int
) -> ReferencePlace:
    return ReferencePlace(
        source_id=source_id,
        record_number=line_number,
        name=parsed.label or "",
        note=parsed.note,
        latitude=parsed.latitude,
        longitude=parsed.longitude,
        original_record=original_text,
        accuracy_m=parsed.accuracy_m or 0.0,
        accuracy_known=parsed.accuracy_m is not None,
        positioning_method=parsed.positioning_method,
    )


def _geo_point(
    parsed: _ParsedRow, line_number: int, original_text: str, source_id: int
) -> GeoPoint:
    assert parsed.timestamp_utc is not None
    accuracy_m = parsed.accuracy_m
    return GeoPoint(
        line_number=line_number,
        latitude=parsed.latitude,
        longitude=parsed.longitude,
        latitude_accuracy_m=accuracy_m or 0.0,
        longitude_accuracy_m=accuracy_m or 0.0,
        timestamp_utc=parsed.timestamp_utc,
        original_line=original_text,
        source_id=source_id,
        label=parsed.label,
        note=parsed.note,
        accuracy_known=accuracy_m is not None,
        time_ambiguity_seconds=parsed.time_ambiguity_seconds,
        positioning_method=parsed.positioning_method,
    )


def _text_field(text: str) -> str | None:
    """Label or note from the file: field_text, at most TEXT_FIELD_MAX_CHARS."""
    cleaned = field_text(text)
    return truncate(cleaned, TEXT_FIELD_MAX_CHARS) if cleaned else None


def _decimal(text: str) -> float | None:
    """An accuracy: a decimal number, a single decimal comma ("12,5") is accepted."""
    cleaned = text.strip()
    if DECIMAL_COMMA_PATTERN.fullmatch(cleaned):
        cleaned = cleaned.replace(",", ".")
    if not DECIMAL_PATTERN.fullmatch(cleaned):
        return None
    return float(cleaned)


def _parse_timestamp(
    text: str, time_format: str, zone: tzinfo | None, date_orders: Mapping[str, str]
) -> ParsedTimestamp | str:
    """The reading of ``text`` in ``time_format``, refused outside PLAUSIBLE_TIME_RANGE_UTC
    like every timestamp GEOSnap reads."""
    parsed = _timestamp_in_format(text, time_format, zone, date_orders)
    if isinstance(parsed, str):
        return parsed
    return implausible_time_reason(parsed.instant, text) or parsed


def _timestamp_in_format(
    text: str, time_format: str, zone: tzinfo | None, date_orders: Mapping[str, str]
) -> ParsedTimestamp | str:
    label = TIME_FORMAT_LABELS.get(time_format, time_format)
    unparsable = f"unparsable timestamp ({label}): {excerpt(text)}"
    if time_format == AUTOMATIC_TIME_FORMAT:
        recognised = recognise_timestamp(text, date_orders)
        if isinstance(recognised, str):
            return recognised
        if recognised.zoned:
            return ParsedTimestamp(recognised.moment, naive=False, ambiguous=False)
        return _localised(recognised.moment, zone, text, unparsable)
    if time_format == EXCEL_SERIAL_TIME_FORMAT:
        wall_time = excel_serial_wall_time(text)
        if isinstance(wall_time, str):
            return wall_time
        return _localised(wall_time, zone, text, unparsable)
    if time_format in ("unix_seconds", "unix_milliseconds"):
        if not UNIX_TIME_PATTERN.fullmatch(text):
            return unparsable
        whole_text, _, fraction = text.partition(".")
        whole_units = bounded_int(whole_text)
        if whole_units is None:
            return unparsable
        units_per_second = 1000 if time_format == "unix_milliseconds" else 1
        earliest, latest = PLAUSIBLE_UNIX_SECONDS
        if not earliest * units_per_second <= whole_units < latest * units_per_second:
            return implausible_time_text(text)
        # Exact to the microsecond, as the automatic format reads Unix times.
        fraction_digits = 3 if units_per_second == 1000 else 6
        microseconds = whole_units * (1_000_000 // units_per_second) + int(
            fraction.ljust(fraction_digits, "0")[:fraction_digits]
        )
        instant = UNIX_EPOCH + timedelta(microseconds=microseconds)
        return ParsedTimestamp(instant, naive=False, ambiguous=False)
    if time_format == "iso":
        return _parse_iso(text, zone, unparsable)
    patterns = {
        "day_month_year": DAY_MONTH_YEAR_PATTERN,
        "month_day_year": MONTH_DAY_YEAR_PATTERN,
        "year_month_day": YEAR_MONTH_DAY_PATTERN,
    }
    pattern = patterns.get(time_format)
    match = pattern.fullmatch(text) if pattern is not None else None
    if match is None:
        return unparsable
    groups = match.groups()
    if time_format == "day_month_year":
        day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
    elif time_format == "month_day_year":
        month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
    else:
        year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
    hour, minute, second = int(groups[3]), int(groups[4]), int(groups[5] or 0)
    if time_format == "month_day_year" and groups[6]:
        if not 1 <= hour <= 12:
            return unparsable
        hour = hour % 12 + (12 if groups[6].casefold() == "pm" else 0)
    try:
        wall_time = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return unparsable
    return _localised(wall_time, zone, text, unparsable)


def _parse_iso(text: str, zone: tzinfo | None, unparsable: str) -> ParsedTimestamp | str:
    zoned = ISO_WITH_ZONE_PATTERN.fullmatch(text)
    if zoned is not None:
        date_part, time_part, zone_part = zoned.groups()
        if len(time_part) == 5:
            time_part += ":00"
        if zone_part != "Z" and ":" not in zone_part:
            zone_part = f"{zone_part[:3]}:{zone_part[3:]}"
        parsed = parse_iso_timestamp(f"{date_part}T{time_part}{zone_part}")
        if parsed is None:
            return unparsable
        if isinstance(parsed, str):
            return parsed
        return ParsedTimestamp(parsed[0], naive=False, ambiguous=False)
    naive = ISO_NAIVE_PATTERN.fullmatch(text)
    if naive is None:
        return unparsable
    year, month, day, hour, minute = (int(part) for part in naive.groups()[:5])
    second = int(naive.group(6) or 0)
    microsecond = int((naive.group(7) or "").ljust(6, "0")[:6])
    try:
        wall_time = datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return unparsable
    return _localised(wall_time, zone, text, unparsable)


def _localised(
    wall_time: datetime, zone: tzinfo | None, text: str, unparsable: str
) -> ParsedTimestamp | str:
    """UTC instant of a wall-clock time in ``zone``: the earlier one when the time occurs
    twice (fold 0, flagged ambiguous), refused when it does not exist (spring gap) or
    lies outside the datetime range in UTC. A wall time outside the plausible window is
    refused first (near year 1 or 9999 the conversion itself would fail)."""
    implausible = implausible_time_reason(wall_time, text)
    if implausible is not None:
        return implausible
    if zone is None:
        return f"{MISSING_ZONE_REASON}: {excerpt(text)}"
    try:
        instant = wall_time.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) != wall_time:
            return f"local time {excerpt(text)} does not exist in {zone} (clock change)"
        later_instant = wall_time.replace(tzinfo=zone, fold=1).astimezone(UTC)
    except (OverflowError, ValueError):
        return unparsable
    return ParsedTimestamp(
        instant,
        naive=True,
        ambiguous=later_instant != instant,
        ambiguity_seconds=(later_instant - instant).total_seconds(),
    )


@functools.cache
def _zone_names_by_folded_name() -> dict[str, str]:
    return {name.casefold(): name for name in available_timezones() if name not in NON_ZONE_NAMES}


def _zone(zone_name: str) -> tzinfo | None:
    canonical_name = canonical_zone_name(zone_name) if zone_name else None
    if canonical_name is None:
        return None
    try:
        return ZoneInfo(canonical_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
