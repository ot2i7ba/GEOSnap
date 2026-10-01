# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Search areas recorded from the map's crystal ball.

The browser sends a search area record (JSON). This module validates it strictly and
renders every file itself from the validated values; markup made by the browser is never
stored. Each recording writes search_areas/search_area_<n>_<stamp>.json|.gpx|.kml|.html and
appends one line to search_areas/records.jsonl, chained by previous_line_sha256, so
that GEOSnap --verify can check the chain and every file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from geosnap import APP_NAME, __version__
from geosnap.analysis.accuracy import RAYLEIGH_68_TO_95
from geosnap.project.manifest import is_link
from geosnap.project.verify import (
    CHAIN_LOCK_FILE_NAME,
    GENESIS_PREVIOUS_LINE_SHA256,
    verify_command,
)
from geosnap.project.workspace import (
    SEARCH_AREA_RECORDS_NAME,
    SEARCH_AREAS_DIR_NAME,
    STAMP_FORMAT,
)
from geosnap.timezones import LOCAL_ZONE_SETTING, display_local_text, resolve_display_timezone

logger = logging.getLogger(__name__)

MAX_RECORD_BYTES = 2 * 1024 * 1024
# records.jsonl is read completely for every recording; a real one stays far below this.
MAX_RECORDS_FILE_BYTES = 64 * 1024 * 1024
RECORD_TYPE = "geosnap-search-area"
RECORD_VERSION = 1
RING_VERTEX_COUNT = 64
EARTH_RADIUS_M = 6_371_008.8
# Half the Earth's circumference (about 20 037 km): no distance on the globe is longer.
MAX_DISTANCE_M = 20_040_000.0
MAX_ELAPSED_SECONDS = 10 * 366 * 86400
SHORT_TEXT = 200
LONG_TEXT = 2000
ESTIMATE_NOTE = (
    "Estimate, not evidence: a probability estimate from the device's own recorded routine, "
    "not a verified statement of where the device is."
)
RING_KEYS = (("r50_m", "50 %"), ("r80_m", "80 %"), ("r95_m", "95 %"))
COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# Characters XML 1.0 cannot carry (C0 controls except tab, LF, CR) and the other C0/C1
# controls that have no place in a label.
FORBIDDEN_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
WALL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PARAMETER_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_PARAMETERS = 40

# Serialises recordings: record numbers and the hash chain need one writer at a time.
# The thread lock covers this process, the lock file (CHAIN_LOCK_FILE_NAME) every process.
_recording_lock = threading.Lock()
CHAIN_LOCK_TIMEOUT_SECONDS = 10.0
CHAIN_LOCK_RETRY_SECONDS = 0.05

if sys.platform == "win32":
    import msvcrt

    def _try_lock_exclusively(descriptor: int) -> bool:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock_exclusively(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class SearchAreaError(ValueError):
    """The record is not a valid search area; the message is short and safe to return."""


class SearchAreaChainError(OSError):
    """records.jsonl cannot be extended; the message names no path and is safe to return."""


@dataclass(frozen=True, slots=True)
class CaseDetails:
    """What a search area needs from the project's metadata.json."""

    reference: str
    examiner: str
    project_name: str
    # settings.display_timezone of the run: an IANA name or "local".
    display_timezone: str = LOCAL_ZONE_SETTING


@dataclass(frozen=True, slots=True)
class ChainedRecordDraft:
    """What one record adds to a record chain directory."""

    files: dict[str, bytes]  # plain file name in the chain directory -> content
    # Fields of the records.jsonl line; previous_line_sha256 as handed to the composer.
    line_document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AppendedChainRecord:
    record_number: int
    line: bytes  # the new records.jsonl line without its line feed
    last_line_sha256: str  # SHA-256 of line: the anchor of the chain


@dataclass(frozen=True, slots=True)
class RecordedSearchArea:
    record_number: int
    files: dict[str, str]  # file name in search_areas/ -> SHA-256
    # SHA-256 of the new records.jsonl line (without its line feed): the anchor of the chain,
    # to be kept in the case file so that a truncated chain can be detected.
    last_line_sha256: str


# --- validation ------------------------------------------------------------------------

Checker = Callable[[object, str], None]


def _fail(path: str, problem: str) -> SearchAreaError:
    return SearchAreaError(f"{path}: {problem}")


def _number(minimum: float, maximum: float, nullable: bool = False) -> Checker:
    def check(value: object, path: str) -> None:
        if value is None and nullable:
            return
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _fail(path, "not a number")
        if not math.isfinite(value):
            raise _fail(path, "not a finite number")
        if not minimum <= value <= maximum:
            raise _fail(path, f"outside {minimum:g}..{maximum:g}")

    return check


def _integer(minimum: int, maximum: int, nullable: bool = False) -> Checker:
    def check(value: object, path: str) -> None:
        if value is None and nullable:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, "not an integer")
        if not minimum <= value <= maximum:
            raise _fail(path, f"outside {minimum}..{maximum}")

    return check


def _text(
    max_length: int, nullable: bool = False, pattern: re.Pattern[str] | None = None
) -> Checker:
    def check(value: object, path: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, str):
            raise _fail(path, "not a string")
        if len(value) > max_length:
            raise _fail(path, f"longer than {max_length} characters")
        if FORBIDDEN_CHARACTERS.search(value):
            raise _fail(path, "contains control characters")
        if pattern is not None and not pattern.fullmatch(value):
            raise _fail(path, "has the wrong format")

    return check


def _boolean(value: object, path: str) -> None:
    if not isinstance(value, bool):
        raise _fail(path, "not true or false")


def _choice(*allowed: object) -> Checker:
    def check(value: object, path: str) -> None:
        # type(...) keeps 1.0 and True from passing as 1.
        if not any(type(value) is type(option) and value == option for option in allowed):
            raise _fail(path, "not an allowed value")

    return check


def _array(item: Checker, max_items: int, min_items: int = 0, nullable: bool = False) -> Checker:
    def check(value: object, path: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, list):
            raise _fail(path, "not a list")
        if not min_items <= len(value) <= max_items:
            raise _fail(path, f"needs {min_items} to {max_items} entries")
        for index, entry in enumerate(value):
            item(entry, f"{path}.{index}")

    return check


def _object(
    fields: Mapping[str, Checker], nullable: bool = False, optional: frozenset[str] = frozenset()
) -> Checker:
    def check(value: object, path: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, dict):
            raise _fail(path, "not an object")
        missing = [key for key in fields if key not in value and key not in optional]
        if missing:
            raise _fail(path, f"missing {', '.join(missing)}")
        unknown = [str(key) for key in value if key not in fields]
        if unknown:
            # ascii(): the names come from the browser and end up in the log.
            raise _fail(path, f"unknown key {ascii(sorted(unknown))[:80]}")
        for key, checker in fields.items():
            if key in value:
                checker(value[key], f"{path}.{key}")

    return check


