# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Speed figures of a range of one source, with records the examiner excluded.

The map's Speed tool computes the same figures in the browser (map_speed.js); the map server
recomputes them here from the map's own payload before it records a calculation, so the
recorded figures never rest on the browser alone.

The headline is a lower bound resting on the two end records: the true path is at least as
long as the straight line between the true end positions, which is at least the recorded
straight line shortened by their two uncertainty radii (the reported radii at the confidence
level of the analysis); the true elapsed time is at most T plus the time uncertainty
(resolution and ambiguous local times of the ends). The stepwise minimum is reported apart
with the number of records its premise needs. Both minimums are also given with the radii
as reported.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from geosnap.analysis.accuracy import RAYLEIGH_68_TO_95
from geosnap.analysis.geometry import geodesic_distance_m
from geosnap.analysis.segments import minimum_step_m

# Written into every recorded speed range and the report's method section.
SPEED_RANGE_METHOD_TEXT = (
    "Distances are geodesics on the WGS84 ellipsoid (GeographicLib; C. F. F. Karney, "
    "Algorithms for geodesics, J. Geodesy 87:43-55, 2013). The minimum average speed is the "
    "straight line between the two ends, shortened by their two uncertainty radii (the "
    "accuracy radii at the confidence level of the analysis), divided by the elapsed time "
    "plus the time resolution of the source. The true path is at least as long as the "
    "straight line between the true end positions, so this bound rests on the two end "
    "records only. The stepwise minimum sums the steps between consecutive retained records, "
    "each shortened the same way (a record without an accuracy adds nothing). It holds only "
    "if every one of those records lies within its circle, so it is given apart with the "
    "number of such records. Both minimums are also given with the radii as reported. The "
    "path over the records is an estimate, not a bound: position noise lengthens it, sparse "
    "records shorten it."
)
SPEED_RANGE_PREMISE_TEXT = (
    "The bounds assume that every true position lies within its uncertainty circle and "
    "every true instant within the time resolution of its source (the coarsest of 1 min, 1 s, "
    "1 ms and 1 us of which every timestamp of the source is a whole multiple). A reported "
    "accuracy is a confidence radius defined by the source, often about 68 %, not a "
    "guaranteed bound. With analysis.accuracy_confidence = p95, the radius of a source at "
    f"68 % or of unknown level is scaled to 95 % (factor {RAYLEIGH_68_TO_95:.4f}), and the "
    "figures as reported are given beside them."
)


class SpeedRangeError(ValueError):
    """The range cannot be computed as asked; the message says why."""


@dataclass(frozen=True, slots=True)
class RangeRecord:
    """One analysed record of the source as the map payload carries it. ``accuracy_m`` is
    the reported radius, None when the record reports no accuracy, and ``accuracy_scale``
    the factor to its uncertainty radius (payload asc); ``path_m``, ``minimum_path_m`` and
    ``minimum_path_reported_m`` are the cumulative sums of the full, unthinned list
    (payload cum, cmb and cmb_rep; None: equal to ``minimum_path_m``)."""

    sequence: int
    record_number: int
    latitude: float
    longitude: float
    accuracy_m: float | None
    utc: datetime
    path_m: float
    minimum_path_m: float
    speed_kmh: float | None
    time_ambiguity_seconds: float = 0.0
    accuracy_scale: float = 1.0
    minimum_path_reported_m: float | None = None

    @property
    def uncertainty_m(self) -> float | None:
        return None if self.accuracy_m is None else self.accuracy_m * self.accuracy_scale


@dataclass(frozen=True, slots=True)
class SpeedRangeFigures:
    records_in_range: int
    excluded_records: int
    complete: bool
    elapsed_seconds: float
    time_resolution_seconds: float
    # Resolution plus the ambiguity of both ends' local times: how much longer the true
    # elapsed time can be.
    time_uncertainty_seconds: float
    straight_m: float
    straight_minimum_m: float
    stepwise_minimum_m: float
    # Records whose true positions must all lie within their circles for the stepwise bound.
    stepwise_premise_records: int
    minimum_average_kmh: float
    stepwise_minimum_average_kmh: float
    # The two minimums with the radii as reported (equal to the above at scale 1).
    minimum_average_reported_kmh: float
    stepwise_minimum_average_reported_kmh: float
    path_m: float
    path_average_kmh: float | None
    straight_average_kmh: float | None
    slowest_step_kmh: float | None
    fastest_step_kmh: float | None
    implausible_steps: int
    known_steps: int


def _pair_minimum(distance_m: float, first: RangeRecord, second: RangeRecord) -> float:
    if first.uncertainty_m is None or second.uncertainty_m is None:
        return 0.0
    return minimum_step_m(distance_m, first.uncertainty_m + second.uncertainty_m)


def _pair_minimum_reported(distance_m: float, first: RangeRecord, second: RangeRecord) -> float:
    if first.accuracy_m is None or second.accuracy_m is None:
        return 0.0
    return minimum_step_m(distance_m, first.accuracy_m + second.accuracy_m)


