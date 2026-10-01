# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Stream a KML file (e.g. a mobile forensic export) into points and saved places.

Placemarks are read one at a time with ElementTree's pull parser and released after
processing, so memory stays flat for any file size. The raw bytes are hashed as they
are fed to the parser, as in the text reader. The file is opened once:
every chunk passes the prolog check before the pull parser sees it.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from geosnap.extraction.extractor import ExtractionProgress, report_extraction_progress
from geosnap.extraction.models import (
    ExtractionCounters,
    GeoPoint,
    ReferencePlace,
    RejectedLine,
    SourceExtractionOutcome,
    SourceFormatError,
)
from geosnap.extraction.reader_support import (
    NOTE_PART_SEPARATOR,
    ORIGINAL_RECORD_MAX_CHARS,
    XmlPrologInspector,
    bounded_field_parts,
    checked_position,
    collapse_whitespace,
    compact_xml,
    field_part,
    first_child,
    first_descendant,
    local_name,
    parse_iso_timestamp,
    qualified_name,
    record_text,
    release_finished_element,
    tool_note,
)
from geosnap.extraction.reader_support import excerpt as _excerpt
from geosnap.extraction.reader_support import truncate as _truncate
from geosnap.extraction.source_reader import SourceReadReport
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

CANCEL_CHECK_INTERVAL_PLACEMARKS = 1000
DOCTYPE_REJECTION_REASON = "DOCTYPE not allowed in KML input"
NOTE_MAX_CHARS = 500
# Description markup is cut to this length before the tag patterns run on it.
NOTE_MARKUP_MAX_CHARS = 8 * NOTE_MAX_CHARS
NOTE_PARAGRAPH_SEPARATOR = NOTE_PART_SEPARATOR
PLACEMARK_NAMES = frozenset({"Placemark"})
EMPTY_TIMESTAMP_REASON = "empty timestamp"
NOTE_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "tr"})

# "[^<>]*" keeps both patterns linear on unterminated tags.
HTML_BLOCK_BOUNDARY_PATTERN = re.compile(r"<\s*/?\s*(?:p|br|div|li|tr)\b[^<>]*>", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"<[^<>]*>")
# ASCII letters stay allowed so "nan"/"inf" reach the non-finite check.
COORDINATE_NUMBER_PATTERN = re.compile(r"[0-9A-Za-z+\-.]+", re.ASCII)
COMMA_WITH_SPACES_PATTERN = re.compile(r"\s*,\s*")


class KmlFormatError(SourceFormatError):
    """The file is not acceptable KML: a DOCTYPE, malformed XML or no root element.

    ``partial_outcome`` holds what was read before the failure (counters, placemarks and
    the digest of the bytes read), so the caller can document the partial read.
    """


# The KML reader's result is the common reader outcome.
KmlExtractionOutcome = SourceExtractionOutcome


def extract_kml(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
) -> KmlExtractionOutcome:
    """Read every placemark once and sort it into points, saved places or rejections.

    Placemarks may sit anywhere (Document/Folder nesting); the KML namespace is not
    required. Progress is reported once per read chunk. Cancellation is checked after
    every chunk and every CANCEL_CHECK_INTERVAL_PLACEMARKS placemarks; a cancelled run
    returns what was read and hashes only the bytes read. Raises KmlFormatError for a
    DOCTYPE declaration or malformed XML (a missing root element included); the error
    carries the partial outcome.
    """
    with source_path.open("rb") as source_file:
        # Size of the opened file, so the report describes exactly what was read.
        size_bytes = os.fstat(source_file.fileno()).st_size
        return extract_kml_stream(
            source_file,
            source_path.name,
            size_bytes,
            settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id,
        )


