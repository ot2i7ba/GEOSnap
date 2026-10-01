# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Results of the forensic analysis, serialised into analysis.json and the map payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from geosnap.extraction.models import GeoPoint, SourceDescriptor


@dataclass(slots=True)
class Address:
    display_name: str
    osm_type: str | None
    osm_id: int | None
    looked_up_utc: datetime


@dataclass(slots=True)
class PositionSummary:
    line_number: int
    latitude: float
    longitude: float
    accuracy_m: float
    timestamp_utc: datetime
    address: Address | None = None
    # False when the source reports no accuracy (KML); accuracy_m is then 0.0.
    accuracy_known: bool = True


@dataclass(slots=True)
class Stay:
    identifier: int
    latitude: float
    longitude: float
    arrive_utc: datetime
    leave_utc: datetime
    duration_minutes: float
    point_count: int
    mean_accuracy_m: float
    first_line: int
    last_line: int
    # Latest true instant of the first record: arrive_utc plus its time ambiguity (a local
    # time that occurred twice may be its later occurrence); arrive_utc when unambiguous.
    arrived_by_utc: datetime
    address: Address | None = None
    is_last: bool = False
    # False when any member point has no reported accuracy; mean_accuracy_m is then meaningless.
    accuracy_known: bool = True
    # The analysed record before (after) the stay when it shows the device elsewhere
    # (stays.find_stays): the device arrived after (left before) it. None: not bounded.
    arrived_after_utc: datetime | None = None
    left_before_utc: datetime | None = None


@dataclass(slots=True)
class Gap:
    identifier: int
    from_line: int
    to_line: int
    start_utc: datetime
    end_utc: datetime
    duration_minutes: float
    distance_m: float
    # Endpoint positions, so the map can draw the gap even when both points were thinned away.
    from_latitude: float
    from_longitude: float
    to_latitude: float
    to_longitude: float


@dataclass(slots=True)
class Segment:
    from_line: int
    to_line: int
    start_utc: datetime
    end_utc: datetime
    distance_m: float
    duration_seconds: float
    speed_kmh: float | None
    bearing_deg: float
    movement_class: str
    # Speed interval under the accuracy and time-resolution premise;
    # None without accuracies (low) or when the elapsed time is within the resolution (high).
    speed_low_kmh: float | None = None
    speed_high_kmh: float | None = None
    # The same interval with the radii as reported (equal to the above at scale 1).
    speed_low_reported_kmh: float | None = None
    speed_high_reported_kmh: float | None = None


@dataclass(slots=True)
class AnalysisTotals:
    time_span_minutes: float
    # Over the plausible steps; implausible and unknown steps are summed apart, so a
    # position jump never counts as distance travelled.
    distance_m: float
    max_speed_kmh: float | None
    stays: int
    gaps: int
    implausible_segments: int
    distance_left_out_m: float = 0.0
    segments_left_out: int = 0


@dataclass(slots=True)
class AnalysisReport:
    parameters: dict[str, int]
    generated_at_utc: datetime
    points_total: int
    points_analysed: int
    points_excluded_accuracy: int
    first: PositionSummary | None
    last: PositionSummary | None
    stays: list[Stay] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    totals: AnalysisTotals = field(default_factory=lambda: AnalysisTotals(0.0, 0.0, None, 0, 0, 0))
    # Of every dated record of the source (segments.time_resolution_seconds).
    time_resolution_seconds: float = 1.0
    # Records within the accuracy limit left out because of their positioning method.
    points_excluded_positioning_method: int = 0
    # Of those, the ones newer than the last analysed record (the last known position).
    points_excluded_positioning_method_after_last: int = 0
    # Their positioning methods, sorted.
    positioning_methods_excluded_after_last: list[str] = field(default_factory=list)
    # Dated records per positioning method ("unknown" for records without one).
    positioning_methods: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceAnalysis:
    """One source of a project: its descriptor, chronological points and own report."""

    source: SourceDescriptor
    points: list[GeoPoint]
    report: AnalysisReport
