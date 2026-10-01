# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Recognise a timestamp per cell ("automatic" time format of the CSV reader).

Only forms with a single reading are accepted:
- year first: 2026-09-21T14:03:11, 2026/09/21 14:03, 20260921T140311, fraction with
  point or comma
- month names, English or German: 21 Sep 2026 14:03, 21. September 2026 14:03:11,
  September 21, 2026 2:03 PM, with an optional weekday in front
- day and month as numbers (21.09.2026, 09/21/2026, 21-09-2026): only in the order chosen
  for the file and for that separator mark (``date_orders``: dotted dates say nothing
  about slashed ones); without one such a cell is refused, and a cell that does not fit
  the order is refused, never re-read the other way round
- Unix time as plain digits, the unit by magnitude: seconds, milliseconds or microseconds
  that fall between 1990 and 2100; anything else is refused
- 12-hour clock with AM/PM, "Uhr" after the time
- zone: Z, +02:00, +0200, +02, UTC+2, GMT+2, or an abbreviation from ZONE_ABBREVIATIONS
  (only ones with a single meaning; EST, CST, IST, BST and the like are refused). An
  offset followed by an abbreviation ("+0200 CEST") is read by its offset; a listed
  abbreviation must agree with it, and with the date: CET/MEZ/CEST/MESZ are checked
  against the rules of Europe/Berlin, WET/WEST against Europe/Lisbon, EET/EEST against
  Europe/Helsinki ("CET" on a summer date is refused; in the repeated hour the
  abbreviation says which one is meant). Offsets: at most 14 hours, minutes 00/15/30/45.
Refused: a date without time of day, two-digit years, a weekday that is not the date's,
texts with anything left over, and any time outside 1990-01-01 to 2099-12-31 (the window
all readers share, PLAUSIBLE_TIME_RANGE_UTC; 0001-01-01 is a typical default value).
A time-of-day cell of its own (parse_time_of_day, for a date column paired with a time
column) holds the clock time of these forms, with or without a zone: 9:30, 09:30:00,
2:03 PM, 14:03 Uhr. Hours and minutes joined by a dot ("9.30", "09.30 Uhr", the older
German written form) are read as well, but only as two parts: "09.30.15" could be a date
with a two-digit year and is refused.
Excel serial dates are never recognised here; excel_serial_wall_time is used only when
the mapping names that format.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from geosnap.extraction.reader_support import (
    NUMBER_TOO_LONG_REASON,
    PLAUSIBLE_TIME_RANGE_UTC,
    bounded_int,
    excerpt,
    implausible_time_reason,
    utc_offset_allowed,
)

DAY_FIRST = "day_first"
MONTH_FIRST = "month_first"
DATE_ORDER_LABELS = {DAY_FIRST: "day first (21.09. / 21/09)", MONTH_FIRST: "month first (09/21)"}
DATE_ORDER_NOT_CHOSEN_REASON = "day/month order not chosen"
# Separator marks of dates with day and month as numbers; each has its own order.
DATE_MARKS = (".", "/", "-")
# Evidence from fewer than this share of the rows that need an order may be a typing error.
WEAK_EVIDENCE_SHARE = 0.05

# Offsets in minutes. Only abbreviations that name one offset worldwide.
ZONE_ABBREVIATIONS = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "WET": 0,
    "WEST": 60,
    "CET": 60,
    "MEZ": 60,
    "CEST": 120,
    "MESZ": 120,
    "EET": 120,
    "EEST": 180,
}
# The zone whose rules say when an abbreviation applies (none for UTC, GMT, Z).
ZONE_OF_ABBREVIATION = {
    "WET": "Europe/Lisbon",
    "WEST": "Europe/Lisbon",
    "CET": "Europe/Berlin",
    "MEZ": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "MESZ": "Europe/Berlin",
    "EET": "Europe/Helsinki",
    "EEST": "Europe/Helsinki",
}
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PLAUSIBLE_UNIX_SECONDS = (
    int(PLAUSIBLE_TIME_RANGE_UTC[0].timestamp()),
    int(PLAUSIBLE_TIME_RANGE_UTC[1].timestamp()),
)
UNIX_UNITS_PER_SECOND = (1, 1_000, 1_000_000)
EXCEL_EPOCH = datetime(1899, 12, 30)
# Serial 60 is 29 February 1900, a day that did not exist; below it Excel is a day off.
EXCEL_SERIAL_MIN = 61
EXCEL_SERIAL_MAX = 2_958_465  # 31 December 9999

