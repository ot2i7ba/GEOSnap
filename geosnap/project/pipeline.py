# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""One project run from source file to map: extraction, exports, metadata."""

from __future__ import annotations

import json
import logging
import platform
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from enum import Enum
from pathlib import Path
from typing import Protocol

from geosnap import APP_NAME, OUTPUT_DIR_NAME, __version__
from geosnap.analysis.accuracy import (
    ACCURACY_LEVEL_TEXTS,
    RAYLEIGH_68_TO_95,
    accuracy_scale_for,
    apply_accuracy_scales,
)
from geosnap.analysis.case_places import (
    LOCATION_GEOCODED,
    CasePlace,
    CasePlaceResult,
    case_place_to_document,
    case_places_map_document,
    case_places_to_document,
    check_case_places,
)
from geosnap.analysis.csv_export import (
    write_case_places_csv,
    write_encounters_csv,
    write_gaps_csv,
    write_presence_matrix_csv,
    write_segments_csv,
    write_shared_places_csv,
    write_stays_csv,
)
from geosnap.analysis.encounters import (
    MOVEMENT_JOINT,
    Encounter,
    SharedPlace,
    encounter_to_document,
    find_encounters,
    find_shared_places,
    shared_place_to_document,
)
from geosnap.analysis.gpx_export import write_gpx
from geosnap.analysis.kml_export import write_kml
from geosnap.analysis.models import AnalysisReport, PositionSummary, SourceAnalysis, Stay
from geosnap.analysis.presence_matrix import (
    PresenceMatrix,
    build_presence_matrix,
    presence_matrix_to_document,
)
from geosnap.analysis.report import (
    build_analysis_report,
    report_to_document,
    select_analysis_points,
    without_excluded_methods,
)
from geosnap.analysis.segments import steps_from_previous
from geosnap.audit_log import attach_project_log, detach_log_handler
from geosnap.extraction.csv_reader import extract_csv
from geosnap.extraction.duplicates import annotate_duplicates
from geosnap.extraction.extractor import ExtractionProgress, extract_points
from geosnap.extraction.geojson_reader import (
    FORMAT_GEOJSON,
    UNRECOGNISED_JSON_REASON,
    detect_geojson,
    extract_geojson,
)
from geosnap.extraction.google_reader import detect_google_format, extract_google
from geosnap.extraction.gpx_reader import extract_gpx
from geosnap.extraction.kml_reader import KmlExtractionOutcome, extract_kml
from geosnap.extraction.kmz_reader import extract_kmz, is_zip_archive
from geosnap.extraction.models import (
    ACCURACY_REPORTED_BY_NONE,
    UNRECOGNISED_FORMAT,
    ExtractionCounters,
    GeoPoint,
    KmzEntryReport,
    ReferencePlace,
    RejectedLine,
    SourceDescriptor,
    SourceExtractionOutcome,
    SourceFormatError,
    accuracy_reporting,
    naive_timestamp_handling,
)
from geosnap.extraction.reader_support import whole_file_read_report
from geosnap.extraction.source_reader import SourceReadReport
from geosnap.mapping.map_builder import (
    MapRenderContext,
    MapSource,
    build_map_html,
    data_days_document,
    local_days_with_data,
    resolve_tile_layer,
)
from geosnap.mapping.point_selection import (
    collapse_duplicates,
    thin_per_source,
    thin_points_per_source,
)
from geosnap.mapping.tile_providers import TILE_PROVIDER_KEYS, tile_provider_by_key
from geosnap.online.http_client import (
    OnlineClient,
    OnlineResponse,
    OnlineServiceError,
    default_user_agent,
)
from geosnap.online.nominatim import AddressLookup
from geosnap.online.overpass import (
    OUTPUT_LIMIT,
    OverpassResponseError,
    Place,
    build_overpass_query,
    categories_within_radius,
    choose_anchors_for_sources,
    fetch_places,
    interleave_round_robin,
)
from geosnap.project.case_place_input import CasePlacesFile
from geosnap.project.csv_export import RejectedLineWriter, write_points_csv
from geosnap.project.manifest import MANIFEST_FILE_NAME, write_manifest
from geosnap.project.metadata import sha256_of_file, write_metadata
from geosnap.project.report_html import write_report
from geosnap.project.workspace import (
    ProjectWorkspace,
    WorkspacePathError,
    create_workspace,
    plan_workspace,
)
from geosnap.settings import (
    DEFAULT_OVERPASS_FALLBACK_OPERATOR,
    ExtractionSettings,
    OnlineSettings,
    Settings,
    TimezoneSettings,
)
from geosnap.timezones import LOCAL_ZONE_SETTING, resolve_display_timezone

logger = logging.getLogger(__name__)

DIRECTORY_COLLISION_HINT = (
    "A project directory with this name and timestamp already exists; "
    "wait a second and start again."
)


class PipelinePhase(Enum):
    READING = "reading and hashing"
    EXPORTING = "exporting"
    ANALYSING = "analysing"
    QUERYING_ONLINE = "querying online services"
    BUILDING_MAP = "building map"


class UnreadableSourcesError(ValueError):
    """Every source of the project failed to read; nothing is left to analyse."""


class RejectedOutputError(RuntimeError):
    """Writing rejected_<stamp>.csv failed: an output problem, never a source failure."""


