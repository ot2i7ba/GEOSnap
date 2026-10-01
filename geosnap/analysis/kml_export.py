# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""KML 2.2 export for Google Earth: a route and timed points per source in the source
colour, stays, last positions and encounters."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from geosnap.analysis.encounters import Encounter, encounter_class_text
from geosnap.analysis.gpx_export import (
    accuracy_text,
    encounter_offsets_text,
    qualified_name,
    record_field_text,
    stay_bounds_text,
)
from geosnap.analysis.models import SourceAnalysis
from geosnap.extraction.models import record_noun

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
# KML colours are aabbggrr.
STAY_COLOUR = "8800a5ff"
LAST_COLOUR = "ff1818c0"
ENCOUNTER_COLOUR = "ff00d7ff"


def kml_colour(hex_colour: str, alpha: str = "ff") -> str:
    """'#rrggbb' as KML 'aabbggrr'."""
    red, green, blue = hex_colour[1:3], hex_colour[3:5], hex_colour[5:7]
    return f"{alpha}{blue}{green}{red}".lower()


def _tag(name: str) -> str:
    return f"{{{KML_NAMESPACE}}}{name}"


def _when(moment_utc: datetime) -> str:
    return moment_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _coordinates(longitude: float, latitude: float) -> str:
    """Full precision: a forensic export must not round the recorded position."""
    return f"{longitude},{latitude},0"


def _style(document: ET.Element, identifier: str, colour: str, line_width: str | None) -> None:
    style = ET.SubElement(document, _tag("Style"), {"id": identifier})
    if line_width is not None:
        line_style = ET.SubElement(style, _tag("LineStyle"))
        ET.SubElement(line_style, _tag("color")).text = colour
        ET.SubElement(line_style, _tag("width")).text = line_width
    icon_style = ET.SubElement(style, _tag("IconStyle"))
    ET.SubElement(icon_style, _tag("color")).text = colour


def _folder(document: ET.Element, name: str) -> ET.Element:
    folder = ET.SubElement(document, _tag("Folder"))
    ET.SubElement(folder, _tag("name")).text = name
    return folder


def _point_placemark(
    folder: ET.Element,
    name: str,
    description: str,
    latitude: float,
    longitude: float,
    style_id: str,
    when: datetime | None,
) -> None:
    placemark = ET.SubElement(folder, _tag("Placemark"))
    ET.SubElement(placemark, _tag("name")).text = name
    ET.SubElement(placemark, _tag("description")).text = description
    # KML 2.2 schema order: the time primitive precedes styleUrl, the geometry comes last.
    if when is not None:
        timestamp = ET.SubElement(placemark, _tag("TimeStamp"))
        ET.SubElement(timestamp, _tag("when")).text = _when(when)
    ET.SubElement(placemark, _tag("styleUrl")).text = f"#{style_id}"
    point = ET.SubElement(placemark, _tag("Point"))
    ET.SubElement(point, _tag("coordinates")).text = _coordinates(longitude, latitude)


def write_kml(
    path: Path,
    sources: Sequence[SourceAnalysis],
    encounters: Sequence[Encounter],
    project_name: str,
) -> None:
    ET.register_namespace("", KML_NAMESPACE)
    root = ET.Element(_tag("kml"))
    document = ET.SubElement(root, _tag("Document"))
    ET.SubElement(document, _tag("name")).text = project_name
    for entry in sources:
        identifier = entry.source.identifier
        _style(document, f"route-{identifier}", kml_colour(entry.source.colour), "3")
        _style(document, f"point-{identifier}", kml_colour(entry.source.colour), None)
    _style(document, "stay", STAY_COLOUR, None)
    _style(document, "last", LAST_COLOUR, None)
    _style(document, "encounter", ENCOUNTER_COLOUR, None)
    multiple_sources = len(sources) > 1

    route_folder = _folder(document, "Route")
    for entry in sources:
        if len(entry.points) < 2:
            continue
        placemark = ET.SubElement(route_folder, _tag("Placemark"))
        ET.SubElement(placemark, _tag("name")).text = entry.source.label
        ET.SubElement(placemark, _tag("styleUrl")).text = f"#route-{entry.source.identifier}"
        line = ET.SubElement(placemark, _tag("LineString"))
        ET.SubElement(line, _tag("tessellate")).text = "1"
        ET.SubElement(line, _tag("coordinates")).text = " ".join(
            _coordinates(point.longitude, point.latitude) for point in entry.points
        )

    points_folder = _folder(document, "Points")
    for entry in sources:
        for point in entry.points:
            accuracy = accuracy_text(point.accuracy_radius_m, point.accuracy_known, 2)
            noun = record_noun(entry.source.format).capitalize()
            # The record's own name and description follow the time and accuracy, marked
            # "record name:"/"record note:"; ElementTree escapes the text.
            _point_placemark(
                points_folder,
                qualified_name(f"{noun} {point.line_number}", entry.source.label, multiple_sources),
                "; ".join(
                    text
                    for text in (
                        f"{_when(point.timestamp_utc)} {accuracy}",
                        record_field_text("name", point.label),
                        record_field_text("note", point.note),
                    )
                    if text
                ),
                point.latitude,
                point.longitude,
                f"point-{entry.source.identifier}",
                point.timestamp_utc,
            )

    stays_folder = _folder(document, "Stays")
    for entry in sources:
        for stay in entry.report.stays:
            _point_placemark(
                stays_folder,
                qualified_name(
                    f"Stay {stay.identifier}: {stay.duration_minutes:.0f} min",
                    entry.source.label,
                    multiple_sources,
                ),
                f"{_when(stay.arrive_utc)} to {_when(stay.leave_utc)}, {stay.point_count} points; "
                f"{stay_bounds_text(stay, _when)}",
                stay.latitude,
                stay.longitude,
                "stay",
                stay.arrive_utc,
            )

    last_folder = _folder(document, "Last known position")
    for entry in sources:
        last = entry.report.last
        if last is not None:
            _point_placemark(
                last_folder,
                qualified_name("Last known position", entry.source.label, multiple_sources),
                f"{_when(last.timestamp_utc)} "
                f"{accuracy_text(last.accuracy_m, last.accuracy_known, 1)}",
                last.latitude,
                last.longitude,
                "last",
                last.timestamp_utc,
            )

    if encounters:
        labels = {entry.source.identifier: entry.source.label for entry in sources}
        encounters_folder = _folder(document, "Encounters")
        for encounter in encounters:
            _point_placemark(
                encounters_folder,
                f"Encounter {encounter.identifier}: {labels.get(encounter.source_a, '')} + "
                f"{labels.get(encounter.source_b, '')}",
                f"{_when(encounter.start_utc)} to {_when(encounter.end_utc)}, "
                f"{encounter.duration_minutes:.0f} min, closest {encounter.min_distance_m:.0f} m, "
                f"{encounter_offsets_text(encounter)}, "
                + encounter_class_text(
                    encounter.movement, encounter.coincided_within_accuracy_only
                ),
                encounter.centre_latitude,
                encounter.centre_longitude,
                "encounter",
                encounter.start_utc,
            )

    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
