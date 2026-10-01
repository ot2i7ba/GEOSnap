# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Stream a GPX 1.0/1.1 file into points (wpt, rtept, trkpt with time) and saved places.

Uses the same technique as the KML reader: ElementTree's pull parser, every chunk hashed
and passed through the prolog check (DOCTYPE refused) before the parser sees it, and
each point element released after processing. GPX has no accuracy in metres; ``hdop``
is a dilution factor and is only kept in the note ("hdop: 1.4"). ``fix`` (none, 2d, 3d,
dgps, pps) gives the positioning method (any fix but "none" is satellite-based) and is
kept in the note too ("fix: 3d"); "none" leaves the method unknown and the note says so.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from collections.abc import Callable
from pathlib import Path

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
    ORIGINAL_RECORD_MAX_CHARS,
    XmlPrologInspector,
    checked_position,
    collapse_whitespace,
    compact_xml,
    excerpt,
    first_child,
    local_name,
    parse_iso_timestamp,
    positioning_method_from_text,
    record_text,
    release_finished_element,
    tool_note,
    truncate,
)
from geosnap.extraction.source_reader import SourceReadReport
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

CANCEL_CHECK_INTERVAL_POINTS = 1000
DOCTYPE_REJECTION_REASON = "DOCTYPE not allowed in GPX input"
POINT_ELEMENT_NAMES = frozenset({"wpt", "rtept", "trkpt"})
# GPX 1.0 and 1.1; elements without a namespace are accepted as well.
GPX_NAMESPACES = frozenset(
    {"", "http://www.topografix.com/GPX/1/0", "http://www.topografix.com/GPX/1/1"}
)
EMPTY_TIMESTAMP_REASON = "empty timestamp"
NOTE_MAX_CHARS = 500
NOTE_SEPARATOR = " · "
NO_FIX_NOTE = tool_note("GPX fix: none (no position fix)")
# Plain decimal degrees as GPX defines them; letters stay out so "nan"/"inf" are refused.
DECIMAL_DEGREES_PATTERN = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)", re.ASCII)


class GpxFormatError(SourceFormatError):
    """The file is not acceptable GPX: a DOCTYPE, malformed XML or no root element."""


