# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Read GeoJSON (RFC 7946): a FeatureCollection streamed feature by feature, or one Feature.

Positions are ``[longitude, latitude[, altitude]]`` in WGS 84 by definition of the
format; a ``crs`` member is ignored. Point features become points, a MultiPoint one point
per position; every other geometry (LineString, Polygon, ..., null) counts as unrelated
unless a GeometryCollection holds a Point. Everything else comes from ``properties``:

- the timestamp: the one property whose name matches the timestamp synonyms of the CSV
  reader (case-insensitive, NFC), or a date property joined with a time-of-day property
  under a plain time name ("time", "Uhrzeit"; not "elapsed_time", which refuses a bare
  date naming it); end-qualified properties ("end_time") stay in the note and never give
  the start, and one without any start refuses the feature (as a KML TimeSpan without a
  begin does); several candidates refuse the feature naming them, none makes it a saved
  place. Values are read per cell like the CSV "automatic" time format; a time without
  offset needs the zone handed to the reader (the project's display zone when it names
  one), else the feature is refused with the CSV wording,
- the accuracy in metres from the one property matching the accuracy synonyms; a property
  named "radius" is taken as accuracy too (a cell sector radius then makes the record wide,
  never falsely exact) and the note says "[GEOSnap] radius taken as accuracy",
- the label from the one property matching the label synonyms,
- the positioning method from the properties matching the positioning method synonyms
  (reader_support.positioning_method_from_text); properties naming different methods
  leave it unknown and the note says so; these properties stay in the note,
- every other property as a "name: value" note part, nested objects flattened with dot
  paths, arrays as JSON text, bounded like the CSV columns without a role.

