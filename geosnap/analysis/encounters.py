# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Cross-source analysis: when two sources were at the same place at the same time
(encounters), whether they moved while they were (joint movement), and which stay locations
several sources share (shared places)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from itertools import combinations, groupby
from typing import NamedTuple

from geosnap.analysis.geometry import haversine_distance_m
from geosnap.analysis.models import Stay
from geosnap.analysis.report import local_stamp
from geosnap.extraction.models import GeoPoint
from geosnap.timezones import offset_minutes

# Encounter.movement: whether the reports of both sources moved during the encounter. An
# encounter that is no joint movement and whose reports never came within max_distance_m
# coincided only through the combined accuracy of its reports: it is not classed same place.
MOVEMENT_SAME_PLACE = "same place"
MOVEMENT_WITHIN_ACCURACY = "within accuracy"
MOVEMENT_JOINT = "joint movement"
JOINT_MOVEMENT_DEFAULT_MIN_M = 500
# Default of analysis.implausible_speed_kmh: a faster step of a path is a position jump.
DEFAULT_IMPLAUSIBLE_SPEED_KMH = 250
# A shorter encounter is never a joint movement, however far its reports lie apart.
JOINT_MOVEMENT_MIN_MINUTES = 5
# The map draws a joint movement through at most this many vertices.
JOINT_MOVEMENT_PATH_VERTICES = 200


@dataclass(frozen=True, slots=True)
class Encounter:
    """Consecutive coincidences of two sources, merged while hits are close in time.

    ``path_length_m`` (plausible steps), ``path_excluded_m`` and ``implausible_steps``
    (position jumps) and ``displacement_m`` describe the path of the hit midpoints (see
    _travelled_path); ``path_parts`` holds its thinned lines of (latitude, longitude),
    broken at the jumps, for a joint movement and is empty otherwise.
    ``min_time_offset_seconds`` is the smallest time difference of a coinciding pair of
    reports, ``closest_pair_offset_seconds`` the time difference of the closest pair (among
    equally close pairs the one nearest in time). Both are differences of the recorded times:
    ``time_ambiguity_seconds`` is the largest by which the local time of one of the reports
    could lie later (it occurred twice, see GeoPoint), 0.0 when none could. Widening the
    offsets by it would not help: the reports coincide only under the recorded times, and
    under the other reading they may not coincide at all.
    ``coincided_within_accuracy_only`` is True when the reports never came within
    max_distance_m (min_distance_m larger), whatever the movement class."""

    identifier: int
    source_a: int
    source_b: int
    start_utc: datetime
    end_utc: datetime
    duration_minutes: float
    centre_latitude: float
    centre_longitude: float
    min_distance_m: float
    hits_a: int
    hits_b: int
    first_line_a: int
    last_line_a: int
    first_line_b: int
    last_line_b: int
    path_length_m: float = 0.0
    displacement_m: float = 0.0
    movement: str = MOVEMENT_SAME_PLACE
    path_parts: tuple[tuple[tuple[float, float], ...], ...] = ()
    path_excluded_m: float = 0.0
    implausible_steps: int = 0
    min_time_offset_seconds: float = 0.0
    closest_pair_offset_seconds: float = 0.0
    coincided_within_accuracy_only: bool = False
    time_ambiguity_seconds: float = 0.0


class SharedPlaceVisit(NamedTuple):
    source_id: int
    arrive_utc: datetime
    leave_utc: datetime
    stay_identifier: int


@dataclass(frozen=True, slots=True)
class SharedPlace:
    """A place formed by stays. group_stays_into_places uses the same record for the places
    of one source only (``sources`` then holds one source)."""

    identifier: int
    latitude: float
    longitude: float
    visits: list[SharedPlaceVisit]
    sources: tuple[int, ...]
    overlapping: bool


