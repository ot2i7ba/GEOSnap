# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Check a project directory against its manifest, the hash chain of every record chain
directory (search_areas/) and the source files recorded in metadata.json (GEOSnap --verify).

Output is plain text with ASCII status words so it reads on any Windows console; control
characters in names are printed escaped. Exit codes: 0 everything matches, 1 at least one
deviation, 2 the call itself failed.

A record chain is not covered by the manifest and carries no key: a chain removed or
rewritten as a whole still reads as intact. The report therefore prints each chain's record
count and last line SHA-256 for comparison with the values noted in the case file.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from geosnap import APP_NAME, __version__
from geosnap.project.manifest import (
    MANIFEST_FILE_NAME,
    RECORD_CHAIN_DIR_NAMES,
    covered_relative_paths,
    is_link,
    parse_manifest_line,
    scan_directory,
    unexpected_project_entries,
)
from geosnap.project.metadata import sha256_of_file
from geosnap.project.workspace import (
    REOPEN_LOG_NAME,
    SEARCH_AREA_RECORDS_NAME,
    SEARCH_AREAS_DIR_NAME,
    SPEED_RANGES_DIR_NAME,
)

EXIT_OK = 0
EXIT_DEVIATION = 1
EXIT_USAGE = 2

STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_MISSING = "MISSING"
STATUS_EXTRA = "EXTRA"
STATUS_UNREADABLE = "UNREADABLE"
STATUS_NOT_AVAILABLE = "NOT AVAILABLE"
STATUS_NOT_RECORDED = "NOT RECORDED"
STATUS_NOT_COMPARED = "NOT COMPARED"
STATUS_NOT_CHECKED = "NOT CHECKED"

# previous_line_sha256 of the first line of search_areas/records.jsonl. Every later line
# carries the SHA-256 of the previous line's UTF-8 bytes without its "\n" terminator.
GENESIS_PREVIOUS_LINE_SHA256 = "0" * 64
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
METADATA_FILE_NAME = "metadata.json"
WHOLE_FILE_SCOPE = "whole file"
# Lock file next to records.jsonl in every record chain directory; the recording processes
# take it exclusively. It is no record file and carries no evidence.
CHAIN_LOCK_FILE_NAME = "records.lock"
# Heading of each record chain directory's report section; one entry per name in
# manifest.RECORD_CHAIN_DIR_NAMES.
RECORD_CHAIN_TITLES: dict[str, str] = {
    SEARCH_AREAS_DIR_NAME: "Search areas",
    SPEED_RANGES_DIR_NAME: "Speed ranges",
}
LINK_NOT_FOLLOWED = "link, not followed"
# UNC (\\host\share, //host/share), device (\\.\, \\?\) and NT (\??\) paths: opening one
# would make Windows contact a server or a device.
NETWORK_OR_DEVICE_PATH_PATTERN = re.compile(r"^(?:[\\/]{2}|\\\?\?\\)")
# Printed escaped: control and format characters (ESC, line feeds, bidi overrides), lone
# surrogates of undecodable file names and the Unicode line and paragraph separators.
ESCAPED_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class VerificationUsageError(ValueError):
    """The path is no project directory that can be verified."""


@dataclass(frozen=True, slots=True)
class FileCheck:
    status: str
    subject: str  # relative path, or "source <id> '<label>': <path>"
    detail: str = ""