def extract_kml_stream(
    kml_stream: BinaryIO,
    display_name: str,
    size_bytes: int,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
) -> KmlExtractionOutcome:
    """extract_kml over an open binary stream (a file, or the KML entry of a KMZ archive).

    size_bytes is the expected length of the stream (progress and read report). A
    KmlFormatError raised by the stream's read() is re-raised with the partial outcome.
    """
    placemark_reader = _PlacemarkReader(source_id, on_rejected)
    counters = placemark_reader.counters
    prolog_inspector = XmlPrologInspector(KmlFormatError, DOCTYPE_REJECTION_REASON, "kml")
    digest = hashlib.sha256()
    bytes_read = 0
    cancelled = False

    def report_progress() -> None:
        report_extraction_progress(on_progress, bytes_read, size_bytes, counters)

    def cancellation_requested() -> bool:
        return is_cancelled is not None and is_cancelled()

    def outcome() -> KmlExtractionOutcome:
        read_report = SourceReadReport(
            sha256_hex=digest.hexdigest(),
            size_bytes=size_bytes,
            bytes_read=bytes_read,
            encoding=prolog_inspector.encoding,
            line_count=placemark_reader.placemark_count,
            decode_replacements=0,
        )
        return KmlExtractionOutcome(
            placemark_reader.points,
            placemark_reader.reference_places,
            counters,
            read_report,
            cancelled,
        )

    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("start", "end"))
    open_elements: list[ET.Element] = []

    def process_events() -> bool:
        """Handle the parsed events; True when cancellation was requested meanwhile."""
        for parsed_event in parser.read_events():
            event_name, element = parsed_event[0], parsed_event[-1]
            if not isinstance(element, ET.Element):
                continue
            if event_name == "start":
                open_elements.append(element)
                continue
            open_elements.pop()
            if local_name(element.tag) != "Placemark":
                release_finished_element(element, open_elements, PLACEMARK_NAMES)
                continue
            placemark_reader.read_placemark(element)
            # Release the processed subtree and detach it so the tree never grows.
            element.clear()
            if open_elements:
                open_elements[-1].remove(element)
            if (
                placemark_reader.placemark_count % CANCEL_CHECK_INTERVAL_PLACEMARKS == 0
                and cancellation_requested()
            ):
                return True
        return False

    logger.info("KML extraction started: %s (%d bytes)", display_name, size_bytes)
    try:
        while chunk := kml_stream.read(settings.read_chunk_bytes):
            digest.update(chunk)
            bytes_read += len(chunk)
            # The prolog check sees every byte before the pull parser does.
            prolog_inspector.inspect(chunk)
            parser.feed(chunk)
            cancelled = process_events()
            report_progress()
            if cancelled or cancellation_requested():
                cancelled = True
                break
        if not cancelled:
            prolog_inspector.finish()
            parser.close()
            process_events()
    except KmlFormatError as error:
        raise KmlFormatError(str(error), outcome()) from error
    except expat.ExpatError as error:
        raise KmlFormatError(
            f"malformed KML in {display_name} at line {error.lineno}, "
            f"column {error.offset}: {expat.ErrorString(error.code)}",
            outcome(),
        ) from error
    except ET.ParseError as error:
        line_number, column_number = error.position
        raise KmlFormatError(
            f"malformed KML in {display_name} at line {line_number}, "
            f"column {column_number}: {expat.ErrorString(error.code)}",
            outcome(),
        ) from error

    if cancelled:
        logger.warning(
            "KML extraction cancelled after placemark %d", placemark_reader.placemark_count
        )
    finished = outcome()
    report_progress()
    logger.info(
        "KML extraction finished: accepted=%d invalid=%d unrelated=%d saved_places=%d "
        "placemarks=%d cancelled=%s",
        counters.accepted,
        counters.invalid,
        counters.unrelated,
        len(finished.reference_places),
        finished.read_report.line_count,
        cancelled,
    )
    return finished


