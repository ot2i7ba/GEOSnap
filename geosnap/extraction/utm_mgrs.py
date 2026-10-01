# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""UTM and MGRS texts to WGS84 latitude and longitude.

The transverse Mercator inverse is the Krüger series in Karney's form (Karney 2011,
"Transverse Mercator with an accuracy of a few nanometers", series to n^4, exact Newton
step for the geodetic latitude). Against PROJ the difference stays below 0.001 mm over
the reference vectors in tests/fixtures/utm_mgrs_vectors.csv. The datum is taken as
WGS84; ETRS89 coordinates differ from it by less than a metre.

UTM letter after the zone: a latitude band (C-X without I and O) gives the hemisphere and
must fit the northing. N means north under both readings (band N lies north of the
equator). S is refused when the northing fits band S (32-40° N, half a degree of
margin), because hemisphere S would put the same numbers south of the equator; a northing
outside band S leaves only the hemisphere reading. The words North/South (Nord/Süd)
are always a hemisphere.

An MGRS text names a square; by the standard its coordinates are the square's south-west
corner (digits are truncated, never rounded), and that corner is the position returned.
The polar grids (UPS; MGRS zones A, B, Y, Z) are refused.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

UTM_NOTATION = "UTM"
MGRS_NOTATION = "MGRS"

WGS84_SEMI_MAJOR_AXIS_M = 6_378_137.0
WGS84_FLATTENING = 1 / 298.257223563
UTM_SCALE_FACTOR = 0.9996
FALSE_EASTING_M = 500_000.0
FALSE_NORTHING_SOUTH_M = 10_000_000.0
EASTING_MIN_M, EASTING_MAX_M = 100_000.0, 900_000.0
UTM_SOUTH_LIMIT_DEGREES, UTM_NORTH_LIMIT_DEGREES = -80.0, 84.0
# A northing rounded to the metre may lie just across a band edge.
BAND_EDGE_SLACK_DEGREES = 1e-4
# Letter S: the band reading is ruled out only when the northing misses band S clearly.
AMBIGUOUS_BAND_MARGIN_DEGREES = 0.5
BAND_LETTERS = "CDEFGHJKLMNPQRSTUVWX"
BAND_HEIGHT_DEGREES = 8.0
# Band X has no zones 32, 34 and 36 (Svalbard).
ZONES_MISSING_IN_BAND_X = frozenset({32, 34, 36})
GRID_SQUARE_M = 100_000.0
GRID_NORTHING_CYCLE_M = 2_000_000.0
GRID_COLUMN_LETTERS = ("STUVWXYZ", "ABCDEFGH", "JKLMNPQR")  # by zone % 3
GRID_ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"
GRID_ROW_OFFSET_EVEN_ZONE = 5
GRID_DIGITS_PER_AXIS_MAX = 5
SQUARE_SIZE_NAMES = ("100 km", "10 km", "1 km", "100 m", "10 m", "1 m")

_NORTH_WORDS = frozenset({"NORTH", "NORD"})
_SOUTH_WORDS = frozenset({"SOUTH", "SÜD", "SUED"})
_UTM_PATTERN = re.compile(
    r"(?:UTM\s*)?(?:ZONE\s*)?(\d{1,2})\s*(NORTH|NORD|SOUTH|SÜD|SUED|[A-Z])\s+"
    r"(\d{1,7}(?:[.,]\d+)?)\s*(?:M?\s*E)?[\s,;]+(\d{1,8}(?:[.,]\d+)?)\s*(?:M?\s*N)?",
    re.ASCII,
)
_MGRS_PATTERN = re.compile(
    r"(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])([A-HJ-NP-V])\s*(\d*)(?:\s+(\d+))?", re.ASCII
)
_MGRS_POLAR_PATTERN = re.compile(r"[ABYZ]\s*[A-Z]{2}\s*[\d\s]*", re.ASCII)

_THIRD_FLATTENING = WGS84_FLATTENING / (2 - WGS84_FLATTENING)
_ECCENTRICITY = math.sqrt(WGS84_FLATTENING * (2 - WGS84_FLATTENING))


def _inverse_series_coefficients(n: float) -> tuple[float, ...]:
    return (
        n / 2 - 2 * n**2 / 3 + 37 * n**3 / 96 - n**4 / 360,
        n**2 / 48 + n**3 / 15 - 437 * n**4 / 1440,
        17 * n**3 / 480 - 37 * n**4 / 840,
        4397 * n**4 / 161280,
    )


_INVERSE_COEFFICIENTS = _inverse_series_coefficients(_THIRD_FLATTENING)
_RECTIFYING_RADIUS_M = (
    WGS84_SEMI_MAJOR_AXIS_M
    / (1 + _THIRD_FLATTENING)
    * (1 + _THIRD_FLATTENING**2 / 4 + _THIRD_FLATTENING**4 / 64)
)


