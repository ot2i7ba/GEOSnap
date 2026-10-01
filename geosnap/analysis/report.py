# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Assemble the analysis report and serialise it for analysis.json and the map."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from datetime import datetime, tzinfo
from typing import NamedTuple

from geosnap.analysis.gaps import find_gaps
from geosnap.analysis.models import (
    Address,
    AnalysisReport,
    AnalysisTotals,
    Gap,
    PositionSummary,
    Segment,
    Stay,
)
from geosnap.analysis.segments import (
    build_segments,
    lower_bound_text_value,
    time_resolution_seconds,
    upper_bound_text_value,
)
from geosnap.analysis.stays import find_stays
from geosnap.extraction.models import GeoPoint
from geosnap.settings import AnalysisSettings
from geosnap.timezones import offset_minutes, to_display_time

LOCAL_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
# Steps whose length is no distance travelled: a position jump, or a step whose movement
# cannot be classed (same instant, slow average across a silence).
LEFT_OUT_MOVEMENT_CLASSES = frozenset({"implausible", "unknown"})


# Key of GeoPoint.positioning_method None in the per-method counts.
UNKNOWN_POSITIONING_METHOD = "unknown"


def select_analysis_points(
    points: Sequence[GeoPoint],
    max_accuracy_m: int,
    excluded_positioning_methods: Collection[str] = (),
) -> list[GeoPoint]:
    """Points precise enough for stays and speeds (the reported radius within the limit)
    and not determined by an excluded positioning method; the rest stays on the map only."""
    return _split_analysis_points(points, max_accuracy_m, excluded_positioning_methods).analysed


class _AnalysisSelection(NamedTuple):
    """The analysed points and how many were left out; the accuracy limit counts first.
    ``excluded_method_after_last`` counts the method exclusions newer than the last
    analysed point, ``methods_after_last`` names their positioning methods."""

    analysed: list[GeoPoint]
    excluded_accuracy: int
    excluded_method: int
    excluded_method_after_last: int
    methods_after_last: list[str]


def _split_analysis_points(
    points: Sequence[GeoPoint], max_accuracy_m: int, excluded_positioning_methods: Collection[str]
) -> _AnalysisSelection:
    analysed: list[GeoPoint] = []
    excluded_accuracy = 0
    excluded_method = 0
    excluded_method_since_analysed = 0
    methods_since_analysed: set[str] = set()
    for point in points:
        if point.accuracy_radius_m > max_accuracy_m:
            excluded_accuracy += 1
        elif point.positioning_method in excluded_positioning_methods:
            excluded_method += 1
            excluded_method_since_analysed += 1
            methods_since_analysed.add(str(point.positioning_method))
        else:
            analysed.append(point)
            excluded_method_since_analysed = 0
            methods_since_analysed.clear()
    return _AnalysisSelection(
        analysed,
        excluded_accuracy,
        excluded_method,
        excluded_method_since_analysed,
        sorted(methods_since_analysed),
    )


def without_excluded_methods(
    points: list[GeoPoint], excluded_positioning_methods: Collection[str]
) -> list[GeoPoint]:
    """The points whose positioning method is not excluded (encounters); the list itself
    when no method is excluded."""
    if not excluded_positioning_methods:
        return points
    return [
        point for point in points if point.positioning_method not in excluded_positioning_methods
    ]


def positioning_method_counts(points: Sequence[GeoPoint]) -> dict[str, int]:
    """Records per positioning method, sorted by method; records without one count as
    "unknown"."""
    counts = Counter(point.positioning_method or UNKNOWN_POSITIONING_METHOD for point in points)
    return dict(sorted(counts.items()))


