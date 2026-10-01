# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""GPX 1.1 export: one track per source plus waypoints for last positions, stays and
encounters."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from geosnap import APP_NAME, __version__
from geosnap.analysis.encounters import Encounter, encounter_class_text
from geosnap.analysis.models import SourceAnalysis, Stay
from geosnap.extraction.models import SourceDescriptor, record_noun

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def _tag(name: str) -> str:
    return f"{{{GPX_NAMESPACE}}}{name}"


def _gpx_time(moment_utc: datetime) -> str:
    return moment_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _coordinate(value: float) -> str:
    """Full precision: a forensic export must not round the recorded position."""
    return str(value)


def qualified_name(name: str, source_label: str, multiple_sources: bool) -> str:
    """Waypoint names carry the source only when a project has more than one."""
    return f"{name} · {source_label}" if multiple_sources else name


def accuracy_text(accuracy_m: float, accuracy_known: bool, decimals: int) -> str:
    """ "±12.3 m", or "accuracy not reported" for a source without accuracies (KML)."""
    return f"±{accuracy_m:.{decimals}f} m" if accuracy_known else "accuracy not reported"


def record_reference(source: SourceDescriptor, number: int) -> str:
    """Where a record came from, e.g. "line 12", "placemark 12", "GPX point 12", "record 12"."""
    return f"{record_noun(source.format)} {number}"


def record_range(source: SourceDescriptor, first: int, last: int) -> str:
    return f"{record_noun(source.format, plural=True)} {first}-{last}"


def record_field_text(field_name: str, text: str | None) -> str | None:
    """A record's own name or description in export text, marked as coming from the
    record ("record name: …", "record note: …"); None when the record has none."""
    return f"record {field_name}: {text}" if text else None


def stay_bounds_text(stay: Stay, moment_text: Callable[[datetime], str]) -> str:
    """ "arrived between X and Y, left between A and B"; an unbounded side reads
    "arrival not bounded" or "departure not bounded"."""
    arrival = (
        "arrival not bounded"
        if stay.arrived_after_utc is None
        else f"arrived between {moment_text(stay.arrived_after_utc)} and "
        f"{moment_text(stay.arrived_by_utc)}"
    )
    departure = (
        "departure not bounded"
        if stay.left_before_utc is None
        else f"left between {moment_text(stay.leave_utc)} and {moment_text(stay.left_before_utc)}"
    )
    return f"{arrival}, {departure}"


def offset_text(seconds: float) -> str:
    """Same rule as the map: "40 s", "2 min 5 s", "1 h 3 min", rounded down to whole
    seconds so an offset is never overstated."""
    whole = math.floor(seconds + 1e-9)
    if whole < 60:
        return f"{whole} s"
    minutes, rest_seconds = divmod(whole, 60)
    if whole < 3600:
        return f"{minutes} min" + (f" {rest_seconds} s" if rest_seconds else "")
    days, rest_minutes = divmod(minutes, 1440)
    hours, minutes = divmod(rest_minutes, 60)
    return (f"{days} d " if days else "") + f"{hours} h {minutes} min"


def encounter_offsets_text(encounter: Encounter) -> str:
    """ "smallest time offset 12 s (closest pair 40 s)", worded as on the map. When a
    report's local time occurred twice, says by how much the recorded times may lie later."""
    text = (
        f"smallest time offset {offset_text(encounter.min_time_offset_seconds)} "
        f"(closest pair {offset_text(encounter.closest_pair_offset_seconds)})"
    )
    if encounter.time_ambiguity_seconds > 0:
        text += (
            "; a local time occurred twice: may lie up to "
            f"{math.ceil(encounter.time_ambiguity_seconds / 60)} min later"
        )
    return text


