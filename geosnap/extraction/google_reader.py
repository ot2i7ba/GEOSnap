# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Read Google location exports: Takeout Records.json, the on-device Timeline export and
Takeout Semantic Location History.

Field names are taken only from public sources (checked 2026-09-18):

- Records.json, ``{"locations": [...]}`` with latitudeE7/longitudeE7 (degrees x 10^7),
  accuracy (metres), timestamp (ISO 8601, since about 2022) or timestampMs (Unix ms,
  earlier), source, deviceTag: https://locationhistoryformat.com/reference/records/ ;
  the 2^32 overflow correction for E7 values above 1800000000 follows
  https://github.com/Scarygami/location-history-json-converter
  (location_history_json_converter.py).
- On-device Timeline export (2024+), keys semanticSegments / rawSignals /
  userLocationProfile; timelinePath[].point "lat°, lng°" and .time; visit.topCandidate
  {placeId, semanticType, probability, placeLocation.latLng}; activity {start.latLng,
  end.latLng, distanceMeters, topCandidate.type}; rawSignals[].position {LatLng (capital
  L), accuracyMeters, source, timestamp}; frequentPlaces {placeId, placeLocation, label}:
  https://pkg.go.dev/github.com/bobg/lohi/schema ; sample with the "°, " notation and
  offset times: https://github.com/Freika/dawarich/discussions/600
- Semantic Location History, ``{"timelineObjects": [...]}`` with placeVisit {location
  {latitudeE7, longitudeE7, name, address, placeId}, duration {startTimestamp,
  endTimestamp}, placeConfidence} and activitySegment {startLocation, endLocation,
  duration, activityType, distance, simplifiedRawPath.points[] {latE7, lngE7,
  accuracyMeters, timestamp}}: https://locationhistoryformat.com/reference/semantic/ ;
  the older duration keys startTimestampMs/endTimestampMs appear in a real sample:
  https://pastebin.com/raw/YQN3H4yH

simplifiedRawPath points are also read with ``timestampMs`` (Unix ms) by analogy with
``duration.startTimestampMs``, and with ``source`` by analogy with Records.json; no real
file showing these keys there has been seen.

``source`` gives the positioning method (GOOGLE_SOURCE_METHODS: GPS, WIFI, CELL; any other
value leaves it unknown) and stays in the note as "source: <value>".

Not supported (refused, never guessed): the iOS Timeline export
(a top-level JSON array with "geo:" strings) and any other JSON layout. waypointPath has
no times and is not imported. Visits and activities are the provider's inference; their
points say so in ``note`` and are never GEOSnap stays by themselves.

Records.json is streamed (files reach gigabytes): the locations array is decoded element
by element with json.JSONDecoder.raw_decode on a sliding buffer. The array is parsed
strictly: exactly one comma between records, and only whitespace and the closing "}"
after the "]". The Timeline and Semantic files are read whole (hashing as they are read)
and then parsed; one larger than MAX_WHOLE_DOCUMENT_BYTES is refused.

A member name that occurs twice in one object (JSON would keep the last one silently)
rejects the location record, or the Timeline or Semantic entry (segment, raw signal,
frequent place, timeline object) it lies in; outside any entry it refuses the file.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from geosnap.extraction.extractor import ExtractionProgress
from geosnap.extraction.json_stream import (
    compact_json_text,
    repeated_member_names,
    repeated_names_reason,
)
from geosnap.extraction.models import (
    UNRECOGNISED_FORMAT,
    ExtractionCounters,
    GeoPoint,
    ReferencePlace,
    RejectedLine,
    SourceExtractionOutcome,
    SourceFormatError,
)
from geosnap.extraction.reader_support import (
    NUMBER_TOO_LONG_REASON,
    ORIGINAL_RECORD_MAX_CHARS,
    PLAUSIBLE_TIME_RANGE_UTC,
    OversizedNumber,
    bounded_int,
    checked_position,
    excerpt,
    implausible_time_text,
    json_int,
    parse_iso_timestamp,
    record_text,
    truncate,
    whole_file_read_report,
)
from geosnap.extraction.source_reader import UTF8_BOM, SourceReadReport
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

FORMAT_RECORDS = "google-records"
FORMAT_TIMELINE = "google-timeline"
FORMAT_SEMANTIC = "google-semantic"
SNIFF_BYTES = 65_536
CANCEL_CHECK_INTERVAL_RECORDS = 1000
# One record larger than this cannot be a location record; the file is refused.
MAX_RECORD_CHARS = 16 * 1_048_576
# Timeline and Semantic files are decoded whole (about ten times their size in memory);
# a larger file is refused so that memory cannot run out.
MAX_WHOLE_DOCUMENT_BYTES = 256 * 1_048_576
E7_OVERFLOW_LIMIT = 1_800_000_000
E7_OVERFLOW_CORRECTION = 2**32
NOTE_SEPARATOR = " · "
NOTE_MAX_CHARS = 500