def _parameters(value: object, path: str) -> None:
    if not isinstance(value, dict):
        raise _fail(path, "not an object")
    if len(value) > MAX_PARAMETERS:
        raise _fail(path, f"more than {MAX_PARAMETERS} entries")
    for key, entry in value.items():
        if not PARAMETER_KEY_PATTERN.fullmatch(key):
            raise _fail(path, "invalid parameter name")
        if entry is None or isinstance(entry, bool):
            continue
        if isinstance(entry, str):
            _text(SHORT_TEXT)(entry, f"{path}.{key}")
        else:
            _number(-1e12, 1e12)(entry, f"{path}.{key}")


_latitude = _number(-90, 90)
_longitude = _number(-180, 180)
_distance = _number(0, MAX_DISTANCE_M)
_bearing = _number(0, 360)
_share = _number(0, 1)
_count = _integer(0, 1_000_000)
_time = _object(
    {
        "local": _text(19, pattern=WALL_PATTERN),
        "offset_minutes": _integer(-18 * 60, 18 * 60),
        "utc": _text(20, pattern=UTC_PATTERN),
    }
)

SEARCH_AREA_SCHEMA = _object(
    {
        "record_type": _choice(RECORD_TYPE),
        "record_version": _choice(RECORD_VERSION),
        "created_utc": _text(20, pattern=UTC_PATTERN),
        "display_zone": _text(64),
        "source": _object(
            {
                "id": _integer(0, 1_000_000),
                "label": _text(SHORT_TEXT),
                "file_name": _text(260, nullable=True),
                "sha256": _text(64, nullable=True, pattern=SHA256_PATTERN),
            }
        ),
        "reference": _time,
        "last_position": _object(
            {
                "lat": _latitude,
                "lon": _longitude,
                "accuracy_m": _number(0, MAX_DISTANCE_M, nullable=True),
                # The radius the estimate used: accuracy_m times its confidence factor.
                "uncertainty_m": _number(0, MAX_DISTANCE_M, nullable=True),
                "time": _time,
                "place": _text(LONG_TEXT, nullable=True),
            },
            optional=frozenset({"uncertainty_m"}),
        ),
        "rings": _object(
            {
                "r50_m": _distance,
                "r80_m": _distance,
                "r95_m": _distance,
                "basis": _text(LONG_TEXT),
                "basis_kind": _choice("conditioned", "global"),
                "days": _count,
                "window_count": _count,
                "window_seconds": _number(0, MAX_ELAPSED_SECONDS),
                "extrapolated": _boolean,
            },
            nullable=True,
        ),
        "rings_note": _text(LONG_TEXT, nullable=True),
        "rose": _object(
            {
                "basis": _text(LONG_TEXT),
                "leaving_days": _count,
                "analog_days": _count,
                "preferred": _boolean,
                "share_available": _boolean,
                "sectors": _array(
                    _object(
                        {
                            "bearing_deg": _bearing,
                            "days": _count,
                            "share": _share,
                            "reach80_m": _number(0, MAX_DISTANCE_M, nullable=True),
                        }
                    ),
                    max_items=16,
                    min_items=4,
                ),
            },
            nullable=True,
        ),
        "cone": _object(
            {
                "bearing_deg": _bearing,
                "half_angle_deg": _number(0, 180),
                "radius_m": _distance,
                "compass": _text(8),
                "speed_kmh": _number(0, 2000),
                "basis": _text(LONG_TEXT),
            },
            nullable=True,
        ),
        "candidates": _array(
            _object(
                {
                    "rank": _integer(1, 100),
                    "kind": _choice("place", "here"),
                    "label": _text(LONG_TEXT),
                    "address": _text(LONG_TEXT, nullable=True),
                    "lat": _latitude,
                    "lon": _longitude,
                    "days": _count,
                    "of_days": _count,
                    "share": _number(0, 1, nullable=True),
                    "interval": _array(_share, max_items=2, min_items=2, nullable=True),
                    "distance_m": _number(0, 40_100_000, nullable=True),
                    "bearing_deg": _number(0, 360, nullable=True),
                }
            ),
            max_items=20,
        ),
        "elsewhere_share": _number(0, 1, nullable=True),
        "destinations": _array(
            _object(
                {
                    "number": _integer(1, 100),
                    "label": _text(LONG_TEXT),
                    "lat": _latitude,
                    "lon": _longitude,
                    "share": _share,
                }
            ),
            max_items=20,
        ),
        "hazards": _array(
            _object(
                {
                    "category": _text(SHORT_TEXT),
                    "name": _text(SHORT_TEXT, nullable=True),
                    "lat": _latitude,
                    "lon": _longitude,
                    "distance_m": _distance,
                    "bearing_deg": _bearing,
                }
            ),
            max_items=20,
        ),
        "headline": _text(LONG_TEXT),
        "confidence": _object(
            {"level": _choice("weak", "medium", "strong"), "reason": _text(LONG_TEXT)},
            nullable=True,
        ),
        "warnings": _array(_text(LONG_TEXT), max_items=30),
        "model": _object(
            {"name": _text(100), "application": _text(100), "parameters": _parameters}
        ),
    }
)