``original_line`` is the feature's own text as written in the file with the whitespace
outside strings removed (key order, repeated keys and number spellings kept), cut at
ORIGINAL_RECORD_MAX_CHARS; a single-Feature document is assembled from its members' text.
A member name that occurs twice in one object refuses the feature (a repeated top-level
name, the document): JSON would keep the last one silently.
The document is decoded value by value with json_stream, so memory stays flat however
many features a FeatureCollection holds; one feature may span at most MAX_FEATURE_CHARS.
"""

from __future__ import annotations

import codecs
import json
import logging
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from geosnap.extraction.csv_reader import (
    AMBIGUOUS_TIME_NOTE,
    AUTOMATIC_TIME_FORMAT,
    DATE_WORDS,
    END_WORDS,
    HEADER_SYNONYMS,
    MISSING_ZONE_REASON,
    START_WORDS,
    TIME_WORDS,
    header_tokens,
    normalised_header,
    parse_timestamp_details,
)
from geosnap.extraction.extractor import ExtractionProgress, report_extraction_progress
from geosnap.extraction.json_stream import (
    JsonValueStream,
    Utf8ChunkSource,
    compact_json_text,
    repeated_names_reason,
)
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
    OversizedNumber,
    bounded_field_parts,
    checked_position,
    disagreeing_methods_note,
    excerpt,
    field_part,
    positioning_method_from_text,
    record_text,
    tool_note,
    truncate,
)
from geosnap.extraction.source_reader import SourceReadReport
from geosnap.extraction.timestamp_text import (
    parse_time_of_day,
    recognise_timestamp,
    time_of_day_text,
)
from geosnap.settings import ExtractionSettings

RADIUS_ACCURACY_NOTE = tool_note("radius taken as accuracy")

logger = logging.getLogger(__name__)

FORMAT_GEOJSON = "geojson"
SNIFF_BYTES = 65_536
CANCEL_CHECK_INTERVAL_FEATURES = 1000
# One feature larger than this cannot be a location record; the file is refused.
MAX_FEATURE_CHARS = 16 * 1_048_576
# A property name deeper than this is not flattened further (its value is JSON text).
MAX_PROPERTY_DEPTH = 8
# How a JSON number that could not be decoded as written is shown in notes and reasons.
NUMBER_TOO_LONG_TEXT = "<number too long>"
NUMBER_OUT_OF_RANGE_TEXT = "<number out of range>"
GEOJSON_TYPE_PATTERN = re.compile(r'"type"\s*:\s*"(?:FeatureCollection|Feature)"')
UNRECOGNISED_JSON_REASON = (
    "not a recognised Google location export (Records.json, on-device Timeline export, "
    "Semantic Location History) and not GeoJSON (a FeatureCollection or Feature, RFC 7946)"
)
# How timestamp_text refuses a date that has no time of day.
DATE_ONLY_REASON = "timestamp with no time of day"
NOT_GEOJSON_REASON = (
    'not GeoJSON: expected a JSON object with "type" "FeatureCollection" or "Feature"'
)


class GeoJsonFormatError(SourceFormatError):
    """Not GeoJSON, not UTF-8, malformed JSON or a feature beyond MAX_FEATURE_CHARS."""


def detect_geojson(path: Path) -> bool:
    """True when the first 64 KB hold ``"type": "FeatureCollection"`` or ``"type":
    "Feature"``; the reader checks the layout properly."""
    with path.open("rb") as source_file:
        sample = source_file.read(SNIFF_BYTES)
    text = codecs.getincrementaldecoder("utf-8-sig")("replace").decode(sample, final=False)
    return GEOJSON_TYPE_PATTERN.search(text) is not None


def extract_geojson(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
    naive_time_zone: str = "",
) -> SourceExtractionOutcome:
    """Read the features once; the record number is the feature's position in the
    collection (1 for a single Feature). ``naive_time_zone`` (an IANA name, or empty) reads
    times without offset. Progress per chunk; cancellation every
    CANCEL_CHECK_INTERVAL_FEATURES features. Raises GeoJsonFormatError (with the partial
    outcome) for a file that is not GeoJSON, not UTF-8 or malformed."""
    features = _FeatureReader(source_id, on_rejected, naive_time_zone)
    counters = features.counters

    def cancellation_requested() -> bool:
        return is_cancelled is not None and is_cancelled()

    with source_path.open("rb") as source_file:
        size_bytes = os.fstat(source_file.fileno()).st_size
        logger.info("GeoJSON extraction started: %s (%d bytes)", source_path.name, size_bytes)
        source = Utf8ChunkSource(
            source_file,
            settings.read_chunk_bytes,
            _ReadFailureError,
            lambda: report_extraction_progress(
                on_progress, source.bytes_read, size_bytes, counters
            ),
        )
        stream = JsonValueStream(source, MAX_FEATURE_CHARS, _ReadFailureError)
        try:
            cancelled = _read_document(stream, features, cancellation_requested)
        except _ReadFailureError as failure:
            raise GeoJsonFormatError(
                f"{source_path.name}: {failure}", _outcome(source, size_bytes, features, False)
            ) from failure
    if cancelled:
        logger.warning("GeoJSON extraction cancelled after feature %d", features.feature_count)
    finished = _outcome(source, size_bytes, features, cancelled)
    report_extraction_progress(on_progress, source.bytes_read, size_bytes, counters)
    logger.info(
        "GeoJSON extraction finished: accepted=%d invalid=%d unrelated=%d saved_places=%d "
        "features=%d cancelled=%s",
        counters.accepted,
        counters.invalid,
        counters.unrelated,
        len(finished.reference_places),
        features.feature_count,
        cancelled,
    )
    return finished


class _ReadFailureError(Exception):
    """Raised by the chunk source and the value stream; re-raised as GeoJsonFormatError
    with the partial outcome by extract_geojson."""


def _outcome(
    source: Utf8ChunkSource, size_bytes: int, features: _FeatureReader, cancelled: bool
) -> SourceExtractionOutcome:
    read_report = SourceReadReport(
        sha256_hex=source.digest.hexdigest(),
        size_bytes=size_bytes,
        bytes_read=source.bytes_read,
        encoding=source.encoding,
        line_count=features.feature_count,
        decode_replacements=0,
    )
    return SourceExtractionOutcome(
        features.points, features.reference_places, features.counters, read_report, cancelled
    )


def _read_document(
    stream: JsonValueStream, features: _FeatureReader, cancellation_requested: Callable[[], bool]
) -> bool:
    """The top-level object member by member: the ``features`` array is streamed, every
    other member is decoded whole (small: type, name, crs, bbox; or the geometry and
    properties of a single Feature). Returns True when cancelled."""
    if stream.peek() != "{":
        raise _ReadFailureError(NOT_GEOJSON_REASON)
    stream.take("{", "at the start of the document")
    members: dict[str, object] = {}
    member_texts: list[str] = []
    repeated_names: list[str] = []
    features_streamed = False
    if not stream.take_if("}"):
        while True:
            key = stream.read_key("the top-level object")
            if key == "features" and stream.peek() == "[":
                if features_streamed:
                    raise _ReadFailureError('malformed JSON: "features" occurs twice')
                features_streamed = True
                if _read_features_array(stream, features, cancellation_requested):
                    return True
            else:
                if key in members:
                    raise _ReadFailureError(
                        f"malformed JSON: top-level member {key!r} occurs twice"
                    )
                value, text = stream.read_value_and_text(f"top-level member {key!r}")
                members[key] = value
                member_texts.append(f"{json.dumps(key, ensure_ascii=False)}:{text}")
                repeated_names.extend(stream.repeated_names)
            if stream.take_if(","):
                continue
            stream.take("}", "after the last top-level member")
            break
    if not stream.at_end():
        raise _ReadFailureError("malformed JSON: unexpected content after the top-level object")
    document_type = members.get("type")
    if document_type == "FeatureCollection":
        if not features_streamed:
            raise _ReadFailureError('not GeoJSON: the FeatureCollection has no "features" array')
        return False
    if document_type == "Feature":
        features.read_feature(members, "{" + ",".join(member_texts) + "}", repeated_names)
        return False
    raise _ReadFailureError(NOT_GEOJSON_REASON)


def _read_features_array(
    stream: JsonValueStream, features: _FeatureReader, cancellation_requested: Callable[[], bool]
) -> bool:
    """The elements of the ``features`` array one by one; True when cancelled."""
    stream.take("[", 'at the start of "features"')
    if stream.take_if("]"):
        return False
    while True:
        feature, text = stream.read_value_and_text(f"feature {features.feature_count + 1}")
        features.read_feature(feature, text, stream.repeated_names)
        if (
            features.feature_count % CANCEL_CHECK_INTERVAL_FEATURES == 0
            and cancellation_requested()
        ):
            return True
        if stream.take_if(","):
            continue
        stream.take("]", f"after feature {features.feature_count}")
        return False


class _FeatureReader:
    """Classifies features one by one and collects the results."""

    def __init__(
        self, source_id: int, on_rejected: Callable[[RejectedLine], None], naive_time_zone: str
    ) -> None:
        self.source_id = source_id
        self.on_rejected = on_rejected
        self.naive_time_zone = naive_time_zone
        self.points: list[GeoPoint] = []
        self.reference_places: list[ReferencePlace] = []
        self.counters = ExtractionCounters()
        self.feature_count = 0
        self._geometry_logged: set[str] = set()

    def read_feature(
        self, feature: object, feature_text: str, repeated_names: Sequence[str] = ()
    ) -> None:
        """``feature_text`` is the feature as written in the file (the original record);
        ``repeated_names`` are member names that occur twice in one of its objects."""
        self.feature_count += 1
        feature_number = self.feature_count
        original_record = truncate(compact_json_text(feature_text), ORIGINAL_RECORD_MAX_CHARS)
        if repeated_names:
            self._reject(feature_number, repeated_names_reason(repeated_names), original_record)
            return
        if not isinstance(feature, dict):
            self._reject(feature_number, "feature is not a JSON object", original_record)
            return
        if feature.get("type") != "Feature":
            self._reject(
                feature_number,
                f'type is {excerpt(_json_text(feature.get("type")))}, not "Feature"',
                original_record,
            )
            return
        positions = _point_positions(feature.get("geometry"))
        if isinstance(positions, str):
            self._reject(feature_number, positions, original_record)
            return
        if not positions:
            self.counters.unrelated += 1
            self._log_unrelated_geometry(feature.get("geometry"), feature_number)
            return
        properties = feature.get("properties")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            self._reject(feature_number, "properties is not a JSON object", original_record)
            return
        fields = _classified_properties(properties)
        if isinstance(fields, str):
            self._reject(feature_number, fields, original_record)
            return
        accuracy_m = _accuracy(fields.accuracy)
        if isinstance(accuracy_m, str):
            self._reject(feature_number, accuracy_m, original_record)
            return
        label = record_text(fields.label)
        note_fields = bounded_field_parts(
            fields.note_parts, "properties", f"feature in the source file, feature {feature_number}"
        )
        # What GEOSnap says comes first and marked; text from the file carries no mark.
        record_notes = [*fields.tool_notes, note_fields]
        if fields.timestamp_text is None:
            if len(positions) > 1:
                self._reject(feature_number, "MultiPoint without a timestamp", original_record)
                return
            self.counters.undated += 1
            self.reference_places.append(
                ReferencePlace(
                    source_id=self.source_id,
                    record_number=feature_number,
                    name=label or "",
                    note=NOTE_PART_SEPARATOR.join(part for part in record_notes if part) or None,
                    latitude=positions[0][0],
                    longitude=positions[0][1],
                    original_record=original_record,
                    accuracy_m=accuracy_m or 0.0,
                    accuracy_known=accuracy_m is not None,
                    positioning_method=fields.positioning_method,
                )
            )
            return
        timestamp = self._timestamp(fields.timestamp_text)
        if isinstance(timestamp, str):
            self._reject(feature_number, timestamp, original_record)
            return
        timestamp_utc, ambiguity_seconds = timestamp
        ambiguous = ambiguity_seconds > 0
        for index, (latitude, longitude) in enumerate(positions, start=1):
            note_parts = [
                AMBIGUOUS_TIME_NOTE if ambiguous else None,
                tool_note(f"MultiPoint {index} of {len(positions)}")
                if len(positions) > 1
                else None,
                *record_notes,
            ]
            self.points.append(
                GeoPoint(
                    line_number=feature_number,
                    latitude=latitude,
                    longitude=longitude,
                    latitude_accuracy_m=accuracy_m or 0.0,
                    longitude_accuracy_m=accuracy_m or 0.0,
                    timestamp_utc=timestamp_utc,
                    original_line=original_record,
                    source_id=self.source_id,
                    label=label,
                    note=NOTE_PART_SEPARATOR.join(part for part in note_parts if part) or None,
                    accuracy_known=accuracy_m is not None,
                    time_ambiguity_seconds=ambiguity_seconds,
                    positioning_method=fields.positioning_method,
                )
            )
            self.counters.accepted += 1

    def _timestamp(self, text: str) -> tuple[datetime, float] | str:
        """The UTC instant of a property value and, for an ambiguous local time, how much
        later its second occurrence lies (the earlier one is used, as the CSV reader does;
        0.0 otherwise); a time without offset is read in the reader's zone or refused."""
        parsed = parse_timestamp_details(text, AUTOMATIC_TIME_FORMAT, self.naive_time_zone)
        if isinstance(parsed, str):
            if parsed.startswith(MISSING_ZONE_REASON):
                self.counters.naive_timestamps += 1
            return parsed
        if parsed.naive:
            self.counters.naive_timestamps += 1
        return parsed.instant, parsed.ambiguity_seconds

    def _reject(self, feature_number: int, reason: str, original_record: str) -> None:
        self.counters.invalid += 1
        self.on_rejected(RejectedLine(feature_number, reason, original_record))
        logger.warning("Feature %d rejected: %s", feature_number, reason)

    def _log_unrelated_geometry(self, geometry: object, feature_number: int) -> None:
        geometry_type = geometry.get("type") if isinstance(geometry, dict) else None
        name = geometry_type if isinstance(geometry_type, str) else "no geometry"
        if name in self._geometry_logged:
            return
        self._geometry_logged.add(name)
        logger.warning(
            "Features with %s are not imported (first: feature %d)", name, feature_number
        )


