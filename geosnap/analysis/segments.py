# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Movement between consecutive points: distance, duration, speed, bearing, class.

Distances are WGS84 geodesics (analysis.geometry). Every speed comes with an interval under
the premise that each true position lies within its uncertainty circle (the reported radius at
the confidence level of the analysis, GeoPoint.uncertainty_radius_m) and each true instant
within the time resolution of its source; a second interval uses the radii as reported."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from geosnap.analysis.geometry import geodesic_distance_and_bearing
from geosnap.analysis.models import Segment
from geosnap.extraction.models import GeoPoint

STATIONARY_MAX_KMH = 1.0
WALKING_MAX_KMH = 7.0
CYCLING_MAX_KMH = 25.0

# The time resolutions a source can be proven to have, coarsest first.
TIME_RESOLUTIONS_SECONDS = (60.0, 1.0, 0.001, 0.000001)


def time_resolution_seconds(points: Sequence[GeoPoint]) -> float:
    """The coarsest of 60 s, 1 s, 1 ms and 1 us of which every timestamp is a whole
    multiple. A finer clock whose values all happen to be whole seconds reads as 1 s: the
    intervals get wider, never wrong."""
    whole_minutes = whole_seconds = whole_milliseconds = True
    for point in points:
        instant = point.timestamp_utc
        if instant.microsecond:
            whole_minutes = whole_seconds = False
            if instant.microsecond % 1000:
                return TIME_RESOLUTIONS_SECONDS[3]
        elif instant.second:
            whole_minutes = False
    if whole_minutes:
        return TIME_RESOLUTIONS_SECONDS[0]
    if whole_seconds:
        return TIME_RESOLUTIONS_SECONDS[1]
    return TIME_RESOLUTIONS_SECONDS[2] if whole_milliseconds else TIME_RESOLUTIONS_SECONDS[3]


def accuracy_sum_m(start: GeoPoint, end: GeoPoint) -> float | None:
    """Sum of the two uncertainty radii, or None when either record reports no accuracy."""
    if not (start.accuracy_known and end.accuracy_known):
        return None
    return start.uncertainty_radius_m + end.uncertainty_radius_m


def reported_accuracy_sum_m(start: GeoPoint, end: GeoPoint) -> float | None:
    """Sum of the two radii as reported, or None when either record reports no accuracy."""
    if not (start.accuracy_known and end.accuracy_known):
        return None
    return start.accuracy_radius_m + end.accuracy_radius_m


def minimum_step_m(distance_m: float, accuracy_sum: float | None) -> float:
    """The shortest the true step can be under the accuracy premise; 0 without accuracies."""
    return 0.0 if accuracy_sum is None else max(0.0, distance_m - accuracy_sum)


def time_uncertainty_seconds(start: GeoPoint, end: GeoPoint, time_resolution: float) -> float:
    """How far the true elapsed time between two records can differ from the recorded one:
    the time resolution of the source plus, for an ambiguous local time read as its earlier
    occurrence, how much later its second occurrence lies."""
    return time_resolution + start.time_ambiguity_seconds + end.time_ambiguity_seconds


def speed_bounds_kmh(
    distance_m: float,
    duration_seconds: float,
    accuracy_sum: float | None,
    time_uncertainty: float,
) -> tuple[float | None, float | None]:
    """(lowest, highest) speed the step allows: (max(0, d - r1 - r2) / (dt + u),
    (d + r1 + r2) / (dt - u)) with u = time_uncertainty_seconds. No interval without
    accuracies; no upper bound when the elapsed time lies within the uncertainty."""
    if accuracy_sum is None:
        return None, None
    lowest = max(0.0, distance_m - accuracy_sum) / (duration_seconds + time_uncertainty) * 3.6
    if duration_seconds <= time_uncertainty:
        return lowest, None
    return lowest, (distance_m + accuracy_sum) / (duration_seconds - time_uncertainty) * 3.6


def lower_bound_text_value(speed_kmh: float) -> float:
    """A lower speed bound rounded down to 0.1 km/h: rounding must never tighten a bound
    (the 1e-9 absorbs binary noise such as 1.2 * 10 = 12.000000000000002)."""
    return math.floor(speed_kmh * 10 + 1e-9) / 10


def upper_bound_text_value(speed_kmh: float) -> float:
    """An upper speed bound rounded up to 0.1 km/h (see lower_bound_text_value)."""
    return math.ceil(speed_kmh * 10 - 1e-9) / 10