class ProjectStatus(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


# Per-source read status in metadata.json and the summary.
SOURCE_READ = "read"
SOURCE_PARTIAL = "partial"
SOURCE_SKIPPED = "skipped"
SOURCE_FAILED = "failed"
# What a source's SHA-256 covers (metadata.json "sha256_scope").
SHA256_SCOPE_WHOLE_FILE = "whole file"
SHA256_SCOPE_BEFORE_FAILURE = "bytes read before the failure"
# Keys of analysis.json that the map embeds as payload.analysis.
MAP_ANALYSIS_KEYS = (
    "per_source",
    "encounters",
    "shared_places",
    "case_places",
    "presence_matrix",
    "parameters",
)

SOURCE_COLOUR_PALETTE = (
    "#1d5c8f",
    "#d55e00",
    "#009e73",
    "#cc79a7",
    "#e69f00",
    "#56b4e9",
    "#7a1fa2",
    "#8c564b",
)


def source_format_for(path: Path) -> str:
    """Input format from the file name; a .json file by its first 64 KB (a Google format,
    else GeoJSON, else "unrecognised"), a .kml file that is a ZIP archive as KMZ.
    Everything else is the line-based text format."""
    suffix = path.suffix.lower()
    if suffix == ".kml":
        try:
            return "kmz" if is_zip_archive(path) else "kml"
        except OSError:
            return "kml"
    if suffix == ".json":
        try:
            detected = detect_google_format(path)
            if detected == UNRECOGNISED_FORMAT and detect_geojson(path):
                return FORMAT_GEOJSON
            return detected
        except OSError:
            return UNRECOGNISED_FORMAT
    return {".kmz": "kmz", ".gpx": "gpx", ".csv": "csv", ".geojson": FORMAT_GEOJSON}.get(
        suffix, "text"
    )


# Where the run's display zone came from (ProjectRequest.display_zone_origin).
DISPLAY_ZONE_ORIGIN_EXAMINER = "examiner"
DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING = "display_setting"
DISPLAY_ZONE_ORIGIN_HOST_SYSTEM = "host_system"
DISPLAY_ZONE_ORIGIN_HOST_SYSTEM_CONFIRMED = "host_system_confirmed"
# Origins on which evidence times without offset may be read in the display zone: the
# examiner chose or confirmed the zone, or config.toml names it. A zone merely taken from
# the host clock is never applied to evidence on its own.
DISPLAY_ZONE_ORIGINS_FOR_NAIVE_TIMES = frozenset(
    {
        DISPLAY_ZONE_ORIGIN_EXAMINER,
        DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING,
        DISPLAY_ZONE_ORIGIN_HOST_SYSTEM_CONFIRMED,
    }
)


def naive_time_zone_for(display_zone: str, display_zone_origin: str) -> str:
    """The zone GeoJSON times without offset are read in: the display zone when it names
    one and its origin allows it (see DISPLAY_ZONE_ORIGINS_FOR_NAIVE_TIMES); "" when
    the setting is "local" or the zone is an unconfirmed host suggestion, so such times
    are refused."""
    if display_zone == LOCAL_ZONE_SETTING:
        return ""
    return display_zone if display_zone_origin in DISPLAY_ZONE_ORIGINS_FOR_NAIVE_TIMES else ""


def single_source_request(
    source_path: Path, project_name: str, remove_duplicates: bool
) -> ProjectRequest:
    """A one-file project with default label, kind and colour (tests and simple callers)."""
    source = SourceDescriptor(
        identifier=1,
        label=source_path.stem[:32] or "source",
        kind="smartphone",
        colour=SOURCE_COLOUR_PALETTE[0],
        path=source_path,
        format=source_format_for(source_path),
    )
    return ProjectRequest((source,), project_name, remove_duplicates)


@dataclass(frozen=True, slots=True)
class ProjectRequest:
    sources: tuple[SourceDescriptor, ...]
    project_name: str
    remove_duplicates: bool
    # Optional case details for the report, the search-area sheet and metadata.json.
    case_reference: str = ""
    examiner: str = ""
    # Places from the case file, checked against every source (identifiers 1..n in order);
    # the CSV they were loaded from, if any, is recorded with its SHA-256.
    case_places: tuple[CasePlace, ...] = ()
    case_places_file: CasePlacesFile | None = None
    # Display zone chosen for this run (an IANA name); "" keeps settings.timezone.display.
    # Its origin decides whether evidence times without offset may be read in it.
    display_zone: str = ""
    display_zone_origin: str = DISPLAY_ZONE_ORIGIN_DISPLAY_SETTING
    # Presence matrix tolerance chosen for this run; None keeps analysis.matrix_tolerance_m.
    matrix_tolerance_m: int | None = None

    @property
    def source_path(self) -> Path:
        """Path of the first source (single-source code paths)."""
        return self.sources[0].path


@dataclass(slots=True)
class OnlineRunSummary:
    enabled: bool = False
    tiles_forced_offline: bool = False
    nominatim_lookups: int = 0
    nominatim_addresses: int = 0
    nominatim_error: str | None = None
    # Address searches for case places entered without coordinates, and how many answered.
    nominatim_searches: int = 0
    case_places_geocoded: int = 0
    # Labels of the case places whose address search was answered with nothing usable.
    case_places_without_answer: list[str] = field(default_factory=list)
    overpass_anchors: int = 0
    # The centres of the circles queried at generation, (latitude, longitude); the map answers
    # a check offline only inside them; the browser does not rebuild them.
    overpass_anchor_positions: list[tuple[float, float]] = field(default_factory=list)
    overpass_places: int = 0
    overpass_error: str | None = None
    overpass_query: str | None = None
    overpass_remark: str | None = None
    overpass_truncated: bool = False
    # Filled only when a request was sent (queried) or the density rule dropped a category.
    overpass_queried_categories: list[str] = field(default_factory=list)
    overpass_skipped_categories: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass(slots=True)
class SourceRunSummary:
    """What happened to one source: read completely, partially (cancelled), skipped after
    a cancel, or failed (unreadable file or format; ``error`` holds the reason).

    A failed source keeps the counters, size and digest of the bytes read before the
    failure (the whole file when it was read to the end); its points are not used.
    """

    descriptor: SourceDescriptor
    status: str = SOURCE_SKIPPED
    counters: ExtractionCounters = field(default_factory=ExtractionCounters)
    lines_total: int = 0
    bytes_processed: int = 0
    # File size when it was read; None when it was never opened.
    size_bytes: int | None = None
    # None when the read was cancelled or skipped: a partial digest would be misleading.
    # A failed source's digest covers the bytes read before the failure (sha256_scope).
    sha256: str | None = None
    encoding: str | None = None
    error: str | None = None
    map_points_rendered: int = 0
    thinning_stride: int = 1
    # Undated records the map renders and the stride they were thinned with.
    undated_rendered: int = 0
    undated_thinning_stride: int = 1
    # extraction.models.accuracy_reporting over the source's accepted records.
    accuracy_reporting: str = ACCURACY_REPORTED_BY_NONE
    # KMZ only: the archive entry that was read; sha256 above covers the .kmz file.
    kmz_entry: KmzEntryReport | None = None

    @property
    def sha256_scope(self) -> str | None:
        if self.sha256 is None:
            return None
        # A refused source may still have been hashed completely (unrecognised JSON, a CSV
        # without a usable mapping): then the digest covers the whole file.
        read_completely = self.size_bytes is not None and self.bytes_processed == self.size_bytes
        if self.status == SOURCE_FAILED and not read_completely:
            return SHA256_SCOPE_BEFORE_FAILURE
        return SHA256_SCOPE_WHOLE_FILE


@dataclass(slots=True)
class ProjectResult:
    """Run result; counters, lines_total and bytes_processed are totals over all sources."""

    status: ProjectStatus
    workspace: ProjectWorkspace | None = None
    counters: ExtractionCounters = field(default_factory=ExtractionCounters)
    lines_total: int = 0
    bytes_processed: int = 0
    sources: list[SourceRunSummary] = field(default_factory=list)
    unique_positions: int = 0
    map_points_total: int = 0
    map_points_rendered: int = 0
    # The largest per-source stride; each source's own stride is in its SourceRunSummary.
    thinning_stride: int = 1
    tile_source_used: str = "none"
    tile_provider_used: str | None = None
    output_hashes: dict[str, str] = field(default_factory=dict)
    error_message: str | None = None
    analysis_by_source: dict[int, AnalysisReport] = field(default_factory=dict)
    encounters: list[Encounter] = field(default_factory=list)
    shared_places: list[SharedPlace] = field(default_factory=list)
    # One entry per case place of the request, in its order (geocoded where that succeeded).
    case_places: list[CasePlaceResult] = field(default_factory=list)
    # Only with two or more sources or with case places.
    presence_matrix: PresenceMatrix | None = None
    online: OnlineRunSummary = field(default_factory=OnlineRunSummary)
    # Problems that did not stop the run, such as a failed source.
    warnings: list[str] = field(default_factory=list)
    # SHA-256 of MANIFEST.sha256; kept outside the project directory (summary, app log).
    manifest_sha256: str | None = None

    @property
    def analysis(self) -> AnalysisReport | None:
        """Report of the first source (single-source callers)."""
        return next(iter(self.analysis_by_source.values()), None)

    @property
    def source_sha256(self) -> str | None:
        """Digest of the first source (single-source callers)."""
        return self.sources[0].sha256 if self.sources else None

    @property
    def source_encoding(self) -> str | None:
        return self.sources[0].encoding if self.sources else None


class PipelineObserver(Protocol):
    def on_phase(self, phase: PipelinePhase) -> None: ...

    def on_progress(self, progress: ExtractionProgress) -> None: ...

    def on_online_progress(self, completed: int, total: int, label: str) -> None: ...


def run_project(
    request: ProjectRequest,
    settings: Settings,
    app_root: Path,
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    online_client: OnlineClient | None = None,
    nominatim_min_interval_seconds: float = 1.1,
) -> ProjectResult:
    """Create the project directory, run the pipeline under its own log and never raise."""
    if request.display_zone:
        settings = replace(settings, timezone=TimezoneSettings(display=request.display_zone))
    if request.matrix_tolerance_m is not None:
        settings = replace(
            settings,
            analysis=replace(settings.analysis, matrix_tolerance_m=request.matrix_tolerance_m),
        )
    started_local = datetime.now().astimezone()
    workspace = plan_workspace(app_root / OUTPUT_DIR_NAME, request.project_name, started_local)
    try:
        create_workspace(workspace)
    except FileExistsError:
        logger.error("Project directory %s already exists", workspace.directory)
        return ProjectResult(status=ProjectStatus.FAILED, error_message=DIRECTORY_COLLISION_HINT)
    except (OSError, WorkspacePathError) as error:
        logger.error("Cannot create project directory %s: %s", workspace.directory, error)
        return ProjectResult(status=ProjectStatus.FAILED, error_message=str(error))

    project_log = attach_project_log(workspace.log_path)
    try:
        result = _run_in_workspace(
            request,
            settings,
            app_root,
            workspace,
            observer,
            is_cancelled,
            online_client,
            nominatim_min_interval_seconds,
        )
        logger.info(
            "Writing the report and %s next; their outcome and the manifest SHA-256 are "
            "recorded in the application log",
            MANIFEST_FILE_NAME,
        )
    finally:
        detach_log_handler(project_log)
    _write_report_and_manifest(settings, workspace, result)
    return result


def _write_report_and_manifest(
    settings: Settings, workspace: ProjectWorkspace, result: ProjectResult
) -> None:
    """Write the report, then the manifest as the last file of the project directory.

    Runs after the project log is closed, so the report and the manifest see its final
    content; messages from here on reach the application log only. Neither failure
    changes the run status, which metadata.json already records.
    """
    try:
        write_report(workspace, settings, datetime.now().astimezone())
    except Exception as error:  # noqa: BLE001 - the manifest must still be written
        logger.exception("Cannot write the report %s", workspace.report_html_path)
        result.warnings.append(f"report not written: {error}")
    else:
        logger.info("Report written: %s", workspace.report_html_path)
    try:
        result.manifest_sha256 = write_manifest(workspace.directory)
    except OSError as error:
        logger.error("Cannot write %s: %s", workspace.manifest_path, error)
        result.warnings.append(f"{MANIFEST_FILE_NAME} not written: {error}")
        return
    logger.info("Manifest %s sha256=%s", workspace.manifest_path, result.manifest_sha256)


def _run_in_workspace(
    request: ProjectRequest,
    settings: Settings,
    app_root: Path,
    workspace: ProjectWorkspace,
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    online_client: OnlineClient | None,
    nominatim_min_interval_seconds: float,
) -> ProjectResult:
    result = ProjectResult(status=ProjectStatus.FAILED, workspace=workspace)
    logger.info(
        "Project '%s' started: sources=%s directory=%s",
        request.project_name,
        ", ".join(
            f"{source.identifier}={source.label} ({source.format}, {source.path})"
            for source in request.sources
        ),
        workspace.directory,
    )
    logger.info(
        "Effective settings: timezone=%s remove_duplicates=%s point_limit=%d tile_source=%s "
        "tile_provider=%s read_chunk_bytes=%d fallback_encoding=%s analysis=%s online=%s",
        settings.timezone.display,
        request.remove_duplicates,
        settings.map.point_limit,
        settings.map.tile_source,
        settings.map.tile_provider,
        settings.extraction.read_chunk_bytes,
        settings.extraction.fallback_encoding,
        settings.analysis,
        settings.online.enabled,
    )
    try:
        _execute(
            request,
            settings,
            app_root,
            workspace,
            observer,
            is_cancelled,
            result,
            online_client,
            nominatim_min_interval_seconds,
        )
    except Exception as error:  # noqa: BLE001 - the UI must survive; the log keeps the trace
        logger.exception("Project '%s' failed", request.project_name)
        result.status = ProjectStatus.FAILED
        result.error_message = f"{type(error).__name__}: {error}"

    # Whatever was written before a failure or cancellation is still evidence: hash it.
    try:
        _record_output_hashes(workspace, result)
    except OSError as error:
        logger.error("Cannot hash the outputs in %s: %s", workspace.directory, error)

    try:
        write_metadata(
            workspace.metadata_path, _metadata_document(request, settings, workspace, result)
        )
    except Exception as error:  # noqa: BLE001 - the UI must survive; the log keeps the trace
        logger.error("Cannot write %s: %s", workspace.metadata_path, error)
        result.status = ProjectStatus.FAILED
        result.error_message = result.error_message or str(error)

    logger.info("Project '%s' finished with status %s", request.project_name, result.status.value)
    return result


def _execute(
    request: ProjectRequest,
    settings: Settings,
    app_root: Path,
    workspace: ProjectWorkspace,
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    result: ProjectResult,
    online_client: OnlineClient | None,
    nominatim_min_interval_seconds: float,
) -> None:
    display_tz = resolve_display_timezone(settings.timezone.display)

    observer.on_phase(PipelinePhase.READING)
    extracted_points, undated_records, reading_cancelled = _read_sources(
        request,
        settings.extraction,
        naive_time_zone_for(settings.timezone.display, request.display_zone_origin),
        workspace,
        observer,
        is_cancelled,
        result,
    )
    # Once, before anything is derived from the points: the analyses find points by identity.
    scale_by_source = {
        source.identifier: accuracy_scale_for(
            source.accuracy_level, settings.analysis.accuracy_confidence
        )
        for source in request.sources
    }
    apply_accuracy_scales(extracted_points, scale_by_source)

    observer.on_phase(PipelinePhase.EXPORTING)
    source_labels = {source.identifier: source.label for source in request.sources}
    annotated = annotate_duplicates(extracted_points)
    write_points_csv(
        workspace.points_csv_path,
        annotated,
        display_tz,
        settings.timezone.display,
        source_labels,
        undated_records,
    )
    # A position is source, coordinates and accuracies; the positioning method separates
    # duplicates on the map, not positions.
    result.unique_positions = len(
        {
            (
                entry.point.source_id,
                entry.point.latitude,
                entry.point.longitude,
                entry.point.latitude_accuracy_m,
                entry.point.longitude_accuracy_m,
            )
            for entry in annotated
        }
    )
    # Collapsing works per source (duplicate keys carry the source); thinning shares the
    # point limit between the sources so a small source never vanishes behind a large one.
    map_points = collapse_duplicates(annotated) if request.remove_duplicates else annotated
    selected, strides = thin_points_per_source(map_points, settings.map.point_limit)
    stride = max(strides.values(), default=1)
    # The undated records share the same rule and limit, on their own.
    undated_selected, undated_strides = thin_per_source(
        undated_records, settings.map.point_limit, lambda record: record.source_id
    )
    result.map_points_total = len(map_points)
    result.map_points_rendered = len(selected)
    result.thinning_stride = stride
    for summary in result.sources:
        source_id = summary.descriptor.identifier
        summary.thinning_stride = strides.get(source_id, 1)
        summary.map_points_rendered = sum(
            1 for entry in selected if entry.point.source_id == source_id
        )
        summary.undated_thinning_stride = undated_strides.get(source_id, 1)
        summary.undated_rendered = sum(
            1 for record in undated_selected if record.source_id == source_id
        )
        if summary.undated_thinning_stride > 1:
            logger.warning(
                "Map thinned the undated records of source '%s' to %d (stride %d)",
                summary.descriptor.label,
                summary.undated_rendered,
                summary.undated_thinning_stride,
            )
        if summary.thinning_stride > 1:
            logger.warning(
                "Map thinned source '%s' to %d points (stride %d); the CSV export is complete",
                summary.descriptor.label,
                summary.map_points_rendered,
                summary.thinning_stride,
            )
    if stride > 1:
        logger.warning(
            "Map thinned to %d of %d points (largest stride %d); the CSV export is complete",
            len(selected),
            len(map_points),
            stride,
        )

    observer.on_phase(PipelinePhase.ANALYSING)
    points_by_source: dict[int, list[GeoPoint]] = {
        source.identifier: [] for source in request.sources
    }
    for annotated_point in annotated:
        points_by_source[annotated_point.point.source_id].append(annotated_point.point)
    for summary in result.sources:
        summary.accuracy_reporting = accuracy_reporting(
            points_by_source[summary.descriptor.identifier]
        )
    generated_at_utc = datetime.now(UTC)
    source_analyses = [
        SourceAnalysis(
            source,
            points_by_source[source.identifier],
            build_analysis_report(
                points_by_source[source.identifier], settings.analysis, generated_at_utc
            ),
        )
        for source in request.sources
    ]
    result.analysis_by_source = {entry.source.identifier: entry.report for entry in source_analyses}
    for entry in source_analyses:
        _log_source_analysis(entry)
    stays_by_source = {entry.source.identifier: entry.report.stays for entry in source_analyses}
    presence_matrix_wanted = len(request.sources) >= 2 or bool(request.case_places)
    if len(request.sources) >= 2:
        excluded_methods = settings.analysis.excluded_positioning_methods
        result.encounters = find_encounters(
            {
                source_id: without_excluded_methods(points, excluded_methods)
                for source_id, points in points_by_source.items()
            },
            settings.analysis.encounter_max_minutes,
            settings.analysis.encounter_max_distance_m,
            settings.analysis.encounter_joint_movement_min_m,
            settings.analysis.implausible_speed_kmh,
        )
        result.shared_places = find_shared_places(stays_by_source, settings.analysis.stop_radius_m)
        logger.info(
            "Cross-source analysis: %d encounter(s), %d of them joint movement(s), "
            "%d shared place(s)",
            len(result.encounters),
            sum(1 for encounter in result.encounters if encounter.movement == MOVEMENT_JOINT),
            len(result.shared_places),
        )
        write_encounters_csv(
            workspace.encounters_csv_path, result.encounters, source_labels, display_tz
        )
        write_shared_places_csv(
            workspace.shared_places_csv_path, result.shared_places, source_labels, display_tz
        )
    write_gaps_csv(workspace.gaps_csv_path, source_analyses, display_tz)
    write_segments_csv(workspace.segments_csv_path, source_analyses)
    write_gpx(
        workspace.gpx_path,
        source_analyses,
        result.encounters,
        workspace.project_name,
        generated_at_utc,
    )
    write_kml(workspace.kml_path, source_analyses, result.encounters, workspace.project_name)

    online = settings.online
    result.online.enabled = online.enabled
    effective_map_settings = settings.map
    if not online.enabled and settings.map.tile_source == "online":
        effective_map_settings = replace(settings.map, tile_source="none")
        result.online.tiles_forced_offline = True
        logger.info("Online services are disabled: map tiles switched from online to none")

    places: dict[str, list[Place]] = {}
    case_places = list(request.case_places)
    if online.enabled and not reading_cancelled:
        observer.on_phase(PipelinePhase.QUERYING_ONLINE)
        client = online_client or OnlineClient(default_user_agent())
        places, case_places = _query_online_services(
            online,
            client,
            source_analyses,
            case_places,
            workspace,
            observer,
            is_cancelled,
            result.online,
            nominatim_min_interval_seconds,
        )

    # After the online phase, so the stays carry their looked-up addresses and the case
    # places entered with an address only carry their geocoded position.
    write_stays_csv(workspace.stays_csv_path, source_analyses, display_tz)
    if case_places:
        _check_case_places(case_places, points_by_source, settings, workspace, result, display_tz)
    if presence_matrix_wanted:
        # A failed or skipped source gets no column: it has no reports to describe.
        result.presence_matrix = build_presence_matrix(
            [
                summary.descriptor.identifier
                for summary in result.sources
                if summary.status in (SOURCE_READ, SOURCE_PARTIAL)
            ],
            stays_by_source,
            result.case_places,
            settings.analysis.matrix_tolerance_m,
        )
        logger.info(
            "Presence matrix: %d place(s) x %d source(s), tolerance %d m",
            len(result.presence_matrix.rows),
            len(result.presence_matrix.source_ids),
            result.presence_matrix.tolerance_m,
        )
        write_presence_matrix_csv(
            workspace.presence_matrix_csv_path, result.presence_matrix, source_labels, display_tz
        )
    analysis_document: dict[str, object] = {
        "sources": [_source_document(source) for source in request.sources],
        "per_source": {
            str(entry.source.identifier): report_to_document(
                entry.report, display_tz, settings.timezone.display
            )
            for entry in source_analyses
        },
        "encounters": [
            encounter_to_document(encounter, display_tz) for encounter in result.encounters
        ],
        "shared_places": [
            shared_place_to_document(place, display_tz) for place in result.shared_places
        ],
        "case_places": case_places_to_document(
            result.case_places,
            display_tz,
            settings.analysis.gap_min_minutes,
            settings.analysis.max_accuracy_m,
        )
        if result.case_places
        else None,
        "presence_matrix": None
        if result.presence_matrix is None
        else presence_matrix_to_document(result.presence_matrix, display_tz),
        # The level wording and the factor, so the map words them as the report does.
        "parameters": {
            **asdict(settings.analysis),
            "accuracy_level_texts": dict(ACCURACY_LEVEL_TEXTS),
            "accuracy_scale_factor": RAYLEIGH_68_TO_95,
        },
    }
    write_metadata(workspace.analysis_path, analysis_document)

    observer.on_phase(PipelinePhase.BUILDING_MAP)
    tile_source, tiles_url = resolve_tile_layer(
        app_root, workspace.directory, effective_map_settings
    )
    result.tile_source_used = tile_source
    logger.info("Base map: %s", tile_source)
    tile_provider_key: str | None = None
    if tile_source == "online":
        provider = tile_provider_by_key(effective_map_settings.tile_provider)
        tile_provider_key = provider.key
        logger.warning(
            "Map uses online tiles from %s; opening it discloses the viewer's IP and the "
            "viewed area to %s",
            provider.label,
            ", ".join(provider.origins),
        )
        tiles_attribution = provider.attribution
        tiles_max_zoom = provider.max_zoom
    else:
        tiles_attribution = settings.map.local_tiles_attribution
        tiles_max_zoom = settings.map.local_tiles_max_zoom
    result.tile_provider_used = tile_provider_key
    map_points_by_source: dict[int, int] = {}
    for map_point in map_points:
        source_id = map_point.point.source_id
        map_points_by_source[source_id] = map_points_by_source.get(source_id, 0) + 1
    map_sources = tuple(
        MapSource(
            descriptor=summary.descriptor,
            points_total=map_points_by_source.get(summary.descriptor.identifier, 0),
            first_utc=_first_timestamp(points_by_source[summary.descriptor.identifier]),
            last_utc=_last_timestamp(points_by_source[summary.descriptor.identifier]),
            accuracy_reporting=summary.accuracy_reporting,
            sha256=summary.sha256,
            thinning_stride=summary.thinning_stride,
            dated_records=len(points_by_source[summary.descriptor.identifier]),
            undated_records=summary.counters.undated,
            undated_thinning_stride=summary.undated_thinning_stride,
            data_days=data_days_document(
                local_days_with_data(points_by_source[summary.descriptor.identifier], display_tz)
            ),
        )
        for summary in result.sources
    )
    context = MapRenderContext(
        project_name=workspace.project_name,
        generated_at_local=workspace.created_local,
        display_zone=settings.timezone.display,
        tile_source=tile_source,
        tiles_url_template=tiles_url,
        tiles_attribution=tiles_attribution,
        tiles_max_zoom=tiles_max_zoom,
        total_points=len(map_points),
        thinning_stride=stride,
        online_enabled=online.enabled,
        overpass_endpoint=online.overpass_browser_endpoint or online.overpass_endpoint,
        overpass_categories=tuple(result.online.overpass_queried_categories),
        overpass_skipped_categories=tuple(result.online.overpass_skipped_categories),
        overpass_truncated=result.online.overpass_truncated,
        overpass_remark=result.online.overpass_remark,
        overpass_anchor_positions=tuple(result.online.overpass_anchor_positions),
        overpass_radius_m=online.overpass_radius_m,
        overpass_timeout_seconds=online.overpass_timeout_seconds,
        overpass_fallback_endpoint=online.overpass_fallback_endpoint or None,
        tile_provider=tile_provider_key,
        sources=map_sources,
        undated_points=tuple(undated_selected),
        # Over the analysed records, the same list the segments run on: a record the
        # accuracy limit excluded gets no speed figures, so the map and the report give the
        # same figures for the same range.
        steps_by_source={
            source_id: steps_from_previous(
                select_analysis_points(
                    points,
                    settings.analysis.max_accuracy_m,
                    settings.analysis.excluded_positioning_methods,
                ),
                result.analysis_by_source[source_id].segments,
                result.analysis_by_source[source_id].time_resolution_seconds,
            )
            for source_id, points in points_by_source.items()
        },
        days_with_data=len(local_days_with_data([entry.point for entry in annotated], display_tz)),
    )
    map_analysis = {key: analysis_document[key] for key in MAP_ANALYSIS_KEYS}
    case_places_document = analysis_document["case_places"]
    assert case_places_document is None or isinstance(case_places_document, dict)
    map_analysis["case_places"] = case_places_map_document(case_places_document)
    workspace.map_html_path.write_text(
        build_map_html(selected, context, display_tz, analysis=map_analysis, places=places),
        encoding="utf-8",
    )

    # A cancel request that arrived after the read still ends the run as cancelled; every
    # output above was written with the complete data, so nothing is discarded.
    cancelled = reading_cancelled or is_cancelled()
    result.status = ProjectStatus.CANCELLED if cancelled else ProjectStatus.COMPLETED


def _check_case_places(
    case_places: list[CasePlace],
    points_by_source: dict[int, list[GeoPoint]],
    settings: Settings,
    workspace: ProjectWorkspace,
    result: ProjectResult,
    display_tz: tzinfo | None,
) -> None:
    """Check every case place against the sources that were read and write the CSV.

    A silence longer than analysis.gap_min_minutes ends a visit; a report without an
    accuracy value counts as clear of a place only beyond radius + analysis.max_accuracy_m;
    a report of an excluded positioning method is at most a possible visit. A failed or
    skipped source is left out: it has no reports to describe.
    """
    read_sources = {
        summary.descriptor.identifier: points_by_source[summary.descriptor.identifier]
        for summary in result.sources
        if summary.status in (SOURCE_READ, SOURCE_PARTIAL)
    }
    result.case_places = check_case_places(
        case_places,
        read_sources,
        settings.analysis.gap_min_minutes,
        settings.analysis.max_accuracy_m,
        settings.analysis.excluded_positioning_methods,
    )
    for entry in result.case_places:
        if not entry.place.located:
            reason = _not_located_reason(entry.place, settings.online, result.online)
            result.warnings.append(f"case place '{entry.place.label}' not located: {reason}")
            logger.warning("Case place '%s' not located: %s", entry.place.label, reason)
            continue
        logger.info(
            "Case place '%s' (%s): %s",
            entry.place.label,
            entry.place.location,
            "; ".join(
                f"source {check.source_id}: {len(check.visits)} visit(s), "
                f"{len(check.possible_reports)} possible report(s)"
                + ("" if check.window is None else f", window: {check.window.verdict}")
                for check in entry.checks
            ),
        )
    write_case_places_csv(
        workspace.case_places_csv_path,
        result.case_places,
        {summary.descriptor.identifier: summary.descriptor.label for summary in result.sources},
        display_tz,
    )


def _not_located_reason(place: CasePlace, online: OnlineSettings, summary: OnlineRunSummary) -> str:
    """Why a case place entered with an address only has no coordinates."""
    if place.label in summary.case_places_without_answer:
        return "Nominatim gave no usable answer for its address"
    if not online.enabled:
        return "online services are off, so its address was not searched"
    if not online.nominatim_enabled:
        return "Nominatim is disabled, so its address was not searched"
    if summary.nominatim_error is not None:
        return f"the address search failed ({summary.nominatim_error})"
    return "the run was cancelled before its address was searched"


def _read_sources(
    request: ProjectRequest,
    extraction_settings: ExtractionSettings,
    naive_time_zone: str,
    workspace: ProjectWorkspace,
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    result: ProjectResult,
) -> tuple[list[GeoPoint], list[ReferencePlace], bool]:
    """Read the sources in turn; a cancel stops the current source and skips the rest.

    Returns all dated points, all undated records and whether the reading was cancelled.
    """
    result.sources = [SourceRunSummary(source) for source in request.sources]
    points: list[GeoPoint] = []
    undated_records: list[ReferencePlace] = []
    source_count = len(request.sources)
    with RejectedLineWriter(workspace.rejected_csv_path) as rejected_writer:
        for index, summary in enumerate(result.sources, start=1):
            source = summary.descriptor
            if index > 1 and is_cancelled():
                logger.warning(
                    "Reading cancelled before source %d; %d source(s) skipped",
                    index,
                    source_count - index + 1,
                )
                return points, undated_records, True

            def report_progress(
                progress: ExtractionProgress, index: int = index, label: str = source.label
            ) -> None:
                observer.on_progress(
                    replace(
                        progress, source_index=index, source_count=source_count, source_label=label
                    )
                )

            logger.info(
                "Reading source %d of %d: '%s' (%s) %s",
                index,
                source_count,
                source.label,
                source.format,
                source.path,
            )
            write_rejected = rejected_writer.for_source(source.identifier, source.label)

            def on_rejected(
                rejected: RejectedLine,
                write_rejected: Callable[[RejectedLine], None] = write_rejected,
            ) -> None:
                # Keeps an output failure apart from the source failures caught below.
                try:
                    write_rejected(rejected)
                except OSError as error:
                    raise RejectedOutputError(
                        f"cannot write {workspace.rejected_csv_path.name}: {error}"
                    ) from error

            try:
                extraction = _extract_source(
                    source,
                    extraction_settings,
                    naive_time_zone,
                    on_rejected,
                    report_progress,
                    is_cancelled,
                )
            except SourceFormatError as error:
                _record_failed_source(result, summary, index, error, error.partial_outcome)
                continue
            except OSError as error:
                _record_failed_source(result, summary, index, error, None)
                continue
            summary.status = SOURCE_PARTIAL if extraction.cancelled else SOURCE_READ
            _record_read_counts(summary, extraction.counters, extraction.read_report)
            summary.sha256 = None if extraction.cancelled else extraction.read_report.sha256_hex
            _record_kmz_entry(result, summary, index, extraction.kmz_entry)
            _add_to_totals(result, summary)
            points.extend(extraction.points)
            undated_records.extend(extraction.reference_places)
            if extraction.cancelled:
                return points, undated_records, True
    if all(summary.status == SOURCE_FAILED for summary in result.sources):
        raise UnreadableSourcesError(
            "No source could be read: "
            + "; ".join(
                f"{summary.descriptor.label}: {summary.error}" for summary in result.sources
            )
        )
    return points, undated_records, False


def _record_read_counts(
    summary: SourceRunSummary, counters: ExtractionCounters, read_report: SourceReadReport
) -> None:
    summary.counters = counters
    summary.lines_total = read_report.line_count
    summary.bytes_processed = read_report.bytes_read
    summary.size_bytes = read_report.size_bytes
    summary.encoding = read_report.encoding


def _record_kmz_entry(
    result: ProjectResult, summary: SourceRunSummary, index: int, kmz_entry: KmzEntryReport | None
) -> None:
    """Keep which archive entry was read; warn about .kml entries that were not."""
    summary.kmz_entry = kmz_entry
    if kmz_entry is not None and kmz_entry.other_kml_entries:
        result.warnings.append(
            f"source {index} '{summary.descriptor.label}': only {kmz_entry.name!r} of the KMZ "
            f"archive was read; {kmz_entry.other_kml_entry_count} further .kml entries not "
            "read, among them: " + ", ".join(repr(name) for name in kmz_entry.other_kml_entries)
        )


def _record_failed_source(
    result: ProjectResult,
    summary: SourceRunSummary,
    index: int,
    error: Exception,
    partial_outcome: KmlExtractionOutcome | None,
) -> None:
    """Mark the source failed and warn; a partial KML read keeps its counters and the
    digest of the bytes read. Its rejected rows stay in the CSV, attributed to it; its
    points are dropped and nothing of it enters the totals."""
    source = summary.descriptor
    summary.status = SOURCE_FAILED
    summary.error = str(error)
    if partial_outcome is not None:
        _record_read_counts(summary, partial_outcome.counters, partial_outcome.read_report)
        summary.sha256 = partial_outcome.read_report.sha256_hex
        _record_kmz_entry(result, summary, index, partial_outcome.kmz_entry)
    result.warnings.append(f"source {index} '{source.label}' failed: {error}")
    logger.error(
        "Source %d '%s' cannot be read: %s (%d byte(s) read, %d rejected record(s) kept)",
        index,
        source.label,
        error,
        summary.bytes_processed,
        summary.counters.invalid,
    )


def _extract_source(
    source: SourceDescriptor,
    extraction_settings: ExtractionSettings,
    naive_time_zone: str,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None],
    is_cancelled: Callable[[], bool],
) -> SourceExtractionOutcome:
    """Hand the source to the reader of its format; a format error carries the partial
    outcome (SourceFormatError). A .json file that is neither a Google export nor
    GeoJSON is refused here, hashed but with nothing read."""
    if source.format == "kml":
        return extract_kml(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
        )
    if source.format == "kmz":
        return extract_kmz(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
        )
    if source.format == "gpx":
        return extract_gpx(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
        )
    if source.format == "csv":
        return extract_csv(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
            mapping_document=source.csv_mapping,
        )
    if source.format == FORMAT_GEOJSON:
        return extract_geojson(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
            naive_time_zone=naive_time_zone,
        )
    if source.format == UNRECOGNISED_FORMAT:
        read_report = whole_file_read_report(
            source.path, extraction_settings.read_chunk_bytes, "unknown"
        )
        raise SourceFormatError(
            f"{source.path.name}: {UNRECOGNISED_JSON_REASON}",
            SourceExtractionOutcome([], [], ExtractionCounters(), read_report, False),
        )
    if source.format.startswith("google-"):
        return extract_google(
            source.path,
            extraction_settings,
            on_rejected,
            on_progress,
            is_cancelled,
            source_id=source.identifier,
            source_format=source.format,
        )
    return extract_points(
        source.path,
        extraction_settings,
        on_rejected,
        on_progress,
        is_cancelled,
        source_id=source.identifier,
    )


