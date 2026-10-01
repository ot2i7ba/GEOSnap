# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""MANIFEST.sha256: the SHA-256 of every file in a project directory, in sha256sum format.

The manifest is the last file a run writes. It lists every file below the project
directory except itself, the search_areas/ directory, which grows after the run and
carries its own hash chain (search_areas/records.jsonl), and reopen.log, which is
appended to whenever the finished project is reopened.

Links (symbolic links, Windows junctions) are never followed and never listed; --verify
reports them, empty directories and special files as unexpected entries.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from geosnap.project.metadata import sha256_of_file
from geosnap.project.workspace import (
    MANIFEST_FILE_NAME,
    REOPEN_LOG_NAME,
    SEARCH_AREAS_DIR_NAME,
    SPEED_RANGES_DIR_NAME,
)

__all__ = [
    "MANIFEST_FILE_NAME",
    "RECORD_CHAIN_DIR_NAMES",
    "DirectoryScan",
    "ProjectFileEntry",
    "UnexpectedEntry",
    "is_link",
    "is_manifest_covered",
    "list_project_files",
    "parse_manifest_line",
    "scan_directory",
    "unexpected_project_entries",
    "write_manifest",
]

# Top-level directories that grow after the run and carry their own hash chain
# (<directory>/records.jsonl); the manifest leaves them out, --verify checks each chain.
RECORD_CHAIN_DIR_NAMES: tuple[str, ...] = (SEARCH_AREAS_DIR_NAME, SPEED_RANGES_DIR_NAME)

LINK_KIND = "link, not followed"
EMPTY_DIRECTORY_KIND = "empty directory"
SPECIAL_FILE_KIND = "not a regular file"
UNREADABLE_DIRECTORY_KIND = "unreadable directory"

# sha256sum lines: 64 lower-case hex digits, a space, then " " (text) or "*" (binary mode).
MANIFEST_LINE_PATTERN = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


@dataclass(frozen=True, slots=True)
class ProjectFileEntry:
    relative_path: str  # forward slashes, relative to the project directory
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UnexpectedEntry:
    relative_path: str  # forward slashes, relative to the scanned directory
    kind: str  # LINK_KIND, EMPTY_DIRECTORY_KIND, SPECIAL_FILE_KIND, UNREADABLE_DIRECTORY_KIND


@dataclass(frozen=True, slots=True)
class DirectoryScan:
    regular_files: list[str]  # relative paths, forward slashes, sorted
    unexpected_entries: list[UnexpectedEntry]


# Windows reparse tags that redirect to another path (name surrogates): symbolic link and
# junction/mount point. Other reparse points (OneDrive files on demand, deduplicated
# files) are ordinary files and must not be reported as links.
REDIRECTING_REPARSE_TAGS = frozenset({0xA000000C, 0xA0000003})


def is_link(status: os.stat_result) -> bool:
    """True for a symbolic link and for a Windows junction or mount point. A reparse point
    whose tag is not reported counts as a link: never followed is the safe side."""
    if stat.S_ISLNK(status.st_mode):
        return True
    reparse_attribute = getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not reparse_attribute:
        return False
    reparse_tag = getattr(status, "st_reparse_tag", None)
    return reparse_tag is None or reparse_tag == 0 or reparse_tag in REDIRECTING_REPARSE_TAGS


def scan_directory(root: Path, skipped_top_level_names: Collection[str] = ()) -> DirectoryScan:
    """Every regular file below root, and every entry that is a link, an empty directory, a
    special file or an unreadable directory. Links are reported, never followed. Entries of
    root named in skipped_top_level_names are left out whatever they are."""
    regular_files: list[str] = []
    unexpected_entries: list[UnexpectedEntry] = []
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as entries:
                listed = list(entries)
        except OSError:
            if directory == root:
                raise
            unexpected_entries.append(UnexpectedEntry(prefix[:-1], UNREADABLE_DIRECTORY_KIND))
            continue
        if not listed and directory != root:
            unexpected_entries.append(UnexpectedEntry(prefix[:-1], EMPTY_DIRECTORY_KIND))
        for entry in listed:
            if directory == root and entry.name in skipped_top_level_names:
                continue
            relative_path = prefix + entry.name
            status = entry.stat(follow_symlinks=False)
            if is_link(status):
                unexpected_entries.append(UnexpectedEntry(relative_path, LINK_KIND))
            elif stat.S_ISDIR(status.st_mode):
                pending.append((Path(entry.path), relative_path + "/"))
            elif stat.S_ISREG(status.st_mode):
                regular_files.append(relative_path)
            else:
                unexpected_entries.append(UnexpectedEntry(relative_path, SPECIAL_FILE_KIND))
    unexpected_entries.sort(key=lambda entry: entry.relative_path)
    return DirectoryScan(sorted(regular_files), unexpected_entries)


def is_manifest_covered(relative_path: str) -> bool:
    """True for files the manifest lists: not the manifest itself, not the reopen log of
    the project root, nothing in a record chain directory such as search_areas/."""
    parts = PurePosixPath(relative_path).parts
    return relative_path not in (MANIFEST_FILE_NAME, REOPEN_LOG_NAME) and parts[:1] not in [
        (name,) for name in RECORD_CHAIN_DIR_NAMES
    ]


def covered_relative_paths(project_dir: Path) -> list[str]:
    """Relative paths of all regular files the manifest covers, sorted by their text."""
    scan = scan_directory(project_dir, RECORD_CHAIN_DIR_NAMES)
    return [path for path in scan.regular_files if is_manifest_covered(path)]


def unexpected_project_entries(project_dir: Path) -> list[UnexpectedEntry]:
    """Links, empty directories and special files in the project directory, outside the
    record chain directories (--verify checks those itself)."""
    return scan_directory(project_dir, RECORD_CHAIN_DIR_NAMES).unexpected_entries


def list_project_files(
    project_dir: Path, excluded_names: Collection[str] = ()
) -> list[ProjectFileEntry]:
    """Size and SHA-256 of every covered file, minus the given relative paths."""
    entries = []
    for relative_path in covered_relative_paths(project_dir):
        if relative_path in excluded_names:
            continue
        path = project_dir / relative_path
        entries.append(ProjectFileEntry(relative_path, path.stat().st_size, sha256_of_file(path)))
    return entries


def write_manifest(project_dir: Path) -> str:
    """Write MANIFEST.sha256 (LF line ends, sorted) and return its own SHA-256."""
    manifest_text = "".join(
        f"{entry.sha256}  {entry.relative_path}\n" for entry in list_project_files(project_dir)
    )
    manifest_bytes = manifest_text.encode("utf-8")
    (project_dir / MANIFEST_FILE_NAME).write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def parse_manifest_line(line: str) -> tuple[str, str] | None:
    """(sha256, relative path) of one manifest line, or None when it is malformed or its
    path could leave the project directory (absolute, drive letter, backslash, "..")."""
    match = MANIFEST_LINE_PATTERN.match(line)
    if match is None:
        return None
    digest, relative_path = match.groups()
    parts = PurePosixPath(relative_path).parts
    unsafe = (
        not parts
        or relative_path.startswith("/")
        or "\\" in relative_path
        or ":" in relative_path
        or any(part in ("..", ".") for part in parts)
        or relative_path.strip() != relative_path
    )
    return None if unsafe else (digest, relative_path)
