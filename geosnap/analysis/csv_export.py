# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""CSV exports of stays, gaps and segments per source, and of the cross-source results."""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from pathlib import Path

from geosnap.analysis.case_places import CasePlaceResult, ReportDistance
from geosnap.analysis.encounters import Encounter, SharedPlace
from geosnap.analysis.models import SourceAnalysis
from geosnap.analysis.presence_matrix import PresenceMatrix
from geosnap.analysis.segments import lower_bound_text_value, upper_bound_text_value
from geosnap.project.csv_export import CSV_ENCODING
from geosnap.timezones import local_iso_with_offset

SOURCE_COLUMNS = ("source_id", "source_label")
STAYS_CSV_COLUMNS = (
    *SOURCE_COLUMNS,
    "id",
    "latitude",
    "longitude",
    "arrive_utc",
    "arrive_local",
    "leave_utc",
    "leave_local",
    "duration_minutes",
    "point_count",
    "mean_accuracy_m",
    "first_line",
    "last_line",
    "is_last",
    "address",
    "arrived_after_utc",
    "arrived_after_local",
    "arrived_by_utc",
    "arrived_by_local",
    "left_before_utc",
    "left_before_local",
)
GAPS_CSV_COLUMNS = (
    *SOURCE_COLUMNS,
    "id",
    "from_line",
    "to_line",
    "start_utc",
    "start_local",
    "end_utc",
    "end_local",
    "duration_minutes",
    "distance_m",
)
SEGMENTS_CSV_COLUMNS = (
    *SOURCE_COLUMNS,
    "from_line",
    "to_line",
    "start_utc",
    "end_utc",
    "distance_m",
    "duration_seconds",
    "speed_kmh",
    "speed_low_kmh",
    "speed_high_kmh",
    "speed_low_reported_kmh",
    "speed_high_reported_kmh",
    "bearing_deg",
    "movement_class",
)
ENCOUNTERS_CSV_COLUMNS = (
    "id",
    "source_a_id",
    "source_a_label",
    "source_b_id",
    "source_b_label",
    "start_utc",
    "start_local",
    "end_utc",
    "end_local",
    "duration_minutes",
    "centre_latitude",
    "centre_longitude",
    "min_distance_m",
    "hits_a",
    "hits_b",
    "first_line_a",
    "last_line_a",
    "first_line_b",
    "last_line_b",
    "movement",
    "path_length_m",
    "displacement_m",
    "path_excluded_m",
    "implausible_steps",
    "min_time_offset_seconds",
    "closest_pair_offset_seconds",
    "coincided_within_accuracy_only",
    "time_ambiguity_seconds",
)
SHARED_PLACES_CSV_COLUMNS = (
    "place_id",
    "latitude",
    "longitude",
    "sources",
    "overlapping",
    "source_id",
    "source_label",
    "stay_id",
    "arrive_utc",
    "arrive_local",
    "leave_utc",
    "leave_local",
)


def write_stays_csv(
    path: Path, sources: Sequence[SourceAnalysis], display_tz: tzinfo | None
) -> int:
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(STAYS_CSV_COLUMNS)
        for entry in sources:
            for stay in entry.report.stays:
                writer.writerow(
                    [
                        entry.source.identifier,
                        entry.source.label,
                        stay.identifier,
                        # The centroid is computed, not recorded; 6 decimals are ~0.1 m.
                        f"{stay.latitude:.6f}",
                        f"{stay.longitude:.6f}",
                        stay.arrive_utc.isoformat(),
                        local_iso_with_offset(stay.arrive_utc, display_tz),
                        stay.leave_utc.isoformat(),
                        local_iso_with_offset(stay.leave_utc, display_tz),
                        f"{stay.duration_minutes:.1f}",
                        stay.point_count,
                        f"{stay.mean_accuracy_m:.1f}" if stay.accuracy_known else "",
                        stay.first_line,
                        stay.last_line,
                        "true" if stay.is_last else "false",
                        "" if stay.address is None else stay.address.display_name,
                        *_moment_cells(stay.arrived_after_utc, display_tz),
                        *_moment_cells(stay.arrived_by_utc, display_tz),
                        *_moment_cells(stay.left_before_utc, display_tz),
                    ]
                )
                written += 1
    return written


def _moment_cells(moment_utc: datetime | None, display_tz: tzinfo | None) -> list[str]:
    """UTC and local cells of a moment; both empty without one."""
    if moment_utc is None:
        return ["", ""]
    return [moment_utc.isoformat(), local_iso_with_offset(moment_utc, display_tz)]