def _utc_of(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _check_time(
    value: Mapping[str, Any], path: str, display_timezone: str, display_tz: tzinfo | None
) -> datetime:
    try:
        utc = _utc_of(value["utc"])
        wall = datetime.strptime(value["local"], "%Y-%m-%dT%H:%M:%S")
    except ValueError as error:
        raise _fail(path, "not a valid date and time") from error
    if utc.replace(tzinfo=None) + timedelta(minutes=value["offset_minutes"]) != wall:
        raise _fail(path, "local time, offset and UTC disagree")
    # "local" is the system zone of whichever machine converted the times; the project
    # records no zone name for it, so there is nothing to compare the offset with.
    if display_timezone != LOCAL_ZONE_SETTING:
        zone_offset = utc.astimezone(display_tz).utcoffset() or timedelta(0)
        if zone_offset != timedelta(minutes=value["offset_minutes"]):
            raise _fail(path, f"offset differs from the display zone {display_timezone}")
    return utc


def _check_uncertainty(last_position: Mapping[str, Any]) -> None:
    """The uncertainty radius lies between the reported accuracy and that accuracy scaled
    to 95 % (with 0.1 m for rounding); null exactly when no accuracy was reported."""
    if "uncertainty_m" not in last_position:
        return
    accuracy = last_position["accuracy_m"]
    uncertainty = last_position["uncertainty_m"]
    if (accuracy is None) != (uncertainty is None):
        raise _fail("record.last_position.uncertainty_m", "must be null exactly with accuracy_m")
    if accuracy is not None and not (
        accuracy - 0.1 <= uncertainty <= accuracy * RAYLEIGH_68_TO_95 + 0.1
    ):
        raise _fail("record.last_position.uncertainty_m", "outside the range of accuracy_m")


def validate_search_area(
    document: object, display_timezone: str = LOCAL_ZONE_SETTING
) -> dict[str, Any]:
    """The document itself when it is a valid search area record, else SearchAreaError.

    display_timezone is the project's settings.display_timezone; an IANA zone must give
    the offsets the record states at its UTC instants.
    """
    SEARCH_AREA_SCHEMA(document, "record")
    assert isinstance(document, dict)  # guaranteed by the schema
    record: dict[str, Any] = document
    try:
        _utc_of(record["created_utc"])
    except ValueError as error:
        raise _fail("record.created_utc", "not a valid date and time") from error
    try:
        display_tz = resolve_display_timezone(display_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise SearchAreaError(f"the project's display zone {display_timezone} is unknown") from (
            error
        )
    reference_utc = _check_time(
        record["reference"], "record.reference", display_timezone, display_tz
    )
    last_utc = _check_time(
        record["last_position"]["time"], "record.last_position.time", display_timezone, display_tz
    )
    if reference_utc <= last_utc:
        raise SearchAreaError("record.reference: must lie after the last known position")
    if reference_utc - last_utc > timedelta(seconds=MAX_ELAPSED_SECONDS):
        raise SearchAreaError("record.reference: too far after the last known position")
    _check_uncertainty(record["last_position"])
    rings = record["rings"]
    if rings is not None and not rings["r50_m"] <= rings["r80_m"] <= rings["r95_m"]:
        raise SearchAreaError("record.rings: r50 <= r80 <= r95 does not hold")
    for index, candidate in enumerate(record["candidates"]):
        interval = candidate["interval"]
        if interval is not None and interval[0] > interval[1]:
            raise _fail(f"record.candidates.{index}.interval", "low above high")
    return record


# --- text pieces shared by the writers ---------------------------------------------------


def distance_text(metres: float) -> str:
    """Same rule as the map (G.formatDistance): metres below 1 km, else km with 2 decimals."""
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{math.floor(metres + 0.5)} m"


def duration_text(seconds: float) -> str:
    """Same rule as the map (G.formatDuration): "1 d 2 h 5 min", "2 h 0 min", "7 min"."""
    total = round(seconds / 60)
    days, rest = divmod(total, 1440)
    hours, minutes = divmod(rest, 60)
    parts = [f"{days} d"] if days else []
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


def bearing_text(bearing: float) -> str:
    return f"{COMPASS_8[round(bearing / 45) % 8]} {round(bearing) % 360}°"


def dms_text(lat: float, lon: float) -> str:
    """Degrees, minutes and seconds with hemisphere letters, seconds to 0.1″."""

    def part(value: float, positive: str, negative: str) -> str:
        tenths = round(abs(value) * 36000)
        degrees, rest = divmod(tenths, 36000)
        minutes, seconds_tenths = divmod(rest, 600)
        hemisphere = positive if value >= 0 else negative
        return f"{degrees}° {minutes}′ {seconds_tenths / 10:.1f}″ {hemisphere}"

    return f"{part(lat, 'N', 'S')}, {part(lon, 'E', 'W')}"


def local_time_text(time: Mapping[str, Any]) -> str:
    """Wall clock with its offset east of UTC: "2026-09-18 12:00:00 +02:00"."""
    record_zone = timezone(timedelta(minutes=time["offset_minutes"]))
    return display_local_text(_utc_of(time["utc"]), record_zone)


def utc_time_text(time: Mapping[str, Any]) -> str:
    return str(time["utc"]).replace("T", " ").replace("Z", " UTC")


def share_text(share: float) -> str:
    return f"{round(share * 100)} %"


def elapsed_seconds(record: Mapping[str, Any]) -> float:
    reference = _utc_of(record["reference"]["utc"])
    last = _utc_of(record["last_position"]["time"]["utc"])
    return (reference - last).total_seconds()


def ring_vertices(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """RING_VERTEX_COUNT points on the circle (spherical destination formula), closed."""
    phi = math.radians(lat)
    lam = math.radians(lon)
    delta = radius_m / EARTH_RADIUS_M
    vertices = []
    for index in range(RING_VERTEX_COUNT):
        theta = 2 * math.pi * index / RING_VERTEX_COUNT
        phi2 = math.asin(
            math.sin(phi) * math.cos(delta) + math.cos(phi) * math.sin(delta) * math.cos(theta)
        )
        lam2 = lam + math.atan2(
            math.sin(theta) * math.sin(delta) * math.cos(phi),
            math.cos(delta) - math.sin(phi) * math.sin(phi2),
        )
        lon2 = (math.degrees(lam2) + 540) % 360 - 180
        vertices.append((round(math.degrees(phi2), 7), round(lon2, 7)))
    vertices.append(vertices[0])
    return vertices


def _rings(record: Mapping[str, Any]) -> list[tuple[str, str, float]]:
    """(key, label, radius) of each ring, innermost first; empty without rings."""
    rings = record["rings"]
    if rings is None:
        return []
    return [(key[:3], label, float(rings[key])) for key, label in RING_KEYS]


def _candidate_name(candidate: Mapping[str, Any]) -> str:
    return f"Candidate {candidate['rank']}: {candidate['label']}"


def _candidate_share_text(candidate: Mapping[str, Any]) -> str:
    if candidate["share"] is None:
        return f"{candidate['days']} of {candidate['of_days']} days"
    return f"≈ {share_text(candidate['share'])}"


def _interval_text(candidate: Mapping[str, Any]) -> str:
    interval = candidate["interval"]
    if interval is None:
        return "—"
    return f"{round(interval[0] * 100)}–{round(interval[1] * 100)} %"


def _coordinate(value: float) -> str:
    return f"{value:.7f}"


# --- GPX 1.1 and KML 2.2 -----------------------------------------------------------------

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"
KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
# KML colours are aabbggrr; the rings use the map's purples, candidates the map's violet.
KML_RING_STYLES = {
    "r50": ("e69a1b6a", "296a1b6a"),
    "r80": ("ccad448e", "1aad448e"),
    "r95": ("b3cc84b0", "0fcc84b0"),
}


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, tag, attributes)
    if text is not None:
        child.text = text
    return child


def _xml_text(root: ET.Element) -> str:
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def _title(record: Mapping[str, Any], record_number: int) -> str:
    return f"Search area {record_number} · {record['source']['label']}"


def _description(record: Mapping[str, Any]) -> str:
    reference = record["reference"]
    return (
        f"{ESTIMATE_NOTE} Reference time {local_time_text(reference)} "
        f"({utc_time_text(reference)}), {duration_text(elapsed_seconds(record))} after the "
        "last known position."
    )


def render_gpx(record: Mapping[str, Any], record_number: int, recorded_at: datetime) -> str:
    """Waypoints for the last position and the candidates; each ring as a closed track."""
    ET.register_namespace("", GPX_NAMESPACE)
    ns = f"{{{GPX_NAMESPACE}}}"
    root = ET.Element(f"{ns}gpx", {"version": "1.1", "creator": f"{APP_NAME} {__version__}"})
    metadata = _sub(root, f"{ns}metadata")
    _sub(metadata, f"{ns}name", _title(record, record_number))
    _sub(metadata, f"{ns}desc", _description(record))
    _sub(metadata, f"{ns}time", recorded_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    last = record["last_position"]

    def waypoint(lat: float, lon: float) -> ET.Element:
        return _sub(root, f"{ns}wpt", lat=_coordinate(lat), lon=_coordinate(lon))

    point = waypoint(last["lat"], last["lon"])
    _sub(point, f"{ns}time", last["time"]["utc"])
    _sub(point, f"{ns}name", "Last known position")
    place = last["place"] or "no known place"
    _sub(point, f"{ns}desc", f"{local_time_text(last['time'])}; {place}")
    for candidate in record["candidates"]:
        point = waypoint(candidate["lat"], candidate["lon"])
        _sub(point, f"{ns}name", _candidate_name(candidate))
        _sub(
            point,
            f"{ns}desc",
            f"{_candidate_share_text(candidate)}, interval {_interval_text(candidate)}",
        )
    for _key, label, radius in _rings(record):
        track = _sub(root, f"{ns}trk")
        _sub(track, f"{ns}name", f"{label} ring ({distance_text(radius)})")
        _sub(track, f"{ns}desc", f"{record['rings']['basis']}; estimate, not evidence")
        segment = _sub(track, f"{ns}trkseg")
        for lat, lon in ring_vertices(last["lat"], last["lon"], radius):
            _sub(segment, f"{ns}trkpt", lat=_coordinate(lat), lon=_coordinate(lon))
    return _xml_text(root)


def render_kml(record: Mapping[str, Any], record_number: int, recorded_at: datetime) -> str:
    """Ring polygons with their own styles, placemarks for the last position and candidates."""
    ET.register_namespace("", KML_NAMESPACE)
    ns = f"{{{KML_NAMESPACE}}}"
    root = ET.Element(f"{ns}kml")
    document = _sub(root, f"{ns}Document")
    _sub(document, f"{ns}name", _title(record, record_number))
    _sub(
        document,
        f"{ns}description",
        f"{_description(record)} Recorded {recorded_at.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC.",
    )
    for key, (line_colour, fill_colour) in KML_RING_STYLES.items():
        style = _sub(document, f"{ns}Style", id=f"ring{key[1:]}")
        line_style = _sub(style, f"{ns}LineStyle")
        _sub(line_style, f"{ns}color", line_colour)
        _sub(line_style, f"{ns}width", "2")
        poly_style = _sub(style, f"{ns}PolyStyle")
        _sub(poly_style, f"{ns}color", fill_colour)
    for style_id, colour in (("last", "ff000000"), ("candidate", "ff9a1b6a")):
        icon_style = _sub(_sub(document, f"{ns}Style", id=style_id), f"{ns}IconStyle")
        _sub(icon_style, f"{ns}color", colour)
    last = record["last_position"]
    for key, label, radius in reversed(_rings(record)):
        placemark = _sub(document, f"{ns}Placemark")
        _sub(placemark, f"{ns}name", f"{label} ring ({distance_text(radius)})")
        _sub(placemark, f"{ns}description", f"{record['rings']['basis']}; estimate, not evidence")
        _sub(placemark, f"{ns}styleUrl", f"#ring{key[1:]}")
        polygon = _sub(placemark, f"{ns}Polygon")
        ring = _sub(_sub(polygon, f"{ns}outerBoundaryIs"), f"{ns}LinearRing")
        _sub(
            ring,
            f"{ns}coordinates",
            " ".join(
                f"{_coordinate(lon)},{_coordinate(lat)},0"
                for lat, lon in ring_vertices(last["lat"], last["lon"], radius)
            ),
        )

    def placemark_at(lat: float, lon: float, name: str, text: str, style_id: str) -> None:
        placemark = _sub(document, f"{ns}Placemark")
        _sub(placemark, f"{ns}name", name)
        _sub(placemark, f"{ns}description", text)
        _sub(placemark, f"{ns}styleUrl", f"#{style_id}")
        point = _sub(placemark, f"{ns}Point")
        _sub(point, f"{ns}coordinates", f"{_coordinate(lon)},{_coordinate(lat)},0")

    placemark_at(
        last["lat"],
        last["lon"],
        "Last known position",
        f"{local_time_text(last['time'])} ({utc_time_text(last['time'])}); "
        f"{last['place'] or 'no known place'}",
        "last",
    )
    for candidate in record["candidates"]:
        placemark_at(
            candidate["lat"],
            candidate["lon"],
            _candidate_name(candidate),
            f"{_candidate_share_text(candidate)}, interval {_interval_text(candidate)}",
            "candidate",
        )
    return _xml_text(root)


# --- search area sheet (one A4 page) ----------------------------------------------------

SEARCH_AREA_SHEET_CSS = """
:root { color-scheme: light; }
body { margin: 0 auto; max-width: 190mm; padding: 10px 14px; background: #fff; color: #111;
  font: 9.5pt/1.35 "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif; }
h1 { font-size: 15pt; margin: 0 0 2px; }
h2 { font-size: 10.5pt; margin: 10px 0 3px; padding-bottom: 1px; border-bottom: 1px solid #333; }
p { margin: 3px 0; }
.subtitle { color: #444; }
.estimate { border: 2px solid #b00020; color: #b00020; padding: 4px 8px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 2px 0 4px; }
th, td { border: 1px solid #bbb; padding: 2px 5px; text-align: left; vertical-align: top;
  overflow-wrap: anywhere; }
th { background: #eee; font-weight: 600; white-space: nowrap; }
table.pairs th { width: 30%; }
td.number { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.hash { font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 8pt;
  word-break: break-all; }
.columns { display: flex; gap: 12px; align-items: flex-start; }
.columns > div { flex: 1 1 0; min-width: 0; }
figure { margin: 0; flex: 0 0 auto; }
figcaption { font-size: 8pt; color: #444; max-width: 320px; }
.warning { color: #8a4b00; }
@page { size: A4; margin: 8mm; }
@media print {
  body { max-width: none; padding: 0; font-size: 8.5pt; line-height: 1.25; }
  h1 { font-size: 13pt; }
  h2 { font-size: 9.5pt; margin: 6px 0 2px; }
  th, td { padding: 1px 4px; }
  h2, table, figure { break-inside: avoid; }
  figure svg { width: 62mm; height: 62mm; }
  figcaption { max-width: 62mm; font-size: 7pt; }
}
@media (max-width: 640px) { .columns { flex-direction: column; } }
"""

SKETCH_SIZE = 320
SKETCH_MARGIN = 26
RING_COLOURS = {"r50": "#6a1b9a", "r80": "#8e44ad", "r95": "#b084cc"}
ROSE_COLOUR = "#00838f"
CANDIDATE_COLOUR = "#6a1b9a"
HAZARD_COLOUR = "#e65100"


def _local_offset_m(origin: Mapping[str, Any], lat: float, lon: float) -> tuple[float, float]:
    """East and north metres of (lat, lon) from the origin (flat approximation)."""
    origin_lat = float(origin["lat"])
    east = (
        math.radians(lon - float(origin["lon"]))
        * EARTH_RADIUS_M
        * math.cos(math.radians(origin_lat))
    )
    north = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    return east, north


def _scale_bar_metres(extent_m: float) -> float:
    """The largest 1-2-5 length not above half the extent."""
    target = max(extent_m / 2, 1.0)
    magnitude = 10.0 ** math.floor(math.log10(target))
    return max(float(step) * magnitude for step in (1, 2, 5) if step * magnitude <= target)


def _wedge_path(centre: float, radius: float, bearing: float, half_angle: float) -> str:
    def point(angle: float) -> str:
        rad = math.radians(angle)
        return f"{centre + radius * math.sin(rad):.1f} {centre - radius * math.cos(rad):.1f}"

    large = 1 if half_angle > 90 else 0
    return (
        f"M{centre:.1f} {centre:.1f} L{point(bearing - half_angle)} "
        f"A{radius:.1f} {radius:.1f} 0 {large} 1 {point(bearing + half_angle)} Z"
    )


def render_sketch(record: Mapping[str, Any]) -> str:
    """To-scale schematic (flat approximation around the last position): rings, rose wedges,
    extrapolation cone, numbered candidates, hazards, north arrow and scale bar."""
    last = record["last_position"]
    rose = record["rose"]
    cone = record["cone"]
    extents = [radius for _, _, radius in _rings(record)]
    if rose is not None:
        extents += [sector["reach80_m"] for sector in rose["sectors"] if sector["reach80_m"]]
    if cone is not None:
        extents.append(cone["radius_m"])
    located = [
        *(("candidate", entry) for entry in record["candidates"]),
        *(("hazard", entry) for entry in record["hazards"]),
        *(("destination", entry) for entry in record["destinations"]),
    ]
    for _, entry in located:
        extents.append(math.hypot(*_local_offset_m(last, entry["lat"], entry["lon"])))
    extent = max([value for value in extents if value > 0] or [500.0]) * 1.05
    centre = SKETCH_SIZE / 2
    scale = (centre - SKETCH_MARGIN) / extent

    def xy(lat: float, lon: float) -> tuple[float, float]:
        east, north = _local_offset_m(last, lat, lon)
        return centre + east * scale, centre - north * scale

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SKETCH_SIZE}" height="{SKETCH_SIZE}" '
        f'viewBox="0 0 {SKETCH_SIZE} {SKETCH_SIZE}" role="img" '
        'aria-label="To-scale sketch of the search area, north up">',
        f'<rect x="0.5" y="0.5" width="{SKETCH_SIZE - 1}" height="{SKETCH_SIZE - 1}" '
        'fill="#fff" stroke="#999"/>',
    ]
    if rose is not None:
        half_angle = 180 / len(rose["sectors"])
        highest = max(sector["share"] for sector in rose["sectors"]) or 1
        for sector in rose["sectors"]:
            if sector["reach80_m"]:
                opacity = 0.1 + 0.45 * sector["share"] / highest
                wedge = _wedge_path(
                    centre, sector["reach80_m"] * scale, sector["bearing_deg"], half_angle
                )
                parts.append(
                    f'<path d="{wedge}" '
                    f'fill="{ROSE_COLOUR}" fill-opacity="{opacity:.2f}" stroke="{ROSE_COLOUR}" '
                    'stroke-width="0.8"/>'
                )
    for key, _, radius in reversed(_rings(record)):
        parts.append(
            f'<circle cx="{centre:.1f}" cy="{centre:.1f}" r="{radius * scale:.1f}" fill="none" '
            f'stroke="{RING_COLOURS[key]}" stroke-width="1.6"/>'
        )
    # Rings closer than 8 px share one label ("80 % = 95 %"), placed on the innermost.
    label_groups: list[tuple[str, float, list[str]]] = []
    for key, label, radius in _rings(record):
        if label_groups and (radius - label_groups[-1][1]) * scale < 8:
            label_groups[-1][2].append(label)
        else:
            label_groups.append((key, radius, [label]))
    for key, radius, labels in label_groups:
        parts.append(
            f'<text x="{centre + 3:.1f}" y="{centre - radius * scale - 2:.1f}" font-size="8" '
            f'fill="{RING_COLOURS[key]}">{escape(" = ".join(labels))}</text>'
        )
    if cone is not None:
        cone_half_angle = max(1.0, cone["half_angle_deg"])
        wedge = _wedge_path(centre, cone["radius_m"] * scale, cone["bearing_deg"], cone_half_angle)
        parts.append(
            f'<path d="{wedge}" '
            'fill="none" stroke="#37474f" stroke-width="1.2" stroke-dasharray="5 4"/>'
        )
    for kind, entry in located:
        x, y = xy(entry["lat"], entry["lon"])
        if kind == "candidate":
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{CANDIDATE_COLOUR}" stroke="#fff"/>'
                f'<text x="{x:.1f}" y="{y + 3:.1f}" font-size="8" font-weight="700" fill="#fff" '
                f'text-anchor="middle">{entry["rank"]}</text>'
            )
        elif kind == "destination":
            parts.append(
                f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" '
                f'fill="{ROSE_COLOUR}" stroke="#fff"/><text x="{x:.1f}" y="{y + 3:.1f}" '
                f'font-size="8" '
                f'font-weight="700" fill="#fff" text-anchor="middle">{entry["number"]}</text>'
            )
        else:
            parts.append(
                f'<path d="M{x:.1f} {y - 6:.1f} L{x + 5.5:.1f} {y + 4:.1f} '
                f'L{x - 5.5:.1f} {y + 4:.1f} Z" '
                f'fill="{HAZARD_COLOUR}" stroke="#fff"/>'
            )
    parts.append(f'<circle cx="{centre:.1f}" cy="{centre:.1f}" r="3.5" fill="#111" stroke="#fff"/>')
    # North arrow (top right) and scale bar (bottom left).
    north_x = SKETCH_SIZE - 18
    parts.append(
        f'<path d="M{north_x} 10 L{north_x + 5} 24 L{north_x} 20 L{north_x - 5} 24 Z" fill="#111"/>'
        f'<text x="{north_x}" y="34" font-size="9" font-weight="700" text-anchor="middle">N</text>'
    )
    bar_metres = _scale_bar_metres(extent)
    bar_pixels = bar_metres * scale
    bar_y = SKETCH_SIZE - 12
    parts.append(
        f'<path d="M10 {bar_y - 4} V{bar_y} H{10 + bar_pixels:.1f} V{bar_y - 4}" fill="none" '
        f'stroke="#111" stroke-width="1.5"/><text x="{10 + bar_pixels + 4:.1f}" y="{bar_y}" '
        f'font-size="8">{escape(distance_text(bar_metres))}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _pairs(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{escape(label)}</th><td>{cell}</td></tr>" for label, cell in rows)
    return f'<table class="pairs">{body}</table>'


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="number">{cell}</td>' if index in numeric else f"<td>{cell}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def rings_explanation(record: Mapping[str, Any]) -> str:
    """The sentence under the rings, as the crystal ball words it."""
    rings = record["rings"]
    if rings is None:
        return f"No rings: {record['rings_note'] or 'no basis'}."
    text = (
        f"Of {rings['window_count']} comparable windows of "
        f"{duration_text(rings['window_seconds'])}, 50 % stayed within "
        f"{distance_text(rings['r50_m'])}, 80 % within {distance_text(rings['r80_m'])}, 95 % "
        f"within {distance_text(rings['r95_m'])} (farthest distance reached, plus the uncertainty "
        "radius of the last report)."
    )

    # Compared on 1 m with JavaScript's Math.round (half up), as in the crystal ball.
    def metres(key: str) -> int:
        return math.floor(float(rings[key]) + 0.5)

    same_upper = metres("r80_m") == metres("r95_m")
    same_lower = metres("r50_m") == metres("r80_m")
    if same_upper and same_lower:
        text += " r50 = r80 = r95: in the upper half of the windows"
    elif same_upper:
        text += " r80 = r95: in the upper fifth of the windows"
    elif same_lower:
        text += " r50 = r80: between the middle and the upper fifth of the windows"
    if same_upper or same_lower:
        text += (
            " the farthest reach was the same distance (typically one destination). More "
            "comparable days would separate them."
        )
    return text


def render_html(
    record: Mapping[str, Any],
    record_number: int,
    recorded_at: datetime,
    case: CaseDetails,
    companion_files: Mapping[str, str],
    verify_command_text: str,
) -> str:
    """The search area sheet: one A4 page, self-contained, no scripts, print CSS.

    recorded_at carries the project's display zone; it is shown with its offset.
    """
    source = record["source"]
    last = record["last_position"]
    reference = record["reference"]
    title = f"Search area {record_number} – {case.project_name}"
    header_rows = [
        ("Case reference", escape(case.reference or "not given")),
        ("Examiner", escape(case.examiner or "not given")),
        ("Project", escape(case.project_name)),
        (
            "Source",
            f"{escape(source['label'])} (id {source['id']}), file "
            f"{escape(source['file_name'] or 'not recorded')}",
        ),
        (
            "Source SHA-256",
            f'<span class="hash">{escape(source["sha256"] or "not recorded")}</span>',
        ),
    ]
    accuracy = (
        f"± {distance_text(last['accuracy_m'])}"
        if last["accuracy_m"] is not None
        else "accuracy not reported"
    )
    uncertainty = last.get("uncertainty_m")
    if uncertainty is not None and distance_text(uncertainty) != distance_text(last["accuracy_m"]):
        accuracy += f"; 95 %: {distance_text(uncertainty)}"
    position_rows = [
        ("Last known position", f"{last['lat']:.6f}, {last['lon']:.6f} ({accuracy})"),
        ("Degrees, minutes, seconds", escape(dms_text(last["lat"], last["lon"]))),
        ("Place", escape(last["place"] or "no known place")),
        (
            "Last report",
            f"{escape(local_time_text(last['time']))} · {escape(utc_time_text(last['time']))}",
        ),
        (
            "Reference time",
            f"{escape(local_time_text(reference))} ({escape(record['display_zone'])}) · "
            f"{escape(utc_time_text(reference))}",
        ),
        ("Elapsed", escape(duration_text(elapsed_seconds(record)))),
    ]
    confidence = record["confidence"]
    headline = escape(record["headline"])
    if confidence is not None:
        headline += (
            f" <strong>Confidence: {escape(confidence['level'])}</strong> "
            f"({escape(confidence['reason'])})."
        )
    rings = record["rings"]
    if rings is not None:
        basis_note = (
            f"Basis: {escape(rings['basis'])}"
            f"{'; extrapolated beyond the recorded history' if rings['extrapolated'] else ''}."
        )
        rings_html = (
            _table(
                ["Ring", "Radius", "Basis", "n"],
                [
                    [
                        escape(label),
                        escape(distance_text(radius)),
                        escape(rings["basis_kind"]),
                        f"{rings['window_count']} windows / {rings['days']} days",
                    ]
                    for _, label, radius in _rings(record)
                ],
            )
            + f"<p>{basis_note}</p>"
        )
    else:
        rings_html = ""
    rings_html += f"<p>{escape(rings_explanation(record))}</p>"
    rose = record["rose"]
    if rose is not None:
        strongest = sorted(rose["sectors"], key=lambda sector: -sector["days"])[:3]
        rings_html += (
            f"<p>Directions: {escape(rose['basis'])}; strongest "
            + ", ".join(
                escape(f"{bearing_text(sector['bearing_deg'])} {sector['days']} d")
                for sector in strongest
                if sector["days"]
            )
            + (" (no preferred direction)" if not rose["preferred"] else "")
            + ". Wedge length = 80 % reach in that direction.</p>"
        )
    cone = record["cone"]
    if cone is not None:
        rings_html += (
            f"<p>Extrapolation cone (dashed): heading {escape(cone['compass'])} at "
            f"~{round(cone['speed_kmh'])} km/h, ± {round(cone['half_angle_deg'])}° up to "
            f"{escape(distance_text(cone['radius_m']))}; {escape(cone['basis'])}.</p>"
        )
    candidate_rows = [
        [
            str(candidate["rank"]),
            escape(candidate["label"]),
            escape(_candidate_share_text(candidate)),
            escape(_interval_text(candidate)),
            escape(distance_text(candidate["distance_m"]))
            if candidate["distance_m"] is not None
            else "here",
            escape(bearing_text(candidate["bearing_deg"]))
            if candidate["bearing_deg"] is not None
            else "—",
        ]
        for candidate in record["candidates"]
    ]
    candidates_html = (
        _table(
            ["#", "Place / address", "Share", "Interval", "Distance", "Bearing"],
            candidate_rows,
            numeric={0, 2, 3, 4, 5},
        )
        if candidate_rows
        else "<p>No ranked known place.</p>"
    )
    if record["elsewhere_share"] is not None:
        candidates_html += (
            f"<p>Elsewhere / moving: ≈ {escape(share_text(record['elsewhere_share']))}.</p>"
        )
    hazard_rows = [
        [
            escape(hazard["category"]),
            escape(hazard["name"] or "—"),
            escape(distance_text(hazard["distance_m"])),
            escape(bearing_text(hazard["bearing_deg"])),
        ]
        for hazard in record["hazards"]
    ]
    hazards_html = (
        _table(["Category", "Name", "Distance", "Bearing"], hazard_rows, numeric={2})
        if hazard_rows
        else "<p>No embedded hazard place within r95 (the embedded places cover only the areas "
        "queried at generation; absence is no proof).</p>"
    )
    warnings_html = (
        "<ul>"
        + "".join(f'<li class="warning">{escape(text)}</li>' for text in record["warnings"])
        + "</ul>"
        if record["warnings"]
        else "<p>None.</p>"
    )
    model = record["model"]
    parameter_text = ", ".join(
        f"{key} = {'null' if value is None else value}"
        for key, value in sorted(model["parameters"].items())
    )
    companion_rows = "".join(
        f'<li>{escape(name)}: <span class="hash">{escape(digest)}</span></li>'
        for name, digest in sorted(companion_files.items())
    )
    recorded_utc = recorded_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    recorded_local = display_local_text(recorded_at, recorded_at.tzinfo)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n<style>{SEARCH_AREA_SHEET_CSS}</style>\n</head>\n<body>\n"
        "<h1>Search area – estimate, not evidence</h1>\n"
        f'<p class="subtitle">Search area {record_number} of project '
        f"{escape(case.project_name)}, recorded {escape(recorded_local)} "
        f"({escape(recorded_utc)}) from the map's crystal "
        "ball. Print on one A4 page.</p>\n"
        f'<p class="estimate">{escape(ESTIMATE_NOTE)} Use it only as a starting point for '
        "planning a search.</p>\n"
        f"{_pairs(header_rows)}\n"
        f"<h2>Position and time</h2>\n{_pairs(position_rows)}\n"
        f"<h2>Estimate</h2>\n<p>{headline}</p>\n"
        '<div class="columns"><div>\n'
        f"<h2>Search rings</h2>\n{rings_html}\n"
        "</div><figure>\n"
        f"{render_sketch(record)}\n"
        "<figcaption>To scale, North up (flat approximation): rings 50/80/95 %, teal wedges = "
        "direction rose (80 % reach), dashed = extrapolation cone, purple circles = candidates, "
        "squares = destinations, orange triangles = hazards, black dot = last known position."
        "</figcaption>\n"
        "</figure></div>\n"
        f"<h2>Candidates</h2>\n{candidates_html}\n"
        f"<h2>Nearest hazards</h2>\n{hazards_html}\n"
        f"<h2>Warnings</h2>\n{warnings_html}\n"
        "<h2>Model and integrity</h2>\n"
        f"<p>{escape(model['name'])}, {escape(model['application'])}; parameters: "
        f"{escape(parameter_text)}. Estimate made in the browser at "
        f"{escape(record['created_utc'])}; this sheet, the GPX, KML and JSON files were "
        f"written by {escape(APP_NAME)} {escape(__version__)} from the validated record.</p>\n"
        "<p>The SHA-256 of this sheet and of every file of this search area is recorded in "
        f"{SEARCH_AREAS_DIR_NAME}/{SEARCH_AREA_RECORDS_NAME} (record {record_number}, hash "
        f"chain); check them with <code>{escape(verify_command_text)}</code>. Without "
        f"{escape(APP_NAME)}, compare a file's SHA-256 with <code>sha256sum &lt;file&gt;</code> "
        "(Linux) or <code>certutil -hashfile &lt;file&gt; SHA256</code> (Windows). "
        f"The other files (SHA-256):</p><ul>{companion_rows}</ul>\n"
        "</body>\n</html>\n"
    )


