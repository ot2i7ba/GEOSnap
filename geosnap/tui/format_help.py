# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""What GEOSnap reads from each input format, in one place.

The file-format help screen (screens/format_help.py) and the format table of MANUAL.md both come
from FORMAT_HELP, so they cannot drift apart (tests/test_tui.py checks the manual against
it). Every entry names the suffixes that lead to the format, what is read, what a file must
have, what is refused, and one minimal example. Detection itself lives in
project.pipeline.source_format_for.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InputFormatHelp:
    """One accepted input format as the help screen and the manual present it."""

    # The format name as metadata.json, the setup screen and the manual use it.
    name: str
    # File name suffixes that lead to this format (lowercase, with the dot).
    suffixes: tuple[str, ...]
    # How the format is recognised beyond its suffix; empty when the suffix decides.
    recognised_by: str
    reads: str
    required: str
    refused: str
    # One minimal example, one line per entry.
    example: tuple[str, ...]

    @property
    def suffix_text(self) -> str:
        return ", ".join(self.suffixes)


FORMAT_HELP: tuple[InputFormatHelp, ...] = (
    InputFormatHelp(
        name="text",
        suffixes=(".txt", ".log"),
        recognised_by="",
        reads=(
            "Every record with a latitude, a longitude and a time, also several in one line; "
            "other text counts as unrelated. The two ± values are the accuracy in metres; the "
            "whole line is kept as original_line."
        ),
        required=(
            "A time with its UTC offset in every record. The line number is the record number."
        ),
        refused=(
            "Coordinates out of range or not finite, null island (within 0.001° of 0/0), a "
            "zone name contradicting the offset (+0200 UTC) or the date (+0100 CEST in "
            "January), non-existent local times, times before 1990 or after 2099, and "
            "record-like text (± and |) in another layout. A zone abbreviation with several "
            "meanings (PDT, EST) is read by the offset alone."
        ),
        example=(
            "51.43612 ± 19.83 meters | 6.90872 ± 19.83 meters | 2026-09-02 06:15:11 +0000 UTC",
        ),
    ),
    InputFormatHelp(
        name="kml",
        suffixes=(".kml",),
        recognised_by="a .kml file that is not a ZIP archive",
        reads=(
            "Placemarks in any Document/Folder nesting: a Point with TimeStamp/when or "
            "TimeSpan/begin becomes a record, name and description become its label and note, "
            "and ExtendedData follows in the note as 'name: value' parts. gx:Track yields one "
            "record per when/coord pair, a MultiGeometry one record per Point. A placemark "
            "without a time is read without a timestamp."
        ),
        required=(
            "The root element must be kml, and a placemark needs a position: a Point, a "
            "gx:Track with as many gx:coord as when elements, or both. The placemark number is "
            "the record number."
        ),
        refused=(
            "A DOCTYPE declaration and malformed XML refuse the whole file (no entity "
            "expansion). Per placemark: an empty when/begin, coordinates that are missing, "
            "unparsable, out of range or at null island, unparsable times, times before 1990 "
            "or after 2099, a TimeSpan with an end but no begin, a MultiGeometry with several "
            "Points but no time, and tracks whose when and coord counts differ. KML reports no "
            "accuracy in metres."
        ),
        example=(
            "<Placemark><name>Stop</name>",
            "  <TimeStamp><when>2026-09-02T06:15:11Z</when></TimeStamp>",
            "  <Point><coordinates>6.90872,51.43612</coordinates></Point>",
            "</Placemark>",
        ),
    ),
    InputFormatHelp(
        name="kmz",
        suffixes=(".kmz", ".kml"),
        recognised_by="a .kmz file, or a .kml file that starts with the ZIP signature",
        reads=(
            "The root-level doc.kml if present, else the first .kml entry in archive order, "
            "decompressed straight into the KML reader; then every KML rule above. Nothing is "
            "unpacked to disk, so entry names with absolute or parent paths cannot write anywhere."
        ),
        required="A ZIP archive holding at least one .kml entry.",
        refused=(
            "As KML, plus an archive without a .kml entry. Further .kml entries are not read; "
            "their number and first names are given in a warning. Images and other entries are "
            "ignored."
        ),
        example=(
            "archive.kmz",
            "  doc.kml      <- read as KML",
            "  files/1.png  <- ignored",
        ),
    ),
    InputFormatHelp(
        name="gpx",
        suffixes=(".gpx",),
        recognised_by="",
        reads=(
            "wpt, rtept and trkpt elements in document order, GPX 1.0 or 1.1: name becomes the "
            "label, desc and cmt the note, hdop stays in the note as a dilution factor, and fix "
            "(2d, 3d, dgps or pps) marks the position as satellite-based and stays in the note. "
            "A point without a time is read without a timestamp."
        ),
        required=(
            "The root element must be gpx in the GPX 1.0 or 1.1 namespace, and a point needs "
            "lat and lon. The position among the points is the record number."
        ),
        refused=(
            "A DOCTYPE declaration, malformed XML and a root element in another namespace "
            "refuse the whole file. Per point: an empty time element, missing or unparsable "
            "lat/lon, values out of range, null island, times before 1990 or after 2099. GPX "
            "has no accuracy in metres, so records carry none."
        ),
        example=(
            '<trkpt lat="51.43612" lon="6.90872">',
            "  <time>2026-09-02T06:15:11Z</time>",
            "</trkpt>",
        ),
    ),
    InputFormatHelp(
        name="csv",
        suffixes=(".csv",),
        recognised_by="",
        reads=(
            "The columns you map in the Columns… dialog: latitude and longitude or one "
            "position column, a date/timestamp and a time of day, the end of a time span, the "
            "accuracy in metres, a label, a note and the positioning method (GNSS, Wi-Fi, cell "
            "or network; other values leave it unknown). Every column without a role, and the "
            "method column, goes into the note as 'header: value'. Of several method columns, "
            "the one naming known methods is used; if they disagree, you confirm the choice."
        ),
        required=(
            "A header row (the first non-blank line) and a usable column mapping: coordinates "
            "are required, a timestamp is optional. Without a time column every row is read "
            "without a timestamp, and so is a row with any mapped time cell that is empty (the "
            "date, the time of day or both). Times without an offset need a time zone, and "
            "the delimiter, the time format and the day/month order are part of the mapping. "
            "A radius or range column used as accuracy needs your confirmation: a cell sector "
            "radius is not an accuracy."
        ),
        refused=(
            "The run cannot start while a CSV source has no usable mapping. Per row: a cell "
            "that is in no single coordinate notation, a time that is there but unreadable "
            "(missing is not wrong), a date whose day/month order was never chosen, a date "
            "cell that contradicts the time-of-day cell, a quoted field that does not close "
            "within 50 lines, a field above 16 MiB and times before 1990 or after 2099. An "
            "empty accuracy means 'not reported', not a refusal."
        ),
        example=(
            "Breite;Länge;Zeitstempel;Genauigkeit",
            "51,43612;6,90872;02.09.2026 06:15:11;19,8",
        ),
    ),
    InputFormatHelp(
        name="geojson",
        suffixes=(".geojson", ".json"),
        recognised_by='"type": "FeatureCollection" or "type": "Feature" in the first 64 KB',
        reads=(
            "Features one by one (RFC 7946): a Point becomes one record, a MultiPoint one "
            "record per position. Timestamp, accuracy, name and positioning method come from the "
            "properties by the same synonyms as the CSV mapping; every other property, the "
            "method included, goes into the note as 'name: value'. A property named radius is "
            "taken as accuracy and the note says so, since it may be a cell sector radius. A "
            "feature without a time candidate is read without a timestamp."
        ),
        required=(
            "A FeatureCollection with a features array, or a single Feature. Positions are "
            "[longitude, latitude] in WGS 84 by definition of the format. The feature's "
            "position in the collection is the record number."
        ),
        refused=(
            "The whole file: not UTF-8, malformed JSON, a top-level value that is no object, a "
            "type other than FeatureCollection or Feature, a missing features array, content "
            "after the closing brace, a single feature above 16 MiB. Per feature: a member "
            "name that occurs twice, several timestamp or accuracy properties, an end time "
            "without a start time, a date beside a time property that is no plain time name, "
            "coordinates that are not two numbers, out of range, not finite or at null island, "
            "times before 1990 or after 2099, and times without an offset while the display "
            "zone is 'local'."
        ),
        example=(
            '{"type": "Feature",',
            ' "geometry": {"type": "Point", "coordinates": [6.90872, 51.43612]},',
            ' "properties": {"time": "2026-09-02T06:15:11Z", "accuracy": 19.8}}',
        ),
    ),
    InputFormatHelp(
        name="google-records",
        suffixes=(".json",),
        recognised_by='{"locations": [ at the start of the file',
        reads=(
            "The location history of a Google Takeout Records.json, streamed record by record, "
            "so files of several GB need no more memory: latitudeE7/longitudeE7, timestamp or "
            "timestampMs, accuracy in metres, with source and deviceTag in the note. A source of "
            "GPS, WIFI or CELL also gives the positioning method."
        ),
        required=(
            "A locations array, and every time must carry an offset. E7 values above "
            "1,800,000,000 are corrected by 2^32, a documented export quirk."
        ),
        refused=(
            "The whole file: exactly one comma is expected between records and nothing but the "
            "closing brace after the array (records read before the error are documented but "
            "not used). Per record: a member name that occurs twice, a time without an offset, "
            "times before 1990 or after 2099, coordinates out of range or at null island."
        ),
        example=(
            '{"locations": [',
            '  {"latitudeE7": 514361200, "longitudeE7": 69087200,',
            '   "timestamp": "2026-09-02T06:15:11Z", "accuracy": 20}',
            "]}",
        ),
    ),
    InputFormatHelp(
        name="google-timeline",
        suffixes=(".json",),
        recognised_by='{"semanticSegments", "rawSignals" or "userLocationProfile"',
        reads=(
            "An on-device Timeline export (2024 and later), read as a whole: timelinePath "
            "points, rawSignals positions with accuracyMeters and source (GPS, WIFI or CELL "
            "gives the positioning method), visits and activities, and the frequent places as "
            "records without a timestamp."
        ),
        required="One of the three top-level members, and every time must carry an offset.",
        refused=(
            "As for Records.json, per entry; files above 256 MiB. Visits and activities are "
            "Google's own inference: their start and end points say so in the note and are not "
            "GEOSnap stays by themselves."
        ),
        example=(
            '{"semanticSegments": [',
            '  {"timelinePath": [{"point": "51.43612°, 6.90872°",',
            '     "time": "2026-09-02T06:15:11.000+02:00"}]}',
            "]}",
        ),
    ),
    InputFormatHelp(
        name="google-semantic",
        suffixes=(".json",),
        recognised_by='{"timelineObjects" at the start of the file',
        reads=(
            "A Takeout Semantic Location History, read as a whole: placeVisit entries, "
            "activitySegment start and end points, and simplifiedRawPath points with "
            "accuracyMeters."
        ),
        required="A timelineObjects array, and every time must carry an offset.",
        refused=(
            "As for Records.json, per entry; files above 256 MiB. Visits and activities are "
            "Google's inference."
        ),
        example=(
            '{"timelineObjects": [',
            '  {"placeVisit": {"location": {"latitudeE7": 514361200,',
            '     "longitudeE7": 69087200}, "duration": {',
            '     "startTimestamp": "2026-09-02T06:15:11Z"}}}',
            "]}",
        ),
    ),
)

# A .json file that is none of the four recognised JSON families is refused, never guessed.
UNRECOGNISED_JSON_HELP = (
    "A .json file that is neither a Google export nor GeoJSON is refused with a reason that "
    "names both families; GEOSnap never guesses a layout."
)


def format_help_by_name(name: str) -> InputFormatHelp:
    """The entry of a format name from SOURCE_FORMATS; raises KeyError for anything else."""
    for entry in FORMAT_HELP:
        if entry.name == name:
            return entry
    raise KeyError(name)
