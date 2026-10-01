# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Periods without any record."""

from __future__ import annotations

from collections.abc import Sequence

from geosnap.analysis.geometry import geodesic_distance_m
from geosnap.analysis.models import Gap
from geosnap.extraction.models import GeoPoint


def find_gaps(points: Sequence[GeoPoint], gap_min_minutes: int) -> list[Gap]:
    """Gaps between chronologically adjacent points of at least gap_min_minutes."""
    gaps: list[Gap] = []
    for previous, following in zip(points, points[1:], strict=False):
        duration_minutes = (following.timestamp_utc - previous.timestamp_utc).total_seconds() / 60
        if duration_minutes < gap_min_minutes:
            continue
        gaps.append(
            Gap(
                identifier=len(gaps) + 1,
                from_line=previous.line_number,
                to_line=following.line_number,
                start_utc=previous.timestamp_utc,
                end_utc=following.timestamp_utc,
                duration_minutes=duration_minutes,
                distance_m=geodesic_distance_m(
                    previous.latitude, previous.longitude, following.latitude, following.longitude
                ),
                from_latitude=previous.latitude,
                from_longitude=previous.longitude,
                to_latitude=following.latitude,
                to_longitude=following.longitude,
            )
        )
    return gaps
