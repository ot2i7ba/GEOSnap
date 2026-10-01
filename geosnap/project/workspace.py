# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Project directory layout below output/ and the rules for naming it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from geosnap import PROJECT_LOG_NAME

PROJECT_NAME_MIN_LENGTH = 3
PROJECT_NAME_MAX_LENGTH = 32
MAX_OUTPUT_PATH_LENGTH = 240
STAMP_FORMAT = "%Y%m%d_%H%M%S"
DISALLOWED_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]")
# Written last; lists every file of the project directory except itself, reopen.log and
# the record chain directories search_areas/ and speed_ranges/.
MANIFEST_FILE_NAME = "MANIFEST.sha256"
# Search areas recorded from the map after the run; outside the manifest, own hash chain.
SEARCH_AREAS_DIR_NAME = "search_areas"
SEARCH_AREA_RECORDS_NAME = "records.jsonl"
# Speed ranges recorded from the map's Speed tool; same kind of hash chain.
SPEED_RANGES_DIR_NAME = "speed_ranges"
# Written when a finished project is reopened; grows after the run, outside the manifest.
# The run's own log is covered by the manifest and is never written again.
REOPEN_LOG_NAME = "reopen.log"


class ProjectNameError(ValueError):
    """The project name is unusable even after sanitising."""


class WorkspacePathError(ValueError):
    """The project directory would exceed the safe path length."""


def sanitize_project_name(raw_name: str) -> str:
    """Strip everything except letters, digits, underscore and hyphen."""
    return DISALLOWED_NAME_CHARACTERS.sub("", raw_name.strip())


def validate_project_name(raw_name: str) -> str:
    """Return the sanitised name or explain why it cannot be used."""
    name = sanitize_project_name(raw_name)
    if len(name) < PROJECT_NAME_MIN_LENGTH:
        raise ProjectNameError(
            f"Project name needs at least {PROJECT_NAME_MIN_LENGTH} characters "
            f"from A-Z, a-z, 0-9, _ or - (after cleaning: '{name}')"
        )
    if len(name) > PROJECT_NAME_MAX_LENGTH:
        raise ProjectNameError(
            f"Project name must not exceed {PROJECT_NAME_MAX_LENGTH} characters "
            f"(after cleaning: '{name}', {len(name)} characters)"
        )
    return name


@dataclass(frozen=True, slots=True)
class ProjectWorkspace:
    directory: Path
    project_name: str
    stamp: str
    created_local: datetime
    created_utc: datetime

    @property
    def log_path(self) -> Path:
        return self.directory / PROJECT_LOG_NAME

    @property
    def points_csv_path(self) -> Path:
        return self.directory / f"points_{self.stamp}.csv"

    @property
    def rejected_csv_path(self) -> Path:
        return self.directory / f"rejected_{self.stamp}.csv"

    @property
    def map_html_path(self) -> Path:
        return self.directory / f"map_{self.stamp}.html"

    @property
    def metadata_path(self) -> Path:
        return self.directory / "metadata.json"

    @property
    def analysis_path(self) -> Path:
        return self.directory / "analysis.json"

    @property
    def stays_csv_path(self) -> Path:
        return self.directory / f"stays_{self.stamp}.csv"

    @property
    def gaps_csv_path(self) -> Path:
        return self.directory / f"gaps_{self.stamp}.csv"

    @property
    def segments_csv_path(self) -> Path:
        return self.directory / f"segments_{self.stamp}.csv"

    @property
    def encounters_csv_path(self) -> Path:
        return self.directory / f"encounters_{self.stamp}.csv"

    @property
    def shared_places_csv_path(self) -> Path:
        return self.directory / f"shared_places_{self.stamp}.csv"

    @property
    def case_places_csv_path(self) -> Path:
        return self.directory / f"case_places_{self.stamp}.csv"

    @property
    def presence_matrix_csv_path(self) -> Path:
        return self.directory / f"presence_matrix_{self.stamp}.csv"

    @property
    def gpx_path(self) -> Path:
        return self.directory / f"route_{self.stamp}.gpx"

    @property
    def kml_path(self) -> Path:
        return self.directory / f"route_{self.stamp}.kml"

    @property
    def overpass_path(self) -> Path:
        return self.directory / f"overpass_{self.stamp}.json"

    @property
    def nominatim_path(self) -> Path:
        return self.directory / f"nominatim_{self.stamp}.json"

    @property
    def report_html_path(self) -> Path:
        return self.directory / f"report_{self.stamp}.html"

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_FILE_NAME

    @property
    def search_areas_directory(self) -> Path:
        return self.directory / SEARCH_AREAS_DIR_NAME

    def output_paths(self) -> tuple[Path, ...]:
        """Every file a run writes; search_areas/ is filled later from the map and left out."""
        return (
            self.log_path,
            self.points_csv_path,
            self.rejected_csv_path,
            self.map_html_path,
            self.metadata_path,
            self.analysis_path,
            self.stays_csv_path,
            self.gaps_csv_path,
            self.segments_csv_path,
            self.encounters_csv_path,
            self.shared_places_csv_path,
            self.case_places_csv_path,
            self.presence_matrix_csv_path,
            self.gpx_path,
            self.kml_path,
            self.overpass_path,
            self.nominatim_path,
            self.report_html_path,
            self.manifest_path,
        )


def plan_workspace(
    output_root: Path, project_name: str, created_local: datetime
) -> ProjectWorkspace:
    """Compute the project directory (no I/O). created_local must be timezone-aware."""
    stamp = created_local.strftime(STAMP_FORMAT)
    directory = (
        output_root / f"{created_local:%Y}" / f"{created_local:%m}" / f"{stamp}_{project_name}"
    )
    return ProjectWorkspace(
        directory=directory,
        project_name=project_name,
        stamp=stamp,
        created_local=created_local,
        created_utc=created_local.astimezone(UTC),
    )


def create_workspace(workspace: ProjectWorkspace) -> None:
    """Create the project directory after checking the longest resulting path."""
    longest_path = max(len(str(path)) for path in workspace.output_paths())
    if longest_path > MAX_OUTPUT_PATH_LENGTH:
        raise WorkspacePathError(
            f"Output path would be {longest_path} characters, above the limit of "
            f"{MAX_OUTPUT_PATH_LENGTH}; move the application to a shorter path"
        )
    workspace.directory.mkdir(parents=True, exist_ok=False)