_MONTH_NAMES = (
    "JANUARY JANUAR JAN JÄNNER",
    "FEBRUARY FEBRUAR FEB",
    "MARCH MÄRZ MAR MÄR MRZ MAERZ",
    "APRIL APR",
    "MAY MAI",
    "JUNE JUNI JUN",
    "JULY JULI JUL",
    "AUGUST AUG",
    "SEPTEMBER SEP SEPT",
    "OCTOBER OKTOBER OCT OKT",
    "NOVEMBER NOV",
    "DECEMBER DEZEMBER DEC DEZ",
)
_MONTH_BY_NAME = {
    name: month_number
    for month_number, names in enumerate(_MONTH_NAMES, start=1)
    for name in names.split()
}
_WEEKDAY_NAMES = (
    "MONDAY MON MONTAG MO",
    "TUESDAY TUE DIENSTAG DI",
    "WEDNESDAY WED MITTWOCH MI",
    "THURSDAY THU DONNERSTAG DO",
    "FRIDAY FRI FREITAG FR",
    "SATURDAY SAT SAMSTAG SONNABEND SA",
    "SUNDAY SUN SONNTAG SO",
)
_WEEKDAY_BY_NAME = {
    name: weekday for weekday, names in enumerate(_WEEKDAY_NAMES) for name in names.split()
}
_WEEKDAY = (
    "(?:(?P<weekday>" + "|".join(sorted(_WEEKDAY_BY_NAME, key=len, reverse=True)) + r")\.?,?\s+)"
)
_TIME = (
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:[.,](?P<fraction>\d+))?)?"
    r"(?:\s*(?P<meridiem>[AP])\.?M\.?)?(?:\s*UHR)?"
)
_ZONE = (
    r"(?:\s*(?P<offset>[+-]\d{2}(?::?\d{2})?)(?:\s+\(?(?P<offset_name>[A-Z]{1,5})\)?)?"
    r"|\s*(?:UTC|GMT)\s*(?P<named_offset>[+-]\d{1,2}(?::?\d{2})?)"
    r"|\s*\(?(?P<abbreviation>[A-Z]{1,5})\)?)?"
)
_DATE_TIME_SEPARATOR = r"(?:T|,?\s+)"
_YEAR_FIRST_PATTERN = re.compile(
    r"(?P<year>\d{4})(?P<mark>[-/.])(?P<month>\d{1,2})(?P=mark)(?P<day>\d{1,2})"
    rf"(?:{_DATE_TIME_SEPARATOR}{_TIME}{_ZONE})?",
    re.ASCII,
)
_COMPACT_PATTERN = re.compile(
    r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})T"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?:[.,](?P<fraction>\d+))?"
    rf"(?P<meridiem>(?!))?{_ZONE}",
    re.ASCII,
)
_NUMBERED_PATTERN = re.compile(
    r"(?P<first>\d{1,2})(?P<mark>[-/.])(?P<second_number>\d{1,2})(?P=mark)(?P<year>\d{4}|\d{2})"
    rf"(?:{_DATE_TIME_SEPARATOR}{_TIME}{_ZONE})?",
    re.ASCII,
)
_DAY_MONTH_NAME_PATTERN = re.compile(
    rf"{_WEEKDAY}?(?P<day>\d{{1,2}})\.?[\s-]+(?P<month_name>[A-ZÄ]+)\.?[\s-]+(?P<year>\d{{4}})"
    rf"(?:{_DATE_TIME_SEPARATOR}{_TIME}{_ZONE})?"
)
_MONTH_NAME_DAY_PATTERN = re.compile(
    rf"{_WEEKDAY}?(?P<month_name>[A-ZÄ]+)\.?\s+(?P<day>\d{{1,2}})(?:ST|ND|RD|TH)?,?\s+"
    rf"(?P<year>\d{{4}})(?:{_DATE_TIME_SEPARATOR}{_TIME}{_ZONE})?"
)
# The commonest form, read without the general patterns (a 1 M-row file is mostly this).
_PLAIN_ISO_PATTERN = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z)?", re.ASCII
)
_TIME_OF_DAY_PATTERN = re.compile(rf"{_TIME}{_ZONE}", re.ASCII)
_DOTTED_TIME_OF_DAY_PATTERN = re.compile(r"(\d{1,2})\.(\d{2})(\s*UHR)?", re.ASCII)
_UNIX_PATTERN = re.compile(r"(\d+)(?:\.(\d+))?", re.ASCII)
_EXCEL_SERIAL_PATTERN = re.compile(r"\d+(?:[.,]\d+)?", re.ASCII)
_OFFSET_PATTERN = re.compile(r"([+-])(\d{1,2})(?::?(\d{2}))?", re.ASCII)