def _add_to_totals(result: ProjectResult, summary: SourceRunSummary) -> None:
    result.lines_total += summary.lines_total
    result.bytes_processed += summary.bytes_processed
    result.counters.accepted += summary.counters.accepted
    result.counters.invalid += summary.counters.invalid
    result.counters.unrelated += summary.counters.unrelated
    result.counters.decode_replacements += summary.counters.decode_replacements
    result.counters.undated += summary.counters.undated


def _first_timestamp(points: list[GeoPoint]) -> datetime | None:
    return points[0].timestamp_utc if points else None


def _last_timestamp(points: list[GeoPoint]) -> datetime | None:
    return points[-1].timestamp_utc if points else None


def _log_source_analysis(entry: SourceAnalysis) -> None:
    report = entry.report
    logger.info(
        "Analysis '%s': %d of %d points analysed (%d left out by positioning method), %d stays, "
        "%d gaps, %d segments, %d implausible",
        entry.source.label,
        report.points_analysed,
        report.points_total,
        report.points_excluded_positioning_method,
        report.totals.stays,
        report.totals.gaps,
        len(report.segments),
        report.totals.implausible_segments,
    )
    for segment in report.segments:
        if segment.movement_class == "implausible":
            logger.warning(
                "Implausible segment in '%s' lines %d-%d: %.0f m in %.0f s (%.0f km/h)",
                entry.source.label,
                segment.from_line,
                segment.to_line,
                segment.distance_m,
                segment.duration_seconds,
                segment.speed_kmh or 0.0,
            )