class _PlacemarkReader:
    """Classifies placemarks one by one and collects the results."""

    def __init__(self, source_id: int, on_rejected: Callable[[RejectedLine], None]) -> None:
        self.source_id = source_id
        self.on_rejected = on_rejected
        self.points: list[GeoPoint] = []
        self.reference_places: list[ReferencePlace] = []
        self.counters = ExtractionCounters()
        self.placemark_count = 0
        self._zone_warning_logged = False
        self._line_string_logged = False
        self._empty_track_logged = False

    def read_placemark(self, placemark: ET.Element) -> None:
        """Import every track (gx:Track, KML 2.3 Track) and every Point of a placemark
        (several in a MultiGeometry); a placemark that yields no point, saved place or
        rejection counts as unrelated."""
        self.placemark_count += 1
        placemark_number = self.placemark_count
        records_before = self._record_count()
        tracks = [node for node in placemark.iter() if local_name(node.tag) == "Track"]
        for track in tracks:
            self._read_track(placemark, track, placemark_number)
        points = [node for node in placemark.iter() if local_name(node.tag) == "Point"]
        if points:
            self._read_points(placemark, points, placemark_number)
        if self._record_count() > records_before:
            return
        self.counters.unrelated += 1
        if tracks and not self._empty_track_logged:
            self._empty_track_logged = True
            logger.warning(
                "Track placemarks without when/coord pairs are not imported (first: placemark %d)",
                placemark_number,
            )
        if first_descendant(placemark, "LineString") is not None and not self._line_string_logged:
            self._line_string_logged = True
            logger.warning(
                "LineString placemarks carry no times and are not imported (first: placemark %d)",
                placemark_number,
            )

    def _record_count(self) -> int:
        return self.counters.accepted + self.counters.invalid + len(self.reference_places)

    def _read_points(
        self, placemark: ET.Element, points: list[ET.Element], placemark_number: int
    ) -> None:
        """The Points of one placemark share its time, name and description; like the
        pairs of a gx:Track they all carry the placemark's number. Several Points without
        a time are refused: one placemark is one saved place."""
        original_record = _truncate(compact_xml(placemark), ORIGINAL_RECORD_MAX_CHARS)
        positions = [
            _parse_point_coordinates(coordinates.text if coordinates is not None else None)
            for coordinates in (first_child(point, "coordinates") for point in points)
        ]
        if len(positions) == 1 and isinstance(positions[0], str):
            self._reject(placemark_number, positions[0], original_record)
            return
        label = record_text(_normalised_text(first_child(placemark, "name")))
        note = _placemark_note(placemark, placemark_number)
        timestamp_text = _timestamp_text(placemark)
        if timestamp_text == "":
            self._reject(placemark_number, EMPTY_TIMESTAMP_REASON, original_record)
            return
        if timestamp_text is None:
            span_end_text = _span_end_text(placemark)
            if span_end_text:
                self._reject(
                    placemark_number,
                    f"TimeSpan with an end but no begin: {_excerpt(span_end_text)}",
                    original_record,
                )
            elif len(positions) > 1:
                self._reject(
                    placemark_number,
                    f"MultiGeometry with {len(positions)} Points without a timestamp",
                    original_record,
                )
            elif not isinstance(positions[0], str):
                self._save_place(placemark_number, positions[0], label, note, original_record)
            return
        timestamp_utc = self._parse_timestamp(timestamp_text)
        if isinstance(timestamp_utc, str):
            self._reject(placemark_number, timestamp_utc, original_record)
            return
        for index, position in enumerate(positions, start=1):
            if isinstance(position, str):
                self._reject(placemark_number, position, original_record)
                continue
            point_note = note
            if len(positions) > 1:
                point_marker = tool_note(f"MultiGeometry point {index} of {len(positions)}")
                point_note = NOTE_PART_SEPARATOR.join(part for part in (point_marker, note) if part)
            latitude, longitude = position
            self._accept(
                placemark_number,
                latitude,
                longitude,
                timestamp_utc,
                original_record,
                label,
                point_note,
            )

    def _save_place(
        self,
        placemark_number: int,
        position: tuple[float, float],
        label: str | None,
        note: str | None,
        original_record: str,
    ) -> None:
        latitude, longitude = position
        self.counters.undated += 1
        self.reference_places.append(
            ReferencePlace(
                source_id=self.source_id,
                record_number=placemark_number,
                name=label or "",
                note=note,
                latitude=latitude,
                longitude=longitude,
                original_record=original_record,
            )
        )

    def _read_track(self, placemark: ET.Element, track: ET.Element, placemark_number: int) -> None:
        when_elements = [child for child in track if local_name(child.tag) == "when"]
        coord_elements = [child for child in track if local_name(child.tag) == "coord"]
        if len(when_elements) != len(coord_elements):
            track_name = qualified_name(track.tag)
            coord_name = "gx:coord" if track_name.startswith("gx:") else "coord"
            self._reject(
                placemark_number,
                f"{track_name} has {len(when_elements)} when and {len(coord_elements)} "
                f"{coord_name} elements",
                _truncate(compact_xml(placemark), ORIGINAL_RECORD_MAX_CHARS),
            )
            return
        label = record_text(_normalised_text(first_child(placemark, "name")))
        note = _placemark_note(placemark, placemark_number)
        for when_element, coord_element in zip(when_elements, coord_elements, strict=True):
            original_record = _truncate(
                compact_xml(when_element) + compact_xml(coord_element),
                ORIGINAL_RECORD_MAX_CHARS,
            )
            position = _parse_track_coordinates(coord_element.text)
            if isinstance(position, str):
                self._reject(placemark_number, position, original_record)
                continue
            timestamp_text = (when_element.text or "").strip()
            timestamp_utc = self._parse_timestamp(timestamp_text)
            if isinstance(timestamp_utc, str):
                self._reject(placemark_number, timestamp_utc, original_record)
                continue
            latitude, longitude = position
            self._accept(
                placemark_number, latitude, longitude, timestamp_utc, original_record, label, note
            )

    def _accept(
        self,
        placemark_number: int,
        latitude: float,
        longitude: float,
        timestamp_utc: datetime,
        original_record: str,
        label: str | None,
        note: str | None,
    ) -> None:
        self.points.append(
            GeoPoint(
                line_number=placemark_number,
                latitude=latitude,
                longitude=longitude,
                latitude_accuracy_m=0.0,
                longitude_accuracy_m=0.0,
                timestamp_utc=timestamp_utc,
                original_line=original_record,
                source_id=self.source_id,
                label=label,
                note=note,
                accuracy_known=False,
            )
        )
        self.counters.accepted += 1

    def _reject(self, placemark_number: int, reason: str, original_record: str) -> None:
        self.counters.invalid += 1
        self.on_rejected(RejectedLine(placemark_number, reason, original_record))
        logger.warning("Placemark %d rejected: %s", placemark_number, reason)

    def _parse_timestamp(self, timestamp_text: str) -> datetime | str:
        """ISO 8601 date and time with optional fraction and zone (no zone means UTC), or
        the rejection reason."""
        parsed = parse_iso_timestamp(timestamp_text)
        if parsed is None:
            return f"unparsable timestamp: {_excerpt(timestamp_text)}"
        if isinstance(parsed, str):
            return parsed
        timestamp_utc, zone_given = parsed
        if not zone_given:
            self.counters.naive_timestamps += 1
            self._warn_missing_zone_once(timestamp_text)
        return timestamp_utc

    def _warn_missing_zone_once(self, timestamp_text: str) -> None:
        if self._zone_warning_logged:
            return
        self._zone_warning_logged = True
        logger.warning(
            "Timestamp without time zone treated as UTC (first: placemark %d, %s)",
            self.placemark_count,
            timestamp_text,
        )