def find_encounters(
    points_by_source: Mapping[int, Sequence[GeoPoint]],
    max_minutes: int,
    max_distance_m: int,
    joint_movement_min_m: int = JOINT_MOVEMENT_DEFAULT_MIN_M,
    implausible_speed_kmh: int = DEFAULT_IMPLAUSIBLE_SPEED_KMH,
) -> list[Encounter]:
    """Encounters of every source pair, ordered by pair and then by time.

    Points a and b coincide when they are at most max_minutes apart and their distance
    is within max(max_distance_m, uncertainty a + uncertainty b) (uncertainty_radius_m).
    Every given point takes part, including those excluded from the per-source analysis by
    accuracy (the caller leaves out excluded positioning methods). Input lists must be
    chronological. An encounter of at least JOINT_MOVEMENT_MIN_MINUTES whose
    reports moved at least joint_movement_min_m, in steps slower than
    implausible_speed_kmh, is classed as a joint movement; any other encounter is classed
    within accuracy when its minimum distance exceeds max_distance_m, else same place.
    """
    encounters: list[Encounter] = []
    for source_a, source_b in combinations(sorted(points_by_source), 2):
        hits = _coincidences(
            points_by_source[source_a], points_by_source[source_b], max_minutes, max_distance_m
        )
        for event_hits in _group_hits(hits, timedelta(minutes=max_minutes)):
            encounters.append(
                _encounter_from_hits(
                    len(encounters) + 1,
                    source_a,
                    source_b,
                    event_hits,
                    max_distance_m,
                    joint_movement_min_m,
                    implausible_speed_kmh,
                )
            )
    return encounters


class _Hit(NamedTuple):
    """A point and its nearest coinciding counterpart; ``smallest_offset_seconds`` is the
    smallest time difference between the point and any counterpart it coincides with."""

    point_a: GeoPoint
    point_b: GeoPoint
    distance_m: float
    smallest_offset_seconds: float


METRES_PER_DEGREE_LATITUDE = 111_195.0


def _coincidences(
    points_a: Sequence[GeoPoint],
    points_b: Sequence[GeoPoint],
    max_minutes: int,
    max_distance_m: int,
) -> list[_Hit]:
    """Each point paired with its nearest coinciding counterpart, in both directions.

    Keeping only the nearest counterpart per point bounds the hits by len(a) + len(b)
    instead of every pair inside the time window; the participating points, the event
    bounds and the minimum distance stay the same.
    """
    hits = _nearest_counterparts(points_a, points_b, max_minutes, max_distance_m, a_first=True)
    hits += _nearest_counterparts(points_b, points_a, max_minutes, max_distance_m, a_first=False)
    hits.sort(key=lambda hit: (_hit_start(hit), _hit_end(hit)))
    return hits