def write_gaps_csv(path: Path, sources: Sequence[SourceAnalysis], display_tz: tzinfo | None) -> int:
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(GAPS_CSV_COLUMNS)
        for entry in sources:
            for gap in entry.report.gaps:
                writer.writerow(
                    [
                        entry.source.identifier,
                        entry.source.label,
                        gap.identifier,
                        gap.from_line,
                        gap.to_line,
                        gap.start_utc.isoformat(),
                        local_iso_with_offset(gap.start_utc, display_tz),
                        gap.end_utc.isoformat(),
                        local_iso_with_offset(gap.end_utc, display_tz),
                        f"{gap.duration_minutes:.1f}",
                        f"{gap.distance_m:.1f}",
                    ]
                )
                written += 1
    return written


def write_segments_csv(path: Path, sources: Sequence[SourceAnalysis]) -> int:
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(SEGMENTS_CSV_COLUMNS)
        for entry in sources:
            for segment in entry.report.segments:
                writer.writerow(
                    [
                        entry.source.identifier,
                        entry.source.label,
                        segment.from_line,
                        segment.to_line,
                        segment.start_utc.isoformat(),
                        segment.end_utc.isoformat(),
                        f"{segment.distance_m:.1f}",
                        f"{segment.duration_seconds:.1f}",
                        "" if segment.speed_kmh is None else f"{segment.speed_kmh:.1f}",
                        ""
                        if segment.speed_low_kmh is None
                        else f"{lower_bound_text_value(segment.speed_low_kmh):.1f}",
                        ""
                        if segment.speed_high_kmh is None
                        else f"{upper_bound_text_value(segment.speed_high_kmh):.1f}",
                        ""
                        if segment.speed_low_reported_kmh is None
                        else f"{lower_bound_text_value(segment.speed_low_reported_kmh):.1f}",
                        ""
                        if segment.speed_high_reported_kmh is None
                        else f"{upper_bound_text_value(segment.speed_high_reported_kmh):.1f}",
                        f"{segment.bearing_deg:.1f}",
                        segment.movement_class,
                    ]
                )
                written += 1
    return written


def write_encounters_csv(
    path: Path,
    encounters: Sequence[Encounter],
    source_labels: Mapping[int, str],
    display_tz: tzinfo | None,
) -> int:
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(ENCOUNTERS_CSV_COLUMNS)
        for encounter in encounters:
            writer.writerow(
                [
                    encounter.identifier,
                    encounter.source_a,
                    source_labels.get(encounter.source_a, ""),
                    encounter.source_b,
                    source_labels.get(encounter.source_b, ""),
                    encounter.start_utc.isoformat(),
                    local_iso_with_offset(encounter.start_utc, display_tz),
                    encounter.end_utc.isoformat(),
                    local_iso_with_offset(encounter.end_utc, display_tz),
                    f"{encounter.duration_minutes:.1f}",
                    f"{encounter.centre_latitude:.6f}",
                    f"{encounter.centre_longitude:.6f}",
                    f"{encounter.min_distance_m:.1f}",
                    encounter.hits_a,
                    encounter.hits_b,
                    encounter.first_line_a,
                    encounter.last_line_a,
                    encounter.first_line_b,
                    encounter.last_line_b,
                    encounter.movement,
                    f"{encounter.path_length_m:.1f}",
                    f"{encounter.displacement_m:.1f}",
                    f"{encounter.path_excluded_m:.1f}",
                    encounter.implausible_steps,
                    f"{encounter.min_time_offset_seconds:.3f}",
                    f"{encounter.closest_pair_offset_seconds:.3f}",
                    "true" if encounter.coincided_within_accuracy_only else "false",
                    f"{encounter.time_ambiguity_seconds:.0f}",
                ]
            )
    return len(encounters)


def write_shared_places_csv(
    path: Path,
    places: Sequence[SharedPlace],
    source_labels: Mapping[int, str],
    display_tz: tzinfo | None,
) -> int:
    """One row per visit, so every stay behind a shared place stays traceable."""
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(SHARED_PLACES_CSV_COLUMNS)
        for place in places:
            sources_text = "; ".join(source_labels.get(source, "") for source in place.sources)
            for visit in place.visits:
                writer.writerow(
                    [
                        place.identifier,
                        f"{place.latitude:.6f}",
                        f"{place.longitude:.6f}",
                        sources_text,
                        "true" if place.overlapping else "false",
                        visit.source_id,
                        source_labels.get(visit.source_id, ""),
                        visit.stay_identifier,
                        visit.arrive_utc.isoformat(),
                        local_iso_with_offset(visit.arrive_utc, display_tz),
                        visit.leave_utc.isoformat(),
                        local_iso_with_offset(visit.leave_utc, display_tz),
                    ]
                )
                written += 1
    return written