@dataclass(frozen=True, slots=True)
class GridPosition:
    """WGS84 position of a UTM or MGRS text; ``remark`` states what was assumed."""

    latitude: float
    longitude: float
    notation: str
    remark: str


def utm_to_wgs84(
    zone: int, northern: bool, easting_m: float, northing_m: float
) -> tuple[float, float]:
    """Latitude and longitude in degrees of a UTM coordinate (no range checks)."""
    true_northing_m = northing_m if northern else northing_m - FALSE_NORTHING_SOUTH_M
    xi = true_northing_m / (UTM_SCALE_FACTOR * _RECTIFYING_RADIUS_M)
    eta = (easting_m - FALSE_EASTING_M) / (UTM_SCALE_FACTOR * _RECTIFYING_RADIUS_M)
    xi_conformal, eta_conformal = xi, eta
    for order, coefficient in enumerate(_INVERSE_COEFFICIENTS, start=1):
        xi_conformal -= coefficient * math.sin(2 * order * xi) * math.cosh(2 * order * eta)
        eta_conformal -= coefficient * math.cos(2 * order * xi) * math.sinh(2 * order * eta)
    conformal_tangent = math.sin(xi_conformal) / math.hypot(
        math.sinh(eta_conformal), math.cos(xi_conformal)
    )
    longitude_from_meridian = math.atan2(math.sinh(eta_conformal), math.cos(xi_conformal))
    central_meridian_degrees = zone * 6 - 183
    return (
        math.degrees(math.atan(_geodetic_tangent(conformal_tangent))),
        central_meridian_degrees + math.degrees(longitude_from_meridian),
    )


def _geodetic_tangent(conformal_tangent: float) -> float:
    """tan(latitude) from the tangent of the conformal latitude (Karney 2011, eq. 19-21)."""
    tangent = conformal_tangent
    for _ in range(8):
        sigma = math.sinh(
            _ECCENTRICITY * math.atanh(_ECCENTRICITY * tangent / math.hypot(1.0, tangent))
        )
        conformal_estimate = tangent * math.hypot(1.0, sigma) - sigma * math.hypot(1.0, tangent)
        correction = (
            (conformal_tangent - conformal_estimate)
            / math.hypot(1.0, conformal_estimate)
            * (1 + (1 - _ECCENTRICITY**2) * tangent**2)
            / ((1 - _ECCENTRICITY**2) * math.hypot(1.0, tangent))
        )
        tangent += correction
        if abs(correction) < 1e-14 * max(1.0, abs(tangent)):
            break
    return tangent


def parse_utm(text: str) -> GridPosition | str:
    """ "32U 354822 5700612", "UTM 32 N 354822 5700612", "32 South 354822mE 5700612mN"."""
    cleaned = " ".join(text.split()).upper()
    match = _UTM_PATTERN.fullmatch(cleaned)
    if match is None:
        return "unparsable UTM coordinate"
    zone, designator = int(match[1]), match[2]
    easting_m = float(match[3].replace(",", "."))
    northing_m = float(match[4].replace(",", "."))
    zone_problem = _zone_problem(zone)
    if zone_problem:
        return zone_problem
    band: str | None = None
    if designator in _NORTH_WORDS or designator == "N":
        northern = True
    elif designator in _SOUTH_WORDS:
        northern = False
    elif designator == "S":
        band_south, band_north = _band_limits("S")
        latitude_if_band = utm_to_wgs84(zone, True, easting_m, northing_m)[0]
        if (
            band_south - AMBIGUOUS_BAND_MARGIN_DEGREES
            <= latitude_if_band
            <= band_north + AMBIGUOUS_BAND_MARGIN_DEGREES
        ):
            return (
                "UTM letter S is ambiguous (latitude band S lies north of the equator,"
                " hemisphere S south of it); write the hemisphere as a word"
            )
        northern = False
    elif designator in BAND_LETTERS:
        band = designator
        northern = band >= "N"
    else:
        return f"{designator} is not a UTM latitude band"
    if band == "X" and zone in ZONES_MISSING_IN_BAND_X:
        return f"UTM zone {zone}X does not exist"
    if not EASTING_MIN_M <= easting_m < EASTING_MAX_M:
        return f"UTM easting out of range: {match[3]}"
    if northing_m > FALSE_NORTHING_SOUTH_M:
        return f"UTM northing out of range: {match[4]}"
    latitude, longitude = utm_to_wgs84(zone, northern, easting_m, northing_m)
    if not (
        UTM_SOUTH_LIMIT_DEGREES - BAND_EDGE_SLACK_DEGREES
        <= latitude
        <= UTM_NORTH_LIMIT_DEGREES + BAND_EDGE_SLACK_DEGREES
    ):
        return f"latitude {latitude:.4f} lies outside the UTM area (80° S to 84° N)"
    if band is not None:
        band_south, band_north = _band_limits(band)
        if not (
            band_south - BAND_EDGE_SLACK_DEGREES <= latitude <= band_north + BAND_EDGE_SLACK_DEGREES
        ):
            return f"UTM northing gives latitude {latitude:.4f}, outside latitude band {band}"
    hemisphere = "northern" if northern else "southern"
    remark = f"zone {zone}, {hemisphere} hemisphere, WGS84 assumed"
    return GridPosition(latitude, _wrapped_longitude(longitude), UTM_NOTATION, remark)


