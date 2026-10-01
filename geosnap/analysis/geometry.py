# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Distances and bearings between positions.

Figures between records (steps, speeds, paths, gaps) use the geodesic on the WGS84
ellipsoid computed by GeographicLib (C. F. F. Karney, "Algorithms for geodesics",
J. Geodesy 87:43-55, 2013), accurate to about 15 nanometres. Proximity tests (stays,
encounters, case places, places) keep the great circle on a sphere of mean radius: its
relative error is at most 0.56 %, far below the position accuracy they compare against,
at a fiftieth of the cost.
"""

from __future__ import annotations

import math

from geographiclib.geodesic import Geodesic

EARTH_RADIUS_M = 6_371_000.0
_WGS84 = Geodesic.WGS84
_DISTANCE_AND_AZIMUTH = Geodesic.DISTANCE | Geodesic.AZIMUTH


def haversine_distance_m(
    latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float
) -> float:
    """Distance in metres along the great circle between two positions (proximity tests)."""
    phi_1 = math.radians(latitude_1)
    phi_2 = math.radians(latitude_2)
    delta_phi = math.radians(latitude_2 - latitude_1)
    delta_lambda = math.radians(longitude_2 - longitude_1)
    chord = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_1) * math.cos(phi_2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(chord))


def geodesic_distance_m(
    latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float
) -> float:
    """Length in metres of the WGS84 geodesic between two positions."""
    distance: float = _WGS84.Inverse(
        latitude_1, longitude_1, latitude_2, longitude_2, Geodesic.DISTANCE
    )["s12"]
    return distance


def geodesic_distance_and_bearing(
    latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float
) -> tuple[float, float]:
    """WGS84 geodesic length (m) and initial azimuth (0-360, clockwise from north). Two
    identical positions have no direction and get 0."""
    line = _WGS84.Inverse(latitude_1, longitude_1, latitude_2, longitude_2, _DISTANCE_AND_AZIMUTH)
    distance: float = line["s12"]
    if distance == 0.0:
        return 0.0, 0.0
    return distance, (float(line["azi1"]) + 360.0) % 360.0
