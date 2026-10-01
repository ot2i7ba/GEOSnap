# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Position check of case places: which reports of each source lie at a place from the case
file (visits), which could (possible visits), how close the source ever came, and what its
reports say about a time window (verdict). A verdict describes reports, never a person."""

from __future__ import annotations

import math
import operator
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from geosnap.analysis.geometry import EARTH_RADIUS_M
from geosnap.analysis.report import local_stamp, moment_fields
from geosnap.extraction.models import GeoPoint
from geosnap.timezones import offset_minutes

CASE_PLACE_DEFAULT_RADIUS_M = 100
CASE_PLACE_MIN_RADIUS_M = 10
CASE_PLACE_MAX_RADIUS_M = 5000

# Where the coordinates of a case place come from.
LOCATION_ENTERED = "entered"
LOCATION_GEOCODED = "geocoded, not verified by the examiner"
LOCATION_NOT_LOCATED = "not located"

VERDICT_PRESENT = "present"
VERDICT_POSSIBLY_PRESENT = "possibly present"
VERDICT_ELSEWHERE = "elsewhere"
VERDICT_NO_REPORTS = "no reports in window"
WINDOW_VERDICTS = (
    VERDICT_PRESENT,
    VERDICT_POSSIBLY_PRESENT,
    VERDICT_ELSEWHERE,
    VERDICT_NO_REPORTS,
)
BASIS_ACCURACY_CIRCLE = "accuracy circle reaches the radius"
BASIS_INSIDE_ACCURACY_EXCEEDS_RADIUS = (
    "inside the radius, but every such record has an accuracy circle larger than the radius"
)
BASIS_INSIDE_ACCURACY_NOT_REPORTED = (
    "inside the radius, but every such record has an accuracy circle larger than the radius "
    "or no accuracy value (assumed {margin_m} m)"
)
BASIS_ACCURACY_NOT_REPORTED = "accuracy not reported; within the assumed {margin_m} m"
BASIS_EXCLUDED_METHOD_INSIDE = "only {methods} records inside"
BASIS_EXCLUDED_METHOD_NEAR = "{methods} records near the radius; within the assumed {margin_m} m"

# analysis.json lists at most this many possible and unrated reports per place and source
# (the counts are complete; case_places_<stamp>.csv lists every one).
LISTED_REPORTS_LIMIT = 100

# Grid cells of this many degrees index a source's reports, so a place only looks at the
# reports near it; the closest approach stays exact (cells are visited by a lower bound).
_CELL_DEGREES = 0.02
# Reports with a larger accuracy are checked against every place instead of widening the
# reach of their cell.
_WIDE_ACCURACY_M = 5000.0
# Subtracted from a cell's lower-bound distance to absorb floating-point rounding.
_BOUND_SLACK_M = 1.0


@dataclass(frozen=True, slots=True)
class CasePlaceWindow:
    """Time window of interest, both bounds inclusive."""

    from_utc: datetime
    to_utc: datetime


@dataclass(frozen=True, slots=True)
class CasePlace:
    """A location from the case file. Latitude and longitude are None while an address
    could not be located; such a place is documented but not checked."""

    identifier: int
    label: str
    latitude: float | None
    longitude: float | None
    radius_m: float = CASE_PLACE_DEFAULT_RADIUS_M
    window: CasePlaceWindow | None = None
    address: str = ""
    note: str = ""
    location: str = LOCATION_ENTERED
    # Display name of the geocoding answer (location == LOCATION_GEOCODED).
    geocoded_display_name: str | None = None
    # Row of the loaded CSV file (header = 1), None for a place typed into the dialog.
    file_row: int | None = None

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True, slots=True)
class ReportDistance:
    """One report and its great-circle distance to the centre of the case place."""

    point: GeoPoint
    distance_m: float


@dataclass(frozen=True, slots=True)
class PlaceVisit:
    """A maximal run of consecutive reports inside the radius without a long silence."""

    first_utc: datetime
    last_utc: datetime
    report_count: int
    closest_distance_m: float
    first_line: int
    last_line: int
    # Smallest reported accuracy among the visit's records; None when none reports one.
    best_accuracy_m: float | None = None

    @property
    def duration_minutes(self) -> float:
        return (self.last_utc - self.first_utc).total_seconds() / 60


@dataclass(frozen=True, slots=True)
class WindowCheck:
    """What the reports of one source say about the window of a case place.

    ``closest`` is the closest report inside the window; ``before`` and ``after`` are the
    neighbouring reports, given only when the window holds no report. A verdict covers only
    the moments of the reports: ``longest_unobserved_minutes`` is the longest span inside
    the window without any report of the source, counted also from the window start to the
    first report and from the last report to the window end (a silent window: its length).
    ``best_inside_accuracy_m`` is the smallest reported accuracy among the window's reports
    inside the radius (None when there is none or none reports an accuracy).
    ``verdict_as_reported`` and ``basis_as_reported`` give the verdict with the radii as
    reported; both are None when it is the same verdict with the same basis."""

    verdict: str
    basis: str | None
    reports_in_window: int
    reports_inside_radius: int
    possible_reports: int
    unrated_reports: int
    closest: ReportDistance | None
    before: ReportDistance | None
    after: ReportDistance | None
    first_report_utc: datetime | None = None
    last_report_utc: datetime | None = None
    longest_unobserved_minutes: float | None = None
    best_inside_accuracy_m: float | None = None
    verdict_as_reported: str | None = None
    basis_as_reported: str | None = None


@dataclass(frozen=True, slots=True)
class SourcePlaceCheck:
    """One case place against one source.

    ``possible_reports``: outside the radius, but the uncertainty radius reaches it, or
    determined by an excluded positioning method and inside it or within the radius plus
    the larger of the assumed margin and its uncertainty radius.
    ``unrated_reports``: outside the radius without an accuracy value, within the assumed
    margin; they are neither visits nor possible visits, but they rule out "elsewhere"."""

    place_id: int
    source_id: int
    visits: list[PlaceVisit]
    possible_reports: list[ReportDistance]
    unrated_reports: list[ReportDistance]
    closest: ReportDistance | None
    window: WindowCheck | None

    @property
    def reports_inside_radius(self) -> int:
        return sum(visit.report_count for visit in self.visits)


@dataclass(frozen=True, slots=True)
class CasePlaceResult:
    place: CasePlace
    # One check per source in source order; empty for a place without coordinates.
    checks: list[SourcePlaceCheck] = field(default_factory=list)


def check_case_places(
    places: Sequence[CasePlace],
    points_by_source: Mapping[int, Sequence[GeoPoint]],
    gap_threshold_minutes: int,
    unknown_accuracy_margin_m: int,
    excluded_positioning_methods: Collection[str] = (),
) -> list[CasePlaceResult]:
    """Every located case place against every source, in place and source order.

    Every point takes part, including those excluded from the per-source analysis by
    accuracy or positioning method. Reports are judged by their uncertainty radius
    (GeoPoint.uncertainty_radius_m). A report without an accuracy value counts as clear of
    the radius only beyond radius + unknown_accuracy_margin_m. A report of an excluded
    positioning method (a cell radius is often a sector size, not an accuracy) never makes
    a visit or "present": inside the radius it is a possible visit, and outside it counts as
    clear only beyond radius + max(unknown_accuracy_margin_m, its uncertainty radius).
    Input lists must be chronological.
    """
    located = [place for place in places if place.located]
    indexes = (
        {
            source_id: _SourceIndex(points_by_source[source_id], excluded_positioning_methods)
            for source_id in points_by_source
        }
        if located
        else {}
    )
    gap_seconds = gap_threshold_minutes * 60.0
    margin_m = float(unknown_accuracy_margin_m)
    return [
        CasePlaceResult(
            place,
            [
                _check_source(place, source_id, indexes[source_id], gap_seconds, margin_m)
                for source_id in sorted(indexes)
            ]
            if place.located
            else [],
        )
        for place in places
    ]


class _Cell:
    __slots__ = (
        "indices",
        "latitude_min",
        "latitude_max",
        "longitude_min",
        "longitude_max",
        "reach_m",
        "has_unknown_accuracy",
        "has_excluded_method",
    )

    def __init__(self, latitude: float, longitude: float) -> None:
        self.indices: list[int] = []
        self.latitude_min = self.latitude_max = latitude
        self.longitude_min = self.longitude_max = longitude
        # Largest known uncertainty radius of the cell's reports (wide ones excepted).
        self.reach_m = 0.0
        self.has_unknown_accuracy = False
        self.has_excluded_method = False


class _SourceIndex:
    """A source's reports as plain float lists plus a grid of cells holding report indices
    in chronological order; built once and shared by all case places."""

    def __init__(
        self, points: Sequence[GeoPoint], excluded_positioning_methods: Collection[str]
    ) -> None:
        self.points = points
        # The indices of the reports of an excluded positioning method.
        self.method_excluded = (
            {
                index
                for index, point in enumerate(points)
                if point.positioning_method in excluded_positioning_methods
            }
            if excluded_positioning_methods
            else set()
        )
        self.seconds = [point.timestamp_utc.timestamp() for point in points]
        # silences[i]: seconds from report i to report i + 1 (window coverage).
        self.silences = list(map(operator.sub, self.seconds[1:], self.seconds))
        self.latitudes_rad = [math.radians(point.latitude) for point in points]
        self.longitudes_rad = [math.radians(point.longitude) for point in points]
        self.latitude_cosines = [math.cos(latitude) for latitude in self.latitudes_rad]
        # Reported radii (shown) and uncertainty radii (judged by).
        self.accuracies = [point.accuracy_radius_m for point in points]
        self.uncertainties = [point.uncertainty_radius_m for point in points]
        self.accuracy_known = [point.accuracy_known for point in points]
        self.wide_indices: list[int] = []
        cells: dict[tuple[int, int], _Cell] = {}
        for index, point in enumerate(points):
            latitude = point.latitude
            longitude = point.longitude
            key = (math.floor(latitude / _CELL_DEGREES), math.floor(longitude / _CELL_DEGREES))
            cell = cells.get(key)
            if cell is None:
                cell = cells[key] = _Cell(latitude, longitude)
            else:
                if latitude < cell.latitude_min:
                    cell.latitude_min = latitude
                elif latitude > cell.latitude_max:
                    cell.latitude_max = latitude
                if longitude < cell.longitude_min:
                    cell.longitude_min = longitude
                elif longitude > cell.longitude_max:
                    cell.longitude_max = longitude
            cell.indices.append(index)
            if index in self.method_excluded:
                cell.has_excluded_method = True
            if not point.accuracy_known:
                cell.has_unknown_accuracy = True
            elif self.uncertainties[index] > _WIDE_ACCURACY_M:
                self.wide_indices.append(index)
            elif self.uncertainties[index] > cell.reach_m:
                cell.reach_m = self.uncertainties[index]
        self.cells = list(cells.values())
        # Cell bounds as plain lists: the lower bound of every cell is computed per place.
        self.cell_latitude_min = [math.radians(cell.latitude_min) for cell in self.cells]
        self.cell_latitude_max = [math.radians(cell.latitude_max) for cell in self.cells]
        self.cell_longitude_min = [math.radians(cell.longitude_min) for cell in self.cells]
        self.cell_longitude_max = [math.radians(cell.longitude_max) for cell in self.cells]
        self.cell_smallest_cosine = [
            min(math.cos(low), math.cos(high))
            for low, high in zip(self.cell_latitude_min, self.cell_latitude_max, strict=True)
        ]

    def distance_m(
        self, index: int, place_latitude_rad: float, place_longitude_rad: float
    ) -> float:
        """Haversine distance; the squared sine makes it safe across the antimeridian."""
        half_latitude = math.sin((self.latitudes_rad[index] - place_latitude_rad) / 2)
        half_longitude = math.sin((self.longitudes_rad[index] - place_longitude_rad) / 2)
        chord = (
            half_latitude * half_latitude
            + math.cos(place_latitude_rad)
            * self.latitude_cosines[index]
            * half_longitude
            * half_longitude
        )
        return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, chord)))


def _chord_of_distance(distance_m: float) -> float:
    """The haversine term of a distance (monotonic, so chords compare like distances)."""
    half_angle = min(distance_m / (2 * EARTH_RADIUS_M), math.pi / 2)
    return math.sin(half_angle) ** 2


def _distance_of_chord(chord: float) -> float:
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, chord)))


class _CellsByBound:
    """The cells of a source ordered by a distance no report of the cell can undercut.

    The haversine terms are bounded separately: nearest latitude, nearest longitude (the
    short way round) and the smallest cosine of the cell. Cells within ``near_limit_m`` are
    ordered at once, the far ones only when a caller walks past the near ones.
    """

    def __init__(
        self, index: _SourceIndex, latitude_rad: float, longitude_rad: float, near_limit_m: float
    ) -> None:
        place_cosine = math.cos(latitude_rad)
        full_turn = 2 * math.pi
        sine = math.sin
        chords = []
        for latitude_min, latitude_max, longitude_min, longitude_max, smallest_cosine in zip(
            index.cell_latitude_min,
            index.cell_latitude_max,
            index.cell_longitude_min,
            index.cell_longitude_max,
            index.cell_smallest_cosine,
            strict=True,
        ):
            latitude_gap = max(0.0, latitude_min - latitude_rad, latitude_rad - latitude_max)
            longitude_gap = max(longitude_min - longitude_rad, longitude_rad - longitude_max)
            if longitude_gap <= 0.0:
                chords.append(sine(latitude_gap / 2) ** 2)
                continue
            the_other_way = full_turn - max(
                longitude_max - longitude_rad, longitude_rad - longitude_min
            )
            chords.append(
                sine(latitude_gap / 2) ** 2
                + place_cosine * smallest_cosine * sine(min(longitude_gap, the_other_way) / 2) ** 2
            )
        near_chord = _chord_of_distance(near_limit_m + _BOUND_SLACK_M)
        self._near = sorted(
            (chord, position) for position, chord in enumerate(chords) if chord <= near_chord
        )
        self._chords = chords
        self._near_chord = near_chord
        self._far: list[tuple[float, int]] | None = None

    def __iter__(self) -> Iterator[tuple[float, int]]:
        """(lower bound in metres, cell position), nearest first."""
        for chord, position in self._near:
            yield _distance_of_chord(chord) - _BOUND_SLACK_M, position
        if self._far is None:
            self._far = sorted(
                (chord, position)
                for position, chord in enumerate(self._chords)
                if chord > self._near_chord
            )
        for chord, position in self._far:
            yield _distance_of_chord(chord) - _BOUND_SLACK_M, position


def _check_source(
    place: CasePlace, source_id: int, index: _SourceIndex, gap_seconds: float, margin_m: float
) -> SourcePlaceCheck:
    assert place.latitude is not None and place.longitude is not None
    radius_m = float(place.radius_m)
    place_latitude_rad = math.radians(place.latitude)
    place_longitude_rad = math.radians(place.longitude)
    widest_reach_m = max(_WIDE_ACCURACY_M, margin_m)
    cells_by_bound = _CellsByBound(
        index, place_latitude_rad, place_longitude_rad, radius_m + widest_reach_m
    )

    # Reports that matter for the place (inside, possible, unrated), by report index.
    near: dict[int, float] = {}
    closest_index = -1
    closest_m = math.inf
    uncertainties = index.uncertainties
    accuracy_known = index.accuracy_known
    method_excluded = index.method_excluded

    def allowance_m(report_index: int) -> float:
        """How far outside the radius a report still counts as near."""
        if not accuracy_known[report_index]:
            return margin_m
        if report_index in method_excluded:
            return max(margin_m, uncertainties[report_index])
        return uncertainties[report_index]

    for bound_m, position in cells_by_bound:
        cell = index.cells[position]
        reach_m = max(
            cell.reach_m,
            margin_m if cell.has_unknown_accuracy or cell.has_excluded_method else 0.0,
        )
        if bound_m > radius_m + reach_m and bound_m >= closest_m:
            if bound_m > radius_m + widest_reach_m:
                break
            continue
        for report_index in cell.indices:
            distance_m = index.distance_m(report_index, place_latitude_rad, place_longitude_rad)
            if distance_m < closest_m:
                closest_m = distance_m
                closest_index = report_index
            if distance_m - allowance_m(report_index) <= radius_m:
                near[report_index] = distance_m
    for report_index in index.wide_indices:
        if report_index not in near:
            distance_m = index.distance_m(report_index, place_latitude_rad, place_longitude_rad)
            if distance_m - allowance_m(report_index) <= radius_m:
                near[report_index] = distance_m

    near_indices = sorted(near)
    inside = [
        entry for entry in near_indices if near[entry] <= radius_m and entry not in method_excluded
    ]
    possible = [
        entry
        for entry in near_indices
        if (near[entry] > radius_m and accuracy_known[entry])
        or (near[entry] <= radius_m and entry in method_excluded)
    ]
    # The possible reports as reported: the reported radius instead of the uncertainty
    # radius (scale 1: all).
    possible_as_reported = [
        entry
        for entry in possible
        if near[entry] - index.accuracies[entry] <= radius_m
        or (entry in method_excluded and near[entry] - margin_m <= radius_m)
    ]
    # Possible reports of an excluded method outside the radius that only the assumed margin
    # brings near, judged and as reported.
    excluded_near = [
        entry
        for entry in possible
        if near[entry] > radius_m
        and entry in method_excluded
        and near[entry] - uncertainties[entry] > radius_m
    ]
    excluded_near_as_reported = [
        entry
        for entry in possible_as_reported
        if near[entry] > radius_m
        and entry in method_excluded
        and near[entry] - index.accuracies[entry] > radius_m
    ]
    unrated = [
        entry for entry in near_indices if near[entry] > radius_m and not accuracy_known[entry]
    ]

    def report_distance(report_index: int, distance_m: float | None = None) -> ReportDistance:
        if distance_m is None:
            distance_m = index.distance_m(report_index, place_latitude_rad, place_longitude_rad)
        return ReportDistance(index.points[report_index], distance_m)

    window_check = None
    if place.window is not None:
        window_check = _check_window(
            place.window,
            index,
            cells_by_bound,
            (inside, possible, unrated),
            possible_as_reported,
            [entry for entry in possible if near[entry] <= radius_m],
            (excluded_near, excluded_near_as_reported),
            radius_m,
            margin_m,
            closest_index,
            report_distance,
        )
    return SourcePlaceCheck(
        place_id=place.identifier,
        source_id=source_id,
        visits=_visits(index, inside, near, gap_seconds),
        possible_reports=[report_distance(entry, near[entry]) for entry in possible],
        unrated_reports=[report_distance(entry, near[entry]) for entry in unrated],
        closest=None if closest_index < 0 else report_distance(closest_index, closest_m),
        window=window_check,
    )


def _visits(
    index: _SourceIndex, inside: Sequence[int], distances: Mapping[int, float], gap_seconds: float
) -> list[PlaceVisit]:
    """Runs of consecutive report indices; a report outside the radius or a silence longer
    than the gap threshold ends the run."""
    visits: list[PlaceVisit] = []
    run: list[int] = []

    def close_run() -> None:
        first, last = index.points[run[0]], index.points[run[-1]]
        visits.append(
            PlaceVisit(
                first_utc=first.timestamp_utc,
                last_utc=last.timestamp_utc,
                report_count=len(run),
                closest_distance_m=min(distances[entry] for entry in run),
                first_line=first.line_number,
                last_line=last.line_number,
                best_accuracy_m=_best_known_accuracy(index, run),
            )
        )

    for report_index in inside:
        if run and (
            report_index != run[-1] + 1
            or index.seconds[report_index] - index.seconds[run[-1]] > gap_seconds
        ):
            close_run()
            run = []
        run.append(report_index)
    if run:
        close_run()
    return visits


def _best_known_accuracy(index: _SourceIndex, report_indices: Sequence[int]) -> float | None:
    return min(
        (index.accuracies[entry] for entry in report_indices if index.accuracy_known[entry]),
        default=None,
    )


def _longest_unobserved_seconds(
    seconds: Sequence[float],
    silences: Sequence[float],
    first: int,
    end: int,
    from_seconds: float,
    to_seconds: float,
) -> float:
    """Longest span of the window without a report: before the first, between neighbours,
    after the last; the whole window when it holds no report."""
    if end == first:
        return to_seconds - from_seconds
    between = max(silences[first : end - 1], default=0.0)
    return max(seconds[first] - from_seconds, between, to_seconds - seconds[end - 1])


def _count_between(indices: Sequence[int], first: int, end: int) -> int:
    return bisect_left(indices, end) - bisect_left(indices, first)


def _check_window(
    window: CasePlaceWindow,
    index: _SourceIndex,
    cells_by_bound: _CellsByBound,
    near_by_class: tuple[Sequence[int], Sequence[int], Sequence[int]],
    possible_as_reported: Sequence[int],
    excluded_inside: Sequence[int],
    excluded_near_pair: tuple[Sequence[int], Sequence[int]],
    radius_m: float,
    margin_m: float,
    closest_index: int,
    report_distance: Callable[[int], ReportDistance],
) -> WindowCheck:
    # Report indices [first, end) lie inside the inclusive window.
    first = bisect_left(index.seconds, window.from_utc.timestamp())
    end = bisect_right(index.seconds, window.to_utc.timestamp())
    inside, possible, unrated = (_count_between(indices, first, end) for indices in near_by_class)
    inside_indices = near_by_class[0]
    inside_in_window = inside_indices[
        bisect_left(inside_indices, first) : bisect_left(inside_indices, end)
    ]
    best_inside_accuracy_m = _best_known_accuracy(index, inside_in_window)

    def methods_in_window(report_indices: Sequence[int]) -> list[str]:
        return sorted(
            {
                str(index.points[entry].positioning_method)
                for entry in report_indices[
                    bisect_left(report_indices, first) : bisect_left(report_indices, end)
                ]
            }
        )

    excluded_inside_methods = methods_in_window(excluded_inside)
    excluded_near, excluded_near_as_reported = excluded_near_pair
    verdict, basis = _window_verdict(
        inside_in_window,
        possible - _count_between(excluded_near, first, end),
        unrated,
        end - first,
        index.uncertainties,
        index,
        radius_m,
        margin_m,
        excluded_inside_methods,
        methods_in_window(excluded_near),
    )
    as_reported = _window_verdict(
        inside_in_window,
        _count_between(possible_as_reported, first, end)
        - _count_between(excluded_near_as_reported, first, end),
        unrated,
        end - first,
        index.accuracies,
        index,
        radius_m,
        margin_m,
        excluded_inside_methods,
        methods_in_window(excluded_near_as_reported),
    )
    verdict_as_reported, basis_as_reported = (
        (None, None) if as_reported == (verdict, basis) else as_reported
    )

    closest = None
    closest_m = math.inf
    if first <= closest_index < end:
        # The overall closest report lies in the window: nothing in it can be closer.
        closest = report_distance(closest_index)
        closest_m = -math.inf
    for bound_m, position in cells_by_bound:
        if bound_m >= closest_m or end == first:
            break
        indices = index.cells[position].indices
        for report_index in indices[bisect_left(indices, first) : bisect_left(indices, end)]:
            candidate = report_distance(report_index)
            if candidate.distance_m < closest_m:
                closest, closest_m = candidate, candidate.distance_m
    silent = end == first
    return WindowCheck(
        verdict=verdict,
        basis=basis,
        reports_in_window=end - first,
        reports_inside_radius=inside,
        possible_reports=possible,
        unrated_reports=unrated,
        closest=closest,
        before=report_distance(first - 1) if silent and first > 0 else None,
        after=report_distance(end) if silent and end < len(index.points) else None,
        first_report_utc=None if silent else index.points[first].timestamp_utc,
        last_report_utc=None if silent else index.points[end - 1].timestamp_utc,
        longest_unobserved_minutes=_longest_unobserved_seconds(
            index.seconds,
            index.silences,
            first,
            end,
            window.from_utc.timestamp(),
            window.to_utc.timestamp(),
        )
        / 60,
        best_inside_accuracy_m=best_inside_accuracy_m,
        verdict_as_reported=verdict_as_reported,
        basis_as_reported=basis_as_reported,
    )


def _window_verdict(
    inside_in_window: Sequence[int],
    possible: int,
    unrated: int,
    reports_in_window: int,
    radii: Sequence[float],
    index: _SourceIndex,
    radius_m: float,
    margin_m: float,
    excluded_inside_methods: Sequence[str],
    excluded_near_methods: Sequence[str],
) -> tuple[str, str | None]:
    """Verdict and basis from the window's reports inside the radius, the counts of its
    possible reports whose circle reaches the radius and of its unrated reports, the radii
    the reports are judged by and the excluded positioning methods of its possible reports
    inside the radius and of those only the assumed margin brings near."""
    if inside_in_window:
        # A record without accuracy counts with the assumed margin, as it does for elsewhere.
        if all(
            (radii[entry] if index.accuracy_known[entry] else margin_m) > radius_m
            for entry in inside_in_window
        ):
            if all(index.accuracy_known[entry] for entry in inside_in_window):
                return VERDICT_PRESENT, BASIS_INSIDE_ACCURACY_EXCEEDS_RADIUS
            return VERDICT_PRESENT, BASIS_INSIDE_ACCURACY_NOT_REPORTED.format(
                margin_m=f"{margin_m:g}"
            )
        return VERDICT_PRESENT, None
    if excluded_inside_methods:
        return VERDICT_POSSIBLY_PRESENT, BASIS_EXCLUDED_METHOD_INSIDE.format(
            methods=" and ".join(excluded_inside_methods)
        )
    if possible:
        return VERDICT_POSSIBLY_PRESENT, BASIS_ACCURACY_CIRCLE
    if excluded_near_methods:
        return VERDICT_POSSIBLY_PRESENT, BASIS_EXCLUDED_METHOD_NEAR.format(
            methods=" and ".join(excluded_near_methods), margin_m=f"{margin_m:g}"
        )
    if unrated:
        return VERDICT_POSSIBLY_PRESENT, BASIS_ACCURACY_NOT_REPORTED.format(
            margin_m=f"{margin_m:g}"
        )
    if reports_in_window:
        return VERDICT_ELSEWHERE, None
    return VERDICT_NO_REPORTS, None


# --- documents (analysis.json, map payload) -----------------------------------------------


def report_distance_to_document(
    entry: ReportDistance | None, display_tz: tzinfo | None
) -> dict[str, object] | None:
    if entry is None:
        return None
    point = entry.point
    return {
        "line": point.line_number,
        "utc": point.timestamp_utc.isoformat(),
        "local": local_stamp(point.timestamp_utc, display_tz),
        "offset": offset_minutes(point.timestamp_utc, display_tz),
        "lat": point.latitude,
        "lon": point.longitude,
        "distance_m": round(entry.distance_m, 1),
        "accuracy_m": round(point.accuracy_radius_m, 1) if point.accuracy_known else None,
        "accuracy_known": point.accuracy_known,
        "positioning_method": point.positioning_method,
    }


def visit_to_document(visit: PlaceVisit, display_tz: tzinfo | None) -> dict[str, object]:
    return {
        **moment_fields("first", visit.first_utc, display_tz),
        **moment_fields("last", visit.last_utc, display_tz),
        "duration_minutes": round(visit.duration_minutes, 1),
        "report_count": visit.report_count,
        "closest_distance_m": round(visit.closest_distance_m, 1),
        "first_line": visit.first_line,
        "last_line": visit.last_line,
        "best_accuracy_m": None
        if visit.best_accuracy_m is None
        else round(visit.best_accuracy_m, 1),
    }


def window_to_document(
    window: CasePlaceWindow | None, display_tz: tzinfo | None
) -> dict[str, object] | None:
    if window is None:
        return None
    return {
        **moment_fields("from", window.from_utc, display_tz),
        **moment_fields("to", window.to_utc, display_tz),
    }


def case_place_to_document(place: CasePlace, display_tz: tzinfo | None) -> dict[str, object]:
    """The place as entered (metadata.json) and as the head of its analysis entry."""
    return {
        "id": place.identifier,
        "label": place.label,
        "lat": place.latitude,
        "lon": place.longitude,
        "radius_m": place.radius_m,
        "window": window_to_document(place.window, display_tz),
        "address": place.address,
        "note": place.note,
        "location": place.location,
        "geocoded_display_name": place.geocoded_display_name,
        "file_row": place.file_row,
    }


def _source_check_to_document(
    check: SourcePlaceCheck, display_tz: tzinfo | None
) -> dict[str, object]:
    window = check.window
    return {
        "source_id": check.source_id,
        "visit_count": len(check.visits),
        "reports_inside_radius": check.reports_inside_radius,
        "visits": [visit_to_document(visit, display_tz) for visit in check.visits],
        "possible_count": len(check.possible_reports),
        "possible_reports": [
            report_distance_to_document(entry, display_tz)
            for entry in check.possible_reports[:LISTED_REPORTS_LIMIT]
        ],
        "unrated_count": len(check.unrated_reports),
        "unrated_reports": [
            report_distance_to_document(entry, display_tz)
            for entry in check.unrated_reports[:LISTED_REPORTS_LIMIT]
        ],
        "closest": report_distance_to_document(check.closest, display_tz),
        "window": None
        if window is None
        else {
            "verdict": window.verdict,
            "basis": window.basis,
            "reports_in_window": window.reports_in_window,
            "reports_inside_radius": window.reports_inside_radius,
            "possible_reports": window.possible_reports,
            "unrated_reports": window.unrated_reports,
            "closest": report_distance_to_document(window.closest, display_tz),
            "before": report_distance_to_document(window.before, display_tz),
            "after": report_distance_to_document(window.after, display_tz),
            **moment_fields("first_report", window.first_report_utc, display_tz),
            **moment_fields("last_report", window.last_report_utc, display_tz),
            "longest_unobserved_minutes": None
            if window.longest_unobserved_minutes is None
            else round(window.longest_unobserved_minutes, 1),
            "best_inside_accuracy_m": None
            if window.best_inside_accuracy_m is None
            else round(window.best_inside_accuracy_m, 1),
            "verdict_as_reported": window.verdict_as_reported,
            "basis_as_reported": window.basis_as_reported,
        },
    }


def case_places_to_document(
    results: Sequence[CasePlaceResult],
    display_tz: tzinfo | None,
    gap_threshold_minutes: int,
    unknown_accuracy_margin_m: int,
) -> dict[str, object]:
    """The case_places block of analysis.json; local wall-clock times carry their offset
    (minutes) alongside, UTC decides."""
    return {
        "gap_threshold_minutes": gap_threshold_minutes,
        "unknown_accuracy_margin_m": unknown_accuracy_margin_m,
        "listed_reports_limit": LISTED_REPORTS_LIMIT,
        "places": [
            {
                **case_place_to_document(result.place, display_tz),
                "checks": [_source_check_to_document(check, display_tz) for check in result.checks],
            }
            for result in results
        ],
    }


# Per-check lists the map does not show (it shows their counts).
_LISTS_LEFT_OUT_OF_THE_MAP = ("possible_reports", "unrated_reports")


def case_places_map_document(document: Mapping[str, object] | None) -> dict[str, object] | None:
    """The case_places block for the map payload: as in analysis.json, without the lists of
    possible and unrated reports, which can be long and are complete in the CSV."""
    if document is None:
        return None
    places = document["places"]
    assert isinstance(places, list)
    return {
        **document,
        "places": [
            {
                **place,
                "checks": [
                    {
                        key: value
                        for key, value in check.items()
                        if key not in _LISTS_LEFT_OUT_OF_THE_MAP
                    }
                    for check in place["checks"]
                ],
            }
            for place in places
        ],
    }