RECORDS_START_PATTERN = re.compile(r'\s*\{\s*"locations"\s*:\s*\[')
TIMELINE_START_PATTERN = re.compile(
    r'\s*\{\s*"(?:semanticSegments|rawSignals|userLocationProfile)"\s*:'
)
SEMANTIC_START_PATTERN = re.compile(r'\s*\{\s*"timelineObjects"\s*:')
LAT_LNG_TEXT_PATTERN = re.compile(
    r"\s*([+-]?\d+(?:\.\d+)?)\s*°?\s*,\s*([+-]?\d+(?:\.\d+)?)\s*°?\s*", re.ASCII
)
JSON_WHITESPACE = " \t\r\n"
NAIVE_TIMESTAMP_REASON = "timestamp without offset"
# Google's ``source`` values that name a positioning method; every other one names none.
GOOGLE_SOURCE_METHODS = {"GPS": "gnss", "WIFI": "wifi", "CELL": "cell"}
UNRECOGNISED_JSON_REASON = (
    "not a recognised Google location export: expected a JSON object starting with "
    '"locations" (Records.json), "semanticSegments", "rawSignals" or '
    '"userLocationProfile" (on-device Timeline export) or "timelineObjects" (Semantic '
    "Location History); the iOS Timeline export (a top-level array) is not supported"
)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

JsonObject = dict[str, Any]
# The lists whose elements are the entries of a Timeline or Semantic file (paths from the
# top-level object); a member name repeated outside them refuses the whole file.
TIMELINE_ENTRY_LISTS = (
    ("semanticSegments",),
    ("rawSignals",),
    ("userLocationProfile", "frequentPlaces"),
)
SEMANTIC_ENTRY_LISTS = (("timelineObjects",),)


class _ObjectWithRepeatedNames(dict[str, Any]):
    """A JSON object in which a member name occurred twice; json kept the last value."""

    def __init__(self, members: dict[str, Any], repeated_names: list[str]) -> None:
        super().__init__(members)
        self.repeated_names = repeated_names


class GoogleFormatError(SourceFormatError):
    """Not a supported Google export, not UTF-8, malformed JSON or an unexpected layout."""


def detect_google_format(path: Path) -> str:
    """google-records, google-timeline or google-semantic from the first 64 KB, or
    UNRECOGNISED_FORMAT."""
    with path.open("rb") as source_file:
        sample = source_file.read(SNIFF_BYTES)
    text = codecs.getincrementaldecoder("utf-8-sig")("replace").decode(sample, final=False)
    if RECORDS_START_PATTERN.match(text):
        return FORMAT_RECORDS
    if TIMELINE_START_PATTERN.match(text):
        return FORMAT_TIMELINE
    if SEMANTIC_START_PATTERN.match(text):
        return FORMAT_SEMANTIC
    return UNRECOGNISED_FORMAT


def extract_google(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
    source_format: str = FORMAT_RECORDS,
) -> SourceExtractionOutcome:
    """Read one Google export in the given format; raises GoogleFormatError (with the
    partial outcome where bytes were read) for an unsupported or broken file."""
    if source_format not in (FORMAT_RECORDS, FORMAT_TIMELINE, FORMAT_SEMANTIC):
        read_report = whole_file_read_report(source_path, settings.read_chunk_bytes, "unknown")
        raise GoogleFormatError(
            f"{source_path.name}: {UNRECOGNISED_JSON_REASON}",
            SourceExtractionOutcome([], [], ExtractionCounters(), read_report, False),
        )
    session = _ReadSession(source_path, settings, on_rejected, on_progress, is_cancelled)
    records = _RecordCollector(source_id, on_rejected)
    session.records = records
    logger.info("Google %s extraction started: %s", source_format, source_path.name)
    with source_path.open("rb") as source_file:
        session.size_bytes = os.fstat(source_file.fileno()).st_size
        if source_format == FORMAT_RECORDS:
            _read_records_stream(source_file, session, records)
        else:
            document = session.read_whole_document(source_file)
            if document is not None:
                if source_format == FORMAT_TIMELINE:
                    session.check_repeated_names_outside_entries(document, TIMELINE_ENTRY_LISTS)
                    _read_timeline(document, session, records)
                else:
                    session.check_repeated_names_outside_entries(document, SEMANTIC_ENTRY_LISTS)
                    _read_semantic(document, session, records)
    finished = session.outcome()
    session.report_progress()
    logger.info(
        "Google extraction finished: accepted=%d invalid=%d unrelated=%d saved_places=%d "
        "records=%d cancelled=%s",
        records.counters.accepted,
        records.counters.invalid,
        records.counters.unrelated,
        len(records.reference_places),
        records.record_count,
        session.cancelled,
    )
    return finished