@dataclass(slots=True)
class _ClassifiedProperties:
    """What the properties of one feature yield for the point."""

    timestamp_text: str | None = None
    accuracy: object = None
    label: str | None = None
    note_parts: list[str] = field(default_factory=list)
    positioning_method: str | None = None
    # GEOSnap's own remarks on how the properties were read.
    tool_notes: list[str] = field(default_factory=list)


def _classified_properties(properties: dict[str, Any]) -> _ClassifiedProperties | str:
    """Timestamp, accuracy and label by the CSV header synonyms; every other property a
    note part. Time candidates are the properties named by a timestamp synonym or by a
    date or time word (header_tokens); their values decide: a time of day is joined with
    the one date, a start-qualified candidate wins over others, and several of the same
    rank refuse the feature naming them. Several accuracy properties refuse the feature;
    several label properties stay in the note, as do the positioning method properties."""
    fields = _ClassifiedProperties()
    time_candidates: list[tuple[str, str]] = []
    accuracies: list[tuple[str, object]] = []
    labels: list[tuple[str, object]] = []
    named_methods: list[tuple[str, str]] = []
    for key, value in properties.items():
        normalised = normalised_header(key)
        tokens = header_tokens(key)
        if value is None:
            continue
        if normalised in HEADER_SYNONYMS["timestamp"] or tokens & (DATE_WORDS | TIME_WORDS):
            cell = _cell_text(value)
            if cell is None:
                return f"unparsable timestamp in {key!r}: {excerpt(_json_text(value))}"
            time_candidates.append((key, cell))
        elif normalised in HEADER_SYNONYMS["accuracy"]:
            accuracies.append((key, value))
        elif normalised in HEADER_SYNONYMS["label"]:
            labels.append((key, value))
        elif normalised in HEADER_SYNONYMS["positioning_method"]:
            method = positioning_method_from_text(_cell_text(value))
            if method is not None:
                named_methods.append((method, key))
            fields.note_parts.extend(_flattened_parts(key, value))
        else:
            fields.note_parts.extend(_flattened_parts(key, value))
    chosen_time = _timestamp_from_candidates(time_candidates)
    if isinstance(chosen_time, _Refusal):
        return chosen_time.reason
    fields.timestamp_text = chosen_time.text
    if len({method for method, _ in named_methods}) == 1:
        fields.positioning_method = named_methods[0][0]
    elif named_methods:
        fields.tool_notes.append(disagreeing_methods_note(named_methods))
    for key, text in time_candidates:
        if key not in chosen_time.keys:
            fields.note_parts.extend(_flattened_parts(key, text))
    if len(accuracies) > 1:
        names = ", ".join(repr(key) for key, _ in accuracies)
        return f"several accuracy properties, none chosen: {names}"
    if accuracies:
        accuracy_key, fields.accuracy = accuracies[0]
        if normalised_header(accuracy_key) == "radius":
            fields.tool_notes.append(RADIUS_ACCURACY_NOTE)
    if len(labels) == 1:
        fields.label = _cell_text(labels[0][1])
    else:
        for key, value in labels:
            fields.note_parts.extend(_flattened_parts(key, value))
    return fields