def _nearest_counterparts(
    points: Sequence[GeoPoint],
    counterparts: Sequence[GeoPoint],
    max_minutes: int,
    max_distance_m: int,
    a_first: bool,
) -> list[_Hit]:
    """Two-pointer sweep: the window of counterparts only ever moves forward in time.

    Times, coordinates and accuracies are read into plain float lists first; the inner loop
    runs on numbers only.
    """
    window_seconds = max_minutes * 60.0
    minimum_threshold_m = float(max_distance_m)
    counterpart_seconds = [point.timestamp_utc.timestamp() for point in counterparts]
    counterpart_latitudes = [point.latitude for point in counterparts]
    counterpart_longitudes = [point.longitude for point in counterparts]
    counterpart_accuracies = [point.uncertainty_radius_m for point in counterparts]
    counterpart_count = len(counterparts)
    hits: list[_Hit] = []
    window_start = 0
    for point in points:
        seconds = point.timestamp_utc.timestamp()
        latitude = point.latitude
        longitude = point.longitude
        accuracy = point.uncertainty_radius_m
        # Equirectangular metres per degree of longitude at this latitude: comparing squared
        # planar distances avoids trigonometry in the inner loop; the error is below 0.01 %
        # at encounter distances. The exact great-circle distance is computed once, for the
        # nearest counterpart.
        metres_per_degree_longitude = METRES_PER_DEGREE_LATITUDE * math.cos(math.radians(latitude))
        earliest = seconds - window_seconds
        while window_start < counterpart_count and counterpart_seconds[window_start] < earliest:
            window_start += 1
        latest = seconds + window_seconds
        nearest_index = -1
        nearest_squared_m2 = 0.0
        smallest_offset_seconds = window_seconds
        index = window_start
        while index < counterpart_count and counterpart_seconds[index] <= latest:
            threshold_m = max(minimum_threshold_m, accuracy + counterpart_accuracies[index])
            north_m = (counterpart_latitudes[index] - latitude) * METRES_PER_DEGREE_LATITUDE
            if -threshold_m <= north_m <= threshold_m:
                east_m = (
                    _longitude_offset(longitude, counterpart_longitudes[index])
                    * metres_per_degree_longitude
                )
                squared_m2 = north_m * north_m + east_m * east_m
                if squared_m2 <= threshold_m * threshold_m:
                    offset_seconds = abs(counterpart_seconds[index] - seconds)
                    if offset_seconds < smallest_offset_seconds:
                        smallest_offset_seconds = offset_seconds
                    if nearest_index < 0 or squared_m2 < nearest_squared_m2:
                        nearest_index = index
                        nearest_squared_m2 = squared_m2
            index += 1
        if nearest_index >= 0:
            nearest = counterparts[nearest_index]
            distance_m = haversine_distance_m(
                latitude, longitude, nearest.latitude, nearest.longitude
            )
            if a_first:
                hits.append(_Hit(point, nearest, distance_m, smallest_offset_seconds))
            else:
                hits.append(_Hit(nearest, point, distance_m, smallest_offset_seconds))
    return hits


def _longitude_offset(from_longitude: float, to_longitude: float) -> float:
    """Signed longitude difference in degrees, the short way round (-180..180)."""
    offset = to_longitude - from_longitude
    if offset > 180.0:
        return offset - 360.0
    if offset < -180.0:
        return offset + 360.0
    return offset


def _hit_start(hit: _Hit) -> datetime:
    return min(hit.point_a.timestamp_utc, hit.point_b.timestamp_utc)


def _hit_end(hit: _Hit) -> datetime:
    return max(hit.point_a.timestamp_utc, hit.point_b.timestamp_utc)


def _group_hits(hits: Sequence[_Hit], max_pause: timedelta) -> list[list[_Hit]]:
    """A hit joins the running event while it starts at most max_pause after its end."""
    events: list[list[_Hit]] = []
    event_end: datetime | None = None
    for hit in hits:
        if event_end is None or _hit_start(hit) - event_end > max_pause:
            events.append([])
            event_end = _hit_end(hit)
        events[-1].append(hit)
        event_end = max(event_end, _hit_end(hit))
    return events


class _Waypoint(NamedTuple):
    """A position in time and the distance below which a step away from it counts as
    scatter."""

    latitude: float
    longitude: float
    tolerance_m: float
    seconds: float


def _normalised_longitude(longitude: float) -> float:
    if longitude > 180.0:
        return longitude - 360.0
    if longitude < -180.0:
        return longitude + 360.0
    return longitude


def _hit_midpoint(hit: _Hit, max_distance_m: int) -> _Waypoint:
    """Half way between the two reports of a hit in space (the short way round the
    antimeridian) and in time. Its tolerance is the distance within which these two reports
    coincide."""
    point_a, point_b = hit.point_a, hit.point_b
    return _Waypoint(
        (point_a.latitude + point_b.latitude) / 2,
        _normalised_longitude(
            point_a.longitude + _longitude_offset(point_a.longitude, point_b.longitude) / 2
        ),
        max(float(max_distance_m), point_a.uncertainty_radius_m + point_b.uncertainty_radius_m),
        (point_a.timestamp_utc.timestamp() + point_b.timestamp_utc.timestamp()) / 2,
    )