class _RecordCollector:
    """Numbers the records and collects points, saved places and rejections."""

    def __init__(self, source_id: int, on_rejected: Callable[[RejectedLine], None]) -> None:
        self.source_id = source_id
        self.on_rejected = on_rejected
        self.points: list[GeoPoint] = []
        self.reference_places: list[ReferencePlace] = []
        self.counters = ExtractionCounters()
        self.record_count = 0

    def next_record(self) -> int:
        self.record_count += 1
        return self.record_count

    def accept(
        self,
        record_number: int,
        position: tuple[float, float],
        timestamp_utc: datetime,
        original_record: str,
        accuracy_m: float | None,
        label: str | None,
        note: str | None,
        positioning_method: str | None = None,
    ) -> None:
        self.points.append(
            GeoPoint(
                line_number=record_number,
                latitude=position[0],
                longitude=position[1],
                latitude_accuracy_m=accuracy_m or 0.0,
                longitude_accuracy_m=accuracy_m or 0.0,
                timestamp_utc=timestamp_utc,
                original_line=original_record,
                source_id=self.source_id,
                label=record_text(label),
                note=record_text(note),
                accuracy_known=accuracy_m is not None,
                positioning_method=positioning_method,
            )
        )
        self.counters.accepted += 1

    def reject(self, record_number: int, reason: str, original_record: str) -> None:
        self.counters.invalid += 1
        if reason.startswith(NAIVE_TIMESTAMP_REASON):
            self.counters.naive_timestamps += 1
        self.on_rejected(RejectedLine(record_number, reason, original_record))
        logger.warning("Google record %d rejected: %s", record_number, reason)


class _ReadSession:
    """Byte counting, hashing, progress, cancellation and the outcome of one read."""

    def __init__(
        self,
        source_path: Path,
        settings: ExtractionSettings,
        on_rejected: Callable[[RejectedLine], None],
        on_progress: Callable[[ExtractionProgress], None] | None,
        is_cancelled: Callable[[], bool] | None,
    ) -> None:
        self.source_path = source_path
        self.chunk_bytes = settings.read_chunk_bytes
        self.on_progress = on_progress
        self.is_cancelled = is_cancelled
        self.digest = hashlib.sha256()
        self.bytes_read = 0
        self.size_bytes = 0
        self.encoding = "utf-8"
        self.cancelled = False
        self.records: _RecordCollector | None = None
        self._decoder: codecs.IncrementalDecoder | None = None
        self._loop_steps = 0
        # Set by the whole-document decoding when any object repeated a member name.
        self.repeated_names_seen = False

    def read_text_chunk(self, source_file: Any) -> str | None:
        """The next chunk decoded as UTF-8 (BOM skipped); None at the end of the file."""
        chunk: bytes = source_file.read(self.chunk_bytes)
        if self._decoder is None:
            if chunk.startswith(UTF8_BOM):
                self.encoding = "utf-8-sig"
            self._decoder = codecs.getincrementaldecoder(self.encoding)("strict")
        if chunk:
            self.digest.update(chunk)
            self.bytes_read += len(chunk)
            self.report_progress()
        try:
            text = self._decoder.decode(chunk, final=not chunk)
        except UnicodeDecodeError as error:
            # error.object holds the bytes still pending from earlier chunks plus this one.
            byte_offset = self.bytes_read - len(error.object) + error.start
            raise self.format_error(
                f"{self.source_path.name} is not UTF-8 JSON (byte {byte_offset}): {error}"
            ) from error
        return text if chunk else None

    def read_whole_document(self, source_file: Any) -> JsonObject | None:
        """The whole JSON object; None when cancelled while reading. A file beyond
        MAX_WHOLE_DOCUMENT_BYTES is refused once that many bytes have been read."""
        parts: list[str] = []
        while (text := self.read_text_chunk(source_file)) is not None:
            if self.bytes_read > MAX_WHOLE_DOCUMENT_BYTES:
                raise self.format_error(
                    f"{self.source_path.name} is larger than {MAX_WHOLE_DOCUMENT_BYTES} bytes,"
                    " the limit for a Timeline or Semantic Location History file (read whole)"
                )
            parts.append(text)
            if self.cancellation_requested():
                self.cancelled = True
                return None
        try:
            document = json.loads(
                "".join(parts),
                parse_int=json_int,
                object_pairs_hook=self._object_noting_repeated_names,
            )
        except json.JSONDecodeError as error:
            raise self.format_error(
                f"malformed JSON in {self.source_path.name} at line {error.lineno}, "
                f"column {error.colno}: {error.msg}"
            ) from error
        except RecursionError as error:
            raise self.format_error(
                f"{self.source_path.name}: JSON nested too deeply to be a location export"
            ) from error
        if not isinstance(document, dict):
            raise self.format_error(f"{self.source_path.name}: {UNRECOGNISED_JSON_REASON}")
        return document

    def _object_noting_repeated_names(self, pairs: list[tuple[str, Any]]) -> JsonObject:
        json_object = dict(pairs)
        if len(json_object) == len(pairs):
            return json_object
        self.repeated_names_seen = True
        return _ObjectWithRepeatedNames(json_object, repeated_member_names(pairs))

    def check_repeated_names_outside_entries(
        self, document: JsonObject, entry_lists: tuple[tuple[str, ...], ...]
    ) -> None:
        """Refuse the file when a member name occurs twice in an object that belongs to
        no single entry: which of the two values was meant cannot be told."""
        if not self.repeated_names_seen:
            return
        entry_ids: set[int] = set()
        for entry_path in entry_lists:
            container: object = document
            for key in entry_path:
                container = container.get(key) if isinstance(container, dict) else None
            if isinstance(container, list):
                entry_ids.update(id(entry) for entry in container)
        repeated = _repeated_names_within(document, entry_ids)
        if repeated:
            raise self.format_error(
                f"{self.source_path.name}: {repeated_names_reason(repeated)} (outside any "
                "single entry)"
            )

    def repeated_names_in_entry(self, entry: object) -> list[str]:
        """The member names repeated anywhere inside one entry ([] in the usual case)."""
        if not self.repeated_names_seen:
            return []
        return _repeated_names_within(entry, set())

    def cancellation_requested(self) -> bool:
        return self.is_cancelled is not None and self.is_cancelled()

    def check_cancel(self) -> bool:
        """Called once per loop step of every loop; asks every CANCEL_CHECK_INTERVAL_RECORDS
        steps. True once cancelled."""
        self._loop_steps += 1
        if (
            not self.cancelled
            and self._loop_steps % CANCEL_CHECK_INTERVAL_RECORDS == 0
            and self.cancellation_requested()
        ):
            self.cancelled = True
        return self.cancelled

    def report_progress(self) -> None:
        if self.on_progress is None:
            return
        counters = self.records.counters if self.records else ExtractionCounters()
        self.on_progress(
            ExtractionProgress(
                bytes_read=self.bytes_read,
                size_bytes=self.size_bytes,
                accepted=counters.accepted,
                invalid=counters.invalid,
                unrelated=counters.unrelated,
            )
        )

    def outcome(self) -> SourceExtractionOutcome:
        records = self.records or _RecordCollector(0, lambda _: None)
        read_report = SourceReadReport(
            sha256_hex=self.digest.hexdigest(),
            size_bytes=self.size_bytes,
            bytes_read=self.bytes_read,
            encoding=self.encoding,
            line_count=records.record_count,
            decode_replacements=0,
        )
        return SourceExtractionOutcome(
            records.points,
            records.reference_places,
            records.counters,
            read_report,
            self.cancelled,
        )

    def format_error(self, message: str) -> GoogleFormatError:
        return GoogleFormatError(message, self.outcome())