@dataclass(frozen=True, slots=True)
class RecognisedTime:
    """``moment`` is the UTC instant when the text carried its zone (``zoned``), else
    the wall-clock time without zone."""

    moment: datetime
    zoned: bool


@dataclass(frozen=True, slots=True)
class DateOrderEvidence:
    """What the sampled dates written with one mark say about their day/month order."""

    order: str
    rows_needing_order: int
    rows_with_evidence: int

    @property
    def weak(self) -> bool:
        """An order concluded from very few rows: one mistyped date could have made it."""
        return bool(self.order) and (
            self.rows_with_evidence < WEAK_EVIDENCE_SHARE * self.rows_needing_order
        )


def recognise_timestamp(text: str, date_orders: Mapping[str, str]) -> RecognisedTime | str:
    """The time a cell states, or the reason it is refused. ``date_orders`` gives
    DAY_FIRST or MONTH_FIRST per separator mark (DATE_MARKS) for dates with day and month
    as numbers; a mark without an entry has no order chosen. A time outside
    PLAUSIBLE_TIME_RANGE_UTC is refused (a wall time without zone compared as UTC)."""
    recognised = _recognised_timestamp(text, date_orders)
    if isinstance(recognised, str):
        return recognised
    return implausible_time_reason(recognised.moment, text) or recognised


def _recognised_timestamp(text: str, date_orders: Mapping[str, str]) -> RecognisedTime | str:
    plain_iso = _PLAIN_ISO_PATTERN.fullmatch(text)
    if plain_iso is not None:
        year, month, day, hour, minute, second, fraction, utc_mark = plain_iso.groups()
        try:
            moment = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second),
                int(fraction.ljust(6, "0")) if fraction else 0,
                UTC if utc_mark else None,
            )
        except ValueError:
            return f"unparsable timestamp: {excerpt(text)}"
        return RecognisedTime(moment, zoned=utc_mark is not None)
    cleaned = " ".join(text.split())
    if not cleaned:
        return "empty timestamp"
    if cleaned[0].isdigit() and _UNIX_PATTERN.fullmatch(cleaned):
        return _unix_time(cleaned)
    upper_text = cleaned.upper()
    for pattern in (_YEAR_FIRST_PATTERN, _COMPACT_PATTERN, _NUMBERED_PATTERN):
        match = pattern.fullmatch(upper_text)
        if match is not None:
            break
    else:
        match = _DAY_MONTH_NAME_PATTERN.fullmatch(upper_text) or _MONTH_NAME_DAY_PATTERN.fullmatch(
            upper_text
        )
    if (
        match is None
        or not cleaned.isascii()
        and any(character.isdigit() and not character.isascii() for character in cleaned)
    ):
        return f"unparsable timestamp: {excerpt(text)}"
    groups = match.groupdict()
    if groups["hour"] is None:
        return f"timestamp with no time of day: {excerpt(text)}"
    if len(groups["year"]) == 2:
        return f"two-digit year: {excerpt(text)}"
    if "month_name" in groups:
        month = _MONTH_BY_NAME.get(groups["month_name"])
        if month is None:
            return f"unparsable timestamp: {excerpt(text)}"
        day = int(groups["day"])
    elif "first" in groups:
        first, second = int(groups["first"]), int(groups["second_number"])
        mark = groups["mark"] or ""
        date_order = date_orders.get(mark, "")
        if first != second and date_order not in (DAY_FIRST, MONTH_FIRST):
            return (
                f"{DATE_ORDER_NOT_CHOSEN_REASON} for dates written with '{mark}': {excerpt(text)}"
            )
        day, month = (second, first) if date_order == MONTH_FIRST else (first, second)
        if month > 12:
            order_label = DATE_ORDER_LABELS[date_order]
            return f"date does not fit the order of the file, {order_label}: {excerpt(text)}"
    else:
        month, day = int(groups["month"]), int(groups["day"])
    hour = int(groups["hour"])
    if groups["meridiem"]:
        if not 1 <= hour <= 12:
            return f"hour outside the 12-hour clock: {excerpt(text)}"
        hour = hour % 12 + (12 if groups["meridiem"] == "P" else 0)
    microsecond = int((groups["fraction"] or "").ljust(6, "0")[:6])
    try:
        wall_time = datetime(
            int(groups["year"]),
            month,
            day,
            hour,
            int(groups["minute"]),
            int(groups["second"] or 0),
            microsecond,
        )
    except ValueError:
        return f"unparsable timestamp: {excerpt(text)}"
    # Before the zone is applied: near year 1 or 9999 the conversion itself would fail.
    implausible = implausible_time_reason(wall_time, text)
    if implausible is not None:
        return implausible
    weekday_name = groups.get("weekday")
    if weekday_name and _WEEKDAY_BY_NAME[weekday_name] != wall_time.weekday():
        return f"weekday does not fit the date: {excerpt(text)}"
    offset_minutes = _offset_minutes(groups, wall_time)
    if isinstance(offset_minutes, str):
        return f"{offset_minutes}: {excerpt(text)}"
    if offset_minutes is None:
        return RecognisedTime(wall_time, zoned=False)
    try:
        instant = wall_time.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
        return RecognisedTime(instant.astimezone(UTC), zoned=True)
    except (OverflowError, ValueError):
        return f"unparsable timestamp: {excerpt(text)}"


