# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Load and validate the user-editable config.toml."""

from __future__ import annotations

import codecs
import logging
import shutil
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from geosnap.mapping.tile_providers import DEFAULT_TILE_PROVIDER_KEY, TILE_PROVIDER_KEYS
from geosnap.online.place_catalogue import DEFAULT_ON_KEYS, PLACE_CATEGORY_KEYS

logger = logging.getLogger(__name__)

LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TILE_SOURCES = ("online", "none", "local")
LEGACY_TILE_SOURCE_OSM = "osm"
# An older config.toml may list one "railway" category; it stands for a station and a line
# category.
LEGACY_RAILWAY_CATEGORY = "railway"
LEGACY_RAILWAY_REPLACEMENTS = ("railway_station", "railway_line")
MIN_READ_CHUNK_BYTES = 4096
MIN_LOG_FILE_BYTES = 65_536
ALLOWED_CATEGORIES_PREVIEW_COUNT = 5
MIN_OVERPASS_RADIUS_M = 50
MAX_OVERPASS_RADIUS_M = 20_000
MAX_ENCOUNTER_MINUTES = 1440
MAX_ENCOUNTER_DISTANCE_M = 50_000
MIN_JOINT_MOVEMENT_M = 100
MAX_JOINT_MOVEMENT_M = 100_000
MAX_MATRIX_TOLERANCE_M = 3000
# "p95": radii of level 68 or unknown are scaled to 95 %; "reported": radii as reported.
ACCURACY_CONFIDENCES = ("p95", "reported")
# GeoPoint.positioning_method values a config.toml may exclude from the analysis.
POSITIONING_METHODS = ("gnss", "wifi", "cell", "network")


class SettingsError(ValueError):
    """Raised when config.toml is missing, malformed or holds an invalid value."""


@dataclass(frozen=True)
class TimezoneSettings:
    display: str = "local"


@dataclass(frozen=True)
class ExtractionSettings:
    read_chunk_bytes: int = 1_048_576
    fallback_encoding: str = "cp1252"
    remove_duplicates: bool = False


@dataclass(frozen=True)
class MapSettings:
    point_limit: int = 50_000
    tile_source: str = "online"
    tile_provider: str = DEFAULT_TILE_PROVIDER_KEY
    local_tiles_path: str = "tiles"
    local_tiles_attribution: str = ""
    local_tiles_max_zoom: int = 18


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    max_bytes: int = 10_485_760
    backup_count: int = 5


@dataclass(frozen=True)
class AnalysisSettings:
    stop_radius_m: int = 50
    stop_min_minutes: int = 10
    gap_min_minutes: int = 30
    max_accuracy_m: int = 200
    implausible_speed_kmh: int = 250
    encounter_max_minutes: int = 10
    encounter_max_distance_m: int = 100
    encounter_joint_movement_min_m: int = 500
    # Stay centres within this distance form one row of the presence matrix (the shared
    # places of the map keep stop_radius_m).
    matrix_tolerance_m: int = 50
    # "p95": radii of level 68 or unknown are scaled to 95 %; "reported": radii as reported.
    accuracy_confidence: str = "p95"
    # Positioning methods left out of speed, stays and encounters (they stay on the map).
    excluded_positioning_methods: tuple[str, ...] = ("cell",)


# Operator of the default online.overpass_fallback_endpoint, named in warnings and notes.
DEFAULT_OVERPASS_FALLBACK_OPERATOR = "Private.coffee"


@dataclass(frozen=True)
class OnlineSettings:
    enabled: bool = True
    overpass_endpoint: str = "https://overpass-api.de/api/interpreter"
    # Endpoint the map's live "Check surroundings" contacts from the browser; empty = the
    # same as overpass_endpoint. Maps opened from disk send no Referer, which some public
    # instances refuse (overpass-api.de answers HTTP 406).
    overpass_browser_endpoint: str = ""
    # Tried once by a map opened from disk when the endpoint above refuses it (no Referer);
    # empty = no fallback. Listed as a public instance on the OpenStreetMap wiki.
    overpass_fallback_endpoint: str = "https://overpass.private.coffee/api/interpreter"
    overpass_radius_m: int = 250
    overpass_categories: list[str] = field(default_factory=lambda: list(DEFAULT_ON_KEYS))
    overpass_timeout_seconds: int = 60
    nominatim_enabled: bool = True
    nominatim_endpoint: str = "https://nominatim.openstreetmap.org/reverse"
    nominatim_max_lookups: int = 25
    # Address search for case places entered without coordinates (one request per place).
    nominatim_search_endpoint: str = "https://nominatim.openstreetmap.org/search"


