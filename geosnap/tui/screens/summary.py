# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Final screen: counts, hashes and output locations of the finished run."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Label, Static

from geosnap.analysis.accuracy import ACCURACY_LEVEL_TEXTS
from geosnap.analysis.case_places import WINDOW_VERDICTS
from geosnap.analysis.encounters import MOVEMENT_JOINT
from geosnap.analysis.gpx_export import accuracy_text
from geosnap.analysis.models import AnalysisReport
from geosnap.extraction.models import record_noun
from geosnap.project.map_server import MapServerPool
from geosnap.project.pipeline import (
    SHA256_SCOPE_BEFORE_FAILURE,
    ProjectResult,
    ProjectStatus,
    SourceRunSummary,
)
from geosnap.project.verify import verify_command
from geosnap.tui.map_opening import serve_and_open_map
from geosnap.tui.widgets import (
    CONTENT_SCROLL_BINDINGS,
    ContentArea,
    ContentScreen,
    StatusHeader,
)

STATUS_LABELS = {
    ProjectStatus.COMPLETED: "Done",
    ProjectStatus.CANCELLED: "Cancelled",
    ProjectStatus.FAILED: "Error",
}


def source_summary_line(source_run: SourceRunSummary) -> str:
    """One source of the run: name, file, status and counters (or failure reason); its
    digest is listed with the other hashes."""
    descriptor = source_run.descriptor
    counters = source_run.counters
    parts = [
        f"Source {descriptor.identifier}: {descriptor.label}",
        f"file {descriptor.path.name}",
        f"status {source_run.status}",
        f"accepted {counters.accepted}, invalid {counters.invalid}, unrelated {counters.unrelated}",
    ]
    if source_run.error:
        parts.append(f"error {source_run.error}")
    return "   ".join(parts)


def source_digest_line(source_run: SourceRunSummary, label_suffix: str) -> str | None:
    """'Source SHA-256 · Phone: ab12'; names the scope when the source failed part-way."""
    if not source_run.sha256:
        return None
    scope = source_run.sha256_scope
    scope_note = f" ({scope})" if scope == SHA256_SCOPE_BEFORE_FAILURE else ""
    return f"Source SHA-256{label_suffix}{scope_note}: {source_run.sha256}"


def multi_source_lines(result: ProjectResult) -> list[str]:
    """A line per source; encounter and shared place counts when there are two or more."""
    lines = [source_summary_line(source_run) for source_run in result.sources]
    if len(result.sources) >= 2:
        joint_movements = sum(
            1 for encounter in result.encounters if encounter.movement == MOVEMENT_JOINT
        )
        lines.append(
            f"Encounters: {len(result.encounters)} (joint movements: {joint_movements})   "
            f"Shared places: {len(result.shared_places)}"
        )
    return lines


def case_place_line(result: ProjectResult) -> str | None:
    """Count of case places and of each window verdict (one verdict per place and source);
    the verdicts describe reports of the devices, the report and the map give the details."""
    if not result.case_places:
        return None
    not_located = sum(1 for entry in result.case_places if not entry.place.located)
    line = f"Case places: {len(result.case_places)}"
    if not_located:
        line += f" ({not_located} not located)"
    verdicts = [
        check.window.verdict
        for entry in result.case_places
        for check in entry.checks
        if check.window is not None
    ]
    if verdicts:
        line += "   Window verdicts per source: " + ", ".join(
            f"{verdict} {verdicts.count(verdict)}"
            for verdict in WINDOW_VERDICTS
            if verdict in verdicts
        )
    return line


def accuracy_level_line(result: ProjectResult) -> str | None:
    """The accuracy level of each source; None while every level is unknown."""
    levels = [source_run.descriptor.accuracy_level for source_run in result.sources]
    if all(level == "unknown" for level in levels):
        return None
    shown = [ACCURACY_LEVEL_TEXTS.get(level, level) for level in levels]
    if len(result.sources) == 1:
        return f"Accuracy level: {shown[0]}"
    return "Accuracy level: " + ", ".join(
        f"{source_run.descriptor.label} {level}"
        for source_run, level in zip(result.sources, shown, strict=True)
    )


def analysis_lines(report: AnalysisReport, label_suffix: str) -> list[str]:
    """Last known position and key figures of one source; the suffix names the source."""
    lines = []
    if report.last is not None:
        last = report.last
        address = "" if last.address is None else f"   {last.address.display_name}"
        lines.append(
            f"Last known position{label_suffix} (UTC): {last.timestamp_utc.isoformat()}   "
            f"{last.latitude:.5f}, {last.longitude:.5f} "
            f"{accuracy_text(last.accuracy_m, last.accuracy_known, 0)}{address}"
        )
    if report.points_excluded_positioning_method_after_last > 0:
        methods = "/".join(report.positioning_methods_excluded_after_last)
        lines.append(
            f"Newer {methods} records left out{label_suffix}: "
            f"{report.points_excluded_positioning_method_after_last}"
        )
    lines.append(
        f"Stays{label_suffix}: {report.totals.stays}   Gaps: {report.totals.gaps}   "
        f"Implausible segments: {report.totals.implausible_segments}   "
        f"Distance: {report.totals.distance_m / 1000:.1f} km"
    )
    return lines


def counted_records_noun(result: ProjectResult) -> str:
    """What the totals count: 'lines', 'placemarks', 'GPX points'; 'records' when mixed."""
    nouns = {
        record_noun(source_run.descriptor.format, plural=True) for source_run in result.sources
    }
    return nouns.pop() if len(nouns) == 1 else "records"