@dataclass(frozen=True, slots=True)
class _Refusal:
    reason: str


@dataclass(frozen=True, slots=True)
class _ChosenTime:
    """The timestamp text of a feature (None without one) and the properties it came from."""

    text: str | None
    keys: frozenset[str]


def _timestamp_from_candidates(candidates: list[tuple[str, str]]) -> _ChosenTime | _Refusal:
    """The one date (joined with the one time of day) among the time-named properties;
    a start-qualified date wins over others; several of the same rank are refused.
    End-qualified properties never give the start (they stay in the note, as the CSV end
    columns do; alone they are refused), and only a time of day under a plain time name
    ("time", "Uhrzeit", "start_time") is joined with the date: "elapsed_time" or
    "time_utc" is not joined, and a bare date beside one is refused naming it."""
    start_candidates = [
        (key, text) for key, text in candidates if not header_tokens(key) & END_WORDS
    ]
    clocks = [
        (key, text)
        for key, text in start_candidates
        if not isinstance(parse_time_of_day(text), str)
    ]
    plain_clocks = [(key, text) for key, text in clocks if _is_plain_clock_name(key)]
    dates = [(key, text) for key, text in start_candidates if (key, text) not in clocks]
    starts = [(key, text) for key, text in dates if header_tokens(key) & START_WORDS]
    if len(dates) > 1 and len(starts) == 1:
        dates = starts
    if len(dates) > 1 or len(plain_clocks) > 1:
        names = ", ".join(repr(key) for key, _ in start_candidates)
        return _Refusal(f"several timestamp properties, none chosen: {names}")
    if clocks and not dates:
        return _Refusal(f"time of day without a date: {clocks[0][0]!r}")
    if not dates:
        if candidates:
            # Only end-qualified properties are left: an end never stands in for the start.
            end_key, end_text = candidates[0]
            return _Refusal(f"end time without a start time: {end_key!r}: {excerpt(end_text)}")
        return _ChosenTime(None, frozenset())
    date_key, date_text = dates[0]
    if not plain_clocks:
        if clocks and _is_date_only(date_text):
            return _Refusal(
                f"date without a joinable time of day: {clocks[0][0]!r} is not a plain time name"
            )
        return _ChosenTime(date_text, frozenset({date_key}))
    clock_key, clock_text = plain_clocks[0]
    return _ChosenTime(
        f"{date_text} {time_of_day_text(clock_text)}", frozenset({date_key, clock_key})
    )


