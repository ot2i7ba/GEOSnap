# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Finished projects below output/: find them and log what happens when one is reopened.

Project directories are output/<YYYY>/<MM>/<stamp>_<name> (workspace.plan_workspace).
Only real directories at exactly that depth are listed; symbolic links are never followed,
so nothing outside the output directory can be listed, verified or served.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geosnap.audit_log import attach_project_log
from geosnap.project.workspace import (
    MANIFEST_FILE_NAME,
    REOPEN_LOG_NAME,
    SEARCH_AREA_RECORDS_NAME,
    SEARCH_AREAS_DIR_NAME,
)

logger = logging.getLogger(__name__)

YEAR_DIR_PATTERN = re.compile(r"^\d{4}$", re.ASCII)
MONTH_DIR_PATTERN = re.compile(r"^\d{2}$", re.ASCII)
# <stamp>_<name> with the characters workspace.sanitize_project_name leaves.
PROJECT_DIR_PATTERN = re.compile(r"^(\d{8}_\d{6})_([A-Za-z0-9_-]+)$", re.ASCII)
METADATA_FILE_NAME = "metadata.json"
# metadata.json of a real project is a few kilobytes; a larger file is not parsed.
METADATA_MAX_BYTES = 8 * 1024 * 1024
RECORDS_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FinishedProject:
    directory: Path
    stamp: str
    project_name: str
    # From metadata.json; None when it is missing, unreadable or lacks the value.
    started_local: str | None
    tile_source_used: str | None
    search_area_count: int
    has_manifest: bool
    # The project's own map file; None when it is missing or not a regular file.
    map_html_path: Path | None


def discover_finished_projects(output_root: Path) -> list[FinishedProject]:
    """Every project directory below output_root, newest first (by stamp, then name)."""
    projects = [
        _describe_project(project_dir, match.group(1), match.group(2))
        for year_dir in _real_subdirectories(output_root, YEAR_DIR_PATTERN)
        for month_dir in _real_subdirectories(year_dir, MONTH_DIR_PATTERN)
        for project_dir in _real_subdirectories(month_dir, PROJECT_DIR_PATTERN)
        if (match := PROJECT_DIR_PATTERN.match(project_dir.name)) is not None
    ]
    return sorted(projects, key=lambda project: project.directory.name, reverse=True)


def attach_reopen_log(project_dir: Path) -> logging.Handler:
    """Append this thread's log records to the project's reopen.log until detached.

    The run log is covered by the manifest, so it is never written again. Raises OSError
    when reopen.log is a symbolic link or cannot be opened for appending.
    """
    reopen_log_path = project_dir / REOPEN_LOG_NAME
    try:
        is_regular_file = stat.S_ISREG(reopen_log_path.lstat().st_mode)
    except FileNotFoundError:
        is_regular_file = True  # created by the handler
    if not is_regular_file:
        # A link would redirect the lines; a FIFO would block the UI thread on open.
        raise OSError(f"{reopen_log_path} is not a regular file; it is not written to")
    return attach_project_log(reopen_log_path)


def servable_map_path(project: FinishedProject) -> Path | None:
    """The project's map file when it still is a regular file directly inside the project
    directory (checked again right before serving); None otherwise."""
    map_html_path = project.map_html_path
    if map_html_path is None or not _is_regular_file(map_html_path):
        return None
    search_areas_dir = project.directory / SEARCH_AREAS_DIR_NAME
    try:
        project_dir = project.directory.resolve(strict=True)
        inside_project = map_html_path.resolve(strict=True).parent == project_dir
        # The map server records search areas there; a link would redirect those writes.
        own_search_areas = not search_areas_dir.exists() or (
            not search_areas_dir.is_symlink()
            and search_areas_dir.resolve(strict=True) == project_dir / SEARCH_AREAS_DIR_NAME
        )
    except OSError:
        return None
    return map_html_path if inside_project and own_search_areas else None


def _real_subdirectories(parent: Path, name_pattern: re.Pattern[str]) -> list[Path]:
    """Directories directly in parent whose name matches; symbolic links are left out."""
    try:
        with os.scandir(parent) as entries:
            candidates = [
                Path(entry.path)
                for entry in entries
                if name_pattern.match(entry.name) and entry.is_dir(follow_symlinks=False)
            ]
        # Resolving catches what is_dir cannot name a link, e.g. a Windows directory junction.
        resolved_parent = parent.resolve(strict=True)
        return [
            candidate
            for candidate in candidates
            if candidate.resolve(strict=True) == resolved_parent / candidate.name
        ]
    except OSError as error:
        if parent.exists():
            logger.warning("Cannot list %s: %s", parent, error)
        return []


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _describe_project(
    project_dir: Path, stamp: str, directory_project_name: str
) -> FinishedProject:
    metadata = _read_metadata(project_dir / METADATA_FILE_NAME)
    project_block = metadata.get("project")
    settings_block = metadata.get("settings")
    map_html_path = project_dir / f"map_{stamp}.html"
    return FinishedProject(
        directory=project_dir,
        stamp=stamp,
        project_name=directory_project_name,
        started_local=_text_value(project_block, "started_local"),
        tile_source_used=_text_value(settings_block, "tile_source_used"),
        search_area_count=_count_search_area_records(project_dir),
        has_manifest=_is_regular_file(project_dir / MANIFEST_FILE_NAME),
        map_html_path=map_html_path if _is_regular_file(map_html_path) else None,
    )


def _read_metadata(metadata_path: Path) -> dict[str, Any]:
    """metadata.json as a mapping; empty when it is missing, too large or not a JSON object."""
    try:
        if not _is_regular_file(metadata_path):
            return {}
        if metadata_path.stat().st_size > METADATA_MAX_BYTES:
            logger.warning("%s is larger than expected; it is not read", metadata_path)
            return {}
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        logger.warning("Cannot read %s: %s", metadata_path, error)
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _text_value(block: object, key: str) -> str | None:
    value = block.get(key) if isinstance(block, dict) else None
    return value if isinstance(value, str) else None


def _count_search_area_records(project_dir: Path) -> int:
    """Lines of search_areas/records.jsonl; the chain itself is checked by verify."""
    search_areas_dir = project_dir / SEARCH_AREAS_DIR_NAME
    records_path = search_areas_dir / SEARCH_AREA_RECORDS_NAME
    try:
        if search_areas_dir.is_symlink() or not _is_regular_file(records_path):
            return 0
        if records_path.stat().st_size > RECORDS_MAX_BYTES:
            logger.warning("%s is larger than expected; its records are not counted", records_path)
            return 0
        return sum(1 for line in records_path.read_bytes().split(b"\n") if line.strip())
    except OSError as error:
        logger.warning("Cannot read %s: %s", records_path, error)
        return 0