# --- Records.json -----------------------------------------------------------------------


def _read_records_stream(
    source_file: Any, session: _ReadSession, records: _RecordCollector
) -> None:
    repeated_names: list[str] = []

    def object_noting_repeated_names(pairs: list[tuple[str, Any]]) -> JsonObject:
        json_object = dict(pairs)
        if len(json_object) != len(pairs):
            repeated_names.extend(repeated_member_names(pairs))
        return json_object

    decoder = json.JSONDecoder(parse_int=json_int, object_pairs_hook=object_noting_repeated_names)
    buffer = ""
    position = 0
    at_end = False

    def fill() -> bool:
        """Append the next chunk to the unread rest; False at the end of the file."""
        nonlocal buffer, position, at_end
        text = session.read_text_chunk(source_file)
        if text is None:
            at_end = True
            return False
        buffer = buffer[position:] + text
        position = 0
        return True

    while not at_end and len(buffer) < SNIFF_BYTES and fill():
        pass
    start = RECORDS_START_PATTERN.match(buffer)
    if start is None:
        raise session.format_error(f"{session.source_path.name}: {UNRECOGNISED_JSON_REASON}")
    position = start.end()
    expecting_record = True
    while True:
        while position < len(buffer) and buffer[position] in JSON_WHITESPACE:
            position += 1
        if position >= len(buffer):
            if fill():
                continue
            raise session.format_error(
                f"malformed JSON in {session.source_path.name}: the locations array is not closed"
            )
        character = buffer[position]
        if not expecting_record:
            if character == ",":
                position += 1
                expecting_record = True
                continue
            if character == "]":
                position += 1
                break
            raise session.format_error(
                f"malformed JSON in {session.source_path.name}: expected ',' or ']' after "
                f"location record {records.record_count}"
            )
        if character == "]" and records.record_count == 0:
            position += 1
            break
        if character in ",]":
            raise session.format_error(
                f"malformed JSON in {session.source_path.name}: expected a location record "
                f"after '{',' if records.record_count else '['}'"
            )
        repeated_names.clear()
        try:
            element, end = decoder.raw_decode(buffer, position)
        except RecursionError as error:
            raise session.format_error(
                f"{session.source_path.name}: location record {records.record_count + 1} is "
                "nested too deeply"
            ) from error
        except json.JSONDecodeError as error:
            if len(buffer) - position > MAX_RECORD_CHARS or not fill():
                raise session.format_error(
                    f"malformed JSON in {session.source_path.name} in location record "
                    f"{records.record_count + 1}: {error.msg}"
                ) from error
            continue
        if end == len(buffer) and not at_end and fill():
            # A value touching the buffer end (a number) may continue in the next chunk.
            continue
        record_start, position = position, end
        expecting_record = False
        record_number = records.next_record()
        if repeated_names:
            record_as_written = compact_json_text(buffer[record_start:end])
            records.reject(
                record_number,
                repeated_names_reason(repeated_names),
                truncate(record_as_written, ORIGINAL_RECORD_MAX_CHARS),
            )
        else:
            _read_location_record(element, record_number, records)
        if session.check_cancel():
            logger.warning("Google extraction cancelled after record %d", record_number)
            return
        if session.bytes_read and position > len(buffer) // 2 and not at_end:
            buffer, position = buffer[position:], 0
    # After the array: only whitespace and the "}" closing the top-level object.
    closing_brace_seen = False
    trailing_text: str | None = buffer[position:]
    while trailing_text is not None:
        stripped = trailing_text.strip(JSON_WHITESPACE)
        if stripped == "}" and not closing_brace_seen:
            closing_brace_seen = True
        elif stripped:
            raise session.format_error(
                f"malformed JSON in {session.source_path.name}: unexpected content after the "
                f"locations array: {excerpt(stripped)}"
            )
        if session.cancellation_requested():
            session.cancelled = True
            return
        trailing_text = session.read_text_chunk(source_file)
    if not closing_brace_seen:
        raise session.format_error(
            f"malformed JSON in {session.source_path.name}: the top-level object is not closed"
        )


