# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Presence matrix: places as rows, sources as columns, each cell the visits of one source
at one place. Rows of shared and stay places are measured by stays (analysis.stop_radius_m,
analysis.stop_min_minutes) whose centres are grouped within analysis.matrix_tolerance_m
(the shared places of the map keep the stop radius); rows of case places by the reports
inside the radius entered by the examiner. A cell describes the reports of a device, never
a person. The map recomputes the rows for another tolerance with the same algorithm
(template/map_matrix.js, proven equal in tests/test_map_matrix.py)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo

from geosnap.analysis.case_places import CasePlaceResult
from geosnap.analysis.encounters import (
    SharedPlace,
    group_stays_into_places,
    overlap_between_sources,
)
from geosnap.analysis.models import Stay
from geosnap.analysis.report import moment_fields

ROW_SHARED_PLACE = "shared place"
ROW_CASE_PLACE = "case place"
ROW_STAY_PLACE = "stay place"
# analysis.json, the report and the map list at most this many rows; the CSV lists all.
PRESENCE_MATRIX_ROW_CAP = 200


@dataclass(frozen=True, slots=True)
class PresenceVisit:
    """One stay (stay rows) or one run of reports inside the radius (case place rows)."""

    first_utc: datetime
    last_utc: datetime
    report_count: int
    first_line: int
    last_line: int
    # None for a visit of a case place, which is not a stay.
    stay_identifier: int | None = None

    @property
    def duration_minutes(self) -> float:
        return (self.last_utc - self.first_utc).total_seconds() / 60


@dataclass(frozen=True, slots=True)
class PresenceCell:
    source_id: int
    # Chronological.
    visits: tuple[PresenceVisit, ...]

    @property
    def visit_count(self) -> int:
        return len(self.visits)

    @property
    def dwell_minutes(self) -> float:
        return sum(visit.duration_minutes for visit in self.visits)

    @property
    def first_utc(self) -> datetime | None:
        return min((visit.first_utc for visit in self.visits), default=None)

    @property
    def last_utc(self) -> datetime | None:
        return max((visit.last_utc for visit in self.visits), default=None)


@dataclass(frozen=True, slots=True)
class PresenceRow:
    identifier: int
    kind: str
    label: str
    # Identifier of the shared place, the case place or the stay place behind the row.
    reference_id: int
    latitude: float
    longitude: float
    radius_m: float
    # One cell per source of the matrix, in its column order.
    cells: tuple[PresenceCell, ...]
    # Visits of different sources overlap in time.
    simultaneous: bool

    @property
    def dwell_minutes(self) -> float:
        return sum(cell.dwell_minutes for cell in self.cells)


@dataclass(frozen=True, slots=True)
class PresenceMatrix:
    source_ids: tuple[int, ...]
    rows: list[PresenceRow]
    # Stay centres within this distance formed one row (analysis.matrix_tolerance_m).
    tolerance_m: int


def build_presence_matrix(
    source_ids: Sequence[int],
    stays_by_source: Mapping[int, Sequence[Stay]],
    case_places: Sequence[CasePlaceResult],
    matrix_tolerance_m: int,
) -> PresenceMatrix:
    """Rows: the shared places and the places of one source only (the columns' stays grouped
    by encounters.group_stays_into_places within matrix_tolerance_m), each group by total
    dwell, the longest first, with the located case places in their entered order between
    them. Case places keep the radius entered by the examiner and are never merged."""
    columns = tuple(source_ids)
    column_stays = {source_id: stays_by_source.get(source_id, ()) for source_id in columns}
    shared_places, single_source_places = group_stays_into_places(column_stays, matrix_tolerance_m)
    stays = {
        (source_id, stay.identifier): stay
        for source_id, source_stays in column_stays.items()
        for stay in source_stays
    }

    def stay_place_cells(place: SharedPlace) -> tuple[PresenceCell, ...]:
        visits_by_source: dict[int, list[PresenceVisit]] = {source_id: [] for source_id in columns}
        for visit in place.visits:
            stay = stays[(visit.source_id, visit.stay_identifier)]
            visits_by_source.setdefault(visit.source_id, []).append(
                PresenceVisit(
                    stay.arrive_utc,
                    stay.leave_utc,
                    stay.point_count,
                    stay.first_line,
                    stay.last_line,
                    stay.identifier,
                )
            )
        return _cells(columns, visits_by_source)

    def stay_place_rows(places: Sequence[SharedPlace], kind: str, noun: str) -> list[PresenceRow]:
        rows = [
            _row(
                kind,
                f"{noun} {place.identifier}",
                place.identifier,
                place.latitude,
                place.longitude,
                float(matrix_tolerance_m),
                stay_place_cells(place),
            )
            for place in places
        ]
        return sorted(rows, key=lambda row: (-row.dwell_minutes, row.reference_id))

    case_place_rows: list[PresenceRow] = []
    for result in case_places:
        place = result.place
        if place.latitude is None or place.longitude is None:
            continue
        visits_by_source = {
            check.source_id: [
                PresenceVisit(
                    visit.first_utc,
                    visit.last_utc,
                    visit.report_count,
                    visit.first_line,
                    visit.last_line,
                )
                for visit in check.visits
            ]
            for check in result.checks
        }
        case_place_rows.append(
            _row(
                ROW_CASE_PLACE,
                place.label,
                place.identifier,
                place.latitude,
                place.longitude,
                float(place.radius_m),
                _cells(columns, visits_by_source),
            )
        )

    ordered = [
        *stay_place_rows(shared_places, ROW_SHARED_PLACE, "Shared place"),
        *case_place_rows,
        *stay_place_rows(single_source_places, ROW_STAY_PLACE, "Stay place"),
    ]
    return PresenceMatrix(
        columns,
        [replace(row, identifier=number) for number, row in enumerate(ordered, start=1)],
        matrix_tolerance_m,
    )