def build_analysis_report(
    points: Sequence[GeoPoint], settings: AnalysisSettings, generated_at_utc: datetime
) -> AnalysisReport:
    """Run every analysis on chronologically ordered points."""
    selection = _split_analysis_points(
        points, settings.max_accuracy_m, settings.excluded_positioning_methods
    )
    analysis_points = selection.analysed
    stays = find_stays(
        analysis_points,
        settings.stop_radius_m,
        settings.stop_min_minutes,
        gap_min_minutes=settings.gap_min_minutes,
        implausible_speed_kmh=settings.implausible_speed_kmh,
    )
    # A gap is a time without any record, so every dated record counts, also one beyond the
    # accuracy limit: it shows the device reported.
    gaps = find_gaps(points, settings.gap_min_minutes)
    # One time resolution per source, over all its dated records.
    resolution = time_resolution_seconds(points)
    segments = build_segments(
        analysis_points, settings.implausible_speed_kmh, settings.gap_min_minutes, resolution
    )
    left_out = [
        segment for segment in segments if segment.movement_class in LEFT_OUT_MOVEMENT_CLASSES
    ]
    counted = [
        segment for segment in segments if segment.movement_class not in LEFT_OUT_MOVEMENT_CLASSES
    ]
    speeds = [segment.speed_kmh for segment in counted if segment.speed_kmh is not None]
    time_span_minutes = (
        (analysis_points[-1].timestamp_utc - analysis_points[0].timestamp_utc).total_seconds() / 60
        if len(analysis_points) >= 2
        else 0.0
    )
    return AnalysisReport(
        parameters={
            "stop_radius_m": settings.stop_radius_m,
            "stop_min_minutes": settings.stop_min_minutes,
            "gap_min_minutes": settings.gap_min_minutes,
            "max_accuracy_m": settings.max_accuracy_m,
            "implausible_speed_kmh": settings.implausible_speed_kmh,
        },
        generated_at_utc=generated_at_utc,
        points_total=len(points),
        points_analysed=len(analysis_points),
        points_excluded_accuracy=selection.excluded_accuracy,
        points_excluded_positioning_method=selection.excluded_method,
        points_excluded_positioning_method_after_last=selection.excluded_method_after_last,
        positioning_methods_excluded_after_last=selection.methods_after_last,
        positioning_methods=positioning_method_counts(points),
        first=_position_summary(analysis_points[0]) if analysis_points else None,
        last=_position_summary(analysis_points[-1]) if analysis_points else None,
        stays=stays,
        gaps=gaps,
        segments=segments,
        time_resolution_seconds=resolution,
        totals=AnalysisTotals(
            time_span_minutes=time_span_minutes,
            distance_m=sum(segment.distance_m for segment in counted),
            max_speed_kmh=max(speeds) if speeds else None,
            stays=len(stays),
            gaps=len(gaps),
            implausible_segments=sum(
                1 for segment in segments if segment.movement_class == "implausible"
            ),
            distance_left_out_m=sum(segment.distance_m for segment in left_out),
            segments_left_out=len(left_out),
        ),
    )


def _position_summary(point: GeoPoint) -> PositionSummary:
    return PositionSummary(
        line_number=point.line_number,
        latitude=point.latitude,
        longitude=point.longitude,
        accuracy_m=point.accuracy_radius_m,
        timestamp_utc=point.timestamp_utc,
        accuracy_known=point.accuracy_known,
    )


def address_to_document(address: Address | None) -> dict[str, object] | None:
    if address is None:
        return None
    return {
        "display_name": address.display_name,
        "osm_type": address.osm_type,
        "osm_id": address.osm_id,
        "looked_up_utc": address.looked_up_utc.isoformat(),
    }


def local_stamp(moment_utc: datetime, display_tz: tzinfo | None) -> str:
    """Wall-clock time without offset; every such field has an offset sibling in minutes."""
    return to_display_time(moment_utc, display_tz).strftime(LOCAL_TIME_FORMAT)


def _position_document(
    position: PositionSummary | None, generated_at_utc: datetime, display_tz: tzinfo | None
) -> dict[str, object] | None:
    if position is None:
        return None
    age_minutes = (generated_at_utc - position.timestamp_utc).total_seconds() / 60
    return {
        "line": position.line_number,
        "lat": position.latitude,
        "lon": position.longitude,
        "accuracy_m": round(position.accuracy_m, 1),
        "accuracy_known": position.accuracy_known,
        "utc": position.timestamp_utc.isoformat(),
        "local": local_stamp(position.timestamp_utc, display_tz),
        "offset": offset_minutes(position.timestamp_utc, display_tz),
        "age_minutes": round(age_minutes, 1),
        "address": address_to_document(position.address),
    }


def _stay_document(stay: Stay, display_tz: tzinfo | None) -> dict[str, object]:
    return {
        "id": stay.identifier,
        "lat": stay.latitude,
        "lon": stay.longitude,
        "arrive_utc": stay.arrive_utc.isoformat(),
        "arrive_local": local_stamp(stay.arrive_utc, display_tz),
        "arrive_offset": offset_minutes(stay.arrive_utc, display_tz),
        "leave_utc": stay.leave_utc.isoformat(),
        "leave_local": local_stamp(stay.leave_utc, display_tz),
        "leave_offset": offset_minutes(stay.leave_utc, display_tz),
        "duration_minutes": round(stay.duration_minutes, 1),
        "point_count": stay.point_count,
        "mean_accuracy_m": round(stay.mean_accuracy_m, 1),
        "accuracy_known": stay.accuracy_known,
        "first_line": stay.first_line,
        "last_line": stay.last_line,
        "is_last": stay.is_last,
        "address": address_to_document(stay.address),
        **moment_fields("arrived_after", stay.arrived_after_utc, display_tz),
        **moment_fields("arrived_by", stay.arrived_by_utc, display_tz),
        **moment_fields("left_before", stay.left_before_utc, display_tz),
    }