def _read_location_record(element: object, record_number: int, records: _RecordCollector) -> None:
    original_record = _original(element)
    if not isinstance(element, dict):
        records.reject(record_number, "record is not a JSON object", original_record)
        return
    position = _e7_position(element, "latitudeE7", "longitudeE7", overflow_fix=True)
    if isinstance(position, str):
        records.reject(record_number, position, original_record)
        return
    if "timestamp" in element:
        timestamp = _iso_time(element["timestamp"])
    elif "timestampMs" in element:
        timestamp = _milliseconds_time(element["timestampMs"])
    else:
        timestamp = "missing timestamp"
    if isinstance(timestamp, str):
        records.reject(record_number, timestamp, original_record)
        return
    accuracy = _accuracy(element.get("accuracy"))
    if isinstance(accuracy, str):
        records.reject(record_number, accuracy, original_record)
        return
    note_parts = [
        f"{key}: {element[key]}"
        for key in ("source", "deviceTag")
        if isinstance(element.get(key), str | int) and not isinstance(element.get(key), bool)
    ]
    records.accept(
        record_number,
        position,
        timestamp,
        original_record,
        accuracy,
        None,
        _note(note_parts),
        _source_method(element.get("source")),
    )


# --- On-device Timeline export ----------------------------------------------------------


def _read_timeline(document: JsonObject, session: _ReadSession, records: _RecordCollector) -> None:
    for segment in _list_member(document, "semanticSegments", session):
        if session.check_cancel():
            return
        if not isinstance(segment, dict):
            _reject_unexpected(segment, records)
            continue
        if _reject_repeated_names(segment, session, records):
            continue
        handled = False
        if isinstance(segment.get("visit"), dict):
            _read_timeline_visit(segment, records)
            handled = True
        if isinstance(segment.get("activity"), dict):
            _read_timeline_activity(segment, records)
            handled = True
        timeline_path = segment.get("timelinePath")
        if timeline_path is not None and not isinstance(timeline_path, list):
            records.reject(records.next_record(), "timelinePath is not a list", _original(segment))
            continue
        if not handled and not timeline_path:
            # e.g. a timelineMemory segment: nothing GEOSnap imports.
            records.next_record()
            records.counters.unrelated += 1
            continue
        for path_point in timeline_path or []:
            if session.check_cancel():
                return
            record_number = records.next_record()
            original_record = _original(path_point)
            if not isinstance(path_point, dict):
                records.reject(record_number, "path point is not a JSON object", original_record)
                continue
            _accept_text_position(
                records,
                record_number,
                path_point.get("point"),
                path_point.get("time"),
                original_record,
                None,
                None,
                "Google timelinePath",
            )
    for raw_signal in _list_member(document, "rawSignals", session):
        if session.check_cancel():
            return
        if not isinstance(raw_signal, dict):
            _reject_unexpected(raw_signal, records)
            continue
        if _reject_repeated_names(raw_signal, session, records):
            continue
        record_number = records.next_record()
        original_record = _original(raw_signal)
        position_record = raw_signal.get("position")
        if not isinstance(position_record, dict):
            records.counters.unrelated += 1
            continue
        accuracy = _accuracy(position_record.get("accuracyMeters"))
        if isinstance(accuracy, str):
            records.reject(record_number, accuracy, original_record)
            continue
        note_parts = ["Google rawSignals position"]
        if isinstance(position_record.get("source"), str):
            note_parts.append(f"source: {position_record['source']}")
        _accept_text_position(
            records,
            record_number,
            position_record.get("LatLng"),
            position_record.get("timestamp"),
            original_record,
            accuracy,
            None,
            _note(note_parts),
            _source_method(position_record.get("source")),
        )
    profile = document.get("userLocationProfile")
    frequent_places = profile.get("frequentPlaces") if isinstance(profile, dict) else None
    for frequent_place in frequent_places if isinstance(frequent_places, list) else []:
        if session.check_cancel():
            return
        if _reject_repeated_names(frequent_place, session, records):
            continue
        record_number = records.next_record()
        original_record = _original(frequent_place)
        if not isinstance(frequent_place, dict):
            records.reject(record_number, "frequent place is not a JSON object", original_record)
            continue
        position = _text_position(frequent_place.get("placeLocation"))
        if isinstance(position, str):
            records.reject(record_number, position, original_record)
            continue
        place_id = frequent_place.get("placeId")
        records.counters.undated += 1
        records.reference_places.append(
            ReferencePlace(
                source_id=records.source_id,
                record_number=record_number,
                name=record_text(str(frequent_place.get("label") or "")) or "",
                note=record_text(
                    _note(
                        ["Google frequent place (provider inference)"]
                        + ([f"placeId: {place_id}"] if place_id else [])
                    )
                ),
                latitude=position[0],
                longitude=position[1],
                original_record=original_record,
            )
        )


