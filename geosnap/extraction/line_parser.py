# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Recognise and validate location records such as
'51.43612 ± 19.83 meters | 6.90872 ± 19.83 meters | 2026-09-02 06:15:11 +0000 UTC'."""

from __future__ import annotations

import math
import re

from geosnap.extraction.models import GeoPoint, RejectedLine
from geosnap.extraction.reader_support import checked_position, excerpt
from geosnap.extraction.timestamp_text import recognise_timestamp

COORDINATE_LINE = re.compile(
    r"(?<![\w.])(?P<lat>[-+]?\d{1,3}\.\d+)\s*±\s*(?P<lat_acc>\d+(?:\.\d+)?)\s*meters?\s*\|\s*"
    r"(?P<lon>[-+]?\d{1,3}\.\d+)\s*±\s*(?P<lon_acc>\d+(?:\.\d+)?)\s*meters?\s*\|\s*"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<offset>[+-]\d{4}) (?P<tzname>[A-Z]{2,5})"
)
# Text left over once the records are taken out that still holds both marks of a record
# (± and |) is a record in another layout (fractional seconds, "m", no zone name):
# refused, not skipped.
NEAR_MISS_MARKS = ("±", "|")
NEAR_MISS_REASON = (
    "record does not match the expected layout"
    " 'lat ± acc meters | lon ± acc meters | YYYY-MM-DD hh:mm:ss +hhmm ZONE'"
)


def parse_line_records(line: str, line_number: int) -> list[GeoPoint | RejectedLine]:
    """Every record of one source line, in line order: a GeoPoint for a valid record, a
    RejectedLine for one that fails validation; then one RejectedLine when the rest of
    the line still looks like a record in another layout. An empty list when the line
    holds no record at all."""
    records: list[GeoPoint | RejectedLine] = []
    leftover_parts: list[str] = []
    previous_end = 0
    for match in COORDINATE_LINE.finditer(line):
        leftover_parts.append(line[previous_end : match.start()])
        previous_end = match.end()
        records.append(_record(match, line, line_number))
    leftover_parts.append(line[previous_end:])
    leftover = " ".join(leftover_parts)
    if all(mark in leftover for mark in NEAR_MISS_MARKS):
        records.append(RejectedLine(line_number, f"{NEAR_MISS_REASON}: {excerpt(leftover)}", line))
    return records


def parse_line(line: str, line_number: int) -> GeoPoint | RejectedLine | None:
    """The first record of one source line (parse_line_records), None when there is none."""
    records = parse_line_records(line, line_number)
    return records[0] if records else None


def _record(match: re.Match[str], line: str, line_number: int) -> GeoPoint | RejectedLine:
    latitude = float(match["lat"])
    longitude = float(match["lon"])
    latitude_accuracy_m = float(match["lat_acc"])
    longitude_accuracy_m = float(match["lon_acc"])
    # The regex only admits digits, but a few hundred of them overflow float() to infinity.
    if not all(
        math.isfinite(number)
        for number in (latitude, longitude, latitude_accuracy_m, longitude_accuracy_m)
    ):
        return RejectedLine(line_number, "non-finite number in coordinates or accuracy", line)
    position = checked_position(latitude, longitude)
    if isinstance(position, str):
        return RejectedLine(line_number, position, line)

    # The shared recogniser reads the offset and refuses a zone name that contradicts it
    # ("+0200 UTC") or the date ("+0100 CEST"); an unlisted name is read by the offset.
    timestamp_text = f"{match['ts']} {match['offset']} {match['tzname']}"
    recognised = recognise_timestamp(timestamp_text, {})
    if isinstance(recognised, str):
        return RejectedLine(line_number, recognised, line)

    return GeoPoint(
        line_number=line_number,
        latitude=latitude,
        longitude=longitude,
        latitude_accuracy_m=latitude_accuracy_m,
        longitude_accuracy_m=longitude_accuracy_m,
        timestamp_utc=recognised.moment,
        original_line=line,
    )