@dataclass(slots=True)
class RecordChainReport:
    """The hash chain <dir_name>/records.jsonl and the files its records list."""

    dir_name: str
    title: str
    record_count: int = 0
    # SHA-256 of the last records.jsonl line (without its line feed): the chain's anchor.
    last_line_sha256: str | None = None
    file_checks: list[FileCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerificationReport:
    project_dir: Path
    manifest_sha256: str
    file_checks: list[FileCheck] = field(default_factory=list)
    manifest_problems: list[str] = field(default_factory=list)
    record_chains: list[RecordChainReport] = field(default_factory=list)
    source_checks: list[FileCheck] = field(default_factory=list)
    source_note: str | None = None
    # reopen.log exists: it grows with every reopening and is outside the manifest.
    reopen_log_present: bool = False

    def _chain(self, dir_name: str) -> RecordChainReport:
        return next(
            (chain for chain in self.record_chains if chain.dir_name == dir_name),
            RecordChainReport(dir_name, RECORD_CHAIN_TITLES[dir_name]),
        )

    @property
    def search_area_records(self) -> int:
        return self._chain(SEARCH_AREAS_DIR_NAME).record_count

    @property
    def search_area_checks(self) -> list[FileCheck]:
        return self._chain(SEARCH_AREAS_DIR_NAME).file_checks

    @property
    def search_area_problems(self) -> list[str]:
        return self._chain(SEARCH_AREAS_DIR_NAME).problems

    @property
    def deviation_count(self) -> int:
        failing_statuses = {STATUS_MISMATCH, STATUS_MISSING, STATUS_EXTRA, STATUS_UNREADABLE}
        checks = self.file_checks + self.source_checks
        for chain in self.record_chains:
            checks = checks + chain.file_checks
        return (
            sum(1 for check in checks if check.status in failing_statuses)
            + len(self.manifest_problems)
            + sum(len(chain.problems) for chain in self.record_chains)
        )


def verify_command(project_dir: Path) -> str:
    """The command line that verifies project_dir with this program."""
    program = Path(sys.executable).name if getattr(sys, "frozen", False) else "python GEOSnap.py"
    return f'{program} --verify "{project_dir}"'


def verify_project(project_dir: Path) -> VerificationReport:
    if not project_dir.is_dir():
        raise VerificationUsageError(f"{project_dir} is not a directory")
    manifest_path = project_dir / MANIFEST_FILE_NAME
    if not manifest_path.is_file():
        raise VerificationUsageError(
            f"{project_dir} has no {MANIFEST_FILE_NAME}: not a {APP_NAME} project directory, "
            "or the run did not finish"
        )
    manifest_bytes = manifest_path.read_bytes()
    report = VerificationReport(project_dir, hashlib.sha256(manifest_bytes).hexdigest())
    _check_manifest(project_dir, manifest_bytes, report)
    report.reopen_log_present = (project_dir / REOPEN_LOG_NAME).is_file()
    for dir_name in RECORD_CHAIN_DIR_NAMES:
        report.record_chains.append(
            check_record_chain(project_dir, dir_name, RECORD_CHAIN_TITLES[dir_name])
        )
    metadata_matches_manifest = any(
        check.subject == METADATA_FILE_NAME and check.status == STATUS_OK
        for check in report.file_checks
    )
    if metadata_matches_manifest:
        _check_sources(project_dir / METADATA_FILE_NAME, report)
    else:
        report.source_note = (
            f"not checked: {METADATA_FILE_NAME} does not match the manifest, "
            "so its recorded paths are not trusted"
        )
    return report


def run_verify(path_text: str, out: TextIO) -> int:
    """Verify the project directory at path_text, print the report, return the exit code."""
    try:
        report = verify_project(Path(path_text))
    except (VerificationUsageError, OSError) as error:
        out.write(f"ERROR: {error}\n")
        return EXIT_USAGE
    for line in render_report(report):
        out.write(line + "\n")
    return EXIT_DEVIATION if report.deviation_count else EXIT_OK


def render_report(report: VerificationReport) -> list[str]:
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"{APP_NAME} {__version__} verification of {report.project_dir}",
        f"Checked at {checked_at}",
        f"{MANIFEST_FILE_NAME} SHA-256 {report.manifest_sha256}",
        "(compare with the value on the summary screen or in the application log)",
        "",
        "Files listed in the manifest:",
    ]
    lines.extend(_check_line(check, 10) for check in report.file_checks)
    lines.extend(f"MALFORMED {problem}" for problem in report.manifest_problems)
    if report.reopen_log_present:
        lines.append(
            f"{REOPEN_LOG_NAME} is not covered by the manifest: it is appended to whenever "
            "the finished project is reopened"
        )
    lines.append("")
    for chain in report.record_chains:
        lines.extend(render_chain(chain))
        lines.append("")
    lines.append("Source files at their recorded paths:")
    if report.source_note:
        lines.append(f"  {report.source_note}")
    lines.extend(_check_line(check, 15) for check in report.source_checks)
    lines.append("")
    deviations = report.deviation_count
    if deviations:
        lines.append(f"RESULT: DEVIATION - {deviations} problem(s) found")
    else:
        lines.append("RESULT: OK - everything matches")
    lines.extend(_case_file_comparison_line(chain) for chain in report.record_chains)
    return [printable_text(line) for line in lines]