def time_of_day_text(text: str) -> str:
    """A time-of-day cell as the timestamp forms read it: whitespace collapsed, and hours
    and minutes joined by a dot ("9.30", "09.30 Uhr") written with a colon."""
    cleaned = " ".join(text.split())
    dotted = _DOTTED_TIME_OF_DAY_PATTERN.fullmatch(cleaned.upper())
    if dotted is None:
        return cleaned
    return f"{dotted[1]}:{dotted[2]}" + (" Uhr" if dotted[3] else "")


def parse_time_of_day(text: str) -> time | str:
    """The clock time in a cell of its own (9:30, 09:30:00, 2:03 PM, 14:03 Uhr, 09.30,
    a zone suffix allowed), or the reason it is refused."""
    cleaned = time_of_day_text(text).upper()
    if not cleaned:
        return "empty time of day"
    match = _TIME_OF_DAY_PATTERN.fullmatch(cleaned)
    if match is None:
        return f"unparsable time of day: {excerpt(text)}"
    hour = int(match["hour"])
    if match["meridiem"]:
        if not 1 <= hour <= 12:
            return f"hour outside the 12-hour clock: {excerpt(text)}"
        hour = hour % 12 + (12 if match["meridiem"] == "P" else 0)
    microsecond = int((match["fraction"] or "").ljust(6, "0")[:6])
    try:
        return time(hour, int(match["minute"]), int(match["second"] or 0), microsecond)
    except ValueError:
        return f"unparsable time of day: {excerpt(text)}"


def needs_date_order(text: str) -> str | None:
    """The separator mark of a date with day and month as different numbers
    (03/04/2026 ...), whose order has to be chosen; None for every other text."""
    match = _NUMBERED_PATTERN.fullmatch(" ".join(text.split()).upper())
    if match is None or match["first"] == match["second_number"]:
        return None
    return match["mark"]


def date_order_evidence(samples: Iterable[str]) -> dict[str, DateOrderEvidence]:
    """Per separator mark that occurs with different day and month numbers: DAY_FIRST or
    MONTH_FIRST when a number above 12 stands in one place in at least one sample and in
    the other place in none; "" without evidence or with both."""
    orders_seen: dict[str, set[str]] = {}
    rows_needing_order: dict[str, int] = {}
    rows_with_evidence: dict[str, int] = {}
    for sample in samples:
        match = _NUMBERED_PATTERN.fullmatch(" ".join(sample.split()).upper())
        if match is None or match["first"] == match["second_number"]:
            continue
        mark = match["mark"]
        rows_needing_order[mark] = rows_needing_order.get(mark, 0) + 1
        seen = orders_seen.setdefault(mark, set())
        if int(match["first"]) > 12:
            seen.add(DAY_FIRST)
        if int(match["second_number"]) > 12:
            seen.add(MONTH_FIRST)
        if int(match["first"]) > 12 or int(match["second_number"]) > 12:
            rows_with_evidence[mark] = rows_with_evidence.get(mark, 0) + 1
    return {
        mark: DateOrderEvidence(
            order=next(iter(orders_seen[mark])) if len(orders_seen[mark]) == 1 else "",
            rows_needing_order=count,
            rows_with_evidence=rows_with_evidence.get(mark, 0),
        )
        for mark, count in rows_needing_order.items()
    }