@dataclass(frozen=True, slots=True)
class SummarySection:
    heading: str
    lines: list[str]


def summary_sections(result: ProjectResult) -> list[SummarySection]:
    """The run in reading order: result, counts, analysis, outputs, hashes, next steps.
    A section without lines is left out."""
    multiple_sources = len(result.sources) >= 2

    outcome = [f"Status: {result.status.value}"]
    if result.error_message:
        outcome.append(f"Error: {result.error_message}")
    outcome.extend(f"Warning: {warning}" for warning in result.warnings)

    counters = result.counters
    noun = counted_records_noun(result)
    thinning = f" (thinned, stride {result.thinning_stride})" if result.thinning_stride > 1 else ""
    counts = [
        f"{noun[0].upper()}{noun[1:]} read: {result.lines_total}   "
        f"Accepted: {counters.accepted}   Invalid: {counters.invalid}   "
        f"Unrelated {noun}: {counters.unrelated}   "
        f"Undecodable bytes replaced: {counters.decode_replacements}",
        f"Unique positions: {result.unique_positions}   "
        f"Map points: {result.map_points_rendered} of {result.map_points_total}{thinning}",
        f"Records without timestamp: {counters.undated}",
    ]
    counts.extend(multi_source_lines(result))
    for source_run in result.sources:
        if source_run.encoding:
            label_suffix = f" · {source_run.descriptor.label}" if multiple_sources else ""
            counts.append(f"Source encoding{label_suffix}: {source_run.encoding}")
    case_places = case_place_line(result)
    if case_places is not None:
        counts.append(case_places)

    analysis = []
    accuracy_levels = accuracy_level_line(result)
    if accuracy_levels is not None:
        analysis.append(accuracy_levels)
    if multiple_sources:
        for source_run in result.sources:
            report = result.analysis_by_source.get(source_run.descriptor.identifier)
            if report is not None:
                analysis.extend(analysis_lines(report, f" · {source_run.descriptor.label}"))
    elif result.analysis is not None:
        analysis.extend(analysis_lines(result.analysis, ""))
    online = result.online
    if online.enabled:
        online_line = (
            f"Online: addresses {online.nominatim_addresses}/{online.nominatim_lookups}   "
            f"places {online.overpass_places} around {online.overpass_anchors} anchor(s)"
        )
        if online.nominatim_error or online.overpass_error:
            online_line += (
                f"   errors: {online.nominatim_error or ''} {online.overpass_error or ''}".rstrip()
            )
        analysis.append(online_line)
    else:
        analysis.append(
            "Online services: off"
            + (" (map tiles forced offline)" if online.tiles_forced_offline else "")
        )

    outputs = []
    if result.workspace is not None:
        outputs.append(f"Project directory: {result.workspace.directory}")
        if result.workspace.report_html_path.is_file():
            outputs.append(f"Report: {result.workspace.report_html_path}")

    hashes = []
    for source_run in result.sources:
        label_suffix = f" · {source_run.descriptor.label}" if multiple_sources else ""
        digest_line = source_digest_line(source_run, label_suffix)
        if digest_line is not None:
            hashes.append(digest_line)
    hashes.extend(f"{name}: {digest}" for name, digest in sorted(result.output_hashes.items()))
    next_steps = []
    if result.workspace is not None and result.manifest_sha256:
        hashes.append(f"Manifest SHA-256: {result.manifest_sha256}")
        next_steps.append(f"Verify: {verify_command(result.workspace.directory)}")
    next_steps.append(
        "O opens the map in the browser, where search areas are recorded. Enter returns "
        "to the file list; P there reopens finished projects."
    )
    sections = [
        SummarySection("Result", outcome),
        SummarySection("Counts", counts),
        SummarySection("Analysis", analysis),
        SummarySection("Outputs", outputs),
        SummarySection("SHA-256 hashes", hashes),
        SummarySection("Next steps", next_steps),
    ]
    return [section for section in sections if section.lines]


def summary_lines(result: ProjectResult) -> list[str]:
    """Every line of the summary in screen order, without the section headings."""
    return [line for section in summary_sections(result) for line in section.lines]


class SummaryScreen(ContentScreen[None]):
    BINDINGS = [
        Binding("enter", "finish", "Back", priority=True),
        Binding("escape", "finish", "Back", show=False),
        Binding("o", "open_map", "Map"),
        *CONTENT_SCROLL_BINDINGS,
    ]

    def __init__(self, result: ProjectResult, map_servers: MapServerPool) -> None:
        super().__init__()
        self.result = result
        self.map_servers = map_servers

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with ContentArea(id="content"):
            for section in summary_sections(self.result):
                yield Label(section.heading, classes="heading section-heading")
                for line in section.lines:
                    yield Static(line, classes="summary-line", markup=False)
            yield Static("", id="map-status", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one(StatusHeader).status = STATUS_LABELS[self.result.status]

    def action_finish(self) -> None:
        self.dismiss(None)

    def action_open_map(self) -> None:
        """Serve the map file on 127.0.0.1 under a secret address and open it from there."""
        workspace = self.result.workspace
        if workspace is None or not workspace.map_html_path.is_file():
            self._show_map_status("This run produced no map.")
            return
        self._show_map_status(
            serve_and_open_map(
                self.map_servers,
                workspace.map_html_path,
                local_tiles_used=self.result.tile_source_used == "local",
            )
        )

    def _show_map_status(self, text: str) -> None:
        self.query_one("#map-status", Static).update(text)