@dataclass(frozen=True, slots=True)
class _TravelledPath:
    """``parts``: the vertices as lines, broken at every implausible step."""

    parts: list[list[tuple[float, float]]]
    length_m: float
    excluded_m: float
    implausible_steps: int


def _travelled_path(waypoints: Sequence[_Waypoint], implausible_speed_kmh: int) -> _TravelledPath:
    """The chronological waypoints without their scatter and without position jumps.

    A waypoint becomes a vertex only when it lies farther from the previous vertex than its
    tolerance (summing every step would turn the scatter of two resting sources into
    kilometres); the last waypoint always ends the path. A step from vertex to vertex adds
    its length only when time passed and its speed stays below implausible_speed_kmh, the
    limit of the segment analysis. Any other step is a position jump of the source data (or
    travel at that speed, such as a flight): the path goes on from the new vertex, the
    step's length is excluded and the line is broken there. Waypoints of the same instant
    have no order of their own: the one nearest to the current vertex comes first, so a
    second position reported for one instant is a jump, not a way there.
    """
    first = waypoints[0]
    vertex = first
    # When the path was last known to be at the vertex: a rest there is not travel time.
    vertex_seconds = first.seconds
    parts = [[(first.latitude, first.longitude)]]
    length_m = 0.0
    excluded_m = 0.0
    implausible_steps = 0
    remaining = len(waypoints) - 1
    for _seconds, same_instant in groupby(waypoints[1:], key=lambda waypoint: waypoint.seconds):
        for waypoint in _nearest_first(list(same_instant), vertex):
            remaining -= 1
            step_m = haversine_distance_m(
                vertex.latitude, vertex.longitude, waypoint.latitude, waypoint.longitude
            )
            if step_m <= waypoint.tolerance_m and (remaining > 0 or step_m == 0.0):
                vertex_seconds = waypoint.seconds
                continue
            elapsed_seconds = waypoint.seconds - vertex_seconds
            if elapsed_seconds > 0 and step_m / elapsed_seconds * 3.6 < implausible_speed_kmh:
                length_m += step_m
                parts[-1].append((waypoint.latitude, waypoint.longitude))
            else:
                excluded_m += step_m
                implausible_steps += 1
                parts.append([(waypoint.latitude, waypoint.longitude)])
            vertex = waypoint
            vertex_seconds = waypoint.seconds
    return _TravelledPath(parts, length_m, excluded_m, implausible_steps)


def _nearest_first(same_instant: list[_Waypoint], vertex: _Waypoint) -> list[_Waypoint]:
    if len(same_instant) > 1:
        same_instant.sort(
            key=lambda waypoint: haversine_distance_m(
                vertex.latitude, vertex.longitude, waypoint.latitude, waypoint.longitude
            )
        )
    return same_instant


def _own_path_length_m(
    points: Sequence[GeoPoint], max_distance_m: int, implausible_speed_kmh: int
) -> float:
    """How far the participating reports of one source moved by themselves."""
    return _travelled_path(
        [
            _Waypoint(
                point.latitude,
                point.longitude,
                max(float(max_distance_m), point.uncertainty_radius_m),
                point.timestamp_utc.timestamp(),
            )
            for point in points
        ],
        implausible_speed_kmh,
    ).length_m