def _source_document(source: SourceDescriptor) -> dict[str, object]:
    return {
        "id": source.identifier,
        "label": source.label,
        "kind": source.kind,
        "colour": source.colour,
        "file_name": source.path.name,
        "format": source.format,
        "accuracy_level": source.accuracy_level,
    }


def _record_output_hashes(workspace: ProjectWorkspace, result: ProjectResult) -> None:
    # Files written after metadata.json cannot be hashed into it; the manifest covers them.
    written_later = (
        workspace.log_path,
        workspace.metadata_path,
        workspace.report_html_path,
        workspace.manifest_path,
    )
    for path in workspace.output_paths():
        if path in written_later or not path.exists():
            continue
        digest = sha256_of_file(path)
        result.output_hashes[path.name] = digest
        logger.info("Output %s sha256=%s size=%d", path.name, digest, path.stat().st_size)


def _query_online_services(
    online: OnlineSettings,
    client: OnlineClient,
    source_analyses: list[SourceAnalysis],
    case_places: list[CasePlace],
    workspace: ProjectWorkspace,
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    summary: OnlineRunSummary,
    nominatim_min_interval_seconds: float,
) -> tuple[dict[str, list[Place]], list[CasePlace]]:
    """Case place addresses and stay addresses (one rate limit), then one Overpass query;
    failures never raise. Returns the places and the case places with geocoded positions.

    Addresses and anchors take the last position and the longest stays of every source in
    turn, so each source gets its share of the lookup and anchor limits.
    """
    multiple_sources = len(source_analyses) > 1
    lookup = AddressLookup(
        client,
        online.nominatim_endpoint,
        search_endpoint=online.nominatim_search_endpoint,
        min_interval_seconds=nominatim_min_interval_seconds,
    )
    if online.nominatim_enabled:
        case_places = _geocode_case_places(lookup, case_places, observer, is_cancelled, summary)
    # The first Nominatim failure ends every further Nominatim request of the run.
    if (
        online.nominatim_enabled
        and online.nominatim_max_lookups > 0
        and summary.nominatim_error is None
        and not summary.cancelled
    ):
        targets_by_source: list[list[tuple[str, PositionSummary | Stay]]] = []
        for entry in source_analyses:
            report = entry.report
            suffix = f" · {entry.source.label}" if multiple_sources else ""
            source_targets: list[tuple[str, PositionSummary | Stay]] = []
            if report.last is not None:
                source_targets.append((f"Last known position{suffix}", report.last))
            for stay in sorted(report.stays, key=lambda stay: stay.duration_minutes, reverse=True):
                source_targets.append((f"Stay {stay.identifier}{suffix}", stay))
            targets_by_source.append(source_targets)
        targets = interleave_round_robin(targets_by_source)[: online.nominatim_max_lookups]
        for index, (label, target) in enumerate(targets):
            if is_cancelled():
                summary.cancelled = True
                logger.warning(
                    "Online lookups cancelled after %d address(es)", summary.nominatim_addresses
                )
                break
            observer.on_online_progress(index, len(targets), "addresses")
            try:
                address = lookup.reverse(target.latitude, target.longitude)
            except (OnlineServiceError, ValueError) as error:
                summary.nominatim_error = str(error)
                logger.warning("Nominatim lookup for %s failed: %s", label, error)
                break
            target.address = address
            if address is not None:
                summary.nominatim_addresses += 1
        summary.nominatim_lookups = lookup.lookups
        observer.on_online_progress(len(targets), len(targets), "addresses")
    if lookup.lookups_log or lookup.searches_log:
        _save_online_responses(workspace.nominatim_path, lookup.lookups_log, lookup.searches_log)

    if not summary.cancelled and is_cancelled():
        summary.cancelled = True
        logger.warning("Overpass query skipped: the run was cancelled")

    places: dict[str, list[Place]] = {}
    if online.overpass_categories and not summary.cancelled:
        queried_categories, skipped_categories = categories_within_radius(
            online.overpass_categories, online.overpass_radius_m
        )
        summary.overpass_skipped_categories = skipped_categories
        if skipped_categories:
            logger.warning(
                "Overpass categories skipped because radius %d m exceeds their density limit: %s",
                online.overpass_radius_m,
                ", ".join(skipped_categories),
            )
        anchors = (
            choose_anchors_for_sources(
                [(entry.source.label, entry.report) for entry in source_analyses],
                online.overpass_radius_m,
            )
            if queried_categories
            else []
        )
        summary.overpass_anchors = len(anchors)
        summary.overpass_anchor_positions = [
            (anchor.latitude, anchor.longitude) for anchor in anchors
        ]
        if anchors:
            observer.on_online_progress(0, 1, "places")
            # Built here as well so the metadata carries the query even when the request fails.
            summary.overpass_query = build_overpass_query(
                anchors,
                online.overpass_radius_m,
                queried_categories,
                online.overpass_timeout_seconds,
            )
            summary.overpass_queried_categories = queried_categories
            try:
                overpass = fetch_places(
                    client,
                    online.overpass_endpoint,
                    anchors,
                    online.overpass_radius_m,
                    queried_categories,
                    online.overpass_timeout_seconds,
                )
            except OverpassResponseError as error:
                # An unparsable answer is still evidence of what the service returned.
                workspace.overpass_path.write_bytes(error.response.body)
                summary.overpass_error = str(error)
                logger.warning("Overpass response unusable: %s", error)
            except (OnlineServiceError, ValueError) as error:
                summary.overpass_error = str(error)
                logger.warning("Overpass query failed: %s", error)
            else:
                places = overpass.places
                summary.overpass_places = sum(len(entries) for entries in places.values())
                summary.overpass_remark = overpass.remark
                summary.overpass_truncated = overpass.truncated
                if overpass.remark:
                    logger.warning("Overpass remark: %s", overpass.remark)
                if overpass.truncated:
                    logger.warning(
                        "Overpass answer truncated at the output limit of %d elements; "
                        "reduce the radius or the categories for a complete result",
                        OUTPUT_LIMIT,
                    )
                workspace.overpass_path.write_bytes(overpass.response.body)
            observer.on_online_progress(1, 1, "places")
    return places, case_places