@dataclass(frozen=True)
class Settings:
    timezone: TimezoneSettings = field(default_factory=TimezoneSettings)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    map: MapSettings = field(default_factory=MapSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    online: OnlineSettings = field(default_factory=OnlineSettings)


SectionT = TypeVar(
    "SectionT",
    TimezoneSettings,
    ExtractionSettings,
    MapSettings,
    LoggingSettings,
    AnalysisSettings,
    OnlineSettings,
)

SECTION_TYPES: dict[str, type[Any]] = {
    "timezone": TimezoneSettings,
    "extraction": ExtractionSettings,
    "map": MapSettings,
    "logging": LoggingSettings,
    "analysis": AnalysisSettings,
    "online": OnlineSettings,
}


def ensure_config_file(config_path: Path, default_config_path: Path) -> bool:
    """Copy the bundled default configuration into place when none exists.

    Returns True when a file was created.
    """
    if config_path.exists():
        return False
    shutil.copyfile(default_config_path, config_path)
    return True


def load_settings(config_path: Path) -> Settings:
    """Parse config.toml and validate every value; unknown keys are rejected."""
    try:
        with config_path.open("rb") as config_file:
            raw_document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise SettingsError(f"Configuration file not found: {config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(
            f"Configuration file {config_path} is not valid TOML: {error}"
        ) from error
    except UnicodeDecodeError as error:
        raise SettingsError(
            f"Configuration file {config_path} is not UTF-8 text; save it as UTF-8: {error}"
        ) from error

    unknown_sections = set(raw_document) - set(SECTION_TYPES)
    if unknown_sections:
        raise SettingsError(
            f"Unknown configuration section(s): {', '.join(sorted(unknown_sections))}"
        )

    settings = Settings(
        timezone=_build_section(TimezoneSettings, raw_document.get("timezone", {}), "timezone"),
        extraction=_build_section(
            ExtractionSettings, raw_document.get("extraction", {}), "extraction"
        ),
        map=_build_section(MapSettings, raw_document.get("map", {}), "map"),
        logging=_build_section(LoggingSettings, raw_document.get("logging", {}), "logging"),
        analysis=_build_section(AnalysisSettings, raw_document.get("analysis", {}), "analysis"),
        online=_build_section(OnlineSettings, raw_document.get("online", {}), "online"),
    )
    settings = _apply_legacy_tile_source(settings)
    settings = _apply_legacy_railway_category(settings)
    _validate(settings)
    _warn_about_implicit_overpass_fallback(settings, raw_document.get("online", {}))
    return settings


def _warn_about_implicit_overpass_fallback(settings: Settings, raw_online: object) -> None:
    """An older config.toml may lack the key, so the default fallback operator would
    receive checked areas without the user having chosen it."""
    if not settings.online.enabled:
        return
    if isinstance(raw_online, dict) and "overpass_fallback_endpoint" in raw_online:
        return
    logger.warning(
        "config.toml has no online.overpass_fallback_endpoint: when a map opened from disk "
        "has its live place check refused, it sends the check once to the default %s "
        '(operated by %s); add overpass_fallback_endpoint = "" to [online] to turn this off',
        settings.online.overpass_fallback_endpoint,
        DEFAULT_OVERPASS_FALLBACK_OPERATOR,
    )


def _apply_legacy_tile_source(settings: Settings) -> Settings:
    """An older config.toml may hold tile_source = "osm"; it means online tiles from the default
    provider.

    The OpenStreetMap standard server is not offered because it answers browsers
    without a Referer header (maps opened from disk) with a blocked tile.
    """
    if settings.map.tile_source != LEGACY_TILE_SOURCE_OSM:
        return settings
    logger.warning(
        'map.tile_source = "osm" is read as tile_source = "online" with '
        'tile_provider = "%s"; the OpenStreetMap standard server is not offered because it '
        "blocks maps opened from disk (no Referer header)",
        DEFAULT_TILE_PROVIDER_KEY,
    )
    return replace(
        settings,
        map=replace(settings.map, tile_source="online", tile_provider=DEFAULT_TILE_PROVIDER_KEY),
    )


def _apply_legacy_railway_category(settings: Settings) -> Settings:
    """An older config.toml may hold overpass_categories = ["railway"]; it means both railway
    categories.

    Each replacement is inserted at the legacy key's position unless already listed.
    """
    if LEGACY_RAILWAY_CATEGORY not in settings.online.overpass_categories:
        return settings
    logger.warning(
        'online.overpass_categories value "%s" is read as %s',
        LEGACY_RAILWAY_CATEGORY,
        " and ".join(f'"{key}"' for key in LEGACY_RAILWAY_REPLACEMENTS),
    )
    expanded: list[str] = []
    for category in settings.online.overpass_categories:
        if category != LEGACY_RAILWAY_CATEGORY:
            expanded.append(category)
            continue
        expanded.extend(key for key in LEGACY_RAILWAY_REPLACEMENTS if key not in expanded)
    return replace(settings, online=replace(settings.online, overpass_categories=expanded))


def _build_section(
    section_type: type[SectionT], raw_section: object, section_name: str
) -> SectionT:
    if not isinstance(raw_section, dict):
        raise SettingsError(f"Configuration section [{section_name}] must be a table")
    defaults = section_type()
    known_keys = {section_field.name for section_field in fields(section_type)}
    values: dict[str, Any] = {}
    for key, value in raw_section.items():
        if key not in known_keys:
            raise SettingsError(f"Unknown configuration key '{section_name}.{key}'")
        expected_type = type(getattr(defaults, key))
        # TOML has no tuples: a tuple setting is written as an array.
        if expected_type is tuple:
            if type(value) is not list or not all(type(entry) is str for entry in value):
                raise SettingsError(
                    f"Configuration key '{section_name}.{key}' must be a list of methods "
                    'such as ["cell"]'
                )
            # Listing a value twice means the same as once.
            value = tuple(dict.fromkeys(value))
        if type(value) is not expected_type:
            raise SettingsError(
                f"Configuration key '{section_name}.{key}' must be "
                f"{expected_type.__name__}, got {type(value).__name__}"
            )
        values[key] = value
    return section_type(**values)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SettingsError(message)


def _validate(settings: Settings) -> None:
    extraction = settings.extraction
    _require(
        extraction.read_chunk_bytes >= MIN_READ_CHUNK_BYTES,
        f"extraction.read_chunk_bytes must be at least {MIN_READ_CHUNK_BYTES}",
    )
    try:
        codecs.lookup(extraction.fallback_encoding)
    except LookupError as error:
        raise SettingsError(
            f"extraction.fallback_encoding is not a known encoding: {extraction.fallback_encoding}"
        ) from error

    map_settings = settings.map
    _require(map_settings.point_limit >= 2, "map.point_limit must be at least 2")
    _require(
        map_settings.tile_source in TILE_SOURCES,
        f"map.tile_source must be one of {', '.join(TILE_SOURCES)}",
    )
    _require(
        map_settings.tile_provider in TILE_PROVIDER_KEYS,
        f"map.tile_provider must be one of {', '.join(TILE_PROVIDER_KEYS)}",
    )
    _require(map_settings.local_tiles_path.strip() != "", "map.local_tiles_path must not be empty")
    _require(
        1 <= map_settings.local_tiles_max_zoom <= 22,
        "map.local_tiles_max_zoom must be between 1 and 22",
    )

    log_settings = settings.logging
    _require(
        log_settings.level in LOG_LEVEL_NAMES,
        f"logging.level must be one of {', '.join(LOG_LEVEL_NAMES)}",
    )
    _require(
        log_settings.max_bytes >= MIN_LOG_FILE_BYTES,
        f"logging.max_bytes must be at least {MIN_LOG_FILE_BYTES}",
    )
    _require(log_settings.backup_count >= 0, "logging.backup_count must not be negative")

    display_zone = settings.timezone.display
    if display_zone != "local":
        try:
            ZoneInfo(display_zone)
        # OSError: a directory of the zone database such as "Europe" (IsADirectoryError,
        # PermissionError on Windows).
        except (ZoneInfoNotFoundError, ValueError, OSError) as error:
            raise SettingsError(
                f"timezone.display must be 'local' or a valid IANA zone name, got '{display_zone}'"
            ) from error

    analysis = settings.analysis
    _require(analysis.stop_radius_m >= 1, "analysis.stop_radius_m must be at least 1")
    _require(analysis.stop_min_minutes >= 1, "analysis.stop_min_minutes must be at least 1")
    _require(analysis.gap_min_minutes >= 1, "analysis.gap_min_minutes must be at least 1")
    _require(analysis.max_accuracy_m >= 1, "analysis.max_accuracy_m must be at least 1")
    _require(
        analysis.implausible_speed_kmh >= 1, "analysis.implausible_speed_kmh must be at least 1"
    )
    _require(
        1 <= analysis.encounter_max_minutes <= MAX_ENCOUNTER_MINUTES,
        f"analysis.encounter_max_minutes must be between 1 and {MAX_ENCOUNTER_MINUTES}",
    )
    _require(
        1 <= analysis.encounter_max_distance_m <= MAX_ENCOUNTER_DISTANCE_M,
        f"analysis.encounter_max_distance_m must be between 1 and {MAX_ENCOUNTER_DISTANCE_M}",
    )
    _require(
        MIN_JOINT_MOVEMENT_M <= analysis.encounter_joint_movement_min_m <= MAX_JOINT_MOVEMENT_M,
        "analysis.encounter_joint_movement_min_m must be between "
        f"{MIN_JOINT_MOVEMENT_M} and {MAX_JOINT_MOVEMENT_M}",
    )
    _require(
        0 <= analysis.matrix_tolerance_m <= MAX_MATRIX_TOLERANCE_M,
        f"analysis.matrix_tolerance_m must be between 0 and {MAX_MATRIX_TOLERANCE_M}",
    )
    _require(
        analysis.accuracy_confidence in ACCURACY_CONFIDENCES,
        f"analysis.accuracy_confidence must be one of {', '.join(ACCURACY_CONFIDENCES)}",
    )
    unknown_methods = [
        method
        for method in analysis.excluded_positioning_methods
        if method not in POSITIONING_METHODS
    ]
    _require(
        not unknown_methods,
        "analysis.excluded_positioning_methods may only list "
        f"{', '.join(POSITIONING_METHODS)}; got {', '.join(map(str, unknown_methods))}",
    )

    online = settings.online
    for endpoint_key, endpoint in (
        ("online.overpass_endpoint", online.overpass_endpoint),
        ("online.nominatim_endpoint", online.nominatim_endpoint),
        ("online.nominatim_search_endpoint", online.nominatim_search_endpoint),
    ):
        _require(endpoint.startswith("https://"), f"{endpoint_key} must start with https://")
    _require(
        online.overpass_browser_endpoint == ""
        or online.overpass_browser_endpoint.startswith("https://"),
        "online.overpass_browser_endpoint must be empty or start with https://",
    )
    _require(
        online.overpass_fallback_endpoint == ""
        or online.overpass_fallback_endpoint.startswith("https://"),
        "online.overpass_fallback_endpoint must be empty or start with https://",
    )
    _require(
        MIN_OVERPASS_RADIUS_M <= online.overpass_radius_m <= MAX_OVERPASS_RADIUS_M,
        f"online.overpass_radius_m must be between "
        f"{MIN_OVERPASS_RADIUS_M} and {MAX_OVERPASS_RADIUS_M}",
    )
    _require(
        online.overpass_timeout_seconds >= 1, "online.overpass_timeout_seconds must be at least 1"
    )
    _require(online.nominatim_max_lookups >= 0, "online.nominatim_max_lookups must not be negative")
    _require(
        all(isinstance(category, str) for category in online.overpass_categories),
        "online.overpass_categories must be a list of strings",
    )
    unknown_categories = [
        category for category in online.overpass_categories if category not in PLACE_CATEGORY_KEYS
    ]
    allowed_preview = ", ".join(PLACE_CATEGORY_KEYS[:ALLOWED_CATEGORIES_PREVIEW_COUNT])
    _require(
        not unknown_categories,
        "online.overpass_categories contains unknown value(s): "
        f"{', '.join(unknown_categories)}; allowed: {allowed_preview}, ... "
        f"({len(PLACE_CATEGORY_KEYS)} keys, listed in config.toml)",
    )
    duplicate_categories = list(
        dict.fromkeys(
            category
            for index, category in enumerate(online.overpass_categories)
            if category in online.overpass_categories[:index]
        )
    )
    _require(
        not duplicate_categories,
        "online.overpass_categories contains duplicate value(s): "
        f"{', '.join(duplicate_categories)}",
    )