CASE_PLACES_CSV_COLUMNS = (
    "place_id",
    "place_label",
    "place_latitude",
    "place_longitude",
    "radius_m",
    "location",
    "window_from_utc",
    "window_from_local",
    "window_to_utc",
    "window_to_local",
    *SOURCE_COLUMNS,
    "row_type",
    "verdict",
    "basis",
    "reports_in_window",
    "first_utc",
    "first_local",
    "last_utc",
    "last_local",
    "duration_minutes",
    "report_count",
    "distance_m",
    "accuracy_m",
    "first_line",
    "last_line",
    "detail",
    "window_first_report_utc",
    "window_first_report_local",
    "window_last_report_utc",
    "window_last_report_local",
    "longest_unobserved_minutes",
    "best_inside_accuracy_m",
    "verdict_as_reported",
    "basis_as_reported",
)
# The columns after "detail": filled in window verdict rows only.
CASE_PLACE_COVERAGE_COLUMN_COUNT = 8
CASE_PLACE_ROW_VISIT = "visit"
CASE_PLACE_ROW_POSSIBLE = "possible report"
CASE_PLACE_ROW_UNRATED = "report without accuracy near the radius"
CASE_PLACE_ROW_CLOSEST = "closest approach"
CASE_PLACE_ROW_VERDICT = "window verdict"
CASE_PLACE_ROW_NOT_LOCATED = "not located"


def write_case_places_csv(
    path: Path,
    results: Sequence[CasePlaceResult],
    source_labels: Mapping[int, str],
    display_tz: tzinfo | None,
) -> int:
    """One row per place x source x visit, every possible and unrated report, the closest
    approach and the window verdict; distance_m is the closest distance of the row's
    report(s) to the place, accuracy_m the report's accuracy (visit: the best reported
    accuracy of its records). A verdict row also names the first and last report of the
    window, the longest span of the window without a report, the best accuracy among its
    reports inside the radius and, when the radii as reported give another verdict or basis,
    that verdict and basis. A place without coordinates gets one "not located" row."""

    def report_cells(entry: ReportDistance | None) -> list[object]:
        if entry is None:
            return [""] * 10
        point = entry.point
        return [
            *_moment_cells(point.timestamp_utc, display_tz),
            *_moment_cells(point.timestamp_utc, display_tz),
            "0.0",
            1,
            f"{entry.distance_m:.1f}",
            f"{point.accuracy_radius_m:.1f}" if point.accuracy_known else "",
            point.line_number,
            point.line_number,
        ]

    def neighbour_text(name: str, entry: ReportDistance | None) -> str:
        if entry is None:
            return f"no report {name} the window"
        return (
            f"nearest report {name} the window: "
            f"{local_iso_with_offset(entry.point.timestamp_utc, display_tz)}, "
            f"{entry.distance_m:.1f} m from the place, record {entry.point.line_number}"
        )

    no_coverage = [""] * CASE_PLACE_COVERAGE_COLUMN_COUNT
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CASE_PLACES_CSV_COLUMNS)
        for result in results:
            place = result.place
            window = place.window
            place_cells: list[object] = [
                place.identifier,
                place.label,
                # 6 decimals (~0.1 m) like every coordinate of the analysis CSVs; the exact
                # entry is in metadata.json and analysis.json.
                "" if place.latitude is None else f"{place.latitude:.6f}",
                "" if place.longitude is None else f"{place.longitude:.6f}",
                f"{place.radius_m:g}",
                place.location,
                *_moment_cells(None if window is None else window.from_utc, display_tz),
                *_moment_cells(None if window is None else window.to_utc, display_tz),
            ]
            if not place.located:
                blank = [""] * (
                    len(CASE_PLACES_CSV_COLUMNS)
                    - len(place_cells)
                    - 4
                    - CASE_PLACE_COVERAGE_COLUMN_COUNT
                )
                writer.writerow(
                    [
                        *place_cells,
                        "",
                        "",
                        CASE_PLACE_ROW_NOT_LOCATED,
                        *blank,
                        place.address,
                        *no_coverage,
                    ]
                )
                written += 1
                continue
            for check in result.checks:
                head = [*place_cells, check.source_id, source_labels.get(check.source_id, "")]
                for visit in check.visits:
                    writer.writerow(
                        [
                            *head,
                            CASE_PLACE_ROW_VISIT,
                            "",
                            "",
                            "",
                            *_moment_cells(visit.first_utc, display_tz),
                            *_moment_cells(visit.last_utc, display_tz),
                            f"{visit.duration_minutes:.1f}",
                            visit.report_count,
                            f"{visit.closest_distance_m:.1f}",
                            "" if visit.best_accuracy_m is None else f"{visit.best_accuracy_m:.1f}",
                            visit.first_line,
                            visit.last_line,
                            "",
                            *no_coverage,
                        ]
                    )
                for row_type, entries in (
                    (CASE_PLACE_ROW_POSSIBLE, check.possible_reports),
                    (CASE_PLACE_ROW_UNRATED, check.unrated_reports),
                ):
                    for entry in entries:
                        # A possible report inside the radius was left out for its method;
                        # one whose own circle misses the radius is possible only because its
                        # method is left out (it is judged by the wider assumed accuracy).
                        detail = ""
                        if entry.distance_m <= place.radius_m:
                            detail = f"{entry.point.positioning_method} record inside the radius"
                        elif (
                            entry.point.accuracy_known
                            and entry.distance_m - entry.point.uncertainty_radius_m > place.radius_m
                        ):
                            detail = f"{entry.point.positioning_method} record near the radius"
                        writer.writerow(
                            [
                                *head,
                                row_type,
                                "",
                                "",
                                "",
                                *report_cells(entry),
                                detail,
                                *no_coverage,
                            ]
                        )
                writer.writerow(
                    [
                        *head,
                        CASE_PLACE_ROW_CLOSEST,
                        "",
                        "",
                        "",
                        *report_cells(check.closest),
                        "" if check.closest is not None else "the source has no reports",
                        *no_coverage,
                    ]
                )
                written += len(check.visits) + len(check.possible_reports)
                written += len(check.unrated_reports) + 1
                if check.window is None:
                    continue
                detail = "closest report inside the window"
                if check.window.reports_in_window == 0:
                    detail = "; ".join(
                        (
                            neighbour_text("before", check.window.before),
                            neighbour_text("after", check.window.after),
                        )
                    )
                writer.writerow(
                    [
                        *head,
                        CASE_PLACE_ROW_VERDICT,
                        check.window.verdict,
                        check.window.basis or "",
                        check.window.reports_in_window,
                        *report_cells(check.window.closest),
                        detail,
                        *_moment_cells(check.window.first_report_utc, display_tz),
                        *_moment_cells(check.window.last_report_utc, display_tz),
                        ""
                        if check.window.longest_unobserved_minutes is None
                        else f"{check.window.longest_unobserved_minutes:.1f}",
                        ""
                        if check.window.best_inside_accuracy_m is None
                        else f"{check.window.best_inside_accuracy_m:.1f}",
                        check.window.verdict_as_reported or "",
                        check.window.basis_as_reported or "",
                    ]
                )
                written += 1
    return written