def moment_fields(
    prefix: str, moment_utc: datetime | None, display_tz: tzinfo | None
) -> dict[str, object]:
    """<prefix>_utc, _local and _offset (minutes) of a moment, as every time of the analysis
    documents is given; all None without a moment."""
    if moment_utc is None:
        return {f"{prefix}_utc": None, f"{prefix}_local": None, f"{prefix}_offset": None}
    return {
        f"{prefix}_utc": moment_utc.isoformat(),
        f"{prefix}_local": local_stamp(moment_utc, display_tz),
        f"{prefix}_offset": offset_minutes(moment_utc, display_tz),
    }


def _gap_document(gap: Gap, display_tz: tzinfo | None) -> dict[str, object]:
    return {
        "id": gap.identifier,
        "from_line": gap.from_line,
        "to_line": gap.to_line,
        "start_utc": gap.start_utc.isoformat(),
        "start_local": local_stamp(gap.start_utc, display_tz),
        "start_offset": offset_minutes(gap.start_utc, display_tz),
        "end_utc": gap.end_utc.isoformat(),
        "end_local": local_stamp(gap.end_utc, display_tz),
        "end_offset": offset_minutes(gap.end_utc, display_tz),
        "duration_minutes": round(gap.duration_minutes, 1),
        "distance_m": round(gap.distance_m, 1),
        "from_lat": gap.from_latitude,
        "from_lon": gap.from_longitude,
        "to_lat": gap.to_latitude,
        "to_lon": gap.to_longitude,
    }


def _segment_document(segment: Segment) -> dict[str, object]:
    return {
        "from_line": segment.from_line,
        "to_line": segment.to_line,
        "start_utc": segment.start_utc.isoformat(),
        "end_utc": segment.end_utc.isoformat(),
        "distance_m": round(segment.distance_m, 1),
        "duration_seconds": round(segment.duration_seconds, 1),
        "speed_kmh": None if segment.speed_kmh is None else round(segment.speed_kmh, 1),
        # Bounds are rounded outwards.
        "speed_low_kmh": None
        if segment.speed_low_kmh is None
        else lower_bound_text_value(segment.speed_low_kmh),
        "speed_high_kmh": None
        if segment.speed_high_kmh is None
        else upper_bound_text_value(segment.speed_high_kmh),
        "speed_low_reported_kmh": None
        if segment.speed_low_reported_kmh is None
        else lower_bound_text_value(segment.speed_low_reported_kmh),
        "speed_high_reported_kmh": None
        if segment.speed_high_reported_kmh is None
        else upper_bound_text_value(segment.speed_high_reported_kmh),
        "bearing_deg": round(segment.bearing_deg, 1),
        "movement_class": segment.movement_class,
    }


def report_to_document(
    report: AnalysisReport, display_tz: tzinfo | None, display_zone: str
) -> dict[str, object]:
    """JSON-ready view of the report with local times in the display zone."""
    totals = report.totals
    return {
        "parameters": dict(report.parameters),
        "generated_at_utc": report.generated_at_utc.isoformat(),
        "display_zone": display_zone,
        "points_total": report.points_total,
        "points_analysed": report.points_analysed,
        "points_excluded_accuracy": report.points_excluded_accuracy,
        "points_excluded_positioning_method": report.points_excluded_positioning_method,
        "points_excluded_positioning_method_after_last": (
            report.points_excluded_positioning_method_after_last
        ),
        "positioning_methods": dict(report.positioning_methods),
        "first": _position_document(report.first, report.generated_at_utc, display_tz),
        "last": _position_document(report.last, report.generated_at_utc, display_tz),
        "stays": [_stay_document(stay, display_tz) for stay in report.stays],
        "gaps": [_gap_document(gap, display_tz) for gap in report.gaps],
        "segments": [_segment_document(segment) for segment in report.segments],
        "totals": {
            "time_span_minutes": round(totals.time_span_minutes, 1),
            "distance_m": round(totals.distance_m, 1),
            "max_speed_kmh": None
            if totals.max_speed_kmh is None
            else round(totals.max_speed_kmh, 1),
            "stays": totals.stays,
            "gaps": totals.gaps,
            "implausible_segments": totals.implausible_segments,
            "distance_left_out_m": round(totals.distance_left_out_m, 1),
            "segments_left_out": totals.segments_left_out,
        },
    }