def extract_gpx(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
) -> SourceExtractionOutcome:
    """Read every wpt, rtept and trkpt once; the record number is the element's position
    among them in document order. Points with a time become GeoPoints, points without
    one saved places. Progress per chunk; cancellation after every chunk and every
    CANCEL_CHECK_INTERVAL_POINTS points. Raises GpxFormatError (with the partial
    outcome) for a DOCTYPE or malformed XML."""
    point_reader = _GpxPointReader(source_id, on_rejected)
    counters = point_reader.counters
    prolog_inspector = XmlPrologInspector(GpxFormatError, DOCTYPE_REJECTION_REASON, "gpx")
    digest = hashlib.sha256()
    bytes_read = 0
    size_bytes = 0
    cancelled = False

    def report_progress() -> None:
        report_extraction_progress(on_progress, bytes_read, size_bytes, counters)

    def cancellation_requested() -> bool:
        return is_cancelled is not None and is_cancelled()

    def outcome() -> SourceExtractionOutcome:
        read_report = SourceReadReport(
            sha256_hex=digest.hexdigest(),
            size_bytes=size_bytes,
            bytes_read=bytes_read,
            encoding=prolog_inspector.encoding,
            line_count=point_reader.record_count,
            decode_replacements=0,
        )
        return SourceExtractionOutcome(
            point_reader.points, point_reader.reference_places, counters, read_report, cancelled
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
                if not open_elements:
                    _check_root_namespace(element)
                open_elements.append(element)
                continue
            open_elements.pop()
            if not _is_gpx_point(element, open_elements):
                release_finished_element(element, open_elements, POINT_ELEMENT_NAMES)
                continue
            point_reader.read_point(element)
            element.clear()
            if open_elements:
                open_elements[-1].remove(element)
            if (
                point_reader.record_count % CANCEL_CHECK_INTERVAL_POINTS == 0
                and cancellation_requested()
            ):
                return True
        return False

    with source_path.open("rb") as source_file:
        size_bytes = os.fstat(source_file.fileno()).st_size
        logger.info("GPX extraction started: %s (%d bytes)", source_path.name, size_bytes)
        try:
            while chunk := source_file.read(settings.read_chunk_bytes):
                digest.update(chunk)
                bytes_read += len(chunk)
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
        except GpxFormatError as error:
            raise GpxFormatError(str(error), outcome()) from error
        except expat.ExpatError as error:
            raise GpxFormatError(
                f"malformed GPX in {source_path.name} at line {error.lineno}, "
                f"column {error.offset}: {expat.ErrorString(error.code)}",
                outcome(),
            ) from error
        except ET.ParseError as error:
            line_number, column_number = error.position
            raise GpxFormatError(
                f"malformed GPX in {source_path.name} at line {line_number}, "
                f"column {column_number}: {expat.ErrorString(error.code)}",
                outcome(),
            ) from error

    if cancelled:
        logger.warning("GPX extraction cancelled after point %d", point_reader.record_count)
    finished = outcome()
    report_progress()
    logger.info(
        "GPX extraction finished: accepted=%d invalid=%d saved_places=%d points=%d cancelled=%s",
        counters.accepted,
        counters.invalid,
        len(finished.reference_places),
        point_reader.record_count,
        cancelled,
    )
    return finished


class _GpxPointReader:
    """Classifies wpt/rtept/trkpt elements one by one and collects the results."""

    def __init__(self, source_id: int, on_rejected: Callable[[RejectedLine], None]) -> None:
        self.source_id = source_id
        self.on_rejected = on_rejected
        self.points: list[GeoPoint] = []
        self.reference_places: list[ReferencePlace] = []
        self.counters = ExtractionCounters()
        self.record_count = 0
        self._zone_warning_logged = False

    def read_point(self, element: ET.Element) -> None:
        self.record_count += 1
        record_number = self.record_count
        original_record = truncate(compact_xml(element), ORIGINAL_RECORD_MAX_CHARS)
        position = _position(element)
        if isinstance(position, str):
            self._reject(record_number, position, original_record)
            return
        latitude, longitude = position
        label = record_text(_child_text(element, "name"))
        fix_text = _child_text(element, "fix")
        positioning_method = positioning_method_from_text(fix_text)
        # What GEOSnap says comes first and marked; text from the file carries no mark.
        note_parts = [
            NO_FIX_NOTE if (fix_text or "").casefold() == "none" else None,
            record_text(_note(element)),
        ]
        note = NOTE_SEPARATOR.join(part for part in note_parts if part) or None
        time_text = _child_text(element, "time")
        if time_text is None and first_child(element, "time") is not None:
            self._reject(record_number, EMPTY_TIMESTAMP_REASON, original_record)
            return
        if time_text is None:
            self.counters.undated += 1
            self.reference_places.append(
                ReferencePlace(
                    source_id=self.source_id,
                    record_number=record_number,
                    name=label or "",
                    note=note,
                    latitude=latitude,
                    longitude=longitude,
                    original_record=original_record,
                    positioning_method=positioning_method,
                )
            )
            return
        parsed_time = parse_iso_timestamp(time_text)
        if parsed_time is None:
            self._reject(
                record_number, f"unparsable timestamp: {excerpt(time_text)}", original_record
            )
            return
        if isinstance(parsed_time, str):
            self._reject(record_number, parsed_time, original_record)
            return
        timestamp_utc, zone_given = parsed_time
        if not zone_given:
            self.counters.naive_timestamps += 1
        if not zone_given and not self._zone_warning_logged:
            self._zone_warning_logged = True
            logger.warning(
                "Timestamp without time zone treated as UTC (first: point %d, %s)",
                record_number,
                time_text,
            )
        self.points.append(
            GeoPoint(
                line_number=record_number,
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
                positioning_method=positioning_method,
            )
        )
        self.counters.accepted += 1

    def _reject(self, record_number: int, reason: str, original_record: str) -> None:
        self.counters.invalid += 1
        self.on_rejected(RejectedLine(record_number, reason, original_record))
        logger.warning("GPX point %d rejected: %s", record_number, reason)


def _namespace(tag: str) -> str:
    return tag[1:].partition("}")[0] if tag.startswith("{") else ""


def _check_root_namespace(root: ET.Element) -> None:
    """A <gpx> root in another namespace would have none of its points read: refused."""
    root_namespace = _namespace(root.tag)
    if root_namespace not in GPX_NAMESPACES:
        raise GpxFormatError(
            f"root element <gpx> is in the namespace '{root_namespace}', not GPX 1.0 or 1.1"
            " (http://www.topografix.com/GPX/1/0 or /1/1)"
        )


def _is_gpx_point(element: ET.Element, open_elements: list[ET.Element]) -> bool:
    """wpt/rtept/trkpt in the GPX namespace (or none), not inside <extensions>."""
    if local_name(element.tag) not in POINT_ELEMENT_NAMES:
        return False
    if _namespace(element.tag) not in GPX_NAMESPACES:
        return False
    return not any(local_name(ancestor.tag) == "extensions" for ancestor in open_elements)


def _position(element: ET.Element) -> tuple[float, float] | str:
    latitude_text = element.get("lat")
    longitude_text = element.get("lon")
    if latitude_text is None or longitude_text is None:
        return "missing lat or lon attribute"
    latitude_text, longitude_text = latitude_text.strip(), longitude_text.strip()
    if not (
        DECIMAL_DEGREES_PATTERN.fullmatch(latitude_text)
        and DECIMAL_DEGREES_PATTERN.fullmatch(longitude_text)
    ):
        return f"unparsable coordinates: {excerpt(f'{latitude_text} {longitude_text}')}"
    return checked_position(float(latitude_text), float(longitude_text))


def _child_text(element: ET.Element, wanted_name: str) -> str | None:
    child = first_child(element, wanted_name)
    text = collapse_whitespace(child.text if child is not None else None)
    return text or None


def _note(element: ET.Element) -> str | None:
    """desc and cmt, then "fix: <value>" and "hdop: <value>" (a dilution factor, not
    metres)."""
    parts = [text for name in ("desc", "cmt") if (text := _child_text(element, name))]
    for name in ("fix", "hdop"):
        value_text = _child_text(element, name)
        if value_text is not None:
            parts.append(f"{name}: {value_text}")
    if not parts:
        return None
    return truncate(NOTE_SEPARATOR.join(parts), NOTE_MAX_CHARS)