def _read_timeline_visit(segment: JsonObject, records: _RecordCollector) -> None:
    record_number = records.next_record()
    visit = segment["visit"]
    original_record = _original(visit)
    candidate = visit.get("topCandidate")
    if not isinstance(candidate, dict):
        records.reject(record_number, "visit without topCandidate", original_record)
        return
    location = candidate.get("placeLocation")
    lat_lng = location.get("latLng") if isinstance(location, dict) else None
    details = [f"{key}: {candidate[key]}" for key in ("placeId", "probability") if key in candidate]
    note = _note(["Google visit (provider inference)", *details])
    label = (
        candidate.get("semanticType") if isinstance(candidate.get("semanticType"), str) else None
    )
    _accept_segment_ends(
        records, record_number, segment, lat_lng, lat_lng, original_record, label, note
    )


def _read_timeline_activity(segment: JsonObject, records: _RecordCollector) -> None:
    record_number = records.next_record()
    activity = segment["activity"]
    original_record = _original(activity)
    start, end = activity.get("start"), activity.get("end")
    candidate = activity.get("topCandidate")
    details = []
    if isinstance(candidate, dict) and isinstance(candidate.get("type"), str):
        details.append(f"activityType: {candidate['type']}")
    distance = activity.get("distanceMeters")
    if _is_number(distance):
        details.append(f"distance: {distance:g} m")
    note = _note(["Google activity (provider inference)", *details])
    _accept_segment_ends(
        records,
        record_number,
        segment,
        start.get("latLng") if isinstance(start, dict) else None,
        end.get("latLng") if isinstance(end, dict) else None,
        original_record,
        None,
        note,
    )


def _accept_segment_ends(
    records: _RecordCollector,
    record_number: int,
    segment: JsonObject,
    start_lat_lng: object,
    end_lat_lng: object,
    original_record: str,
    label: str | None,
    note: str | None,
) -> None:
    """A point at startTime and one at endTime; the first failure rejects the record."""
    checked = []
    for lat_lng, time_key in ((start_lat_lng, "startTime"), (end_lat_lng, "endTime")):
        position = _text_position(lat_lng)
        timestamp = _iso_time(segment.get(time_key))
        for outcome in (position, timestamp):
            if isinstance(outcome, str):
                records.reject(record_number, outcome, original_record)
                return
        checked.append((position, timestamp))
    for position, timestamp in checked:
        assert not isinstance(position, str) and not isinstance(timestamp, str)
        records.accept(record_number, position, timestamp, original_record, None, label, note)


def _accept_text_position(
    records: _RecordCollector,
    record_number: int,
    lat_lng: object,
    time_value: object,
    original_record: str,
    accuracy: float | None,
    label: str | None,
    note: str | None,
    positioning_method: str | None = None,
) -> None:
    position = _text_position(lat_lng)
    if isinstance(position, str):
        records.reject(record_number, position, original_record)
        return
    timestamp = _iso_time(time_value)
    if isinstance(timestamp, str):
        records.reject(record_number, timestamp, original_record)
        return
    records.accept(
        record_number,
        position,
        timestamp,
        original_record,
        accuracy,
        label,
        note,
        positioning_method,
    )


# --- Semantic Location History ----------------------------------------------------------


