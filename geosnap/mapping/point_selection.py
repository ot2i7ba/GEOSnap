# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Decide which points the HTML map renders; the CSV export is never affected."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import TypeVar

from geosnap.extraction.duplicates import AnnotatedPoint

# Dated map points or undated records: thinning only counts and keeps order.
Thinned = TypeVar("Thinned")


def collapse_duplicates(points: Sequence[AnnotatedPoint]) -> list[AnnotatedPoint]:
    """Keep the chronologically first report per position. Input must be chronological."""
    seen_positions: set[tuple[int, float, float, float, float, str | None]] = set()
    kept: list[AnnotatedPoint] = []
    for entry in points:
        position = entry.point.duplicate_key
        if position in seen_positions:
            continue
        seen_positions.add(position)
        kept.append(entry)
    return kept


def thin_points(points: Sequence[Thinned], point_limit: int) -> tuple[list[Thinned], int]:
    """Reduce a chronological sequence to at most point_limit entries.

    Every stride-th point is kept, plus the last one, so the first and last report
    always survive. The result is deterministic for a given input and limit.
    """
    if point_limit < 2:
        raise ValueError("point_limit must be at least 2")
    total = len(points)
    if total <= point_limit:
        return list(points), 1
    stride = math.ceil((total - 1) / (point_limit - 1))
    selected = [points[index] for index in range(0, total, stride)]
    if selected[-1] is not points[-1]:
        selected.append(points[-1])
    return selected, stride


def thin_points_per_source(
    points: Sequence[AnnotatedPoint], point_limit: int
) -> tuple[list[AnnotatedPoint], dict[int, int]]:
    """thin_per_source for the dated map points."""
    return thin_per_source(points, point_limit, lambda entry: entry.point.source_id)


def thin_per_source(
    points: Sequence[Thinned], point_limit: int, source_id_of: Callable[[Thinned], int]
) -> tuple[list[Thinned], dict[int, int]]:
    """Thin every source on its own, so a small source never vanishes behind a large one.

    Each source keeps min(count, 2) points (its first and last) and shares the rest of
    point_limit in proportion to its count; ``thin_points`` then selects within that
    quota. The result keeps the order of the input (chronological: time, source, line)
    and comes with the stride per source. With more sources than point_limit / 2 the
    guaranteed minimums may exceed point_limit. ``source_id_of`` names the source of an
    entry; the same rule thins the dated map points and the undated records.
    """
    by_source: dict[int, list[Thinned]] = {}
    for entry in points:
        by_source.setdefault(source_id_of(entry), []).append(entry)
    if len(points) <= point_limit:
        return list(points), {source_id: 1 for source_id in by_source}

    minimums = {source_id: min(len(entries), 2) for source_id, entries in by_source.items()}
    shareable = max(point_limit - sum(minimums.values()), 0)
    beyond_minimum_total = sum(
        len(entries) - minimums[source_id] for source_id, entries in by_source.items()
    )
    kept_ids: set[int] = set()
    strides: dict[int, int] = {}
    for source_id, entries in by_source.items():
        beyond_minimum = len(entries) - minimums[source_id]
        quota = minimums[source_id] + (
            shareable * beyond_minimum // beyond_minimum_total if beyond_minimum_total else 0
        )
        if len(entries) <= quota:
            selected, strides[source_id] = entries, 1
        else:
            selected, strides[source_id] = thin_points(entries, quota)
        kept_ids.update(id(entry) for entry in selected)
    return [entry for entry in points if id(entry) in kept_ids], strides