# --- recording ---------------------------------------------------------------------------


def _json_bytes(document: object, indent: int | None = 2) -> bytes:
    separators = None if indent else (",", ":")
    return json.dumps(
        document,
        indent=indent,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=separators,
    ).encode("utf-8")


def read_case_details(project_directory: Path) -> CaseDetails:
    """Case reference, examiner and project name from metadata.json; empty when absent."""
    try:
        metadata = json.loads((project_directory / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    case = metadata.get("case")
    project = metadata.get("project")
    settings = metadata.get("settings")

    def text(mapping: object, key: str, default: str) -> str:
        value = mapping.get(key) if isinstance(mapping, dict) else None
        return value if isinstance(value, str) and value else default

    return CaseDetails(
        reference=text(case, "reference", ""),
        examiner=text(case, "examiner", ""),
        project_name=text(project, "name", project_directory.name),
        display_timezone=text(settings, "display_timezone", LOCAL_ZONE_SETTING),
    )


def _require_own_chain_directory(project_directory: Path, chain_dir_name: str) -> None:
    """Refuse a chain directory or records.jsonl that leads out of the project directory.

    A reopened project directory is untrusted: a link (or Windows junction) in place of
    the chain directory or records.jsonl would redirect the writes of a recording.
    """
    chain_dir = project_directory / chain_dir_name
    own_directory = project_directory.resolve(strict=True) / chain_dir_name
    if chain_dir.is_symlink() or chain_dir.resolve(strict=True) != own_directory:
        raise SearchAreaChainError(
            f"{chain_dir_name} is not a directory of this project (it is a link); "
            "nothing was recorded"
        )
    records_path = chain_dir / SEARCH_AREA_RECORDS_NAME
    try:
        records_status = records_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(records_status.st_mode):
        raise SearchAreaChainError(
            f"{SEARCH_AREA_RECORDS_NAME} is not a regular file; nothing was recorded"
        )
    if records_status.st_size > MAX_RECORDS_FILE_BYTES:
        raise SearchAreaChainError(
            f"{SEARCH_AREA_RECORDS_NAME} is larger than {MAX_RECORDS_FILE_BYTES} bytes; "
            "nothing was recorded"
        )


@contextmanager
def _exclusive_chain_lock(chain_dir: Path) -> Iterator[None]:
    """Hold the lock file of chain_dir exclusively against every other process, waiting
    at most CHAIN_LOCK_TIMEOUT_SECONDS."""
    lock_path = chain_dir / CHAIN_LOCK_FILE_NAME
    try:
        lock_status = lock_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if is_link(lock_status) or not stat.S_ISREG(lock_status.st_mode):
            raise SearchAreaChainError(
                f"{CHAIN_LOCK_FILE_NAME} is not a regular file; nothing was recorded"
            )
    open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock_path, open_flags, 0o644)
    except OSError as error:
        raise SearchAreaChainError(
            f"{CHAIN_LOCK_FILE_NAME} cannot be opened ({error.strerror}); nothing was recorded"
        ) from error
    try:
        deadline = time.monotonic() + CHAIN_LOCK_TIMEOUT_SECONDS
        while not _try_lock_exclusively(descriptor):
            if time.monotonic() >= deadline:
                raise SearchAreaChainError(
                    f"{SEARCH_AREA_RECORDS_NAME} is in use by another process; nothing was "
                    "recorded, try again"
                )
            time.sleep(CHAIN_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            _unlock(descriptor)
    finally:
        os.close(descriptor)


def _chain_state(records_path: Path) -> tuple[int, str]:
    """(next record number, previous_line_sha256 for it) from records.jsonl."""
    if not records_path.exists():
        return 1, GENESIS_PREVIOUS_LINE_SHA256
    raw = records_path.read_bytes()
    if not raw:
        return 1, GENESIS_PREVIOUS_LINE_SHA256
    if not raw.endswith(b"\n"):
        raise SearchAreaChainError(
            f"{SEARCH_AREA_RECORDS_NAME} does not end with a line feed; it was changed "
            f"outside {APP_NAME} (run --verify)"
        )
    lines = raw[:-1].split(b"\n")
    return len(lines) + 1, hashlib.sha256(lines[-1]).hexdigest()


def _write_new_file(path: Path, content: bytes) -> None:
    with path.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def append_chained_record(
    project_directory: Path,
    chain_dir_name: str,
    compose: Callable[[int, str], ChainedRecordDraft],
) -> AppendedChainRecord:
    """Add one record to the chain directory <chain_dir_name>/ of the project.

    Under the thread lock and the lock file, compose(record number, previous_line_sha256)
    builds the record; its files are written as new files and its line is appended to
    records.jsonl, unless the chain changed meanwhile. On any failure the files written
    are removed again.
    """
    chain_dir = project_directory / chain_dir_name
    with _recording_lock:
        chain_dir.mkdir(exist_ok=True)
        _require_own_chain_directory(project_directory, chain_dir_name)
        with _exclusive_chain_lock(chain_dir):
            _require_own_chain_directory(project_directory, chain_dir_name)
            records_path = chain_dir / SEARCH_AREA_RECORDS_NAME
            chain_state = _chain_state(records_path)
            record_number, previous_line_sha256 = chain_state
            draft = compose(record_number, previous_line_sha256)
            line = _json_bytes(draft.line_document, indent=None)
            written: list[Path] = []
            try:
                for name, content in draft.files.items():
                    path = chain_dir / name
                    _write_new_file(path, content)
                    written.append(path)
                if _chain_state(records_path) != chain_state:
                    raise SearchAreaChainError(
                        f"{SEARCH_AREA_RECORDS_NAME} was changed while recording; nothing was "
                        f"recorded (run --verify)"
                    )
                with records_path.open("ab") as records_file:
                    records_file.write(line + b"\n")
                    records_file.flush()
                    os.fsync(records_file.fileno())
            except BaseException:
                # A record without its chain line would read as EXTRA files in --verify.
                for path in written:
                    path.unlink(missing_ok=True)
                raise
    return AppendedChainRecord(record_number, line, hashlib.sha256(line).hexdigest())


def record_search_area(
    project_directory: Path,
    document: object,
    case: CaseDetails,
    recorded_at: datetime | None = None,
) -> RecordedSearchArea:
    """Validate the record, write its four files and append the chained records.jsonl line."""
    record = validate_search_area(document, case.display_timezone)
    moment = recorded_at or datetime.now().astimezone()
    # Validation resolved the zone already, so this cannot fail.
    moment_local = moment.astimezone(resolve_display_timezone(case.display_timezone))
    recorded_utc = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    search_areas_dir = project_directory / SEARCH_AREAS_DIR_NAME
    digests: dict[str, str] = {}

    def compose(record_number: int, previous_line_sha256: str) -> ChainedRecordDraft:
        base_name = f"search_area_{record_number}_{moment.strftime(STAMP_FORMAT)}"
        names = {suffix: f"{base_name}.{suffix}" for suffix in ("json", "gpx", "kml", "html")}
        generation = {
            "record_number": record_number,
            "recorded_utc": recorded_utc,
            "recorded_local": moment_local.isoformat(timespec="seconds"),
            "application": f"{APP_NAME} {__version__}",
            "case": {"reference": case.reference, "examiner": case.examiner},
            "project": case.project_name,
            "files": sorted(names.values()),
            "note": ESTIMATE_NOTE,
        }
        contents = {
            names["json"]: _json_bytes({"generation": generation, "search_area": record}) + b"\n",
            names["gpx"]: render_gpx(record, record_number, moment).encode("utf-8"),
            names["kml"]: render_kml(record, record_number, moment).encode("utf-8"),
        }
        digests.update(
            {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
        )
        contents[names["html"]] = render_html(
            record,
            record_number,
            moment_local,
            case,
            dict(digests),
            verify_command(project_directory),
        ).encode("utf-8")
        digests[names["html"]] = hashlib.sha256(contents[names["html"]]).hexdigest()
        line_document = {
            "record_number": record_number,
            "recorded_utc": recorded_utc,
            "recorded_local": generation["recorded_local"],
            "source": {
                "id": record["source"]["id"],
                "label": record["source"]["label"],
                "sha256": record["source"]["sha256"],
            },
            "reference_utc": record["reference"]["utc"],
            "files": dict(sorted(digests.items())),
            "previous_line_sha256": previous_line_sha256,
        }
        return ChainedRecordDraft(contents, line_document)

    appended = append_chained_record(project_directory, SEARCH_AREAS_DIR_NAME, compose)
    logger.info(
        "Search area %d recorded in %s: %s; records.jsonl last line sha256 %s",
        appended.record_number,
        search_areas_dir,
        ", ".join(f"{name} sha256 {digest}" for name, digest in sorted(digests.items())),
        appended.last_line_sha256,
    )
    return RecordedSearchArea(
        appended.record_number, dict(sorted(digests.items())), appended.last_line_sha256
    )