PRESENCE_MATRIX_CSV_COLUMNS = (
    "row_id",
    "place_kind",
    "place_label",
    "reference_id",
    "latitude",
    "longitude",
    "radius_m",
    "simultaneous",
    *SOURCE_COLUMNS,
    "cell_visit_count",
    "cell_dwell_minutes",
    "first_utc",
    "first_local",
    "last_utc",
    "last_local",
    "duration_minutes",
    "report_count",
    "first_line",
    "last_line",
    "stay_id",
)


def write_presence_matrix_csv(
    path: Path,
    matrix: PresenceMatrix,
    source_labels: Mapping[int, str],
    display_tz: tzinfo | None,
) -> int:
    """Long form, every row of the matrix: one line per place x source x visit, and one line
    with empty visit columns for a source without a visit at the place. stay_id is empty for
    the visits of a case place, which are runs of reports inside its radius, not stays."""
    written = 0
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(PRESENCE_MATRIX_CSV_COLUMNS)
        for row in matrix.rows:
            for cell in row.cells:
                head = [
                    row.identifier,
                    row.kind,
                    row.label,
                    row.reference_id,
                    f"{row.latitude:.6f}",
                    f"{row.longitude:.6f}",
                    f"{row.radius_m:g}",
                    "true" if row.simultaneous else "false",
                    cell.source_id,
                    source_labels.get(cell.source_id, ""),
                    cell.visit_count,
                    f"{cell.dwell_minutes:.1f}",
                ]
                if not cell.visits:
                    writer.writerow([*head, *[""] * 9])
                    written += 1
                for visit in cell.visits:
                    writer.writerow(
                        [
                            *head,
                            visit.first_utc.isoformat(),
                            local_iso_with_offset(visit.first_utc, display_tz),
                            visit.last_utc.isoformat(),
                            local_iso_with_offset(visit.last_utc, display_tz),
                            f"{visit.duration_minutes:.1f}",
                            visit.report_count,
                            visit.first_line,
                            visit.last_line,
                            "" if visit.stay_identifier is None else visit.stay_identifier,
                        ]
                    )
                    written += 1
    return written