def render_chain(chain: RecordChainReport) -> list[str]:
    """The report section of one record chain directory."""
    if chain.record_count or chain.file_checks or chain.problems:
        chain_state = "CHAIN BROKEN" if chain.problems else "chain intact"
        lines = [f"{chain.title}: {chain.record_count} record(s), {chain_state}"]
        lines.extend(f"  {problem}" for problem in chain.problems)
        if chain.last_line_sha256:
            lines.append(
                f"  {chain.dir_name}/{SEARCH_AREA_RECORDS_NAME} last line SHA-256 "
                f"{chain.last_line_sha256}"
            )
    else:
        lines = [f"{chain.title}: none recorded"]
    lines.append(
        f"  {chain.dir_name}/ is not covered by {MANIFEST_FILE_NAME}: its hash chain shows "
        "that the records fit together, not that none were removed or all rewritten."
    )
    lines.append(
        "  Compare the record count and the last line SHA-256 with the values noted in the "
        "case file (repeated at the end of this report)."
    )
    lines.extend(_check_line(check, 10) for check in chain.file_checks)
    return lines


def _case_file_comparison_line(chain: RecordChainReport) -> str:
    anchor = f", last line SHA-256 {chain.last_line_sha256}" if chain.last_line_sha256 else ""
    return f"COMPARE WITH THE CASE FILE: {chain.dir_name}/ {chain.record_count} record(s){anchor}"


def printable_text(text: str) -> str:
    """text with control, format and separator characters escaped (\\x1b, \\u202e); other
    characters, umlauts included, stay as they are."""
    return "".join(
        character.encode("unicode_escape", "backslashreplace").decode("ascii")
        if unicodedata.category(character) in ESCAPED_CHARACTER_CATEGORIES
        else character
        for character in text
    )


def _check_line(check: FileCheck, width: int) -> str:
    detail = f" ({check.detail})" if check.detail else ""
    return f"{check.status:<{width}}{check.subject}{detail}"


def _compare_file(
    path: Path, subject: str, expected_sha256: str, follow_links: bool = True
) -> FileCheck:
    """Without follow_links (files inside the project), a link at path is a mismatch."""
    if not follow_links:
        try:
            status = path.lstat()
        except OSError:
            return FileCheck(STATUS_MISSING, subject)
        if is_link(status):
            return FileCheck(STATUS_MISMATCH, subject, LINK_NOT_FOLLOWED)
        if not stat.S_ISREG(status.st_mode):
            return FileCheck(STATUS_MISSING, subject)
    elif not path.is_file():
        return FileCheck(STATUS_MISSING, subject)
    try:
        found = sha256_of_file(path)
    except OSError as error:
        return FileCheck(STATUS_UNREADABLE, subject, str(error))
    if found == expected_sha256:
        return FileCheck(STATUS_OK, subject)
    return FileCheck(STATUS_MISMATCH, subject, f"expected {expected_sha256}, found {found}")


def _check_manifest(project_dir: Path, manifest_bytes: bytes, report: VerificationReport) -> None:
    listed: set[str] = set()
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        report.manifest_problems.append(f"{MANIFEST_FILE_NAME} is not UTF-8 text: {error}")
        return
    for line_number, line in enumerate(manifest_text.splitlines(), start=1):
        if not line.strip():
            continue
        entry = parse_manifest_line(line)
        if entry is None:
            report.manifest_problems.append(f"line {line_number}: {line!r}")
            continue
        expected_sha256, relative_path = entry
        listed.add(relative_path)
        report.file_checks.append(
            _compare_file(
                project_dir / relative_path, relative_path, expected_sha256, follow_links=False
            )
        )
    for relative_path in covered_relative_paths(project_dir):
        if relative_path not in listed:
            report.file_checks.append(FileCheck(STATUS_EXTRA, relative_path))
    for unexpected_entry in unexpected_project_entries(project_dir):
        if unexpected_entry.relative_path not in listed:
            report.file_checks.append(
                FileCheck(STATUS_EXTRA, unexpected_entry.relative_path, unexpected_entry.kind)
            )