def _parse_point_coordinates(coordinates_text: str | None) -> tuple[float, float] | str:
    """Latitude and longitude from the first "lon,lat[,alt]" tuple, or a rejection reason."""
    stripped_text = (coordinates_text or "").strip()
    if not stripped_text:
        return "missing coordinates"
    first_tuple = COMMA_WITH_SPACES_PATTERN.sub(",", stripped_text).split()[0]
    return _validated_position(first_tuple.split(","), stripped_text)


def _parse_track_coordinates(coord_text: str | None) -> tuple[float, float] | str:
    """Latitude and longitude from a gx:coord "lon lat [alt]", or a rejection reason."""
    stripped_text = (coord_text or "").strip()
    if not stripped_text:
        return "missing coordinates"
    return _validated_position(stripped_text.split(), stripped_text)


def _validated_position(parts: list[str], raw_text: str) -> tuple[float, float] | str:
    unparsable_reason = f"unparsable coordinates: {_excerpt(raw_text)}"
    if len(parts) < 2 or not all(COORDINATE_NUMBER_PATTERN.fullmatch(part) for part in parts[:2]):
        return unparsable_reason
    try:
        longitude, latitude = float(parts[0]), float(parts[1])
    except ValueError:
        return unparsable_reason
    return checked_position(latitude, longitude)