def classify_movement(
    distance_m: float,
    duration_seconds: float,
    speed_kmh: float | None,
    combined_accuracy_m: float,
    implausible_speed_kmh: int,
    speed_low_kmh: float | None = None,
    gap_min_minutes: float | None = None,
) -> str:
    """Estimate how the device moved; movement inside the measurement uncertainty is
    stationary. Two records at the same instant are implausible when even the lowest speed
    their interval allows reaches the threshold, else unknown; a slow average across a
    silence of at least gap_min_minutes says nothing about standing still, so a step longer
    than the combined accuracy is unknown there."""
    if duration_seconds <= 0 or speed_kmh is None:
        if speed_low_kmh is not None and speed_low_kmh >= implausible_speed_kmh:
            return "implausible"
        return "unknown"
    if speed_kmh >= implausible_speed_kmh:
        return "implausible"
    if distance_m <= combined_accuracy_m:
        return "stationary"
    if speed_kmh < STATIONARY_MAX_KMH:
        if gap_min_minutes is not None and duration_seconds >= gap_min_minutes * 60:
            return "unknown"
        return "stationary"
    if speed_kmh < WALKING_MAX_KMH:
        return "walking"
    if speed_kmh < CYCLING_MAX_KMH:
        return "cycling"
    return "vehicle"


@dataclass(frozen=True, slots=True)
class StepFromPrevious:
    """How an analysed record relates to the previous analysed record of its source in the
    full, unthinned list (map payload spd/dpm/dts): the first record has zero
    distance and duration and no speed. ``path_m`` is the path distance from the first
    record and ``sequence`` the 1-based position in that list, so a thinned map can still
    sum a range over every record. ``speed_low_kmh``/``speed_high_kmh`` bound the speed
    (speed_bounds_kmh), ``minimum_path_m`` sums minimum_step_m from the first record and
    ``movement_class`` is the class of the step's segment. The ``_reported`` fields are the
    same figures with the radii as reported."""

    distance_m: float
    duration_seconds: float
    speed_kmh: float | None
    path_m: float
    sequence: int
    speed_low_kmh: float | None
    speed_high_kmh: float | None
    minimum_path_m: float
    movement_class: str | None
    speed_low_reported_kmh: float | None = None
    speed_high_reported_kmh: float | None = None
    minimum_path_reported_m: float = 0.0


class SourceSteps:
    """The steps of every analysed record of one source, index-aligned with the list they
    were built from. Columns instead of one object per record, so a million records do not
    cost seconds. Points are found by identity: a gx:Track placemark gives all of its points
    the same record number, so a lookup by number would hand every one of them the last
    point's figures. ``_points`` keeps the objects alive, so the identities stay valid."""

    __slots__ = (
        "_index_by_identity",
        "_points",
        "distance_m",
        "duration_seconds",
        "minimum_path_m",
        "minimum_path_reported_m",
        "movement_class",
        "path_m",
        "speed_high_kmh",
        "speed_high_reported_kmh",
        "speed_kmh",
        "speed_low_kmh",
        "speed_low_reported_kmh",
        "time_resolution_seconds",
    )

    def __init__(self, points: Sequence[GeoPoint]) -> None:
        self.distance_m: list[float] = []
        self.duration_seconds: list[float] = []
        self.speed_kmh: list[float | None] = []
        self.speed_low_kmh: list[float | None] = []
        self.speed_high_kmh: list[float | None] = []
        self.path_m: list[float] = []
        self.minimum_path_m: list[float] = []
        self.movement_class: list[str | None] = []
        self.speed_low_reported_kmh: list[float | None] = []
        self.speed_high_reported_kmh: list[float | None] = []
        self.minimum_path_reported_m: list[float] = []
        # Holds the points alive so the identities below stay valid; a list is not copied.
        self._points: Sequence[GeoPoint] = points if isinstance(points, list) else list(points)
        self._index_by_identity: dict[int, int] = {}
        self.time_resolution_seconds = 1.0

    def __len__(self) -> int:
        return len(self.distance_m)

    def __getitem__(self, index: int) -> StepFromPrevious:
        return StepFromPrevious(
            self.distance_m[index],
            self.duration_seconds[index],
            self.speed_kmh[index],
            self.path_m[index],
            index + 1,
            self.speed_low_kmh[index],
            self.speed_high_kmh[index],
            self.minimum_path_m[index],
            self.movement_class[index],
            self.speed_low_reported_kmh[index],
            self.speed_high_reported_kmh[index],
            self.minimum_path_reported_m[index],
        )

    def for_point(self, point: GeoPoint) -> StepFromPrevious | None:
        """The step of this exact point, or None when it is not one of the analysed
        records (a record the accuracy limit excluded has no speed figures)."""
        index = self._index_by_identity.get(id(point))
        return None if index is None else self[index]