def _waypoint(
    root: ET.Element,
    latitude: float,
    longitude: float,
    when: datetime,
    name: str,
    description: str,
) -> None:
    waypoint = ET.SubElement(
        root, _tag("wpt"), {"lat": _coordinate(latitude), "lon": _coordinate(longitude)}
    )
    ET.SubElement(waypoint, _tag("time")).text = _gpx_time(when)
    ET.SubElement(waypoint, _tag("name")).text = name
    ET.SubElement(waypoint, _tag("desc")).text = description


def write_gpx(
    path: Path,
    sources: Sequence[SourceAnalysis],
    encounters: Sequence[Encounter],
    project_name: str,
    generated_at_utc: datetime,
) -> None:
    ET.register_namespace("", GPX_NAMESPACE)
    root = ET.Element(_tag("gpx"), {"version": "1.1", "creator": f"{APP_NAME} {__version__}"})
    metadata = ET.SubElement(root, _tag("metadata"))
    ET.SubElement(metadata, _tag("name")).text = project_name
    ET.SubElement(metadata, _tag("time")).text = _gpx_time(generated_at_utc)

    # GPX 1.1 schema order: all waypoints precede the tracks.
    multiple_sources = len(sources) > 1
    labels = {entry.source.identifier: entry.source.label for entry in sources}
    for entry in sources:
        report, label = entry.report, entry.source.label
        if report.last is not None:
            _waypoint(
                root,
                report.last.latitude,
                report.last.longitude,
                report.last.timestamp_utc,
                qualified_name("Last known position", label, multiple_sources),
                f"{accuracy_text(report.last.accuracy_m, report.last.accuracy_known, 1)}; "
                f"{record_reference(entry.source, report.last.line_number)}",
            )
        for stay in report.stays:
            _waypoint(
                root,
                stay.latitude,
                stay.longitude,
                stay.arrive_utc,
                qualified_name(
                    f"Stay {stay.identifier}: {stay.duration_minutes:.0f} min",
                    label,
                    multiple_sources,
                ),
                f"{_gpx_time(stay.arrive_utc)} to {_gpx_time(stay.leave_utc)}, "
                f"{stay.point_count} points, "
                f"{record_range(entry.source, stay.first_line, stay.last_line)}; "
                f"{stay_bounds_text(stay, _gpx_time)}",
            )
    for encounter in encounters:
        _waypoint(
            root,
            encounter.centre_latitude,
            encounter.centre_longitude,
            encounter.start_utc,
            f"Encounter {encounter.identifier}: {labels.get(encounter.source_a, '')} + "
            f"{labels.get(encounter.source_b, '')}, {encounter.duration_minutes:.0f} min",
            f"{_gpx_time(encounter.start_utc)} to {_gpx_time(encounter.end_utc)}, "
            f"closest {encounter.min_distance_m:.0f} m, {encounter_offsets_text(encounter)}, "
            + encounter_class_text(encounter.movement, encounter.coincided_within_accuracy_only),
        )

    for entry in sources:
        if not entry.points:
            continue
        track = ET.SubElement(root, _tag("trk"))
        ET.SubElement(track, _tag("name")).text = entry.source.label
        track_segment = ET.SubElement(track, _tag("trkseg"))
        for point in entry.points:
            track_point = ET.SubElement(
                track_segment,
                _tag("trkpt"),
                {"lat": _coordinate(point.latitude), "lon": _coordinate(point.longitude)},
            )
            ET.SubElement(track_point, _tag("time")).text = _gpx_time(point.timestamp_utc)
            # GPX 1.1 schema order: name precedes desc. The record's own name goes to
            # <name>; its description follows the accuracy and the record reference,
            # marked "record note:" so that it cannot pose as GEOSnap's own text.
            if point.label:
                ET.SubElement(track_point, _tag("name")).text = point.label
            accuracy = accuracy_text(point.accuracy_radius_m, point.accuracy_known, 2)
            ET.SubElement(track_point, _tag("desc")).text = "; ".join(
                text
                for text in (
                    f"{accuracy}; {record_reference(entry.source, point.line_number)}",
                    record_field_text("note", point.note),
                )
                if text
            )

    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