def _timestamp_text(placemark: ET.Element) -> str | None:
    """TimeStamp/when, else TimeSpan/begin; None when the placemark has no time, "" when
    the time element is present but empty (a rejection, not a saved place)."""
    empty_seen = False
    for container_name, value_name in (("TimeStamp", "when"), ("TimeSpan", "begin")):
        container = first_child(placemark, container_name)
        value = first_child(container, value_name) if container is not None else None
        if value is not None and value.text is not None and value.text.strip():
            return value.text.strip()
        empty_seen = empty_seen or value is not None
    return "" if empty_seen else None


def _span_end_text(placemark: ET.Element) -> str | None:
    """The text of TimeSpan/end, None when there is none."""
    time_span = first_child(placemark, "TimeSpan")
    span_end = first_child(time_span, "end") if time_span is not None else None
    return collapse_whitespace(span_end.text if span_end is not None else None) or None


def _placemark_note(placemark: ET.Element, placemark_number: int) -> str | None:
    """The description's paragraphs, then the ExtendedData fields as "name: value"."""
    description = record_text(_note_from_description(first_child(placemark, "description")))
    fields = _note_from_extended_data(placemark, placemark_number)
    return NOTE_PARAGRAPH_SEPARATOR.join(part for part in (description, fields) if part) or None


def _note_from_extended_data(placemark: ET.Element, placemark_number: int) -> str | None:
    """Data[@name]/value and SchemaData/SimpleData[@name] of the placemark's ExtendedData as
    "name: value" parts, bounded like the CSV columns without a role. Untyped Data without
    a value, unnamed fields and empty values are skipped."""
    extended_data = first_child(placemark, "ExtendedData")
    if extended_data is None:
        return None
    parts: list[str] = []
    for node in extended_data.iter():
        node_name = local_name(node.tag)
        if node_name == "Data":
            value = first_child(node, "value")
            part = field_part(node.get("name"), value.text if value is not None else None)
        elif node_name == "SimpleData":
            part = field_part(node.get("name"), node.text)
        else:
            continue
        if part is not None:
            parts.append(part)
    return bounded_field_parts(
        parts, "fields", f"placemark in the source file, placemark {placemark_number}"
    )


def _note_from_description(description: ET.Element | None) -> str | None:
    """Description as plain text: tags removed, entities resolved, paragraphs joined.

    Escaped HTML (text or CDATA) and XHTML child elements are both read; the markup is
    cut to NOTE_MARKUP_MAX_CHARS before the tag patterns run.
    """
    if description is None:
        return None
    markup = _description_markup(description)[:NOTE_MARKUP_MAX_CHARS]
    paragraphs = []
    for block in HTML_BLOCK_BOUNDARY_PATTERN.split(markup):
        paragraph = " ".join(html.unescape(HTML_TAG_PATTERN.sub(" ", block)).split())
        if paragraph:
            paragraphs.append(paragraph)
    if not paragraphs:
        return None
    return _truncate(NOTE_PARAGRAPH_SEPARATOR.join(paragraphs), NOTE_MAX_CHARS)


def _description_markup(description: ET.Element) -> str:
    """The description's text with its child elements' text; block-level child elements
    become "<br>" boundaries. Iterative, so deep nesting cannot exhaust the stack."""
    parts: list[str] = [description.text or ""]
    pending: list[tuple[ET.Element, bool]] = [
        (child, False) for child in reversed(list(description))
    ]
    while pending:
        node, closing = pending.pop()
        is_block = local_name(node.tag).lower() in NOTE_BLOCK_TAGS
        if closing:
            if is_block:
                parts.append("<br>")
            parts.append(node.tail or "")
            continue
        if is_block:
            parts.append("<br>")
        parts.append(node.text or "")
        pending.append((node, True))
        pending.extend((child, False) for child in reversed(list(node)))
    return "".join(parts)


def _normalised_text(element: ET.Element | None) -> str | None:
    text = collapse_whitespace(element.text if element is not None else None)
    return text or None