def steps_from_previous(
    points: Sequence[GeoPoint],
    segments: Sequence[Segment],
    time_resolution: float | None = None,
) -> SourceSteps:
    """The steps of one source's analysed points in chronological order (see
    StepFromPrevious), taken from the segments of the same list (``report.segments``, built
    over ``report.select_analysis_points``): the map's figures are the report's, number for
    number, including the movement class. time_resolution is the one the segments were built
    with (``AnalysisReport.time_resolution_seconds``; by default that of ``points``)."""
    steps = SourceSteps(points)
    steps.time_resolution_seconds = (
        time_resolution if time_resolution is not None else time_resolution_seconds(steps._points)
    )
    ordered = steps._points
    if len(segments) != max(0, len(ordered) - 1):
        raise ValueError("the segments do not belong to these points")
    index_by_identity = steps._index_by_identity
    path_m = 0.0
    minimum_path_m = 0.0
    minimum_path_reported_m = 0.0
    for index, point in enumerate(ordered):
        index_by_identity[id(point)] = index
        if index == 0:
            steps.distance_m.append(0.0)
            steps.duration_seconds.append(0.0)
            steps.speed_kmh.append(None)
            steps.speed_low_kmh.append(None)
            steps.speed_high_kmh.append(None)
            steps.speed_low_reported_kmh.append(None)
            steps.speed_high_reported_kmh.append(None)
            steps.movement_class.append(None)
        else:
            segment = segments[index - 1]
            if (
                segment.start_utc != ordered[index - 1].timestamp_utc
                or segment.end_utc != point.timestamp_utc
            ):
                raise ValueError("the segments do not belong to these points")
            path_m += segment.distance_m
            minimum_path_m += minimum_step_m(
                segment.distance_m, accuracy_sum_m(ordered[index - 1], point)
            )
            minimum_path_reported_m += minimum_step_m(
                segment.distance_m, reported_accuracy_sum_m(ordered[index - 1], point)
            )
            steps.distance_m.append(segment.distance_m)
            steps.duration_seconds.append(segment.duration_seconds)
            steps.speed_kmh.append(segment.speed_kmh)
            steps.speed_low_kmh.append(segment.speed_low_kmh)
            steps.speed_high_kmh.append(segment.speed_high_kmh)
            steps.speed_low_reported_kmh.append(segment.speed_low_reported_kmh)
            steps.speed_high_reported_kmh.append(segment.speed_high_reported_kmh)
            steps.movement_class.append(segment.movement_class)
        steps.path_m.append(path_m)
        steps.minimum_path_m.append(minimum_path_m)
        steps.minimum_path_reported_m.append(minimum_path_reported_m)
    return steps


def build_segments(
    points: Sequence[GeoPoint],
    implausible_speed_kmh: int,
    gap_min_minutes: float | None = None,
    time_resolution: float | None = None,
) -> list[Segment]:
    """One segment per pair of chronologically adjacent points. time_resolution is that of
    the source (all its dated records); by default that of ``points``."""
    resolution = time_resolution if time_resolution is not None else time_resolution_seconds(points)
    segments: list[Segment] = []
    for start, end in zip(points, points[1:], strict=False):
        distance_m, bearing_deg = geodesic_distance_and_bearing(
            start.latitude, start.longitude, end.latitude, end.longitude
        )
        duration_seconds = (end.timestamp_utc - start.timestamp_utc).total_seconds()
        speed_kmh = distance_m / duration_seconds * 3.6 if duration_seconds > 0 else None
        accuracy_sum = accuracy_sum_m(start, end)
        time_uncertainty = time_uncertainty_seconds(start, end, resolution)
        speed_low, speed_high = speed_bounds_kmh(
            distance_m, duration_seconds, accuracy_sum, time_uncertainty
        )
        speed_low_reported, speed_high_reported = speed_bounds_kmh(
            distance_m, duration_seconds, reported_accuracy_sum_m(start, end), time_uncertainty
        )
        segments.append(
            Segment(
                from_line=start.line_number,
                to_line=end.line_number,
                start_utc=start.timestamp_utc,
                end_utc=end.timestamp_utc,
                distance_m=distance_m,
                duration_seconds=duration_seconds,
                speed_kmh=speed_kmh,
                bearing_deg=bearing_deg,
                movement_class=classify_movement(
                    distance_m,
                    duration_seconds,
                    speed_kmh,
                    start.uncertainty_radius_m + end.uncertainty_radius_m,
                    implausible_speed_kmh,
                    speed_low,
                    gap_min_minutes,
                ),
                speed_low_kmh=speed_low,
                speed_high_kmh=speed_high,
                speed_low_reported_kmh=speed_low_reported,
                speed_high_reported_kmh=speed_high_reported,
            )
        )
    return segments
