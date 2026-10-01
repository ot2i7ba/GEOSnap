# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Conversion of UTC instants into the configured display time zone, and the name of
the host system's zone."""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import TZPATH, ZoneInfo, available_timezones

from geosnap.windows_zones import WINDOWS_ZONE_NAMES

LOCAL_ZONE_SETTING = "local"
# Where a Unix host records its zone; module attributes so that tests can point elsewhere.
LOCALTIME_LINK = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")
ZONEINFO_ROOTS: tuple[str, ...] = tuple(TZPATH)
WINDOWS_ZONE_REGISTRY_KEY = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
sys_platform = sys.platform
# Zone transitions reach this far beyond the data, so conversions at the edges stay exact.
ZONE_TRANSITION_MARGIN = timedelta(days=2)
# Offsets are sampled at this step and each change is bisected to the second; real zones
# never change twice within it.
TRANSITION_PROBE_SECONDS = 6 * 3600
# The widened span stays a day inside the datetime range, so no offset can overflow it.
EARLIEST_TRANSITION_UTC = datetime.min.replace(tzinfo=UTC) + timedelta(days=1)
LATEST_TRANSITION_UTC = datetime.max.replace(tzinfo=UTC) - timedelta(days=1)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

ZoneTransition = tuple[int, int, str]


def resolve_display_timezone(display: str) -> tzinfo | None:
    """Return the zone for the 'timezone.display' setting. 'local' is the host's zone by
    its IANA name (host_zone_name), so its rules come from the zone database; None (the
    C library's local time) only when the host zone has no such name."""
    if display == LOCAL_ZONE_SETTING:
        host_zone = host_zone_name()
        return ZoneInfo(host_zone) if host_zone is not None else None
    return ZoneInfo(display)


def to_display_time(moment_utc: datetime, display_tz: tzinfo | None) -> datetime:
    """Convert an aware UTC instant; daylight saving is resolved per instant."""
    return moment_utc.astimezone(display_tz)


def display_zone_label(moment_local: datetime, display: str) -> str:
    """Label such as 'Europe/Berlin (CEST)' or 'local (CET)' for CSV and tooltips."""
    abbreviation = moment_local.tzname() or "?"
    return f"{display} ({abbreviation})"


def offset_minutes(moment_utc: datetime, display_tz: tzinfo | None) -> int:
    """Offset of the display zone at this instant in minutes east of UTC."""
    offset = to_display_time(moment_utc, display_tz).utcoffset()
    return 0 if offset is None else round(offset.total_seconds() / 60)


def local_iso_with_offset(moment_utc: datetime, display_tz: tzinfo | None) -> str:
    """Wall-clock time with its offset, e.g. '2026-10-25T02:30:00+02:00' (whole seconds)."""
    return to_display_time(moment_utc, display_tz).isoformat(timespec="seconds")


def _zone_state(epoch_seconds: int, display_tz: tzinfo | None) -> tuple[int, str]:
    # Not datetime.fromtimestamp: Windows refuses negative epochs there.
    moment = UNIX_EPOCH + timedelta(seconds=epoch_seconds)
    return offset_minutes(moment, display_tz), to_display_time(moment, display_tz).tzname() or "?"


def zone_transitions(
    display_tz: tzinfo | None, start_utc: datetime, end_utc: datetime
) -> list[ZoneTransition]:
    """[(utc_epoch_seconds, offset_minutes, abbreviation), ...] over the span plus the margin.

    The first entry holds from the start of the widened span, every further entry from the
    instant of a change of offset or abbreviation. The widened span is clipped to
    EARLIEST_TRANSITION_UTC .. LATEST_TRANSITION_UTC.
    """
    first_moment = max(start_utc, EARLIEST_TRANSITION_UTC + ZONE_TRANSITION_MARGIN)
    last_moment = min(end_utc, LATEST_TRANSITION_UTC - ZONE_TRANSITION_MARGIN)
    first_second = int((first_moment - ZONE_TRANSITION_MARGIN).timestamp())
    last_second = int((last_moment + ZONE_TRANSITION_MARGIN).timestamp())
    state = _zone_state(first_second, display_tz)
    transitions: list[ZoneTransition] = [(first_second, *state)]
    probe = first_second
    while probe < last_second:
        next_probe = min(probe + TRANSITION_PROBE_SECONDS, last_second)
        next_state = _zone_state(next_probe, display_tz)
        if next_state != state:
            unchanged, changed = probe, next_probe
            while changed - unchanged > 1:
                middle = (unchanged + changed) // 2
                if _zone_state(middle, display_tz) == state:
                    unchanged = middle
                else:
                    changed = middle
            transitions.append((changed, *next_state))
            state = next_state
        probe = next_probe
    return transitions


def display_local_text(moment: datetime, display_tz: tzinfo | None) -> str:
    """Wall clock in the display zone with its offset, whole seconds, for reports and the
    search area sheet: '2026-10-25 02:30:00 +02:00'."""
    local = to_display_time(moment, display_tz)
    offset = local.utcoffset() or timedelta(0)
    offset_total = round(offset.total_seconds() / 60)
    sign = "+" if offset_total >= 0 else "-"
    hours, minutes = divmod(abs(offset_total), 60)
    return f"{local:%Y-%m-%d %H:%M:%S} {sign}{hours:02d}:{minutes:02d}"


# Present in some zoneinfo directories but not zones one could record.
NON_ZONE_TZ_VALUES = frozenset({"localtime", "posixrules"})


def host_zone_name() -> str | None:
    """IANA name of the host system's zone, or None when it cannot be determined.

    Probes in order: the TZ variable, the Windows registry (TimeZoneKeyName mapped through
    the CLDR table), the /etc/localtime symlink into a zoneinfo directory, /etc/timezone.
    The name is a suggestion for the examiner: the evidence device may use another zone.
    """
    zone_names = available_timezones()
    tz_variable = os.environ.get("TZ", "").lstrip(":")
    if tz_variable in zone_names and tz_variable not in NON_ZONE_TZ_VALUES:
        return tz_variable
    if tz_variable:
        # A POSIX rule string (UTC0, CET-1CEST,M3.5.0,M10.5.0/3) or a placeholder file
        # name: the process zone is no IANA zone, so nothing behind it is a default.
        return None
    if sys_platform == "win32":
        return _windows_zone_name()
    if LOCALTIME_LINK.is_symlink():
        link_target = LOCALTIME_LINK.resolve()
        for zoneinfo_root in ZONEINFO_ROOTS:
            try:
                candidate = link_target.relative_to(zoneinfo_root).as_posix()
            except ValueError:
                continue
            if candidate in zone_names:
                return candidate
    try:
        recorded_name = TIMEZONE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return recorded_name if recorded_name in zone_names else None


def _windows_zone_name() -> str | None:
    # Windows only; loaded when the probe runs, typed Any because the stub is empty elsewhere.
    winreg: Any = importlib.import_module("winreg")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINDOWS_ZONE_REGISTRY_KEY) as key:
            windows_id, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    return WINDOWS_ZONE_NAMES.get(str(windows_id).rstrip("\0"))