def _geocode_case_places(
    lookup: AddressLookup,
    case_places: list[CasePlace],
    observer: PipelineObserver,
    is_cancelled: Callable[[], bool],
    summary: OnlineRunSummary,
) -> list[CasePlace]:
    """One address search per case place entered without coordinates. The first answer is
    taken as it is and marked "geocoded, not verified by the examiner"; a failure ends the
    searches and leaves the remaining places not located."""
    pending = [place for place in case_places if not place.located and place.address]
    geocoded: dict[int, CasePlace] = {}
    for index, place in enumerate(pending):
        if is_cancelled():
            summary.cancelled = True
            logger.warning("Address searches cancelled after %d case place(s)", index)
            break
        observer.on_online_progress(index, len(pending), "case place addresses")
        try:
            answer = lookup.search(place.address)
        except (OnlineServiceError, ValueError) as error:
            summary.nominatim_error = str(error)
            logger.warning("Nominatim search for case place '%s' failed: %s", place.label, error)
            break
        if answer is None:
            summary.case_places_without_answer.append(place.label)
            continue
        geocoded[place.identifier] = replace(
            place,
            latitude=answer.latitude,
            longitude=answer.longitude,
            location=LOCATION_GEOCODED,
            geocoded_display_name=answer.display_name,
        )
    summary.nominatim_searches = lookup.searches
    summary.case_places_geocoded = len(geocoded)
    if pending:
        observer.on_online_progress(len(pending), len(pending), "case place addresses")
    return [geocoded.get(place.identifier, place) for place in case_places]


