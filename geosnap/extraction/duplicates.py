# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Attach duplicate statistics to extracted points."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from geosnap.extraction.models import GeoPoint


@dataclass(frozen=True, slots=True)
class AnnotatedPoint:
    """A point plus how often, and when, its exact position was reported."""

    point: GeoPoint
    duplicate_count: int
    first_seen_utc: datetime
    last_seen_utc: datetime


def annotate_duplicates(points: Sequence[GeoPoint]) -> list[AnnotatedPoint]:
    """Sort points chronologically and count reports per position.

    Two records of one source are duplicates when latitude, longitude and both accuracies
    match; the timestamp is ignored so a stationary device shows up as one
    position with a report count. Ties in time are ordered by source, then line.
    """
    ordered = sorted(
        points, key=lambda point: (point.timestamp_utc, point.source_id, point.line_number)
    )
    groups: dict[tuple[int, float, float, float, float, str | None], list[GeoPoint]] = {}
    for point in ordered:
        groups.setdefault(point.duplicate_key, []).append(point)
    return [
        AnnotatedPoint(
            point=point,
            duplicate_count=len(groups[point.duplicate_key]),
            first_seen_utc=groups[point.duplicate_key][0].timestamp_utc,
            last_seen_utc=groups[point.duplicate_key][-1].timestamp_utc,
        )
        for point in ordered
    ]