def excel_serial_wall_time(text: str) -> datetime | str:
    """Wall-clock time of an Excel serial date (1900 date system, days since 1899-12-30),
    rounded to the millisecond; the 1904 system cannot be told apart and is not read."""
    cleaned = text.strip()
    if not _EXCEL_SERIAL_PATTERN.fullmatch(cleaned):
        return f"unparsable Excel serial date: {excerpt(text)}"
    serial_days = float(cleaned.replace(",", "."))
    if not EXCEL_SERIAL_MIN <= serial_days < EXCEL_SERIAL_MAX + 1:
        return f"Excel serial date out of range: {excerpt(text)}"
    return EXCEL_EPOCH + timedelta(milliseconds=round(serial_days * 86_400_000))


def _unix_time(digits_text: str) -> RecognisedTime | str:
    match = _UNIX_PATTERN.fullmatch(digits_text)
    assert match is not None
    whole_number, fraction = bounded_int(match[1]), match[2]
    if whole_number is None:
        return f"{NUMBER_TOO_LONG_REASON}: {excerpt(digits_text)}"
    earliest, latest = PLAUSIBLE_UNIX_SECONDS
    for units_per_second in UNIX_UNITS_PER_SECOND:
        if earliest * units_per_second <= whole_number < latest * units_per_second:
            if fraction and units_per_second != 1:
                break
            microseconds = whole_number * (1_000_000 // units_per_second)
            microseconds += int((fraction or "").ljust(6, "0")[:6])
            return RecognisedTime(UNIX_EPOCH + timedelta(microseconds=microseconds), zoned=True)
    return (
        "number is not a Unix time in seconds, milliseconds or microseconds between 1990 and"
        f" 2100 (choose the time format yourself): {excerpt(digits_text)}"
    )


def _offset_minutes(groups: dict[str, str | None], wall_time: datetime) -> int | str | None:
    """Minutes east of UTC, None without a zone, or the reason the zone is refused."""
    offset_text = groups["offset"] or groups["named_offset"]
    abbreviation = groups["abbreviation"] or groups["offset_name"]
    listed_minutes = ZONE_ABBREVIATIONS.get(abbreviation) if abbreviation else None
    if abbreviation and listed_minutes is not None:
        date_problem = _abbreviation_date_problem(abbreviation, listed_minutes, wall_time)
        if date_problem:
            return date_problem
    if offset_text is None:
        if abbreviation is None:
            return None
        if listed_minutes is None:
            return f"time zone abbreviation {abbreviation} has no single meaning (use an offset)"
        return listed_minutes
    match = _OFFSET_PATTERN.fullmatch(offset_text)
    assert match is not None
    hours, minutes = int(match[2]), int(match[3] or 0)
    if not utc_offset_allowed(hours, minutes):
        return f"UTC offset out of range: {offset_text}"
    offset_minutes = (hours * 60 + minutes) * (-1 if match[1] == "-" else 1)
    if listed_minutes is not None and listed_minutes != offset_minutes:
        return f"offset {offset_text} and abbreviation {abbreviation} contradict each other"
    return offset_minutes


def _abbreviation_date_problem(
    abbreviation: str, listed_minutes: int, wall_time: datetime
) -> str | None:
    """CET on a summer date and the like: the abbreviation's offset must be one the zone
    it belongs to has at that wall time (either one in the repeated hour)."""
    zone_name = ZONE_OF_ABBREVIATION.get(abbreviation)
    if zone_name is None:
        return None
    # The instant the text names must show this very wall time in that zone; that also
    # refuses wall times the zone skipped (spring) and keeps both of a repeated hour.
    instant = wall_time.replace(tzinfo=timezone(timedelta(minutes=listed_minutes)))
    try:
        wall_time_in_zone = instant.astimezone(ZoneInfo(zone_name)).replace(tzinfo=None)
    except (OverflowError, ValueError):
        return None
    if wall_time_in_zone == wall_time:
        return None
    return (
        f"{abbreviation} written for a time at which {zone_name} has another offset;"
        " write an offset"
    )