def _is_date_only(text: str) -> bool:
    """True when the text reads as a date without a time of day."""
    reading = recognise_timestamp(text, {})
    return isinstance(reading, str) and reading.startswith(DATE_ONLY_REASON)


def _is_plain_clock_name(key: str) -> bool:
    """True when every word of the name is a time or start word, or a compound of them
    ("Anfangszeit")."""
    clock_words = TIME_WORDS | START_WORDS
    for token in header_tokens(key):
        if token in clock_words:
            continue
        compound_parts = header_tokens(token) - {token}
        if not compound_parts or not compound_parts <= clock_words:
            return False
    return True


def _cell_text(value: object) -> str | None:
    """A property value as the text a CSV cell would hold; None for objects, arrays and
    numbers that could not be decoded as written."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and math.isfinite(value):
        return json.dumps(value)
    return None


def _json_text(value: object) -> str:
    """A property value as compact JSON text; a number that could not be decoded as
    written (json_int's OversizedNumber, an overflowing float, NaN) is named instead of
    printed as Infinity."""
    if isinstance(value, OversizedNumber):
        return NUMBER_TOO_LONG_TEXT
    if isinstance(value, float) and not math.isfinite(value):
        return NUMBER_OUT_OF_RANGE_TEXT
    if isinstance(value, list):
        return "[" + ",".join(_json_text(entry) for entry in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{_json_text(entry)}"
                for key, entry in value.items()
            )
            + "}"
        )
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _flattened_parts(name: str, value: object) -> list[str]:
    """ "name: value" parts of a property: nested objects with dot paths ("a.b.c"),
    arrays as JSON text, null and empty values skipped."""
    parts: list[str] = []
    pending: list[tuple[str, object, int]] = [(name, value, 1)]
    while pending:
        path, current, depth = pending.pop()
        if isinstance(current, dict) and depth < MAX_PROPERTY_DEPTH:
            pending.extend(
                (f"{path}.{child_name}", child_value, depth + 1)
                for child_name, child_value in reversed(list(current.items()))
            )
            continue
        if current is None:
            continue
        text = current if isinstance(current, str) else _json_text(current)
        part = field_part(path, text)
        if part is not None:
            parts.append(part)
    return parts


def _accuracy(value: object) -> float | None | str:
    """Metres from a number or a numeric string; None when the property is absent."""
    if value is None:
        return None
    text = _cell_text(value)
    try:
        accuracy_m = float(text) if text is not None else math.nan
    except ValueError:
        accuracy_m = math.nan
    if not math.isfinite(accuracy_m) or accuracy_m < 0:
        return f"invalid accuracy: {excerpt(_json_text(value))}"
    return accuracy_m


def _point_positions(geometry: object) -> list[tuple[float, float]] | str:
    """(latitude, longitude) of a Point, of every MultiPoint position and of the Point
    members of a GeometryCollection; [] for other geometries or null; a rejection reason
    for a point whose coordinates are not a valid position."""
    positions: list[tuple[float, float]] = []
    pending: list[object] = [geometry]
    while pending:
        current = pending.pop()
        if not isinstance(current, dict):
            continue
        geometry_type = current.get("type")
        if geometry_type == "GeometryCollection":
            members = current.get("geometries")
            pending.extend(reversed(members) if isinstance(members, list) else [])
        elif geometry_type == "Point":
            position = _position(current.get("coordinates"))
            if isinstance(position, str):
                return position
            positions.append(position)
        elif geometry_type == "MultiPoint":
            coordinates = current.get("coordinates")
            if not isinstance(coordinates, list):
                return "unparsable coordinates: MultiPoint coordinates are not an array"
            for entry in coordinates:
                position = _position(entry)
                if isinstance(position, str):
                    return position
                positions.append(position)
    return positions


def _position(coordinates: object) -> tuple[float, float] | str:
    """A GeoJSON position [lon, lat, ...] as (latitude, longitude), checked."""
    if (
        not isinstance(coordinates, list)
        or len(coordinates) < 2
        or any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in coordinates[:2]
        )
    ):
        return f"unparsable coordinates: {excerpt(_json_text(coordinates))}"
    longitude, latitude = float(coordinates[0]), float(coordinates[1])
    return checked_position(latitude, longitude)