def _thinned_parts(
    parts: Sequence[Sequence[tuple[float, float]]],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """The parts that are lines, with at most JOINT_MOVEMENT_PATH_VERTICES vertices in all.

    Every part keeps its two ends; the vertices between them are thinned evenly. Should the
    ends alone exceed the limit, the parts with the most vertices stay.
    """
    lines = [part for part in parts if len(part) >= 2]
    most_lines = JOINT_MOVEMENT_PATH_VERTICES // 2
    if len(lines) > most_lines:
        kept = set(sorted(range(len(lines)), key=lambda index: -len(lines[index]))[:most_lines])
        lines = [part for index, part in enumerate(lines) if index in kept]
    inner_total = sum(len(part) - 2 for part in lines)
    inner_allowed = JOINT_MOVEMENT_PATH_VERTICES - 2 * len(lines)
    if inner_total <= inner_allowed:
        return tuple(tuple(part) for part in lines)
    thinned: list[tuple[tuple[float, float], ...]] = []
    for part in lines:
        inner = part[1:-1]
        keep = inner_allowed * len(inner) // inner_total
        chosen = [inner[index * len(inner) // keep] for index in range(keep)]
        thinned.append((part[0], *chosen, part[-1]))
    return tuple(thinned)


def _encounter_from_hits(
    identifier: int,
    source_a: int,
    source_b: int,
    hits: Sequence[_Hit],
    max_distance_m: int,
    joint_movement_min_m: int,
    implausible_speed_kmh: int,
) -> Encounter:
    points_a = _distinct_points(hit.point_a for hit in hits)
    points_b = _distinct_points(hit.point_b for hit in hits)
    involved = points_a + points_b
    start_utc = min(_hit_start(hit) for hit in hits)
    end_utc = max(_hit_end(hit) for hit in hits)
    duration_minutes = (end_utc - start_utc).total_seconds() / 60
    # Mean offset from the first point, so an encounter across the antimeridian stays there.
    reference_longitude = involved[0].longitude
    centre_longitude = _normalised_longitude(
        reference_longitude
        + sum(_longitude_offset(reference_longitude, point.longitude) for point in involved)
        / len(involved)
    )
    # Each pair of reports once (the sweep finds most pairs from both sides), in the order
    # of the midpoints' own times: hits are ordered by their earlier report.
    paired_reports = {(id(hit.point_a), id(hit.point_b)): hit for hit in hits}
    midpoints = sorted(
        (_hit_midpoint(hit, max_distance_m) for hit in paired_reports.values()),
        key=lambda midpoint: midpoint.seconds,
    )
    midpoint_path = _travelled_path(midpoints, implausible_speed_kmh)
    # The midpoints also move when only one source does (a coarse report coincides from far
    # away), so each source's own reports must cover the length as well.
    is_joint_movement = (
        duration_minutes >= JOINT_MOVEMENT_MIN_MINUTES
        and midpoint_path.length_m >= joint_movement_min_m
        and _own_path_length_m(points_a, max_distance_m, implausible_speed_kmh)
        >= joint_movement_min_m
        and _own_path_length_m(points_b, max_distance_m, implausible_speed_kmh)
        >= joint_movement_min_m
    )
    # Ties in distance are broken by the smaller time offset, so the closest pair does not
    # depend on the order in which the hits were found.
    closest_hit = min(hits, key=lambda hit: (hit.distance_m, _pair_offset_seconds(hit)))
    min_distance_m = closest_hit.distance_m
    if is_joint_movement:
        movement = MOVEMENT_JOINT
    elif min_distance_m > max_distance_m:
        movement = MOVEMENT_WITHIN_ACCURACY
    else:
        movement = MOVEMENT_SAME_PLACE
    return Encounter(
        identifier=identifier,
        source_a=source_a,
        source_b=source_b,
        start_utc=start_utc,
        end_utc=end_utc,
        duration_minutes=duration_minutes,
        centre_latitude=sum(point.latitude for point in involved) / len(involved),
        centre_longitude=centre_longitude,
        min_distance_m=min_distance_m,
        hits_a=len(points_a),
        hits_b=len(points_b),
        first_line_a=points_a[0].line_number,
        last_line_a=points_a[-1].line_number,
        first_line_b=points_b[0].line_number,
        last_line_b=points_b[-1].line_number,
        path_length_m=midpoint_path.length_m,
        path_excluded_m=midpoint_path.excluded_m,
        implausible_steps=midpoint_path.implausible_steps,
        displacement_m=haversine_distance_m(
            midpoints[0].latitude,
            midpoints[0].longitude,
            midpoints[-1].latitude,
            midpoints[-1].longitude,
        ),
        movement=movement,
        path_parts=_thinned_parts(midpoint_path.parts) if is_joint_movement else (),
        min_time_offset_seconds=min(hit.smallest_offset_seconds for hit in hits),
        closest_pair_offset_seconds=_pair_offset_seconds(closest_hit),
        coincided_within_accuracy_only=min_distance_m > max_distance_m,
        time_ambiguity_seconds=max(point.time_ambiguity_seconds for point in involved),
    )


def encounter_class_text(movement: str, coincided_within_accuracy_only: bool) -> str:
    """The movement class, "(only within accuracy)" added when the reports never came within
    the encounter distance and the class does not already say so."""
    if coincided_within_accuracy_only and movement != MOVEMENT_WITHIN_ACCURACY:
        return f"{movement} (only within accuracy)"
    return movement


def _pair_offset_seconds(hit: _Hit) -> float:
    return abs((hit.point_a.timestamp_utc - hit.point_b.timestamp_utc).total_seconds())


def _distinct_points(points: Iterable[GeoPoint]) -> list[GeoPoint]:
    """Each point once, in chronological order (a point may take part in several hits)."""
    unique: dict[int, GeoPoint] = {}
    for point in points:
        unique.setdefault(id(point), point)
    return sorted(unique.values(), key=lambda point: (point.timestamp_utc, point.line_number))


def find_shared_places(
    stays_by_source: Mapping[int, Sequence[Stay]], stop_radius_m: int
) -> list[SharedPlace]:
    """The places where at least two sources stayed (see group_stays_into_places)."""
    return group_stays_into_places(stays_by_source, stop_radius_m)[0]


def group_stays_into_places(
    stays_by_source: Mapping[int, Sequence[Stay]], place_radius_m: int
) -> tuple[list[SharedPlace], list[SharedPlace]]:
    """Stay centres within place_radius_m of a running place centre form one place
    (analysis.stop_radius_m for the shared places, analysis.matrix_tolerance_m for the
    rows of the presence matrix).

    Stays are assigned greedily in source order. Returns the shared places (at least two
    sources) and the places of one source only, each numbered from 1. Visits overlap when
    two sources' arrive/leave intervals intersect.
    """
    clusters: list[list[SharedPlaceVisit]] = []
    centres: list[tuple[float, float]] = []
    for source_id in sorted(stays_by_source):
        for stay in stays_by_source[source_id]:
            visit = SharedPlaceVisit(source_id, stay.arrive_utc, stay.leave_utc, stay.identifier)
            position = (stay.latitude, stay.longitude)
            index = _nearest_cluster(centres, position, place_radius_m)
            if index is None:
                clusters.append([visit])
                centres.append(position)
                continue
            count = len(clusters[index])
            latitude, longitude = centres[index]
            # The longitude moves by its share of the offset, the short way round, so a
            # place across the antimeridian stays there.
            centres[index] = (
                (latitude * count + stay.latitude) / (count + 1),
                _normalised_longitude(
                    longitude + _longitude_offset(longitude, stay.longitude) / (count + 1)
                ),
            )
            clusters[index].append(visit)

    shared_places: list[SharedPlace] = []
    single_source_places: list[SharedPlace] = []
    for visits, (latitude, longitude) in zip(clusters, centres, strict=True):
        sources = tuple(sorted({visit.source_id for visit in visits}))
        places = shared_places if len(sources) >= 2 else single_source_places
        places.append(
            SharedPlace(
                identifier=len(places) + 1,
                latitude=latitude,
                longitude=longitude,
                visits=visits,
                sources=sources,
                overlapping=overlap_between_sources(
                    (visit.source_id, visit.arrive_utc, visit.leave_utc) for visit in visits
                ),
            )
        )
    return shared_places, single_source_places


def _nearest_cluster(
    centres: Sequence[tuple[float, float]], position: tuple[float, float], radius_m: int
) -> int | None:
    best_index: int | None = None
    best_distance_m = float(radius_m)
    for index, (latitude, longitude) in enumerate(centres):
        distance_m = haversine_distance_m(latitude, longitude, position[0], position[1])
        if distance_m <= best_distance_m:
            best_index, best_distance_m = index, distance_m
    return best_index


def overlap_between_sources(intervals: Iterable[tuple[int, datetime, datetime]]) -> bool:
    """Whether two intervals (source, start, end; bounds included) of different sources
    intersect. One pass in start order against the latest end seen per source."""
    latest_end: dict[int, datetime] = {}
    for source_id, start, end in sorted(intervals, key=lambda interval: interval[1]):
        if any(other_end >= start for other, other_end in latest_end.items() if other != source_id):
            return True
        if source_id not in latest_end or end > latest_end[source_id]:
            latest_end[source_id] = end
    return False


def encounter_to_document(encounter: Encounter, display_tz: tzinfo | None) -> dict[str, object]:
    """JSON-ready encounter; local wall-clock times carry their offset (minutes) alongside."""
    return {
        "id": encounter.identifier,
        "source_a": encounter.source_a,
        "source_b": encounter.source_b,
        "start_utc": encounter.start_utc.isoformat(),
        "start_local": local_stamp(encounter.start_utc, display_tz),
        "start_offset": offset_minutes(encounter.start_utc, display_tz),
        "end_utc": encounter.end_utc.isoformat(),
        "end_local": local_stamp(encounter.end_utc, display_tz),
        "end_offset": offset_minutes(encounter.end_utc, display_tz),
        "duration_minutes": round(encounter.duration_minutes, 1),
        "lat": encounter.centre_latitude,
        "lon": encounter.centre_longitude,
        "min_distance_m": round(encounter.min_distance_m, 1),
        "hits_a": encounter.hits_a,
        "hits_b": encounter.hits_b,
        "first_line_a": encounter.first_line_a,
        "last_line_a": encounter.last_line_a,
        "first_line_b": encounter.first_line_b,
        "last_line_b": encounter.last_line_b,
        "movement": encounter.movement,
        "path_length_m": round(encounter.path_length_m, 1),
        "displacement_m": round(encounter.displacement_m, 1),
        "path_excluded_m": round(encounter.path_excluded_m, 1),
        "implausible_steps": encounter.implausible_steps,
        "min_time_offset_seconds": round(encounter.min_time_offset_seconds, 3),
        "closest_pair_offset_seconds": round(encounter.closest_pair_offset_seconds, 3),
        "coincided_within_accuracy_only": encounter.coincided_within_accuracy_only,
        "time_ambiguity_seconds": encounter.time_ambiguity_seconds,
        "path_parts": [
            [[round(latitude, 6), round(longitude, 6)] for latitude, longitude in part]
            for part in encounter.path_parts
        ],
    }


def shared_place_to_document(place: SharedPlace, display_tz: tzinfo | None) -> dict[str, object]:
    return {
        "id": place.identifier,
        "lat": place.latitude,
        "lon": place.longitude,
        "sources": list(place.sources),
        "overlapping": place.overlapping,
        "visits": [
            {
                "source_id": visit.source_id,
                "stay_id": visit.stay_identifier,
                "arrive_utc": visit.arrive_utc.isoformat(),
                "arrive_local": local_stamp(visit.arrive_utc, display_tz),
                "arrive_offset": offset_minutes(visit.arrive_utc, display_tz),
                "leave_utc": visit.leave_utc.isoformat(),
                "leave_local": local_stamp(visit.leave_utc, display_tz),
                "leave_offset": offset_minutes(visit.leave_utc, display_tz),
            }
            for visit in place.visits
        ],
    }
