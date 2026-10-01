# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Read coordinates written as text: one strict parser for every free-text notation.

A cell is read per notation, never by guessing: the parser returns the value together
with the name of the notation it recognised, or the reason for refusing the text.

Single angle (latitude or longitude), hemisphere as sign or as letter N S E W (and the
German O for east) in front or behind:
- decimal degrees: 51.4361, 51,4361, 51.4361°, N51.4361, 51.4361 N, +51.4361
- degrees and decimal minutes: 51° 26.166' N, N 51 26.166, 51°26,166′N
- degrees, minutes, seconds: 51°26'09.9"N, 51 26 09.9 N, 51:26:09.9 N, 51d26m09.9s
Degree signs ° º ˚ (or d), typographic primes and quotes, '' for seconds, any Unicode
space and the minus sign U+2212 are understood. Conversion is exact to double precision:
degrees + minutes / 60 + seconds / 3600.

Position (both axes in one cell): two angles separated by comma, semicolon, slash or
space, latitude first unless hemisphere letters say otherwise; WKT "POINT(lon lat)";
"geo:lat,lon"; map URLs that name a marked position (q=, query=, mlat= and mlon=); UTM
and MGRS (see utm_mgrs).

Refused, each with its reason: minutes or seconds of 60 or more, a fraction on anything
but the last part, a sign inside the angle, a sign contradicting the letter, a letter of
the other axis, values out of range, NaN, infinity, exponents, underscores, non-ASCII
digits, an O glued to a digit (it could be a zero), a number with grouped thousands
("7.800,2" is not 7.8° N 2° E), a position text with more than one reading (also
"51 26.166", which could be degrees and minutes of one angle), map URLs
that only carry the centre of a view (@lat,lon and #map=), geo URIs in another
reference system. Integers scaled by 1e7 or 1e6 are read only through
parse_scaled_integer, which the caller uses when the column header says E7 or E6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from geosnap.extraction.reader_support import NUMBER_TOO_LONG_REASON, bounded_int, excerpt
from geosnap.extraction.utm_mgrs import parse_mgrs, parse_utm

DECIMAL_DEGREES = "decimal degrees"
HEMISPHERE_DECIMAL_DEGREES = "decimal degrees with hemisphere letter"
DEGREES_DECIMAL_MINUTES = "degrees and decimal minutes"
DEGREES_MINUTES_SECONDS = "degrees, minutes, seconds"
WKT_POINT = "WKT point"
GEO_URI = "geo URI"
MAP_URL = "map URL"

LAT_LON = "lat_lon"
LON_LAT = "lon_lat"
POSITION_ORDER_LABELS = {LAT_LON: "latitude, longitude", LON_LAT: "longitude, latitude"}
LONGITUDE_FIRST_MARK = ", longitude first"

BARE_PARTS_REASON = (
    "ambiguous position (degrees and minutes need unit signs or hemisphere letters in a position)"
)
# A scaled integer below this magnitude is refused (see parse_scaled_integer).
SCALED_MINIMUM_DEGREES = 1e-3

LATITUDE = "latitude"
LONGITUDE = "longitude"
_AXIS_LIMIT_DEGREES = {LATITUDE: 90.0, LONGITUDE: 180.0}
_AXIS_BY_LETTER = {"N": LATITUDE, "S": LATITUDE, "E": LONGITUDE, "O": LONGITUDE, "W": LONGITUDE}
_NEGATIVE_LETTERS = frozenset({"S", "W"})

_CHARACTER_EQUIVALENTS = str.maketrans(
    {
        "º": "°",
        "˚": "°",
        "′": "'",
        "’": "'",
        "‘": "'",
        "ʹ": "'",
        "´": "'",
        "″": '"',
        "”": '"',
        "“": '"',
        "ʺ": '"',
        "−": "-",
    }
)
# Upper case of the same length, so that positions in both texts agree.
_UPPER_CASE = str.maketrans("abcdefghijklmnopqrstuvwxyzü", "ABCDEFGHIJKLMNOPQRSTUVWXYZÜ")
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_PLAIN_DECIMAL_PATTERN = re.compile(_NUMBER, re.ASCII)
_PLAIN_PAIR_PATTERN = re.compile(rf"({_NUMBER})(?:\s*[,;]\s*|\s+)({_NUMBER})", re.ASCII)
_PART = r"\d{1,3}(?:\.\d*|,\d+)?"
_MINOR_PART = r"\d{1,2}(?:\.\d*|,\d+)?"
_LETTER = r"[NSEWO]"
# Parts are separated by their unit sign, a colon or a space; digits never run together.
# The sign stands directly before the degrees.
_SIGN_STYLE_PATTERN = re.compile(
    rf"(?P<prefix>{_LETTER})?\s*(?P<sign>[+-])?(?P<degrees>{_PART})"
    rf"(?:(?:\s*[°D]\s*|\s*:\s*|\s+)(?P<minutes>{_MINOR_PART})"
    rf"(?:(?:\s*'\s*|\s*:\s*|\s+)(?P<seconds>{_MINOR_PART})(?P<seconds_mark>\s*\")?|(?:\s*')?)"
    rf"|(?:\s*[°D])?)"
    rf"\s*(?P<suffix>{_LETTER})?",
    re.ASCII,
)
# "51d26m09.9s": every part carries its unit letter, a hemisphere letter may follow.
_LETTER_STYLE_PATTERN = re.compile(
    rf"(?P<prefix>{_LETTER})?\s*(?P<sign>[+-])?(?P<degrees>{_PART})\s*D(?:EG)?"
    rf"\s*(?P<minutes>{_MINOR_PART})\s*M(?:IN)?"
    rf"(?:\s*(?P<seconds>{_MINOR_PART})\s*(?P<seconds_unit>S(?:EC)?))?"
    rf"\s*(?P<suffix>{_LETTER})?",
    re.ASCII,
)
_LONE_DECIMAL_COMMA_PATTERN = re.compile(r"[+-]?\d+,\d+", re.ASCII)
_DETACHED_SIGN_PATTERN = re.compile(r"[+-]\s+[\d.]")
_INNER_SIGN_PATTERN = re.compile(r"[\d°'\"]\s*[+-]")
_EXPONENT_LIKE_PATTERN = re.compile(r"\dE\s*[+-]\s*\d", re.ASCII)
# Map URL parameters that hold a position of their own (view centre, route ends).
_OTHER_POSITION_PARAMETERS = ("ll", "sll", "center", "saddr", "daddr", "origin", "destination")
_UNIT_SIGNS = "°':\"D"
_SEPARATOR_PATTERN = re.compile(r"\s*[,;/]\s*|\s+")
_WKT_POINT_PATTERN = re.compile(rf"POINT\s*\(\s*({_NUMBER})\s+({_NUMBER})\s*\)", re.ASCII)
_GEO_URI_PATTERN = re.compile(rf"GEO:({_NUMBER}),({_NUMBER})(?:,{_NUMBER})?((?:;[^;]+)*)", re.ASCII)
_SCALED_INTEGER_PATTERN = re.compile(r"[+-]?\d+", re.ASCII)
# One number with grouped thousands ("7.800,2", "1,100.5"), never two angles.
_THOUSANDS_PATTERN = re.compile(
    r"[+-]?(?:\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:,\d{3})+\.\d+)", re.ASCII
)
_UTM_SHAPE_PATTERN = re.compile(
    r"(?:UTM\b|ZONE\b|\d{1,2}\s*(?:[A-Z]|NORTH|NORD|SOUTH|SÜD|SUED)\s+\d{6})", re.ASCII
)
_MGRS_SHAPE_PATTERN = re.compile(r"(?:\d{1,2}\s*[A-Z]|[ABYZ])\s*[A-Z]{2}(?:\s*\d|$)", re.ASCII)
_VIEW_CENTRE_PATTERN = re.compile(rf"@{_NUMBER},{_NUMBER}|#MAP=", re.ASCII)


@dataclass(frozen=True, slots=True)
class ParsedAngle:
    degrees: float
    notation: str


@dataclass(frozen=True, slots=True)
class ParsedPosition:
    """``remark`` states what a grid notation assumed (UTM hemisphere, MGRS square)."""

    latitude: float
    longitude: float
    notation: str
    remark: str | None = None


@dataclass(frozen=True, slots=True)
class _Angle:
    degrees: float
    notation: str
    axis: str | None
    # Several parts with neither unit signs nor a hemisphere letter ("51 26 09.9").
    bare_parts: bool


def parse_latitude(text: str) -> ParsedAngle | str:
    """The latitude in a cell of its own, or the reason it is refused."""
    return _parse_single_axis(text, LATITUDE)


def parse_longitude(text: str) -> ParsedAngle | str:
    """The longitude in a cell of its own, or the reason it is refused."""
    return _parse_single_axis(text, LONGITUDE)


def parse_scaled_integer(text: str, axis: str, exponent: int) -> ParsedAngle | str:
    """Degrees stored as an integer scaled by 10**exponent (E7: 514361000 = 51.4361)."""
    cleaned = text.strip()
    if not _SCALED_INTEGER_PATTERN.fullmatch(cleaned):
        return f"{axis} is not an E{exponent} integer: {excerpt(text)}"
    scaled = bounded_int(cleaned)
    if scaled is None:
        return f"{NUMBER_TOO_LONG_REASON}: {excerpt(text)}"
    # 51 under an E7 header is far more likely plain degrees than 0.0000051°. Zero is
    # zero either way.
    if 0 < abs(scaled) < SCALED_MINIMUM_DEGREES * 10**exponent:
        return f"E{exponent} column holds a value that looks like plain degrees: {excerpt(text)}"
    degrees = scaled / 10**exponent
    return _range_problem(degrees, axis) or ParsedAngle(degrees, f"E{exponent} integer")


def parse_position(text: str, order: str = LAT_LON) -> ParsedPosition | str:
    """Latitude and longitude from one cell, or the reason it is refused. ``order``
    (LAT_LON or LON_LAT) is that of the column and applies to two plain angles;
    hemisphere letters name their axes and must not contradict it."""
    cleaned = text.strip()
    if _THOUSANDS_PATTERN.fullmatch(cleaned):
        return f"thousands separators: {excerpt(text)}"
    plain_pair = _PLAIN_PAIR_PATTERN.fullmatch(cleaned)
    if plain_pair is not None:
        if _LONE_DECIMAL_COMMA_PATTERN.fullmatch(cleaned):
            return (
                "ambiguous position (one number with a decimal comma or two whole"
                f" degrees): {excerpt(text)}"
            )
        # "51 26.166" is 51° 26.166' as well (the guard of _angle_pair); only whole
        # degrees can have minutes after them.
        if plain_pair[1].lstrip("+-").isdigit():
            single_angle = _angle(_normalised(cleaned))
            if isinstance(single_angle, _Angle) and single_angle.bare_parts:
                return f"{BARE_PARTS_REASON}: {excerpt(text)}"
        first, second = float(plain_pair[1]), float(plain_pair[2])
        if order == LON_LAT:
            return _checked_position(second, first, DECIMAL_DEGREES + LONGITUDE_FIRST_MARK)
        return _checked_position(first, second, DECIMAL_DEGREES)
    if not cleaned:
        return "empty position"
    normalised = _normalised(cleaned)
    if normalised[0] in "([" and normalised[-1] == {"(": ")", "[": "]"}[normalised[0]]:
        normalised = normalised[1:-1].strip()
    upper_text = normalised.translate(_UPPER_CASE)
    if upper_text.startswith("POINT"):
        wkt_point = _WKT_POINT_PATTERN.fullmatch(upper_text)
        if wkt_point is None:
            return f"unparsable WKT point: {excerpt(text)}"
        return _checked_position(float(wkt_point[2]), float(wkt_point[1]), WKT_POINT)
    if upper_text.startswith("GEO:"):
        return _geo_uri_position(upper_text, text)
    if upper_text.startswith(("HTTP://", "HTTPS://")):
        return _map_url_position(cleaned)
    if _UTM_SHAPE_PATTERN.match(upper_text) or _MGRS_SHAPE_PATTERN.match(upper_text):
        grid_parser = parse_utm if _UTM_SHAPE_PATTERN.match(upper_text) else parse_mgrs
        grid_position = grid_parser(upper_text)
        if isinstance(grid_position, str):
            return f"{grid_position}: {excerpt(text)}"
        return ParsedPosition(
            grid_position.latitude,
            grid_position.longitude,
            grid_position.notation,
            grid_position.remark,
        )
    return _angle_pair(normalised, text, order)


def _parse_single_axis(text: str, axis: str) -> ParsedAngle | str:
    cleaned = text.strip()
    if _PLAIN_DECIMAL_PATTERN.fullmatch(cleaned):
        degrees = float(cleaned)
        return _range_problem(degrees, axis) or ParsedAngle(degrees, DECIMAL_DEGREES)
    if not cleaned:
        return f"empty {axis}"
    normalised = _normalised(cleaned)
    angle = _angle(normalised)
    if angle is None:
        return f"{_no_angle_reason(normalised, axis)}: {excerpt(text)}"
    if isinstance(angle, str):
        return f"{angle}: {excerpt(text)}"
    if angle.axis is not None and angle.axis != axis:
        return f"{angle.axis} letter in the {axis}: {excerpt(text)}"
    return _range_problem(angle.degrees, axis) or ParsedAngle(angle.degrees, angle.notation)


def _normalised(text: str) -> str:
    """One space between words, one character per sign, '' as seconds; case is kept."""
    unified = " ".join(text.translate(_CHARACTER_EQUIVALENTS).split())
    return unified.replace("''", '"')


def _no_angle_reason(normalised: str, axis: str) -> str:
    if _DETACHED_SIGN_PATTERN.match(normalised):
        return f"unparsable {axis} (the sign must stand directly before the number)"
    if _INNER_SIGN_PATTERN.search(normalised):
        return f"unparsable {axis} (sign inside the angle)"
    return f"unparsable {axis}"


def _angle(normalised: str) -> _Angle | str | None:
    """The angle of a normalised text, a refusal reason, or None when it is no angle."""
    upper_text = normalised.translate(_UPPER_CASE)
    match = _SIGN_STYLE_PATTERN.fullmatch(upper_text)
    letter_style = match is None
    if match is None:
        match = _LETTER_STYLE_PATTERN.fullmatch(upper_text)
    if match is None:
        return None
    prefix, suffix, sign = match["prefix"], match["suffix"], match["sign"]
    if prefix and suffix:
        return None
    if suffix == "O" and upper_text[match.start("suffix") - 1].isdigit():
        return None  # "6O" could be sixty
    letter = prefix or suffix
    if letter and sign:
        return f"sign and hemisphere letter {letter} together (write one of them)"
    if letter_style:
        if not letter and not sign and normalised[match.end("seconds_unit") - 1 :] == "S":
            return "ambiguous final S (seconds or south)"
    elif suffix == "S" and _may_mean_seconds(normalised, match):
        return "ambiguous s (seconds or south)"
    minutes_text, seconds_text = match["minutes"], match["seconds"]
    if seconds_text is not None:
        parts, notation = [match["degrees"], minutes_text, seconds_text], DEGREES_MINUTES_SECONDS
    elif minutes_text is not None:
        parts, notation = [match["degrees"], minutes_text], DEGREES_DECIMAL_MINUTES
    else:
        parts = [match["degrees"]]
        notation = HEMISPHERE_DECIMAL_DEGREES if letter else DECIMAL_DEGREES
    if any("." in part or "," in part for part in parts[:-1]):
        return "only the last part of an angle may have a fraction, the others must be whole"
    values = [float(part.replace(",", ".")) for part in parts]
    if len(values) > 1 and values[1] >= 60:
        return "minutes of 60 or more"
    if len(values) > 2 and values[2] >= 60:
        return "seconds of 60 or more"
    degrees = values[0]
    if len(values) > 1:
        degrees += values[1] / 60
    if len(values) > 2:
        degrees += values[2] / 3600
    negative = letter in _NEGATIVE_LETTERS if letter else sign == "-"
    return _Angle(
        degrees=-degrees if negative else degrees,
        notation=notation,
        axis=_AXIS_BY_LETTER[letter] if letter else None,
        bare_parts=len(parts) > 1
        and not letter
        and not letter_style
        and not any(unit_sign in upper_text for unit_sign in _UNIT_SIGNS),
    )


def _may_mean_seconds(normalised: str, match: re.Match[str]) -> bool:
    """A trailing S/s in the sign style. It is south only where it cannot be the unit
    of seconds: a capital S after a space or after the closing seconds mark; a small s
    only after a space, and with seconds only after their closing mark as well."""
    suffix_start = match.start("suffix")
    after_space = normalised[suffix_start - 1] == " "
    open_seconds = match["seconds"] is not None and match["seconds_mark"] is None
    if normalised[suffix_start] == "s":
        return not after_space or open_seconds
    return open_seconds and not after_space


def _angle_pair(normalised: str, original_text: str, order: str) -> ParsedPosition | str:
    """Try every separator as the border between the two angles; all readings must
    give the same position. A comma between two digits is a decimal comma as soon as the
    text separates anywhere else ("51,43 6,90"); alone it may separate ("51.4,6.9")."""
    if _EXPONENT_LIKE_PATTERN.search(normalised.translate(_UPPER_CASE)):
        return (
            f"unparsable position (looks like a number with an exponent): {excerpt(original_text)}"
        )
    separators = list(_SEPARATOR_PATTERN.finditer(normalised))
    true_separators = [
        separator for separator in separators if not _between_digits(normalised, separator)
    ]
    positions: dict[tuple[float, float], tuple[str, str | None]] = {}
    refusal: str | None = None
    for separator in true_separators or separators:
        first = _angle(normalised[: separator.start()])
        second = _angle(normalised[separator.end() :])
        if isinstance(first, _Angle) and isinstance(second, _Angle):
            if first.bare_parts or second.bare_parts:
                # "5 1.4361, 6.9": a stray space would pass as degrees and minutes.
                refusal = BARE_PARTS_REASON
                continue
            resolved = _resolved_pair(first, second, order)
            if isinstance(resolved, str):
                refusal = resolved
            else:
                positions[resolved[0], resolved[1]] = resolved[2:]
        elif first is not None and second is not None:
            refusal = next(angle for angle in (first, second) if isinstance(angle, str))
    if len(positions) > 1:
        return f"ambiguous position (more than one reading): {excerpt(original_text)}"
    if not positions:
        return f"{refusal or 'unparsable position'}: {excerpt(original_text)}"
    (((latitude, longitude), (notation, order_problem)),) = positions.items()
    if order_problem is not None:
        return f"{order_problem}: {excerpt(original_text)}"
    return _checked_position(latitude, longitude, notation)


def _between_digits(text: str, separator: re.Match[str]) -> bool:
    return (
        separator.group() == ","
        and text[separator.start() - 1 : separator.start()].isdigit()
        and text[separator.end() : separator.end() + 1].isdigit()
    )


def _resolved_pair(
    first: _Angle, second: _Angle, order: str
) -> tuple[float, float, str, str | None] | str:
    """Latitude, longitude, notation of two angles in text order and, when hemisphere
    letters contradict the column's order, that problem (it must not decide between
    readings); or the reason the two are no position."""
    if first.axis is not None and first.axis == second.axis:
        return f"two {first.axis}s in one position"
    notation = (
        first.notation
        if first.notation == second.notation
        else f"{first.notation} / {second.notation}"
    )
    if first.axis is None and second.axis is None:
        if order == LON_LAT:
            return second.degrees, first.degrees, notation + LONGITUDE_FIRST_MARK, None
        return first.degrees, second.degrees, notation, None
    written_order = LON_LAT if first.axis == LONGITUDE or second.axis == LATITUDE else LAT_LON
    order_problem = None
    if written_order != order:
        order_problem = (
            f"hemisphere letters give the order {POSITION_ORDER_LABELS[written_order]},"
            f" the column is read as {POSITION_ORDER_LABELS[order]}"
        )
    if written_order == LON_LAT:
        return second.degrees, first.degrees, notation, order_problem
    return first.degrees, second.degrees, notation, order_problem


def _geo_uri_position(upper_text: str, original_text: str) -> ParsedPosition | str:
    match = _GEO_URI_PATTERN.fullmatch(upper_text)
    if match is None:
        return f"unparsable geo URI: {excerpt(original_text)}"
    for parameter in match[3].split(";"):
        if parameter.startswith("CRS=") and parameter != "CRS=WGS84":
            return f"geo URI in another reference system (crs): {excerpt(original_text)}"
    return _checked_position(float(match[1]), float(match[2]), GEO_URI)


def _map_url_position(url: str) -> ParsedPosition | str:
    """Only parameters that name a marked position; a view centre is not a position."""
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return f"unparsable map URL: {excerpt(url)}"
    if any(name in query for name in _OTHER_POSITION_PARAMETERS):
        return f"map URL with more than one position: {excerpt(url)}"
    pair_values = query.get("q", []) + query.get("query", [])
    marker_latitudes, marker_longitudes = query.get("mlat", []), query.get("mlon", [])
    if len(pair_values) + max(len(marker_latitudes), len(marker_longitudes)) > 1:
        return f"map URL with more than one position: {excerpt(url)}"
    if pair_values:
        pair = _PLAIN_PAIR_PATTERN.fullmatch(pair_values[0].strip())
        if pair is not None and "," in pair_values[0]:
            return _checked_position(float(pair[1]), float(pair[2]), MAP_URL)
    elif marker_latitudes and marker_longitudes:
        latitude_text, longitude_text = marker_latitudes[0].strip(), marker_longitudes[0].strip()
        if _PLAIN_DECIMAL_PATTERN.fullmatch(latitude_text) and _PLAIN_DECIMAL_PATTERN.fullmatch(
            longitude_text
        ):
            return _checked_position(float(latitude_text), float(longitude_text), MAP_URL)
    elif _VIEW_CENTRE_PATTERN.search(url.upper()):
        return f"map URL shows a map view, not a marked position: {excerpt(url)}"
    return f"unparsable map URL: {excerpt(url)}"


def _checked_position(latitude: float, longitude: float, notation: str) -> ParsedPosition | str:
    problem = _range_problem(latitude, LATITUDE) or _range_problem(longitude, LONGITUDE)
    return problem or ParsedPosition(latitude, longitude, notation)


def _range_problem(degrees: float, axis: str) -> str | None:
    limit = _AXIS_LIMIT_DEGREES[axis]
    # Written so that NaN and infinity fail too.
    return None if -limit <= degrees <= limit else f"{axis} out of range: {degrees}"