def _cells(
    columns: Sequence[int], visits_by_source: Mapping[int, Sequence[PresenceVisit]]
) -> tuple[PresenceCell, ...]:
    return tuple(
        PresenceCell(
            source_id,
            tuple(sorted(visits_by_source.get(source_id, ()), key=lambda visit: visit.first_utc)),
        )
        for source_id in columns
    )


def _row(
    kind: str,
    label: str,
    reference_id: int,
    latitude: float,
    longitude: float,
    radius_m: float,
    cells: tuple[PresenceCell, ...],
) -> PresenceRow:
    """A row still without its number, which depends on the final order."""
    simultaneous = overlap_between_sources(
        (cell.source_id, visit.first_utc, visit.last_utc) for cell in cells for visit in cell.visits
    )
    return PresenceRow(
        0, kind, label, reference_id, latitude, longitude, radius_m, cells, simultaneous
    )


def listed_rows(
    matrix: PresenceMatrix, row_cap: int = PRESENCE_MATRIX_ROW_CAP
) -> list[PresenceRow]:
    """The rows within the cap, in matrix order. Case places are always listed; the shared
    and stay places fill what is left, so the ones with the shortest dwell drop out, stay
    places before shared places."""
    stay_rows_left = max(0, row_cap - sum(1 for row in matrix.rows if row.kind == ROW_CASE_PLACE))
    listed: list[PresenceRow] = []
    for row in matrix.rows:
        if row.kind != ROW_CASE_PLACE:
            if stay_rows_left == 0:
                continue
            stay_rows_left -= 1
        listed.append(row)
    return listed


def presence_matrix_to_document(
    matrix: PresenceMatrix, display_tz: tzinfo | None, row_cap: int = PRESENCE_MATRIX_ROW_CAP
) -> dict[str, object]:
    """JSON-ready matrix; local wall-clock times carry their offset (minutes) alongside."""

    def cell_document(cell: PresenceCell) -> dict[str, object]:
        return {
            "source_id": cell.source_id,
            "visit_count": cell.visit_count,
            "dwell_minutes": round(cell.dwell_minutes, 1),
            **moment_fields("first", cell.first_utc, display_tz),
            **moment_fields("last", cell.last_utc, display_tz),
            "visits": [
                {
                    **moment_fields("first", visit.first_utc, display_tz),
                    **moment_fields("last", visit.last_utc, display_tz),
                    "duration_minutes": round(visit.duration_minutes, 1),
                    "report_count": visit.report_count,
                    "first_line": visit.first_line,
                    "last_line": visit.last_line,
                    "stay_id": visit.stay_identifier,
                }
                for visit in cell.visits
            ],
        }

    rows = listed_rows(matrix, row_cap)
    return {
        "row_cap": row_cap,
        "rows_total": len(matrix.rows),
        "tolerance_m": matrix.tolerance_m,
        "rows_omitted": len(matrix.rows) - len(rows),
        "sources": list(matrix.source_ids),
        "rows": [
            {
                "id": row.identifier,
                "kind": row.kind,
                "label": row.label,
                "reference_id": row.reference_id,
                "lat": row.latitude,
                "lon": row.longitude,
                "radius_m": row.radius_m,
                "simultaneous": row.simultaneous,
                "dwell_minutes": round(row.dwell_minutes, 1),
                "cells": [cell_document(cell) for cell in row.cells],
            }
            for row in rows
        ],
    }