def check_record_chain(project_dir: Path, dir_name: str, title: str) -> RecordChainReport:
    """Check <dir_name>/records.jsonl line by line (previous_line_sha256 chain, file
    hashes) and report every file of the directory that no record lists."""
    chain = RecordChainReport(dir_name, title)
    chain_dir = project_dir / dir_name
    try:
        chain_dir_status = chain_dir.lstat()
    except FileNotFoundError:
        return chain
    if is_link(chain_dir_status) or not stat.S_ISDIR(chain_dir_status.st_mode):
        chain.problems.append(f"{dir_name} is a link or no directory; not checked")
        return chain
    scan = scan_directory(chain_dir)
    recorded_names: set[str] = set()
    if SEARCH_AREA_RECORDS_NAME in scan.regular_files:
        raw_lines = (chain_dir / SEARCH_AREA_RECORDS_NAME).read_bytes().split(b"\n")
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        if raw_lines:
            chain.last_line_sha256 = hashlib.sha256(raw_lines[-1]).hexdigest()
        previous_line: bytes | None = None
        for line_number, raw_line in enumerate(raw_lines, start=1):
            expected_previous = (
                GENESIS_PREVIOUS_LINE_SHA256
                if previous_line is None
                else hashlib.sha256(previous_line).hexdigest()
            )
            previous_line = raw_line
            chain.record_count += 1
            record = _parse_record(raw_line)
            if isinstance(record, str):
                chain.problems.append(f"line {line_number}: {record}")
                continue
            if record["previous_line_sha256"] != expected_previous:
                chain.problems.append(
                    f"CHAIN BROKEN at line {line_number}: previous_line_sha256 does not match "
                    + ("the start value" if line_number == 1 else f"line {line_number - 1}")
                )
            for name, expected_sha256 in record["files"].items():
                if not _is_plain_file_name(name):
                    chain.problems.append(f"line {line_number}: invalid file name {name!r}")
                    continue
                recorded_names.add(name)
                chain.file_checks.append(
                    _compare_file(
                        chain_dir / name, f"{dir_name}/{name}", expected_sha256, follow_links=False
                    )
                )
    for relative_path in scan.regular_files:
        if relative_path not in (SEARCH_AREA_RECORDS_NAME, CHAIN_LOCK_FILE_NAME) and (
            relative_path not in recorded_names
        ):
            chain.file_checks.append(FileCheck(STATUS_EXTRA, f"{dir_name}/{relative_path}"))
    for entry in scan.unexpected_entries:
        if entry.relative_path == SEARCH_AREA_RECORDS_NAME:
            chain.problems.append(f"{SEARCH_AREA_RECORDS_NAME} is {entry.kind}; not read")
        elif entry.relative_path not in recorded_names:
            chain.file_checks.append(
                FileCheck(STATUS_EXTRA, f"{dir_name}/{entry.relative_path}", entry.kind)
            )
    return chain


def _parse_record(raw_line: bytes) -> dict[str, Any] | str:
    """The record of one records.jsonl line, or the reason it is unusable."""
    try:
        record = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return f"not a JSON record ({error})"
    except RecursionError:
        return "not a JSON record (nested too deeply)"
    if not isinstance(record, dict):
        return "not a JSON object"
    previous = record.get("previous_line_sha256")
    if not isinstance(previous, str) or not SHA256_PATTERN.match(previous):
        return "previous_line_sha256 missing or not a SHA-256"
    files = record.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) and SHA256_PATTERN.match(digest)
        for name, digest in files.items()
    ):
        return "files missing or not a mapping of file name to SHA-256"
    return record


def _is_plain_file_name(name: str) -> bool:
    return (
        bool(name)
        and name not in (".", "..", SEARCH_AREA_RECORDS_NAME, CHAIN_LOCK_FILE_NAME)
        and not any(character in name for character in "/\\:")
    )


def _check_sources(metadata_path: Path, report: VerificationReport) -> None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        sources = metadata["sources"]
        if not isinstance(sources, list):
            raise TypeError("sources is not a list")
    except (OSError, ValueError, KeyError, TypeError) as error:
        report.source_note = f"not checked: {METADATA_FILE_NAME} missing or unreadable ({error})"
        return
    except RecursionError:
        report.source_note = (
            f"not checked: {METADATA_FILE_NAME} missing or unreadable (nested too deeply)"
        )
        return
    if not sources:
        report.source_note = f"none recorded in {METADATA_FILE_NAME}"
    for source in sources:
        if not isinstance(source, dict):
            continue
        path_text = str(source.get("path", ""))
        subject = f"source {source.get('id')} '{source.get('label')}': {path_text}"
        recorded_sha256 = source.get("sha256")
        if not isinstance(recorded_sha256, str):
            report.source_checks.append(
                FileCheck(STATUS_NOT_RECORDED, subject, "no hash recorded for this source")
            )
        elif NETWORK_OR_DEVICE_PATH_PATTERN.match(path_text):
            report.source_checks.append(
                FileCheck(STATUS_NOT_CHECKED, subject, "network or device path, not opened")
            )
        elif not path_text or not Path(path_text).is_file():
            report.source_checks.append(
                FileCheck(STATUS_NOT_AVAILABLE, subject, "file not present at this path")
            )
        elif source.get("sha256_scope") != WHOLE_FILE_SCOPE:
            report.source_checks.append(
                FileCheck(
                    STATUS_NOT_COMPARED,
                    subject,
                    f"the recorded hash covers {source.get('sha256_scope')}",
                )
            )
        else:
            report.source_checks.append(_compare_file(Path(path_text), subject, recorded_sha256))