def _minimum_path_reported_m(record: RangeRecord) -> float:
    if record.minimum_path_reported_m is None:
        return record.minimum_path_m
    return record.minimum_path_reported_m


def speed_range_figures(
    records: Sequence[RangeRecord],
    first_sequence: int,
    last_sequence: int,
    excluded_sequences: Collection[int],
    time_resolution_seconds: float,
    implausible_speed_kmh: float,
) -> SpeedRangeFigures:
    """Figures from record first_sequence to last_sequence of one source's analysed records
    (``records`` in sequence order, the ones the payload carries). With every record of the
    range present the sums run over the retained records; with a thinned source they come
    from the cumulative figures, and exclusions are refused."""
    if last_sequence <= first_sequence:
        raise SpeedRangeError("the last record must come after the first")
    inside = [record for record in records if first_sequence <= record.sequence <= last_sequence]
    if not inside or inside[0].sequence != first_sequence or inside[-1].sequence != last_sequence:
        raise SpeedRangeError("an end of the range is not an analysed record of this source")
    excluded = set(excluded_sequences)
    if first_sequence in excluded or last_sequence in excluded:
        raise SpeedRangeError("an end of the range cannot be excluded; choose another end")
    unknown = excluded - {record.sequence for record in inside}
    if unknown:
        raise SpeedRangeError("an excluded record does not lie inside the range")
    complete = len(inside) == last_sequence - first_sequence + 1
    if excluded and not complete:
        raise SpeedRangeError(
            "this source is thinned on the map, so the records between the ends are not all "
            "present; exclusions need every record of the range"
        )
    first, last = inside[0], inside[-1]
    elapsed_seconds = (last.utc - first.utc).total_seconds()
    straight_m = geodesic_distance_m(first.latitude, first.longitude, last.latitude, last.longitude)
    straight_minimum_m = _pair_minimum(straight_m, first, last)
    straight_minimum_reported_m = _pair_minimum_reported(straight_m, first, last)

    step_speeds: list[float] = []
    if complete:
        retained = [record for record in inside if record.sequence not in excluded]
        path_m = 0.0
        stepwise_minimum_m = 0.0
        stepwise_minimum_reported_m = 0.0
        for previous, record in zip(retained, retained[1:], strict=False):
            distance_m = geodesic_distance_m(
                previous.latitude, previous.longitude, record.latitude, record.longitude
            )
            path_m += distance_m
            stepwise_minimum_m += _pair_minimum(distance_m, previous, record)
            stepwise_minimum_reported_m += _pair_minimum_reported(distance_m, previous, record)
            seconds = (record.utc - previous.utc).total_seconds()
            if seconds > 0:
                step_speeds.append(distance_m / seconds * 3.6)
    else:
        path_m = last.path_m - first.path_m
        stepwise_minimum_m = last.minimum_path_m - first.minimum_path_m
        stepwise_minimum_reported_m = _minimum_path_reported_m(last) - _minimum_path_reported_m(
            first
        )
        # The payload's own steps of the records it carries (each to its full-list
        # predecessor); the first end's step lies before the range.
        step_speeds = [record.speed_kmh for record in inside[1:] if record.speed_kmh is not None]

    # The headline rests on the two ends only; the stepwise bound needs every record of the
    # range inside its circle, which with 68 % circles is unlikely for many records, so it is
    # given apart with that count.
    premise_records = (
        len(inside) - len(excluded) if complete else last_sequence - first_sequence + 1
    )
    time_uncertainty = (
        time_resolution_seconds + first.time_ambiguity_seconds + last.time_ambiguity_seconds
    )
    stretched_seconds = elapsed_seconds + time_uncertainty
    return SpeedRangeFigures(
        records_in_range=last_sequence - first_sequence + 1,
        excluded_records=len(excluded),
        complete=complete,
        elapsed_seconds=elapsed_seconds,
        time_resolution_seconds=time_resolution_seconds,
        time_uncertainty_seconds=time_uncertainty,
        straight_m=straight_m,
        straight_minimum_m=straight_minimum_m,
        stepwise_minimum_m=stepwise_minimum_m,
        stepwise_premise_records=premise_records,
        minimum_average_kmh=straight_minimum_m / stretched_seconds * 3.6,
        stepwise_minimum_average_kmh=stepwise_minimum_m / stretched_seconds * 3.6,
        minimum_average_reported_kmh=straight_minimum_reported_m / stretched_seconds * 3.6,
        stepwise_minimum_average_reported_kmh=stepwise_minimum_reported_m / stretched_seconds * 3.6,
        path_m=path_m,
        path_average_kmh=path_m / elapsed_seconds * 3.6 if elapsed_seconds > 0 else None,
        straight_average_kmh=straight_m / elapsed_seconds * 3.6 if elapsed_seconds > 0 else None,
        slowest_step_kmh=min(step_speeds) if step_speeds else None,
        fastest_step_kmh=max(step_speeds) if step_speeds else None,
        implausible_steps=sum(1 for speed in step_speeds if speed >= implausible_speed_kmh),
        known_steps=len(step_speeds),
    )