def _read_semantic(document: JsonObject, session: _ReadSession, records: _RecordCollector) -> None:
    for timeline_object in _list_member(document, "timelineObjects", session):
        if session.check_cancel():
            return
        if not isinstance(timeline_object, dict):
            _reject_unexpected(timeline_object, records)
        elif _reject_repeated_names(timeline_object, session, records):
            continue
        elif isinstance(timeline_object.get("placeVisit"), dict):
            _read_place_visit(timeline_object["placeVisit"], records)
        elif isinstance(timeline_object.get("activitySegment"), dict):
            _read_activity_segment(timeline_object["activitySegment"], session, records)
            if session.cancelled:
                return
        else:
            records.next_record()
            records.counters.unrelated += 1


def _read_place_visit(place_visit: JsonObject, records: _RecordCollector) -> None:
    record_number = records.next_record()
    original_record = _original(place_visit)
    location = place_visit.get("location")
    location = location if isinstance(location, dict) else {}
    details = [
        f"{key}: {value}"
        for key, value in (
            ("address", location.get("address")),
            ("placeConfidence", place_visit.get("placeConfidence")),
        )
        if isinstance(value, str) and value
    ]
    note = _note(["Google placeVisit (provider inference)", *details])
    label = location.get("name") if isinstance(location.get("name"), str) else None
    _accept_duration_ends(
        records, record_number, place_visit, location, location, original_record, label, note
    )


def _read_activity_segment(
    segment: JsonObject, session: _ReadSession, records: _RecordCollector
) -> None:
    record_number = records.next_record()
    original_record = _original(segment)
    details = []
    if isinstance(segment.get("activityType"), str):
        details.append(f"activityType: {segment['activityType']}")
    if _is_number(segment.get("distance")):
        details.append(f"distance: {segment['distance']:g} m")
    note = _note(["Google activitySegment (provider inference)", *details])
    start, end = segment.get("startLocation"), segment.get("endLocation")
    _accept_duration_ends(
        records,
        record_number,
        segment,
        start if isinstance(start, dict) else {},
        end if isinstance(end, dict) else {},
        original_record,
        None,
        note,
    )
    raw_path = segment.get("simplifiedRawPath")
    raw_points = raw_path.get("points") if isinstance(raw_path, dict) else None
    for raw_point in raw_points if isinstance(raw_points, list) else []:
        if session.check_cancel():
            return
        point_number = records.next_record()
        point_record = _original(raw_point)
        if not isinstance(raw_point, dict):
            records.reject(point_number, "path point is not a JSON object", point_record)
            continue
        position = _e7_position(raw_point, "latE7", "lngE7", overflow_fix=False)
        if "timestamp" in raw_point:
            timestamp = _iso_time(raw_point["timestamp"])
        elif "timestampMs" in raw_point:
            timestamp = _milliseconds_time(raw_point["timestampMs"])
        else:
            timestamp = "missing timestamp"
        accuracy = _accuracy(raw_point.get("accuracyMeters"))
        problem = next(
            (value for value in (position, timestamp, accuracy) if isinstance(value, str)), None
        )
        if problem is not None:
            records.reject(point_number, problem, point_record)
            continue
        assert not isinstance(position, str) and not isinstance(timestamp, str)
        assert not isinstance(accuracy, str)
        point_source = raw_point.get("source")
        point_note = ["Google simplifiedRawPath"]
        if isinstance(point_source, str):
            point_note.append(f"source: {point_source}")
        records.accept(
            point_number,
            position,
            timestamp,
            point_record,
            accuracy,
            None,
            _note(point_note),
            _source_method(point_source),
        )


def _accept_duration_ends(
    records: _RecordCollector,
    record_number: int,
    entry: JsonObject,
    start_location: JsonObject,
    end_location: JsonObject,
    original_record: str,
    label: str | None,
    note: str | None,
) -> None:
    duration = entry.get("duration")
    duration = duration if isinstance(duration, dict) else {}
    checked = []
    for location, time_key in ((start_location, "startTimestamp"), (end_location, "endTimestamp")):
        position = _e7_position(location, "latitudeE7", "longitudeE7", overflow_fix=False)
        if time_key in duration:
            timestamp = _iso_time(duration[time_key])
        elif f"{time_key}Ms" in duration:
            timestamp = _milliseconds_time(duration[f"{time_key}Ms"])
        else:
            timestamp = f"missing duration.{time_key}"
        for outcome in (position, timestamp):
            if isinstance(outcome, str):
                records.reject(record_number, outcome, original_record)
                return
        checked.append((position, timestamp))
    for position, timestamp in checked:
        assert not isinstance(position, str) and not isinstance(timestamp, str)
        records.accept(record_number, position, timestamp, original_record, None, label, note)


# --- Values -----------------------------------------------------------------------------


def _list_member(document: JsonObject, key: str, session: _ReadSession) -> list[Any]:
    member = document.get(key, [])
    if not isinstance(member, list):
        raise session.format_error(f"{session.source_path.name}: {key} is not a list")
    return member


