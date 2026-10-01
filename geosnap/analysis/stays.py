# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Places where the device lingered: sequential clustering around a running centre."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from geosnap.analysis.geometry import haversine_distance_m
from geosnap.analysis.models import Stay
from geosnap.extraction.models import GeoPoint


def find_stays(
    points: Sequence[GeoPoint],
    stop_radius_m: int,
    stop_min_minutes: int,
    *,
    gap_min_minutes: int,
    implausible_speed_kmh: int,
) -> list[Stay]:
    """Group consecutive points within stop_radius_m of their running centre.

    A silence of at least gap_min_minutes (a gap) ends a candidate: nothing is known about
    the device while it is silent, so a stay never spans one. Only candidates lasting at
    least stop_min_minutes become stays; the chronologically last one is marked.

    The record before (after) a stay bounds its arrival (departure) only when it shows the
    device elsewhere: it reports an accuracy and lies farther than stop_radius_m plus
    its uncertainty radius from the stay centre, and the step to the stay's first (from its
    last) record takes time and stays below implausible_speed_kmh. A record that could lie
    at the place, a position jump or a second position of the same instant bounds nothing.
    The departure bound lies later by the next record's time ambiguity and the arrival bound
    (arrived_by_utc) by that of the stay's first record (a local time that occurred twice may
    be its later occurrence); the record before needs no widening, since its true instant
    can only lie later than recorded.
    """
    gap_seconds = gap_min_minutes * 60
    stays: list[Stay] = []
    # The candidate is points[candidate_start:candidate_end].
    candidate_start = 0
    candidate_end = 0
    # Running sums make the centre of the candidate an O(1) update per point.
    latitude_sum = 0.0
    longitude_sum = 0.0

    def shows_device_elsewhere(
        neighbour_index: int, stay_end: GeoPoint, latitude: float, longitude: float
    ) -> bool:
        if not 0 <= neighbour_index < len(points):
            return False
        neighbour = points[neighbour_index]
        if not neighbour.accuracy_known:
            return False
        distance_from_centre_m = haversine_distance_m(
            latitude, longitude, neighbour.latitude, neighbour.longitude
        )
        if distance_from_centre_m <= stop_radius_m + neighbour.uncertainty_radius_m:
            return False
        step_seconds = abs((stay_end.timestamp_utc - neighbour.timestamp_utc).total_seconds())
        if step_seconds == 0:
            return False
        step_m = haversine_distance_m(
            stay_end.latitude, stay_end.longitude, neighbour.latitude, neighbour.longitude
        )
        return step_m / step_seconds * 3.6 < implausible_speed_kmh

    def close_candidate() -> None:
        if candidate_end == candidate_start:
            return
        candidate = points[candidate_start:candidate_end]
        duration_minutes = (
            candidate[-1].timestamp_utc - candidate[0].timestamp_utc
        ).total_seconds() / 60
        if duration_minutes >= stop_min_minutes:
            latitude = latitude_sum / len(candidate)
            longitude = longitude_sum / len(candidate)
            stays.append(
                Stay(
                    identifier=len(stays) + 1,
                    latitude=latitude,
                    longitude=longitude,
                    arrive_utc=candidate[0].timestamp_utc,
                    leave_utc=candidate[-1].timestamp_utc,
                    duration_minutes=duration_minutes,
                    point_count=len(candidate),
                    mean_accuracy_m=sum(point.accuracy_radius_m for point in candidate)
                    / len(candidate),
                    first_line=candidate[0].line_number,
                    last_line=candidate[-1].line_number,
                    arrived_by_utc=candidate[0].timestamp_utc
                    + timedelta(seconds=candidate[0].time_ambiguity_seconds),
                    accuracy_known=all(point.accuracy_known for point in candidate),
                    arrived_after_utc=points[candidate_start - 1].timestamp_utc
                    if shows_device_elsewhere(
                        candidate_start - 1, candidate[0], latitude, longitude
                    )
                    else None,
                    left_before_utc=points[candidate_end].timestamp_utc
                    + timedelta(seconds=points[candidate_end].time_ambiguity_seconds)
                    if shows_device_elsewhere(candidate_end, candidate[-1], latitude, longitude)
                    else None,
                )
            )

    for index, point in enumerate(points):
        if candidate_end > candidate_start:
            count = candidate_end - candidate_start
            centre_latitude = latitude_sum / count
            centre_longitude = longitude_sum / count
            silence_seconds = (
                point.timestamp_utc - points[candidate_end - 1].timestamp_utc
            ).total_seconds()
            distance_m = haversine_distance_m(
                centre_latitude, centre_longitude, point.latitude, point.longitude
            )
            if silence_seconds < gap_seconds and distance_m <= stop_radius_m:
                candidate_end = index + 1
                latitude_sum += point.latitude
                longitude_sum += point.longitude
                continue
            close_candidate()
        candidate_start = index
        candidate_end = index + 1
        latitude_sum = point.latitude
        longitude_sum = point.longitude
    close_candidate()

    if stays:
        stays[-1].is_last = True
    return stays