def _stored_response(query: dict[str, object], response: OnlineResponse) -> dict[str, object]:
    text = response.body.decode("utf-8", errors="replace")
    try:
        body: object = json.loads(text)
    except json.JSONDecodeError:
        body = text
    return {
        "query": query,
        "sha256": response.sha256_hex,
        "elapsed_seconds": round(response.elapsed_seconds, 3),
        "body_raw": text,
        "body": body,
    }


def _save_online_responses(
    path: Path,
    lookups_log: list[tuple[float, float, OnlineResponse]],
    searches_log: list[tuple[str, OnlineResponse]],
) -> None:
    """Keep raw service answers next to the analysis so addresses can be re-checked later:
    "responses" are the reverse lookups, "searches" the address searches for case places."""
    document: dict[str, object] = {
        "responses": [
            _stored_response({"lat": latitude, "lon": longitude}, response)
            for latitude, longitude, response in lookups_log
        ]
    }
    if searches_log:
        document["searches"] = [
            _stored_response({"q": address}, response) for address, response in searches_log
        ]
    write_metadata(path, document)


def _metadata_document(
    request: ProjectRequest,
    settings: Settings,
    workspace: ProjectWorkspace,
    result: ProjectResult,
) -> dict[str, object]:
    finished_local = datetime.now().astimezone()
    display_tz = resolve_display_timezone(settings.timezone.display)
    counters = result.counters
    notes = [
        "CSV cells contain unmodified source text; import as text, do not open by double-click"
    ]
    if result.tile_source_used == "online" and result.tile_provider_used is not None:
        provider = tile_provider_by_key(result.tile_provider_used)
        notes.append(
            f"Map uses online tiles from {provider.label}; opening it sends tile requests "
            f"(viewer IP, viewed area) to {', '.join(provider.origins)}. The map style can "
            "be switched in the browser to any built-in provider; that choice is a view "
            "setting and is not recorded"
        )
    if result.online.nominatim_searches:
        notes.append(
            f"Nominatim address search sent the address text of "
            f"{result.online.nominatim_searches} case place(s) to "
            f"{settings.online.nominatim_search_endpoint}; an answer is the first match of "
            "the service, not verified by the examiner"
        )
    if result.online.nominatim_lookups or result.online.nominatim_error:
        notes.append(
            "Nominatim reverse geocoding sent the last position and stay centres to "
            f"{settings.online.nominatim_endpoint}"
        )
    # The query was sent whenever it was built, even when it failed or matched nothing.
    if result.online.overpass_query is not None:
        notes.append(
            f"Overpass queries sent the analysed area to {settings.online.overpass_endpoint}"
        )
    fallback_endpoint = settings.online.overpass_fallback_endpoint
    if settings.online.enabled and fallback_endpoint:
        operator = (
            f" (operated by {DEFAULT_OVERPASS_FALLBACK_OPERATOR})"
            if fallback_endpoint == OnlineSettings().overpass_fallback_endpoint
            else ""
        )
        notes.append(
            "When the primary endpoint refuses a live place check of a map opened from disk, "
            f"the map asks the fallback {fallback_endpoint}{operator} once; a map opened "
            "from GEOSnap (key O) never uses the fallback"
        )
    return {
        "application": {
            "name": APP_NAME,
            "version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "case": {"reference": request.case_reference, "examiner": request.examiner},
        # As entered; geocoded positions and the check are in analysis.json.
        "case_places": {
            "file": None
            if request.case_places_file is None
            else {
                "name": request.case_places_file.name,
                "sha256": request.case_places_file.sha256,
            },
            "places": [case_place_to_document(place, display_tz) for place in request.case_places],
        },
        "project": {
            "name": workspace.project_name,
            "directory": str(workspace.directory),
            "started_local": workspace.created_local.isoformat(),
            "started_utc": workspace.created_utc.isoformat(),
            "finished_local": finished_local.isoformat(),
            "finished_utc": finished_local.astimezone(UTC).isoformat(),
            "status": result.status.value,
            "error": result.error_message,
        },
        "warnings": list(result.warnings),
        "sources": [
            _source_metadata(
                summary,
                naive_time_zone_for(settings.timezone.display, request.display_zone_origin),
                request.display_zone_origin,
            )
            for summary in _source_summaries(request, result)
        ],
        "counts": {
            "lines_total": result.lines_total,
            "bytes_processed": result.bytes_processed,
            "accepted": counters.accepted,
            "invalid": counters.invalid,
            "unrelated": counters.unrelated,
            "decode_replacements": counters.decode_replacements,
            "undated_records": counters.undated,
            "unique_positions": result.unique_positions,
            "map_points_total": result.map_points_total,
            "map_points_rendered": result.map_points_rendered,
            "thinning_stride": result.thinning_stride,
        },
        "settings": {
            "display_timezone": settings.timezone.display,
            "display_timezone_origin": request.display_zone_origin,
            "remove_duplicates": request.remove_duplicates,
            "point_limit": settings.map.point_limit,
            "tile_source_requested": settings.map.tile_source,
            "tile_source_used": result.tile_source_used,
            "tile_provider_requested": settings.map.tile_provider,
            "tile_provider_used": result.tile_provider_used,
            "tile_providers_selectable": list(TILE_PROVIDER_KEYS)
            if result.tile_source_used == "online"
            else [],
            "accuracy_confidence": settings.analysis.accuracy_confidence,
            "excluded_positioning_methods": list(settings.analysis.excluded_positioning_methods),
            "read_chunk_bytes": settings.extraction.read_chunk_bytes,
            "fallback_encoding": settings.extraction.fallback_encoding,
        },
        "outputs": {name: {"sha256": digest} for name, digest in result.output_hashes.items()},
        "analysis": None
        if not result.analysis_by_source
        else {
            "parameters": asdict(settings.analysis),
            "per_source": {
                str(source_id): _report_metadata(report)
                for source_id, report in result.analysis_by_source.items()
            },
            "encounters": len(result.encounters),
            "joint_movements": sum(
                1 for encounter in result.encounters if encounter.movement == MOVEMENT_JOINT
            ),
            "shared_places": len(result.shared_places),
            "case_places": len(result.case_places),
            "presence_matrix_rows": None
            if result.presence_matrix is None
            else len(result.presence_matrix.rows),
        },
        "online": {
            "enabled": result.online.enabled,
            "tiles_forced_offline": result.online.tiles_forced_offline,
            "cancelled": result.online.cancelled,
            "nominatim": {
                "enabled": settings.online.nominatim_enabled,
                "endpoint": settings.online.nominatim_endpoint,
                "lookups": result.online.nominatim_lookups,
                "search_endpoint": settings.online.nominatim_search_endpoint,
                "searches": result.online.nominatim_searches,
                "case_places_geocoded": result.online.case_places_geocoded,
                "addresses": result.online.nominatim_addresses,
                "error": result.online.nominatim_error,
            },
            "overpass": {
                "enabled": bool(settings.online.overpass_categories),
                "endpoint": settings.online.overpass_endpoint,
                # Online off: the map contacts no endpoint, so none is recorded.
                "browser_endpoint": (
                    settings.online.overpass_browser_endpoint or settings.online.overpass_endpoint
                )
                if settings.online.enabled
                else None,
                "fallback_endpoint": (settings.online.overpass_fallback_endpoint or None)
                if settings.online.enabled
                else None,
                "radius_m": settings.online.overpass_radius_m,
                "categories": list(settings.online.overpass_categories),
                "anchors": result.online.overpass_anchors,
                "places": result.online.overpass_places,
                "error": result.online.overpass_error,
                "query": result.online.overpass_query,
                "remark": result.online.overpass_remark,
                "truncated": result.online.overpass_truncated,
                "skipped_categories": list(result.online.overpass_skipped_categories),
            },
        },
        "notes": notes,
    }


def _source_summaries(request: ProjectRequest, result: ProjectResult) -> list[SourceRunSummary]:
    """The run's summaries, or all-skipped ones when the run failed before reading."""
    return result.sources or [SourceRunSummary(source) for source in request.sources]


def _kmz_entry_metadata(kmz_entry: KmzEntryReport | None) -> dict[str, object] | None:
    """The archive entry a KMZ source was read from; None for every other format."""
    if kmz_entry is None:
        return None
    return {
        "name": kmz_entry.name,
        "sha256": kmz_entry.sha256_hex,
        "bytes_read": kmz_entry.bytes_read,
        "read_completely": kmz_entry.read_completely,
        "other_kml_entries": list(kmz_entry.other_kml_entries),
        "other_kml_entry_count": kmz_entry.other_kml_entry_count,
    }


def _source_metadata(
    summary: SourceRunSummary, naive_time_zone: str, display_zone_origin: str
) -> dict[str, object]:
    source = summary.descriptor
    return {
        **_source_document(source),
        "csv_mapping": source.csv_mapping,
        "csv_zone_origin": source.zone_origin,
        "naive_timestamps": {
            "count": summary.counters.naive_timestamps,
            "handling": naive_timestamp_handling(
                source.format, source.csv_mapping, naive_time_zone, display_zone_origin
            ),
        },
        "path": str(source.path),
        "size_bytes": summary.size_bytes,
        "sha256": summary.sha256,
        "sha256_scope": summary.sha256_scope,
        "kmz_entry": _kmz_entry_metadata(summary.kmz_entry),
        "accuracy_reporting": summary.accuracy_reporting,
        "encoding": summary.encoding,
        "status": summary.status,
        "error": summary.error,
        "thinning_stride": summary.thinning_stride,
        "counts": {
            "lines_total": summary.lines_total,
            "bytes_processed": summary.bytes_processed,
            "accepted": summary.counters.accepted,
            "invalid": summary.counters.invalid,
            "unrelated": summary.counters.unrelated,
            "decode_replacements": summary.counters.decode_replacements,
            "undated_records": summary.counters.undated,
            "map_points_rendered": summary.map_points_rendered,
            "undated_rendered": summary.undated_rendered,
            "undated_thinning_stride": summary.undated_thinning_stride,
        },
    }


def _report_metadata(report: AnalysisReport) -> dict[str, object]:
    return {
        "points_analysed": report.points_analysed,
        "points_excluded_accuracy": report.points_excluded_accuracy,
        "points_excluded_positioning_method": report.points_excluded_positioning_method,
        "positioning_methods": dict(report.positioning_methods),
        "totals": {
            "time_span_minutes": round(report.totals.time_span_minutes, 1),
            "distance_m": round(report.totals.distance_m, 1),
            "max_speed_kmh": None
            if report.totals.max_speed_kmh is None
            else round(report.totals.max_speed_kmh, 1),
            "stays": report.totals.stays,
            "gaps": report.totals.gaps,
            "implausible_segments": report.totals.implausible_segments,
            "distance_left_out_m": round(report.totals.distance_left_out_m, 1),
            "segments_left_out": report.totals.segments_left_out,
        },
    }
