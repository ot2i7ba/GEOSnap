# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Render the single-file, offline Leaflet map for one project."""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import urlsplit

from geosnap import APP_NAME, __version__
from geosnap.analysis.segments import SourceSteps
from geosnap.extraction.duplicates import AnnotatedPoint
from geosnap.extraction.models import (
    ACCURACY_REPORTED_BY_ALL,
    GeoPoint,
    ReferencePlace,
    SourceDescriptor,
    record_noun,
)
from geosnap.mapping.tile_providers import (
    all_tile_origins,
    tile_provider_by_key,
    tile_providers_document,
)
from geosnap.online.overpass import OUTPUT_LIMIT, Place
from geosnap.online.place_catalogue import catalogue_document
from geosnap.runtime_paths import bundled_resource
from geosnap.settings import MapSettings
from geosnap.timezones import (
    ZoneTransition,
    display_zone_label,
    offset_minutes,
    to_display_time,
    zone_transitions,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = "geosnap/mapping/template"
VENDOR_DIR = "geosnap/mapping/vendor"
LICENCE_LINES = (
    "Copyright (c) 2026 ot2i7ba",
    "https://github.com/ot2i7ba/",
    "This code is licensed under the MIT License (see LICENSE for details).",
)
TEMPLATE_LICENCE_HEADERS = (
    "".join(f"// {line}\n" for line in LICENCE_LINES) + "\n",
    "/*\n" + "".join(f" * {line}\n" for line in LICENCE_LINES) + " */\n\n",
)
CSP_POLICY_OFFLINE = (
    "default-src 'none'; img-src data: file: blob:; "
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:"
)
LOCAL_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
# The crystal ball converts reference times after the data (default: now) with the same
# transitions, so they reach a year past generation.
REFERENCE_TIME_HORIZON = timedelta(days=366)
TEMPLATE_TOKEN = re.compile(r"__[A-Z_]+__")
# Emoji per source kind; text-default code points carry U+FE0F for emoji presentation.
SOURCE_KIND_ICONS = {
    "smartphone": "\U0001f4f1",
    "computer": "\U0001f5a5\ufe0f",
    "laptop": "\U0001f4bb",
    "tablet": "\U0001f4df",
    "watch": "\u231a",
    "vehicle": "\U0001f697",
    "other": "\U0001f4cd",
}


def csp_policy_for(
    tile_source: str,
    overpass_endpoint: str | None = None,
    overpass_fallback_endpoint: str | None = None,
) -> str:
    """Content-Security-Policy: offline by default; online tiles add the origins of every
    built-in provider (the style is switchable in the browser), live search adds the connect
    origin of its endpoint and of the fallback a map opened from disk may use. 'self' lets a
    map served by GEOSnap record a search area on its own loopback server; a
    map opened from disk has no origin that 'self' could reach."""
    policy = CSP_POLICY_OFFLINE
    if tile_source == "online":
        policy = policy.replace(
            "img-src data: file: blob:",
            "img-src data: file: blob: " + " ".join(all_tile_origins()),
        )
    origins: list[str] = []
    if overpass_endpoint:
        for endpoint in (overpass_endpoint, overpass_fallback_endpoint):
            if not endpoint:
                continue
            parts = urlsplit(endpoint)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in origins:
                origins.append(origin)
    origins.append("'self'")
    return f"{policy}; connect-src {' '.join(origins)}"


@dataclass(frozen=True, slots=True)
class MapSource:
    """A source as the map presents it: map points before thinning, time span, digest and
    the stride its points were thinned with; the accepted dated and undated record counts
    and the days with data feed the capabilities."""

    descriptor: SourceDescriptor
    points_total: int
    first_utc: datetime | None
    last_utc: datetime | None
    # extraction.models.accuracy_reporting: "all", "some" or "none".
    accuracy_reporting: str
    sha256: str | None
    thinning_stride: int = 1
    # Accepted records with a timestamp (before duplicate collapse) and without one.
    dated_records: int = 0
    undated_records: int = 0
    undated_thinning_stride: int = 1
    # Distinct local dates (display zone, ISO) of the dated records, sorted.
    data_days: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MapRenderContext:
    project_name: str
    generated_at_local: datetime
    display_zone: str
    tile_source: str
    tiles_url_template: str | None
    tiles_attribution: str
    tiles_max_zoom: int
    total_points: int
    thinning_stride: int
    online_enabled: bool = False
    overpass_endpoint: str | None = None
    # The categories actually sent to Overpass for this map, and those the density rule
    # skipped; the help page presents both as what happened at generation.
    overpass_categories: tuple[str, ...] = ()
    overpass_skipped_categories: tuple[str, ...] = ()
    # A generation answer cut at the output limit or carrying a runtime-error remark is
    # incomplete: the browser must not answer checks from it alone.
    overpass_truncated: bool = False
    overpass_remark: str | None = None
    # Centres of the circles queried at generation (latitude, longitude).
    overpass_anchor_positions: tuple[tuple[float, float], ...] = ()
    overpass_radius_m: int = 250
    overpass_timeout_seconds: int = 60
    # Asked once by a map opened from disk when the endpoint above refuses it (no Referer).
    overpass_fallback_endpoint: str | None = None
    tile_provider: str | None = None
    sources: tuple[MapSource, ...] = ()
    # The undated records the map renders (thinned per source like the dated points).
    undated_points: tuple[ReferencePlace, ...] = ()
    # Per source id: the steps of its dated records (analysis.segments.steps_from_previous).
    steps_by_source: Mapping[int, SourceSteps] = field(default_factory=dict)
    # Distinct local dates with dated records over all sources.
    days_with_data: int = 0


def resolve_tile_layer(
    app_root: Path, html_directory: Path, map_settings: MapSettings
) -> tuple[str, str | None]:
    """Return the effective tile source and its URL template.

    "online" resolves to the configured provider's template without touching the file
    system. "local" resolves to ("local", relative URL template) when the tile directory
    exists, else falls back to ("none", None); "none" itself always is that pair.
    """
    if map_settings.tile_source == "online":
        return "online", tile_provider_by_key(map_settings.tile_provider).url_template
    if map_settings.tile_source != "local":
        return "none", None
    tiles_dir = (app_root / map_settings.local_tiles_path).resolve()
    if not tiles_dir.is_dir():
        logger.warning("Local tile directory %s not found; rendering without a base map", tiles_dir)
        return "none", None
    try:
        relative = Path(os.path.relpath(tiles_dir, html_directory.resolve())).as_posix()
    except ValueError as error:
        # On Windows there is no relative path between different drives.
        logger.warning(
            "Local tile directory %s has no relative path from %s (%s); rendering without a "
            "base map",
            tiles_dir,
            html_directory,
            error,
        )
        return "none", None
    return "local", f"{relative}/{{z}}/{{x}}/{{y}}.png"


def local_days_with_data(points: Sequence[GeoPoint], display_tz: tzinfo | None) -> set[date]:
    """The distinct local dates (display zone) the dated records fall on."""
    return {to_display_time(point.timestamp_utc, display_tz).date() for point in points}


def data_days_document(days: set[date]) -> tuple[str, ...]:
    """The days as sorted ISO dates for the payload (the browser unions them per shown
    source, so its day count agrees with the capabilities computed here)."""
    return tuple(day.isoformat() for day in sorted(days))


def build_point_payload(
    points: Sequence[AnnotatedPoint],
    display_tz: tzinfo | None,
    display_zone: str,
    steps_by_source: Mapping[int, SourceSteps] | None = None,
) -> list[dict[str, object]]:
    """Flatten points into the compact records the map script consumes. With the steps of
    the full, unthinned lists every row says how it relates to the previous dated record
    of its source: spd km/h (null for the first record), dpm metres, dts
    seconds, cum path metres from the first record and seq, its 1-based position, so
    the browser sums a range exactly even between thinned points. spd_lo/spd_hi bound
    the speed, cmb sums the minimum steps from the first record and cls is the movement
    class of the step's segment; spd_lo_rep, spd_hi_rep and cmb_rep are the same with
    the radii as reported. asc is the factor from the reported radius to the uncertainty
    radius the bounds use, pm the positioning method (null: unknown)."""
    rows: list[dict[str, object]] = []
    for entry in points:
        point = entry.point
        local_time = to_display_time(point.timestamp_utc, display_tz)
        step = None
        if steps_by_source is not None and point.source_id in steps_by_source:
            step = steps_by_source[point.source_id].for_point(point)
        rows.append(
            {
                "n": point.line_number,
                "lat": point.latitude,
                "lon": point.longitude,
                "lat_acc": point.latitude_accuracy_m,
                "lon_acc": point.longitude_accuracy_m,
                "radius": point.accuracy_radius_m,
                "utc": point.timestamp_utc.isoformat(),
                "local": local_time.strftime(LOCAL_TIME_FORMAT),
                "zone": display_zone_label(local_time, display_zone),
                "off": offset_minutes(point.timestamp_utc, display_tz),
                "dup": entry.duplicate_count,
                "first": entry.first_seen_utc.isoformat(),
                "last": entry.last_seen_utc.isoformat(),
                "line": point.original_line,
                "s": point.source_id,
                "lbl": point.label,
                "note": point.note,
                "acc_known": point.accuracy_known,
                "spd": None if step is None else step.speed_kmh,
                "dpm": None if step is None else step.distance_m,
                "dts": None if step is None else step.duration_seconds,
                "cum": None if step is None else step.path_m,
                "seq": None if step is None else step.sequence,
                "spd_lo": None if step is None else step.speed_low_kmh,
                "spd_hi": None if step is None else step.speed_high_kmh,
                "cmb": None if step is None else step.minimum_path_m,
                "cls": None if step is None else step.movement_class,
                "tam": point.time_ambiguity_seconds,
                "asc": point.accuracy_scale,
                "pm": point.positioning_method,
                "spd_lo_rep": None if step is None else step.speed_low_reported_kmh,
                "spd_hi_rep": None if step is None else step.speed_high_reported_kmh,
                "cmb_rep": None if step is None else step.minimum_path_reported_m,
            }
        )
    return rows


def build_undated_points_payload(records: Sequence[ReferencePlace]) -> list[dict[str, object]]:
    """The undated records as the map draws them in the Points layer."""
    return [
        {
            "s": record.source_id,
            "n": record.record_number,
            "lat": record.latitude,
            "lon": record.longitude,
            "acc": record.accuracy_m,
            "acc_known": record.accuracy_known,
            "lbl": record.name or None,
            "note": record.note,
            "line": record.original_record,
        }
        for record in records
    ]


def build_capabilities(
    sources: Sequence[MapSource], days_with_data: int, analysis: Mapping[str, object] | None
) -> dict[str, object]:
    """The facts the browser gates its controls on; it recomputes the same
    numbers per shown source from the sources payload and the analysis. Every key the
    browser judges a feature by is here, so the payload is the complete record of the
    gating basis at generation time."""
    dated_points = sum(source.dated_records for source in sources)
    undated_points = sum(source.undated_records for source in sources)
    records = dated_points + undated_points
    document = analysis or {}
    per_source = document.get("per_source")
    per_source = per_source if isinstance(per_source, Mapping) else {}

    def report_count(key: str) -> int:
        return sum(
            len(entries)
            for report in per_source.values()
            if isinstance(report, Mapping)
            for entries in (report.get(key),)
            if isinstance(entries, list)
        )

    def rows_of(key: str, list_key: str) -> int:
        block = document.get(key)
        rows = block.get(list_key) if isinstance(block, Mapping) else None
        return len(rows) if isinstance(rows, list) else 0

    encounters = document.get("encounters")
    shared_places = document.get("shared_places")
    return {
        "dated_points": dated_points,
        "undated_points": undated_points,
        "dated_share": dated_points / records if records else 0.0,
        "days_with_data": days_with_data,
        "sources": len(sources),
        "sources_with_route": sum(1 for source in sources if source.dated_records >= 2),
        "encounters": len(encounters) if isinstance(encounters, list) else 0,
        "shared_places": len(shared_places) if isinstance(shared_places, list) else 0,
        "matrix_rows": rows_of("presence_matrix", "rows"),
        "case_places": rows_of("case_places", "places"),
        "stays": report_count("stays"),
        "gaps": report_count("gaps"),
        "segments": report_count("segments"),
        # A view state: the Route layer is off when a map opens, and the browser
        # recomputes this once it is switched on.
        "route_shown": 0,
    }


def build_sources_payload(
    sources: Sequence[MapSource],
    points: Sequence[AnnotatedPoint],
    display_tz: tzinfo | None,
    undated_points: Sequence[ReferencePlace] = (),
    steps_by_source: Mapping[int, SourceSteps] | None = None,
) -> list[dict[str, object]]:
    """One entry per source in project order; points_rendered and undated_rendered count
    the thinned points. time_resolution_s is the resolution the speed intervals of the
    source's analysed records assume, null without analysed records."""
    rendered_by_source: dict[int, int] = {}
    for entry in points:
        source_id = entry.point.source_id
        rendered_by_source[source_id] = rendered_by_source.get(source_id, 0) + 1
    undated_rendered_by_source: dict[int, int] = {}
    for record in undated_points:
        undated_rendered_by_source[record.source_id] = (
            undated_rendered_by_source.get(record.source_id, 0) + 1
        )

    def local(moment_utc: datetime | None) -> str | None:
        if moment_utc is None:
            return None
        return to_display_time(moment_utc, display_tz).strftime(LOCAL_TIME_FORMAT)

    def offset(moment_utc: datetime | None) -> int | None:
        return None if moment_utc is None else offset_minutes(moment_utc, display_tz)

    def time_resolution_of(source_id: int) -> float | None:
        steps = (steps_by_source or {}).get(source_id)
        return steps.time_resolution_seconds if steps is not None else None

    return [
        {
            "id": source.descriptor.identifier,
            "label": source.descriptor.label,
            "kind": source.descriptor.kind,
            "icon": SOURCE_KIND_ICONS.get(source.descriptor.kind, SOURCE_KIND_ICONS["other"]),
            "colour": source.descriptor.colour,
            "points_total": source.points_total,
            "points_rendered": rendered_by_source.get(source.descriptor.identifier, 0),
            "thinning_stride": source.thinning_stride,
            "dated_records": source.dated_records,
            "undated_total": source.undated_records,
            "undated_rendered": undated_rendered_by_source.get(source.descriptor.identifier, 0),
            "undated_thinning_stride": source.undated_thinning_stride,
            "time_resolution_s": time_resolution_of(source.descriptor.identifier),
            "data_days": list(source.data_days),
            "first_local": local(source.first_utc),
            "first_offset": offset(source.first_utc),
            "last_local": local(source.last_utc),
            "last_offset": offset(source.last_utc),
            "format": source.descriptor.format,
            "record_noun": record_noun(source.descriptor.format),
            "record_noun_plural": record_noun(source.descriptor.format, plural=True),
            "accuracy_reporting": source.accuracy_reporting,
            # "68", "95" or "unknown": the confidence level of the source's radii.
            "accuracy_level": source.descriptor.accuracy_level,
            # Summary flag for readers without accuracy_reporting: every record has an accuracy.
            "accuracy_known": source.accuracy_reporting == ACCURACY_REPORTED_BY_ALL,
        }
        for source in sources
    ]


def build_zone_transitions(
    points: Sequence[AnnotatedPoint], context: MapRenderContext, display_tz: tzinfo | None
) -> list[ZoneTransition]:
    """Transitions of the display zone over the data and up to a year after generation;
    the browser converts wall-clock times only through these."""
    generated_utc = context.generated_at_local.astimezone(UTC)
    moments = [entry.point.timestamp_utc for entry in points]
    for source in context.sources:
        moments.extend(moment for moment in (source.first_utc, source.last_utc) if moment)
    start_utc = min(moments, default=generated_utc)
    end_utc = max([*moments, generated_utc + REFERENCE_TIME_HORIZON])
    return zone_transitions(display_tz, start_utc, end_utc)


def serialize_payload(document: dict[str, object]) -> str:
    """JSON that is safe inside a <script> block: every '<' becomes a unicode escape."""
    serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return serialized.replace("<", "\\u003c")


def _read_resource(relative_path: str) -> str:
    return bundled_resource(relative_path).read_text(encoding="utf-8")


def _read_template(file_name: str) -> str:
    """A script or stylesheet of the template without its licence header: the page carries
    the header once, in map.html."""
    template_text = _read_resource(f"{TEMPLATE_DIR}/{file_name}")
    for header in TEMPLATE_LICENCE_HEADERS:
        template_text = template_text.removeprefix(header)
    return template_text


def _fill_template(template: str, replacements: dict[str, str]) -> str:
    """Substitute every token exactly once; inserted content is never scanned again."""

    def lookup(match: re.Match[str]) -> str:
        token = match.group(0)
        if token not in replacements:
            raise KeyError(f"Template token without replacement: {token}")
        return replacements[token]

    return TEMPLATE_TOKEN.sub(lookup, template)


def build_places_payload(
    places: Mapping[str, Sequence[Place]],
) -> dict[str, list[dict[str, object]]]:
    """Flatten Overpass places into the compact records the browser consumes."""
    return {
        category: [
            {
                "name": place.name,
                "lat": place.latitude,
                "lon": place.longitude,
                "osm_type": place.osm_type,
                "osm_id": place.osm_id,
                "tags": dict(place.tags),
                "far_centre": place.far_centre,
            }
            for place in entries
        ]
        for category, entries in places.items()
    }


def build_map_html(
    points: Sequence[AnnotatedPoint],
    context: MapRenderContext,
    display_tz: tzinfo | None,
    analysis: dict[str, object] | None = None,
    places: Mapping[str, Sequence[Place]] | None = None,
) -> str:
    """Fill the template with vendored libraries, own scripts and the serialized payload."""
    overpass_for_browser = context.overpass_endpoint if context.online_enabled else None
    fallback_for_browser = (
        context.overpass_fallback_endpoint if overpass_for_browser else None
    ) or None
    document: dict[str, object] = {
        "project": context.project_name,
        "generated_at": context.generated_at_local.isoformat(timespec="seconds"),
        "display_zone": context.display_zone,
        "tile_source": context.tile_source,
        "tile_provider": context.tile_provider,
        "tile_providers": tile_providers_document() if context.tile_source == "online" else [],
        "tiles_url": context.tiles_url_template,
        "tiles_attribution": context.tiles_attribution,
        "tiles_max_zoom": context.tiles_max_zoom,
        "total_points": context.total_points,
        "rendered_points": len(points),
        "thinning_stride": context.thinning_stride,
        "points": build_point_payload(
            points, display_tz, context.display_zone, context.steps_by_source
        ),
        "undated_points": build_undated_points_payload(context.undated_points),
        "sources": build_sources_payload(
            context.sources, points, display_tz, context.undated_points, context.steps_by_source
        ),
        "zone_transitions": build_zone_transitions(points, context, display_tz),
        "capabilities": build_capabilities(context.sources, context.days_with_data, analysis),
        "analysis": analysis,
        "places": build_places_payload(places or {}),
        "provenance": {
            "application": f"{APP_NAME} {__version__}",
            "project": context.project_name,
            "sources": [
                {
                    "id": source.descriptor.identifier,
                    "label": source.descriptor.label,
                    "file_name": source.descriptor.path.name,
                    "sha256": source.sha256,
                }
                for source in context.sources
            ],
            "generated_at": context.generated_at_local.isoformat(timespec="seconds"),
            "tile_source": context.tile_source,
            "tile_provider": context.tile_provider,
            "online_enabled": context.online_enabled,
            "overpass_endpoint": overpass_for_browser,
        },
        "online": {
            "enabled": context.online_enabled,
            "overpass_endpoint": overpass_for_browser,
            "overpass_radius_m": context.overpass_radius_m,
            "overpass_categories": list(context.overpass_categories),
            "overpass_skipped_categories": list(context.overpass_skipped_categories),
            "overpass_truncated": context.overpass_truncated,
            "overpass_remark": context.overpass_remark,
            "overpass_anchors": [
                {"lat": latitude, "lon": longitude}
                for latitude, longitude in context.overpass_anchor_positions
            ],
            "overpass_timeout_seconds": context.overpass_timeout_seconds,
            "overpass_output_limit": OUTPUT_LIMIT,
            "overpass_fallback_endpoint": fallback_for_browser,
            # The live search derives its query and classification from the same catalogue.
            "place_catalogue": catalogue_document(),
        },
    }
    replacements = {
        "__CSP__": csp_policy_for(context.tile_source, overpass_for_browser, fallback_for_browser),
        "__TITLE__": html.escape(f"GEOSnap – {context.project_name}"),
        "__LEAFLET_CSS__": _read_resource(f"{VENDOR_DIR}/leaflet.css"),
        "__MAP_CSS__": _read_template("map.css"),
        "__PAYLOAD__": serialize_payload(document),
        "__LEAFLET_JS__": _read_resource(f"{VENDOR_DIR}/leaflet.js"),
        "__HEAT_JS__": _read_resource(f"{VENDOR_DIR}/leaflet-heat.js"),
        "__GEODESIC_JS__": _read_resource(f"{VENDOR_DIR}/geographiclib-geodesic.min.js"),
        "__MAP_CORE_JS__": _read_template("map_core.js"),
        "__MAP_ANALYSIS_JS__": _read_template("map_analysis.js"),
        "__MAP_PLACES_JS__": _read_template("map_places.js"),
        "__MAP_TOOLS_JS__": _read_template("map_tools.js"),
        "__MAP_SPEED_JS__": _read_template("map_speed.js"),
        "__MAP_FORECAST_MODEL_JS__": _read_template("map_forecast_model.js"),
        "__MAP_CRYSTAL_BALL_JS__": _read_template("map_crystal_ball.js"),
        "__MAP_ONLINE_JS__": _read_template("map_online.js"),
        "__MAP_MATRIX_JS__": _read_template("map_matrix.js"),
        "__MAP_HELP_JS__": _read_template("map_help.js"),
        "__MAP_TIMELINE_JS__": _read_template("map_timeline.js"),
    }
    page = _read_resource(f"{TEMPLATE_DIR}/map.html")
    return _fill_template(page, replacements)