def _repeated_names_within(value: object, skipped_ids: set[int]) -> list[str]:
    """The repeated member names of every object inside ``value``, not descending into the
    objects whose id is in ``skipped_ids``."""
    repeated: list[str] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if id(current) in skipped_ids:
            continue
        if isinstance(current, _ObjectWithRepeatedNames):
            repeated.extend(current.repeated_names)
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return repeated


def _reject_repeated_names(entry: object, session: _ReadSession, records: _RecordCollector) -> bool:
    """Reject the entry as one record when a member name occurs twice inside it; True then."""
    repeated = session.repeated_names_in_entry(entry)
    if not repeated:
        return False
    records.reject(records.next_record(), repeated_names_reason(repeated), _original(entry))
    return True


def _reject_unexpected(element: object, records: _RecordCollector) -> None:
    records.reject(records.next_record(), "entry is not a JSON object", _original(element))


def _e7_position(
    record: JsonObject, latitude_key: str, longitude_key: str, overflow_fix: bool
) -> tuple[float, float] | str:
    if latitude_key not in record or longitude_key not in record:
        return f"missing {latitude_key} or {longitude_key}"
    latitude_e7, longitude_e7 = record[latitude_key], record[longitude_key]
    for key, value in ((latitude_key, latitude_e7), (longitude_key, longitude_e7)):
        if isinstance(value, OversizedNumber):
            return f"{key}: {NUMBER_TOO_LONG_REASON}"
        if not _is_integer(value):
            return f"{key} is not an integer: {excerpt(str(value))}"
    assert isinstance(latitude_e7, int) and isinstance(longitude_e7, int)
    if overflow_fix:
        # Documented Takeout quirk: values above 1800000000 wrapped around 2^32.
        if latitude_e7 > E7_OVERFLOW_LIMIT:
            latitude_e7 -= E7_OVERFLOW_CORRECTION
        if longitude_e7 > E7_OVERFLOW_LIMIT:
            longitude_e7 -= E7_OVERFLOW_CORRECTION
    return checked_position(latitude_e7 / 1e7, longitude_e7 / 1e7)


def _text_position(lat_lng: object) -> tuple[float, float] | str:
    """ "51.1234567°, 7.1234567°" (degree signs optional)."""
    if not isinstance(lat_lng, str):
        return "missing position"
    match = LAT_LNG_TEXT_PATTERN.fullmatch(lat_lng)
    if match is None:
        return f"unparsable position: {excerpt(lat_lng)}"
    return checked_position(float(match.group(1)), float(match.group(2)))


def _iso_time(value: object) -> datetime | str:
    """ISO 8601 with offset; a time without offset is refused, never assumed UTC."""
    if not isinstance(value, str):
        return "missing timestamp"
    parsed = parse_iso_timestamp(value.strip())
    if parsed is None:
        return f"unparsable timestamp: {excerpt(value)}"
    if isinstance(parsed, str):
        return parsed
    timestamp_utc, zone_given = parsed
    if not zone_given:
        return f"{NAIVE_TIMESTAMP_REASON}: {excerpt(value)}"
    return timestamp_utc


def _milliseconds_time(value: object) -> datetime | str:
    if isinstance(value, OversizedNumber):
        return f"timestampMs: {NUMBER_TOO_LONG_REASON}"
    text = str(value) if _is_integer(value) or isinstance(value, str) else ""
    if not text.isascii() or not text.isdigit():
        return f"unparsable timestampMs: {excerpt(str(value))}"
    milliseconds = bounded_int(text)
    if milliseconds is None:
        return f"timestampMs: {NUMBER_TOO_LONG_REASON}"
    earliest, latest = PLAUSIBLE_TIME_RANGE_UTC
    if not earliest.timestamp() * 1000 <= milliseconds < latest.timestamp() * 1000:
        return f"timestampMs: {implausible_time_text(text)}"
    return UNIX_EPOCH + timedelta(milliseconds=milliseconds)


def _accuracy(value: object) -> float | None | str:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"invalid accuracy: {excerpt(str(value))}"
    if not math.isfinite(value) or value < 0:
        return f"invalid accuracy: {excerpt(str(value))}"
    return float(value)


def _source_method(source: object) -> str | None:
    """The positioning method a Google ``source`` value names, None for any other value."""
    return GOOGLE_SOURCE_METHODS.get(source.strip().upper()) if isinstance(source, str) else None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _note(parts: list[str]) -> str | None:
    """Note parts joined as in every reader: a descriptive header part of its own, then
    named fields as "name: value" (one tooltip row each)."""
    if not parts:
        return None
    return truncate(NOTE_SEPARATOR.join(parts), NOTE_MAX_CHARS)


def _original(element: object) -> str:
    return truncate(
        json.dumps(element, ensure_ascii=False, separators=(",", ":")), ORIGINAL_RECORD_MAX_CHARS
    )