def parse_mgrs(text: str) -> GridPosition | str:
    """ "32ULC5482200612" or "32U LC 54822 00612", one to five digits per axis or none."""
    cleaned = " ".join(text.split()).upper()
    match = _MGRS_PATTERN.fullmatch(cleaned)
    if match is None:
        if _MGRS_POLAR_PATTERN.fullmatch(cleaned):
            return "polar MGRS (UPS) is not supported"
        return "unparsable MGRS coordinate"
    zone, band, column_letter, row_letter = int(match[1]), match[2], match[3], match[4]
    zone_problem = _zone_problem(zone)
    if zone_problem:
        return zone_problem
    if band == "X" and zone in ZONES_MISSING_IN_BAND_X:
        return f"MGRS zone {zone}X does not exist"
    if match[6] is None:
        digits = match[5]
        if len(digits) % 2:
            return "MGRS needs an even number of digits"
        easting_digits, northing_digits = digits[: len(digits) // 2], digits[len(digits) // 2 :]
    else:
        easting_digits, northing_digits = match[5], match[6]
        if len(easting_digits) != len(northing_digits):
            return "MGRS easting and northing need the same number of digits"
    digit_count = len(easting_digits)
    if digit_count > GRID_DIGITS_PER_AXIS_MAX:
        return f"MGRS takes at most {GRID_DIGITS_PER_AXIS_MAX} digits per axis"
    column_index = GRID_COLUMN_LETTERS[zone % 3].find(column_letter)
    if column_index < 0:
        return f"MGRS column letter {column_letter} does not occur in zone {zone}"
    row_index = GRID_ROW_LETTERS.index(row_letter)
    if zone % 2 == 0:
        row_index = (row_index - GRID_ROW_OFFSET_EVEN_ZONE) % len(GRID_ROW_LETTERS)
    square_size_m = GRID_SQUARE_M / 10**digit_count
    easting_m = (column_index + 1) * GRID_SQUARE_M + int(easting_digits or "0") * square_size_m
    northing_in_cycle_m = row_index * GRID_SQUARE_M + int(northing_digits or "0") * square_size_m
    northern = band >= "N"
    corner = _corner_in_band(zone, northern, band, easting_m, northing_in_cycle_m, square_size_m)
    if corner is None:
        return f"MGRS square {column_letter}{row_letter} does not occur in latitude band {band}"
    remark = f"{SQUARE_SIZE_NAMES[digit_count]} square, south-west corner, WGS84 assumed"
    return GridPosition(corner[0], _wrapped_longitude(corner[1]), MGRS_NOTATION, remark)


def _corner_in_band(
    zone: int,
    northern: bool,
    band: str,
    easting_m: float,
    northing_in_cycle_m: float,
    square_size_m: float,
) -> tuple[float, float] | None:
    """South-west corner of the one square, among the repeats of the row letters every
    2000 km, that reaches into the latitude band; None when none or several do."""
    band_south, band_north = _band_limits(band)
    corners_in_band = []
    northing_m = northing_in_cycle_m
    while northing_m < FALSE_NORTHING_SOUTH_M:
        corner_latitudes = [
            utm_to_wgs84(zone, northern, corner_easting_m, corner_northing_m)[0]
            for corner_easting_m in (easting_m, easting_m + square_size_m)
            for corner_northing_m in (northing_m, northing_m + square_size_m)
        ]
        if (
            min(corner_latitudes) <= band_north + BAND_EDGE_SLACK_DEGREES
            and max(corner_latitudes) >= band_south - BAND_EDGE_SLACK_DEGREES
        ):
            corners_in_band.append(utm_to_wgs84(zone, northern, easting_m, northing_m))
        northing_m += GRID_NORTHING_CYCLE_M
    return corners_in_band[0] if len(corners_in_band) == 1 else None


def _band_limits(band: str) -> tuple[float, float]:
    band_south = UTM_SOUTH_LIMIT_DEGREES + BAND_LETTERS.index(band) * BAND_HEIGHT_DEGREES
    band_north = UTM_NORTH_LIMIT_DEGREES if band == "X" else band_south + BAND_HEIGHT_DEGREES
    return band_south, band_north


def _zone_problem(zone: int) -> str | None:
    return None if 1 <= zone <= 60 else f"UTM zone out of range: {zone}"


def _wrapped_longitude(longitude: float) -> float:
    """Zones 1 and 60 reach across the antimeridian."""
    if longitude > 180.0:
        return longitude - 360.0
    if longitude < -180.0:
        return longitude + 360.0
    return longitude
