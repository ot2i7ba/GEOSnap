# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""report_<stamp>.html: the self-contained, script-free run report for handing over a case.

The report is built from what the run wrote (metadata.json, analysis.json, the Nominatim
responses) plus the effective configuration, so it states exactly what the files record.
It is printed to PDF from the browser; the print CSS lays it out on A4.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from geosnap import APP_NAME, __version__
from geosnap.analysis.accuracy import accuracy_level_text
from geosnap.analysis.encounters import encounter_class_text
from geosnap.extraction.models import record_noun
from geosnap.project.manifest import MANIFEST_FILE_NAME, ProjectFileEntry, list_project_files
from geosnap.project.method_text import (
    CASE_PLACE_VERDICT_TEXTS,
    METHOD_DEFINITIONS,
    REPORT_LIMITS,
)
from geosnap.project.verify import verify_command
from geosnap.project.workspace import (
    REOPEN_LOG_NAME,
    SEARCH_AREA_RECORDS_NAME,
    SEARCH_AREAS_DIR_NAME,
    SPEED_RANGES_DIR_NAME,
    ProjectWorkspace,
)
from geosnap.settings import Settings
from geosnap.timezones import display_local_text

REPORT_SECTION_TITLES = (
    "Case and run",
    "Sources",
    "Method and parameters",
    "Results per source",
    "Across sources",
    "Case places",
    "Presence matrix",
    "Online services",
    "Limits",
    "Files and verification",
)
EMPTY_VALUE = "—"
# metadata.json sources[].accuracy_reporting -> report wording.
ACCURACY_REPORTING_TEXTS = {
    "all": "by every record",
    "some": "by some records",
    "none": "by no record",
}
# The report lists at most this many stays per source; the stays CSV has all.
STAYS_LISTED = 40

REPORT_CSS = """
:root { color-scheme: light; }
body { margin: 0 auto; max-width: 190mm; padding: 12px 16px; background: #fff; color: #111;
  font: 10.5pt/1.4 "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif; }
h1 { font-size: 17pt; margin: 0 0 4px; }
h2 { font-size: 13pt; margin: 22px 0 6px; padding-bottom: 2px; border-bottom: 1.5px solid #333; }
h3 { font-size: 11pt; margin: 14px 0 4px; }
h4 { font-size: 10.5pt; margin: 10px 0 3px; }
p { margin: 4px 0; }
.subtitle { color: #444; margin-bottom: 10px; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; margin: 4px 0 8px; }
th, td { border: 1px solid #bbb; padding: 3px 6px; text-align: left; vertical-align: top;
  overflow-wrap: anywhere; }
th { background: #eee; font-weight: 600; }
table.pairs th { width: 32%; }
td.number { text-align: right; font-variant-numeric: tabular-nums; }
.hash, code, pre { font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 9pt; }
.hash { word-break: break-all; }
pre { white-space: pre-wrap; word-break: break-all; background: #f5f5f5;
  padding: 6px; margin: 4px 0; }
dt { font-weight: 600; margin-top: 6px; }
dd { margin: 0 0 0 16px; }
footer { margin-top: 24px; color: #555; font-size: 9pt; }
@page { size: A4; margin: 15mm 12mm 16mm; }
@media print {
  body { max-width: none; padding: 0; font-size: 9.5pt; }
  h2, h3, h4 { break-after: avoid; }
  tr, dt, dd { break-inside: avoid; }
  thead { display: table-header-group; }
}
"""


def write_report(
    workspace: ProjectWorkspace, settings: Settings, generated_local: datetime
) -> None:
    """Write the report from the files of the finished run; metadata.json must exist."""
    metadata = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
    analysis = _read_optional_json(workspace.analysis_path)
    nominatim = _read_optional_json(workspace.nominatim_path)
    report_html = build_report_html(
        metadata=metadata,
        analysis=analysis,
        configuration=asdict(settings),
        project_files=list_project_files(
            workspace.directory, excluded_names={workspace.report_html_path.name}
        ),
        nominatim_responses=_mapping_list(_mapping(nominatim).get("responses")),
        nominatim_searches=_mapping_list(_mapping(nominatim).get("searches")),
        generated_local=generated_local,
    )
    workspace.report_html_path.write_bytes(report_html.encode("utf-8"))


def build_report_html(
    *,
    metadata: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    configuration: Mapping[str, Any],
    project_files: Sequence[ProjectFileEntry],
    nominatim_responses: Sequence[Mapping[str, Any]],
    generated_local: datetime,
    nominatim_searches: Sequence[Mapping[str, Any]] = (),
) -> str:
    project = _mapping(metadata.get("project"))
    analysis_document = _mapping(analysis)
    sections = (
        _case_and_run(metadata, generated_local),
        _sources(metadata, analysis_document),
        _method(metadata, configuration),
        _results_per_source(metadata, analysis_document),
        _across_sources(metadata, analysis_document),
        _case_places(metadata, analysis_document),
        _presence_matrix(metadata, analysis_document),
        _online_services(metadata, project_files, nominatim_responses, nominatim_searches),
        _limits(),
        _files_and_verification(project_files, project.get("directory")),
    )
    body = "\n".join(
        f'<section>\n<h2 id="section-{number}">{number}. {escape(title)}</h2>\n{content}\n'
        "</section>"
        for number, (title, content) in enumerate(
            zip(REPORT_SECTION_TITLES, sections, strict=True), start=1
        )
    )
    title = f"{APP_NAME} report {_text(project.get('name'))}"
    generated_text = f"{_utc_text(generated_local)} ({_local_moment_text(generated_local)})"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n<style>{REPORT_CSS}</style>\n</head>\n<body>\n"
        f"<h1>{escape(title)}</h1>\n"
        f'<p class="subtitle">Run report of the project directory '
        f"{_code(project.get('directory'))}. Print it to PDF from the browser (A4).</p>\n"
        f"{body}\n"
        f"<footer>Generated by {escape(APP_NAME)} {escape(__version__)} at "
        f"{escape(generated_text)}.</footer>\n</body>\n</html>\n"
    )


# --- sections ---------------------------------------------------------------------------


def _case_and_run(metadata: Mapping[str, Any], generated_local: datetime) -> str:
    case = _mapping(metadata.get("case"))
    project = _mapping(metadata.get("project"))
    application = _mapping(metadata.get("application"))
    rows: list[tuple[str, object]] = [
        ("Case reference", case.get("reference") or "not given"),
        ("Examiner", case.get("examiner") or "not given"),
        ("Project", project.get("name")),
        ("Status", project.get("status")),
        ("Error", project.get("error")),
        ("Project directory", project.get("directory")),
        ("Started (UTC)", _utc_of_iso(project.get("started_utc"))),
        ("Started (local)", _local_of_iso(project.get("started_local"))),
        ("Finished (UTC)", _utc_of_iso(project.get("finished_utc"))),
        ("Finished (local)", _local_of_iso(project.get("finished_local"))),
        ("Report generated (UTC)", _utc_text(generated_local)),
        ("Report generated (local)", _local_moment_text(generated_local)),
        ("Application", f"{_text(application.get('name'))} {_text(application.get('version'))}"),
        ("Python", application.get("python")),
        ("Platform", application.get("platform")),
    ]
    warnings = [str(warning) for warning in _list(metadata.get("warnings"))]
    warning_html = (
        "<h3>Warnings</h3>\n<ul>" + "".join(f"<li>{escape(w)}</li>" for w in warnings) + "</ul>"
        if warnings
        else "<p>No warnings were recorded.</p>"
    )
    return _pairs(rows) + "\n" + warning_html


def _sources(metadata: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    parts = []
    per_source = _mapping(analysis.get("per_source"))
    for source in _mapping_list(metadata.get("sources")):
        counts = _mapping(source.get("counts"))
        source_analysis = _mapping(per_source.get(str(source.get("id"))))
        first = _mapping(source_analysis.get("first"))
        last = _mapping(source_analysis.get("last"))
        records = record_noun(str(source.get("format")), plural=True)
        naive = _mapping(source.get("naive_timestamps"))
        rows: list[tuple[str, object]] = [
            ("Kind", source.get("kind")),
            ("File", source.get("file_name")),
            ("Path at read time", source.get("path")),
            ("Format", source.get("format")),
            ("Size (bytes)", source.get("size_bytes")),
            ("SHA-256", _Hash(source.get("sha256"))),
            ("Hash covers", source.get("sha256_scope")),
            ("Encoding", source.get("encoding")),
            ("Status", source.get("status")),
            ("Error", source.get("error")),
            (f"{records[0].upper()}{records[1:]} read", counts.get("lines_total")),
            (f"Accepted {records}", counts.get("accepted")),
            (f"Rejected {records}", counts.get("invalid")),
            (f"Unrelated {records}", counts.get("unrelated")),
            ("Undecodable bytes replaced", counts.get("decode_replacements")),
            ("Records without timestamp", counts.get("undated_records")),
            (
                "Times without offset",
                f"{_text(naive.get('count'))}, {_text(naive.get('handling'))}" if naive else None,
            ),
            (
                "Accuracy reported",
                ACCURACY_REPORTING_TEXTS.get(str(source.get("accuracy_reporting"))),
            ),
            (
                "Accuracy level",
                accuracy_level_text(
                    source.get("accuracy_level"),
                    _mapping(metadata.get("settings")).get("accuracy_confidence"),
                ),
            ),
            (
                "Time span of analysed records (UTC)",
                f"{_text(first.get('utc'))} to {_text(last.get('utc'))}" if first else None,
            ),
        ]
        if source.get("format") == "csv":
            rows.append(("CSV column mapping", _mapping_text(source.get("csv_mapping"))))
        kmz_entry = _mapping(source.get("kmz_entry"))
        if kmz_entry:
            entry_scope = (
                "" if kmz_entry.get("read_completely") else " (bytes read before the failure)"
            )
            rows.append(("KMZ entry read", kmz_entry.get("name")))
            rows.append(
                (f"SHA-256 of the decompressed entry{entry_scope}", _Hash(kmz_entry.get("sha256")))
            )
        parts.append(f"<h3>{_source_title(source)}</h3>\n{_pairs(rows)}")
    if not parts:
        return "<p>No source was recorded.</p>"
    return _records_table(metadata) + "\n" + "\n".join(parts)


def _records_table(metadata: Mapping[str, Any]) -> str:
    """Records per source: with a timestamp (dated) and without (undated); the undated
    ones are on the map and in the points CSV but in no time-based analysis."""
    rows = [
        (
            f"Source {_text(source.get('id'))}: {_text(source.get('label'))}",
            _mapping(source.get("counts")).get("accepted"),
            _mapping(source.get("counts")).get("undated_records"),
        )
        for source in _mapping_list(metadata.get("sources"))
    ]
    return (
        "<h3>Records</h3>\n"
        + _table(("Source", "Dated records", "Records without timestamp"), rows)
        + "\n<p>Records without timestamp are drawn on the map (hollow ring) and listed in the "
        "points CSV with empty time columns; stays, gaps, segments, encounters, the presence "
        "matrix, the crystal ball and the timeline use dated records only.</p>"
    )


def _method(metadata: Mapping[str, Any], configuration: Mapping[str, Any]) -> str:
    parameters = dict(_mapping(configuration.get("analysis")))
    parameters.update(_mapping(_mapping(metadata.get("analysis")).get("parameters")))
    definitions = "".join(
        f"<dt>{escape(term)}</dt><dd>{escape(_fill(text, parameters))}</dd>"
        for term, text in METHOD_DEFINITIONS
    )
    run_settings = [(str(key), value) for key, value in _mapping(metadata.get("settings")).items()]
    configuration_rows: list[tuple[str, object]] = []
    for section_name, section in configuration.items():
        if isinstance(section, Mapping):
            configuration_rows.extend(
                (f"{section_name}.{key}", value) for key, value in section.items()
            )
        else:
            configuration_rows.append((str(section_name), section))
    return (
        f"<h3>Definitions</h3>\n<dl>{definitions}</dl>\n"
        "<h3>Settings of this run</h3>\n" + _pairs(run_settings) + "\n"
        "<h3>Configuration (config.toml, effective values)</h3>\n"
        "<p>extraction.remove_duplicates is only the default of the duplicate switch in the "
        "project setup; the run used remove_duplicates above.</p>\n" + _pairs(configuration_rows)
    )


def _results_per_source(metadata: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    summaries = _mapping(_mapping(metadata.get("analysis")).get("per_source"))
    details = _mapping(analysis.get("per_source"))
    parts = []
    for source in _mapping_list(metadata.get("sources")):
        source_key = str(source.get("id"))
        summary = _mapping(summaries.get(source_key))
        if not summary:
            parts.append(f"<h3>{_source_title(source)}</h3>\n<p>No analysis was recorded.</p>")
            continue
        totals = _mapping(summary.get("totals"))
        counts = _mapping(source.get("counts"))
        last = _mapping(_mapping(details.get(source_key)).get("last"))
        rows: list[tuple[str, object]] = [
            ("Records analysed", summary.get("points_analysed")),
            ("Excluded for accuracy", summary.get("points_excluded_accuracy")),
            (
                "Positioning methods (dated records)",
                _method_counts_text(summary.get("positioning_methods")),
            ),
            (
                "Excluded for positioning method",
                _excluded_methods_text(
                    summary.get("points_excluded_positioning_method"),
                    _mapping(metadata.get("settings")).get("excluded_positioning_methods"),
                ),
            ),
            ("Time span", _minutes_text(totals.get("time_span_minutes"))),
            ("Distance over plausible steps", _kilometres_text(totals.get("distance_m"))),
            (
                "Left out: implausible or unknown steps",
                _left_out_text(totals.get("segments_left_out"), totals.get("distance_left_out_m")),
            ),
            ("Highest plausible speed (km/h)", totals.get("max_speed_kmh")),
            ("Stays", totals.get("stays")),
            ("Gaps", totals.get("gaps")),
            ("Implausible segments", totals.get("implausible_segments")),
            ("Map points shown", counts.get("map_points_rendered")),
            ("Map thinning stride", source.get("thinning_stride")),
        ]
        if last:
            address = _mapping(last.get("address")).get("display_name")
            accuracy = (
                f"± {_text(last.get('accuracy_m'))} m"
                if last.get("accuracy_known")
                else "not reported"
            )
            rows += [
                ("Last known position (UTC)", last.get("utc")),
                ("Last known position (local)", _local_text(last, "local", "offset")),
                ("Coordinates", f"{_text(last.get('lat'))}, {_text(last.get('lon'))}"),
                ("Accuracy", accuracy),
                ("Address (Nominatim)", address),
            ]
            newer_left_out = _mapping(details.get(source_key)).get(
                "points_excluded_positioning_method_after_last"
            )
            if isinstance(newer_left_out, int) and newer_left_out > 0:
                method_names = ", ".join(
                    str(method)
                    for method in _list(
                        _mapping(metadata.get("settings")).get("excluded_positioning_methods")
                    )
                )
                rows.append(
                    (
                        "Newer records left out",
                        f"{newer_left_out} (positioning method: {method_names})",
                    )
                )
        parts.append(f"<h3>{_source_title(source)}</h3>\n{_pairs(rows)}")
        stays = _mapping_list(_mapping(details.get(source_key)).get("stays"))
        if stays:
            parts.append(_stays_table(stays))
    parts.append(
        "<p>Every stay, gap and segment is listed in analysis.json and in the stays_, gaps_ "
        "and segments_ CSV files of the project directory.</p>"
    )
    return "\n".join(parts)


def _across_sources(metadata: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    sources = _mapping_list(metadata.get("sources"))
    if len(sources) < 2:
        return "<p>The project has one source; there is no cross-source analysis.</p>"
    labels = {str(source.get("id")): _text(source.get("label")) for source in sources}
    summary = _mapping(metadata.get("analysis"))
    encounters = _mapping_list(analysis.get("encounters"))
    shared_places = _mapping_list(analysis.get("shared_places"))
    parts = [
        _pairs(
            [
                ("Encounters", summary.get("encounters")),
                ("Shared places", summary.get("shared_places")),
            ]
        )
    ]
    if encounters:
        parts.append("<h3>Encounters</h3>")
        parts.append(
            _table(
                (
                    "#",
                    "Sources",
                    "Start (UTC)",
                    "End (UTC)",
                    "Minutes",
                    "Closest (m)",
                    "Centre",
                    "Class",
                    "Path (m)",
                    "Displacement (m)",
                    "Excluded (m)",
                    "Implausible steps",
                    "Smallest time offset (s)",
                    "Closest pair offset (s)",
                ),
                [
                    (
                        encounter.get("id"),
                        f"{labels.get(str(encounter.get('source_a')), '?')} / "
                        f"{labels.get(str(encounter.get('source_b')), '?')}",
                        encounter.get("start_utc"),
                        encounter.get("end_utc"),
                        encounter.get("duration_minutes"),
                        encounter.get("min_distance_m"),
                        f"{_text(encounter.get('lat'))}, {_text(encounter.get('lon'))}",
                        encounter_class_text(
                            _text(encounter.get("movement")),
                            encounter.get("coincided_within_accuracy_only") is True,
                        ),
                        encounter.get("path_length_m"),
                        encounter.get("displacement_m"),
                        encounter.get("path_excluded_m"),
                        encounter.get("implausible_steps"),
                        encounter.get("min_time_offset_seconds"),
                        encounter.get("closest_pair_offset_seconds"),
                    )
                    for encounter in encounters
                ],
            )
        )
        speed_limit = _text(
            _mapping(_mapping(metadata.get("analysis")).get("parameters")).get(
                "implausible_speed_kmh"
            )
        )
        parts.append(
            "<p>Path: length of the plausible steps of the path through the midpoints of the "
            "coinciding records. Excluded: length of the steps without elapsed time or at "
            f"{escape(speed_limit)} km/h or faster (position jumps of the source data, or travel "
            "at that speed, such as a flight), with their number; they add nothing to the path "
            "and cannot make a joint movement. Displacement: first to last midpoint, jumps "
            "included. The class applies to the whole encounter, which may span rest and "
            "movement; it describes the records of two devices, not persons or a vehicle. "
            "Smallest time offset: the smallest time difference of two coinciding records; "
            "closest pair offset: the time difference of the two closest records (of equally "
            "close pairs, the one nearest in time). Only within accuracy: the records never "
            "came within the encounter distance and coincide only through their accuracy.</p>"
        )
        ambiguous = [
            f"{_text(encounter.get('id'))} (up to "
            f"{float(encounter.get('time_ambiguity_seconds') or 0) / 60:.0f} min)"
            for encounter in encounters
            if float(encounter.get("time_ambiguity_seconds") or 0) > 0
        ]
        if ambiguous:
            parts.append(
                "<p>Encounters with a local time that occurred twice (clocks going back): "
                f"{escape(', '.join(ambiguous))}. Their times and offsets are those recorded; "
                "the true ones may lie that much later.</p>"
            )
    if shared_places:
        parts.append("<h3>Shared places</h3>")
        parts.append(
            _table(
                ("#", "Sources", "Visits", "Visits overlap", "Centre"),
                [
                    (
                        place.get("id"),
                        ", ".join(labels.get(str(key), "?") for key in _list(place.get("sources"))),
                        len(_list(place.get("visits"))),
                        place.get("overlapping"),
                        f"{_text(place.get('lat'))}, {_text(place.get('lon'))}",
                    )
                    for place in shared_places
                ],
            )
        )
    parts.append("<p>Details: encounters_ and shared_places_ CSV files and analysis.json.</p>")
    return "\n".join(parts)


# The report lists at most this many visits per case place and source; the CSV has all.
CASE_PLACE_VISITS_LISTED = 40


def _report_distance_text(entry: object, noun: str) -> str | None:
    """'2026-09-01 14:00:00 +02:00, 35.2 m, ± 10 m, record 7' for a closest or neighbour."""
    report = _mapping(entry)
    if not report:
        return None
    accuracy = report.get("accuracy_m")
    accuracy_text = "accuracy not reported" if accuracy is None else f"± {accuracy} m"
    return (
        f"{_text(_local_text(report, 'local', 'offset'))}, {_text(report.get('distance_m'))} m "
        f"from the centre, {accuracy_text}, {noun} {_text(report.get('line'))}"
    )


def _unobserved_text(minutes: object) -> str | None:
    """'715.0 min (11.9 h), including before the first and after the last record'."""
    if not isinstance(minutes, int | float):
        return None
    return (
        f"{minutes:.1f} min ({minutes / 60:.1f} h), counted also before the first and after "
        "the last record of the window"
    )


def _case_places(metadata: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    block = _mapping(analysis.get("case_places"))
    places = _mapping_list(block.get("places"))
    if not places:
        return "<p>No case places were entered for this project.</p>"
    sources = {str(source.get("id")): source for source in _mapping_list(metadata.get("sources"))}
    file_record = _mapping(_mapping(metadata.get("case_places")).get("file"))
    parts = [
        "<p>A verdict describes the records of a device, never a person. "
        f"A silence of more than {escape(_text(block.get('gap_threshold_minutes')))} minutes "
        "ends a visit; a record without an accuracy value counts as elsewhere only beyond the "
        f"radius plus {escape(_text(block.get('unknown_accuracy_margin_m')))} m. Definitions: "
        "section 3.</p>"
    ]
    if file_record:
        parts.append(
            _pairs(
                [
                    ("Case places file", file_record.get("name")),
                    ("SHA-256 of the file", _Hash(file_record.get("sha256"))),
                ]
            )
        )
    for place in places:
        parts.append(
            f"<h3>Case place {escape(_text(place.get('id')))}: "
            f"{escape(_text(place.get('label')))}</h3>"
        )
        window = _mapping(place.get("window"))
        located = place.get("lat") is not None
        parts.append(
            _pairs(
                [
                    (
                        "Position",
                        f"{_text(place.get('lat'))}, {_text(place.get('lon'))}"
                        if located
                        else None,
                    ),
                    ("Radius (m)", place.get("radius_m")),
                    ("Position origin", place.get("location")),
                    ("Address search answer", place.get("geocoded_display_name")),
                    ("Address", place.get("address")),
                    ("Note", place.get("note")),
                    (
                        "Window (local)",
                        f"{_text(_local_text(window, 'from_local', 'from_offset'))} to "
                        f"{_text(_local_text(window, 'to_local', 'to_offset'))}"
                        if window
                        else "none: visits and closest approach only",
                    ),
                    (
                        "Window (UTC)",
                        f"{_text(_utc_of_iso(window.get('from_utc')))} to "
                        f"{_text(_utc_of_iso(window.get('to_utc')))}"
                        if window
                        else None,
                    ),
                ]
            )
        )
        if not located:
            parts.append(
                "<p>Not located: the place has no coordinates, so no source was checked "
                "against it (see the warnings in section 1).</p>"
            )
            continue
        for check in _mapping_list(place.get("checks")):
            source = _mapping(sources.get(str(check.get("source_id"))))
            noun = record_noun(str(source.get("format")))
            window_check = _mapping(check.get("window"))
            rows: list[tuple[str, object]] = []
            if window_check:
                verdict = str(window_check.get("verdict"))
                basis = window_check.get("basis")
                rows.append(("Window verdict", f"{verdict} ({basis})" if basis else verdict))
                verdict_as_reported = window_check.get("verdict_as_reported")
                if verdict_as_reported:
                    basis_as_reported = window_check.get("basis_as_reported")
                    rows.append(
                        (
                            "Verdict with the accuracy as reported",
                            f"{verdict_as_reported} ({basis_as_reported})"
                            if basis_as_reported
                            else verdict_as_reported,
                        )
                    )
                rows.append(("Meaning", CASE_PLACE_VERDICT_TEXTS.get(verdict)))
                rows.append(("Records in the window", window_check.get("reports_in_window")))
                if window_check.get("reports_in_window"):
                    rows.append(
                        (
                            "Closest record in the window",
                            _report_distance_text(window_check.get("closest"), noun),
                        )
                    )
                    # The verdict covers only the moments of the records.
                    first_record = _local_text(
                        window_check, "first_report_local", "first_report_offset"
                    )
                    last_record = _local_text(
                        window_check, "last_report_local", "last_report_offset"
                    )
                    rows.append(
                        (
                            "First and last record in the window",
                            f"{_text(first_record)} to {_text(last_record)}",
                        )
                    )
                    rows.append(
                        (
                            "Longest span of the window without a record",
                            _unobserved_text(window_check.get("longest_unobserved_minutes")),
                        )
                    )
                    if window_check.get("reports_inside_radius"):
                        best_accuracy = window_check.get("best_inside_accuracy_m")
                        rows.append(
                            (
                                "Best accuracy of the records inside the radius in the window",
                                "accuracy not reported"
                                if best_accuracy is None
                                else f"± {best_accuracy} m",
                            )
                        )
                else:
                    rows.append(
                        (
                            "Nearest record before the window",
                            _report_distance_text(window_check.get("before"), noun) or "none",
                        )
                    )
                    rows.append(
                        (
                            "Nearest record after the window",
                            _report_distance_text(window_check.get("after"), noun) or "none",
                        )
                    )
            rows.extend(
                [
                    ("Visits", check.get("visit_count")),
                    ("Records inside the radius", check.get("reports_inside_radius")),
                    ("Possible visits (records)", check.get("possible_count")),
                    (
                        "Records without accuracy near the radius",
                        check.get("unrated_count"),
                    ),
                    (
                        "Closest approach overall",
                        _report_distance_text(check.get("closest"), noun)
                        or "the source has no records",
                    ),
                ]
            )
            parts.append(f"<h4>{_source_title(source or {'id': check.get('source_id')})}</h4>")
            parts.append(_pairs(rows))
            visits = _mapping_list(check.get("visits"))
            if visits:
                parts.append(
                    _table(
                        (
                            "First record (local)",
                            "Last record (local)",
                            "Minutes",
                            "Records",
                            "Closest (m)",
                            "Best accuracy (m)",
                            f"{noun.capitalize()} numbers",
                        ),
                        [
                            (
                                _local_text(visit, "first_local", "first_offset"),
                                _local_text(visit, "last_local", "last_offset"),
                                visit.get("duration_minutes"),
                                visit.get("report_count"),
                                visit.get("closest_distance_m"),
                                "not reported"
                                if visit.get("best_accuracy_m") is None
                                else visit.get("best_accuracy_m"),
                                f"{_text(visit.get('first_line'))}–{_text(visit.get('last_line'))}",
                            )
                            for visit in visits[:CASE_PLACE_VISITS_LISTED]
                        ],
                    )
                )
                if len(visits) > CASE_PLACE_VISITS_LISTED:
                    parts.append(
                        f"<p>The first {CASE_PLACE_VISITS_LISTED} of {len(visits)} visits are "
                        "listed; the case_places_ CSV file lists every visit.</p>"
                    )
    parts.append(
        "<p>Details: case_places_ CSV file (every visit, possible visit and verdict, UTC and "
        "local time with offset) and analysis.json.</p>"
    )
    return "\n".join(parts)


def _presence_matrix(metadata: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    matrix = _mapping(analysis.get("presence_matrix"))
    if not matrix:
        return (
            "<p>No presence matrix: it is built for projects with two or more sources or with "
            "case places.</p>"
        )
    sources = {source.get("id"): source for source in _mapping_list(metadata.get("sources"))}
    columns = _list(matrix.get("sources"))
    rows = _mapping_list(matrix.get("rows"))

    def cell_text(cell: Mapping[str, Any]) -> str:
        visit_count = cell.get("visit_count")
        if not visit_count:
            return "no visit"
        return (
            f"{visit_count} {'visit' if visit_count == 1 else 'visits'}; "
            f"{_minutes_text(cell.get('dwell_minutes'))}; "
            f"first record {_local_text(cell, 'first_local', 'first_offset')}; "
            f"last record {_local_text(cell, 'last_local', 'last_offset')}"
        )

    table = _table(
        (
            "#",
            "Place",
            "Kind",
            "Centre, radius",
            "Simultaneous",
            *(_text(_mapping(sources.get(source_id)).get("label")) for source_id in columns),
        ),
        [
            (
                row.get("id"),
                row.get("label"),
                row.get("kind"),
                f"{_text(row.get('lat'))}, {_text(row.get('lon'))}; {_text(row.get('radius_m'))} m",
                row.get("simultaneous"),
                *(cell_text(cell) for cell in _mapping_list(row.get("cells"))),
            )
            for row in rows
        ],
    )
    return (
        f"<p>Places as rows, sources as columns; listed are {len(rows)} of "
        f"{_text(matrix.get('rows_total'))} places (at most {_text(matrix.get('row_cap'))} rows: "
        "every case place, then the shared places and the stay places with the longest dwell). "
        "The two kinds of rows are measured differently: at a shared place or stay place "
        f"(stay centres grouped within the matrix tolerance of {_text(matrix.get('tolerance_m'))} "
        "m; the shared places of the map keep the stop radius) a visit is a stay, at a case "
        "place a run of consecutive records inside the radius entered by the examiner; their "
        "counts and dwell must not be compared with each other. "
        "Simultaneous: visits of different sources overlap in time. Times are local with "
        "their UTC offset. A cell describes the records of a device, never a person; "
        '"no visit" means that no visit of that source was found at the place.</p>\n'
        f"{table}\n"
        "<p>Details: presence_matrix_ CSV file (every place and every visit, UTC and local) "
        "and analysis.json; definitions in section 3.</p>"
    )


def _online_services(
    metadata: Mapping[str, Any],
    project_files: Sequence[ProjectFileEntry],
    nominatim_responses: Sequence[Mapping[str, Any]],
    nominatim_searches: Sequence[Mapping[str, Any]] = (),
) -> str:
    online = _mapping(metadata.get("online"))
    settings = _mapping(metadata.get("settings"))
    parts = [
        _pairs(
            [
                ("Online services enabled", online.get("enabled")),
                ("Map tiles switched offline", online.get("tiles_forced_offline")),
                ("Base map used", settings.get("tile_source_used")),
                ("Tile provider used", settings.get("tile_provider_used")),
                ("Cancelled", online.get("cancelled")),
            ]
        )
    ]
    nominatim = dict(_mapping(online.get("nominatim")))
    parts.append("<h3>Nominatim (addresses)</h3>\n" + _pairs(list(nominatim.items())))
    if nominatim_responses:
        parts.append(
            _table(
                ("Query (lat, lon)", "Response SHA-256"),
                [
                    (
                        f"{_text(_mapping(response.get('query')).get('lat'))}, "
                        f"{_text(_mapping(response.get('query')).get('lon'))}",
                        _Hash(response.get("sha256")),
                    )
                    for response in nominatim_responses
                ],
            )
        )
    if nominatim_searches:
        parts.append(
            _table(
                ("Address searched (case place)", "Response SHA-256"),
                [
                    (_mapping(search.get("query")).get("q"), _Hash(search.get("sha256")))
                    for search in nominatim_searches
                ],
            )
        )
    overpass = dict(_mapping(online.get("overpass")))
    query = overpass.pop("query", None)
    parts.append("<h3>Overpass (places)</h3>\n" + _pairs(list(overpass.items())))
    if query:
        parts.append(f"<p>Query sent:</p>\n<pre>{escape(str(query))}</pre>")
    response_files = [
        entry
        for entry in project_files
        if entry.relative_path.startswith(("overpass_", "nominatim_"))
    ]
    if response_files:
        parts.append("<h3>Stored responses</h3>")
        parts.append(
            _table(
                ("File", "SHA-256"),
                [(entry.relative_path, _Hash(entry.sha256)) for entry in response_files],
            )
        )
    notes = [str(note) for note in _list(metadata.get("notes"))]
    if notes:
        parts.append(
            "<h3>Notes</h3>\n<ul>" + "".join(f"<li>{escape(n)}</li>" for n in notes) + "</ul>"
        )
    return "\n".join(parts)


def _limits() -> str:
    items = "".join(
        f"<dt>{escape(topic)}</dt><dd>{escape(text)}</dd>" for topic, text in REPORT_LIMITS
    )
    return f"<dl>{items}</dl>"


def _files_and_verification(
    project_files: Sequence[ProjectFileEntry], project_directory: object
) -> str:
    table = _table(
        ("File", "Size (bytes)", "SHA-256"),
        [(entry.relative_path, entry.size_bytes, _Hash(entry.sha256)) for entry in project_files],
    )
    return (
        f"{table}\n"
        f"<p>Not listed: this report, which cannot contain its own hash, and "
        f"{MANIFEST_FILE_NAME}, which is written after it. The manifest lists every file of "
        f"the project directory including this report, except itself, {REOPEN_LOG_NAME} "
        f"(written when the project is reopened) and the {SEARCH_AREAS_DIR_NAME}/ and "
        f"{SPEED_RANGES_DIR_NAME}/ directories (search areas and speed ranges recorded later "
        f"from the map, each with its own hash chain in {SEARCH_AREA_RECORDS_NAME}). The "
        f"SHA-256 of the manifest is shown on the {APP_NAME} summary screen and recorded in "
        "the application log output/GEOSnap.log; it is kept outside the project directory, "
        "because a file cannot record its own hash. metadata.json lists the hashes of the "
        "files written before it.</p>\n"
        "<h3>How to verify</h3>\n<ol>"
        f"<li>With {APP_NAME}: "
        f"<code>{escape(verify_command(Path(_text(project_directory))))}</code> (replace the "
        "path when the directory was moved or copied). "
        "It compares every file with the manifest (OK, MISMATCH, MISSING, EXTRA), checks the "
        "hash chains of search areas and speed ranges and, where they are still present, the "
        "source files at their recorded paths. Exit code 0 means everything matches, 1 a "
        "deviation, 2 a call error.</li>"
        "<li>Without GEOSnap, inside the project directory: "
        f"<code>sha256sum -c {MANIFEST_FILE_NAME}</code> (GNU coreutils); on Windows compare "
        "each file's <code>certutil -hashfile &lt;file&gt; SHA256</code> with its line in "
        f"{MANIFEST_FILE_NAME}.</li>"
        "<li>Compare the SHA-256 of the manifest with the value recorded at the end of the "
        "run.</li></ol>\n"
        "<p>Search areas and speed ranges recorded later are chained line by line in the "
        f"{SEARCH_AREA_RECORDS_NAME} of their directory, but a chain cannot show that its last "
        "lines were removed. After each recording the map shows the SHA-256 of the last "
        f"{SEARCH_AREA_RECORDS_NAME} line, which is also written to the application log. Keep "
        "that value in the case file.</p>"
    )


# --- formatting -------------------------------------------------------------------------


class _Hash:
    """A value shown in the monospace, wrapping hash style."""

    def __init__(self, digest: object) -> None:
        self.digest = digest


def _cell(value: object) -> str:
    if isinstance(value, _Hash):
        if value.digest is None:
            return EMPTY_VALUE
        return f'<span class="hash">{escape(str(value.digest))}</span>'
    return escape(_text(value))


def _text(value: object) -> str:
    if value is None or value == "":
        return EMPTY_VALUE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple):
        return ", ".join(_text(entry) for entry in value) or EMPTY_VALUE
    if isinstance(value, Mapping):
        return _mapping_text(value)
    return str(value)


def _mapping_text(value: object) -> str:
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_text(entry)}" for key, entry in value.items()) or EMPTY_VALUE
    return _text(value)


def _code(value: object) -> str:
    return f"<code>{escape(_text(value))}</code>"


def _pairs(rows: Iterable[tuple[str, object]]) -> str:
    body = "".join(
        f"<tr><th>{escape(label)}</th><td>{_cell(value)}</td></tr>" for label, value in rows
    )
    return f'<table class="pairs"><tbody>{body}</tbody></table>'


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="number">{_cell(value)}</td>'
            if isinstance(value, int | float) and not isinstance(value, bool)
            else f"<td>{_cell(value)}</td>"
            for value in row
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _method_counts_text(counts: object) -> str | None:
    """'gnss 120, wifi 30, cell 5'; None for a summary without counts."""
    if not isinstance(counts, Mapping) or not counts:
        return None
    return ", ".join(f"{method} {count}" for method, count in counts.items())


def _excluded_methods_text(count: object, methods: object) -> str | None:
    """'5 (excluded methods: cell)'; the setting is named even when nothing was left out."""
    if not isinstance(count, int):
        return None
    method_names = [str(method) for method in _list(methods)]
    return f"{count} (excluded methods: {', '.join(method_names) or 'none'})"


def _stay_bound_text(stay: Mapping[str, Any], bound: str) -> str:
    """'between <after> and <arrived by>' / 'between <departure> and <before>'; 'not
    bounded' when no record bounds that side (stays.find_stays)."""
    if bound == "arrival":
        earlier = _local_text(stay, "arrived_after_local", "arrived_after_offset")
        later = _local_text(stay, "arrived_by_local", "arrived_by_offset")
    else:
        earlier = _local_text(stay, "leave_local", "leave_offset")
        later = _local_text(stay, "left_before_local", "left_before_offset")
    if earlier is None or later is None:
        return "not bounded"
    return f"between {earlier} and {later}"


def _stays_table(stays: Sequence[Mapping[str, Any]]) -> str:
    table = _table(
        ("#", "First record (local)", "Last record (local)", "Minutes", "Arrived", "Left"),
        [
            (
                stay.get("id"),
                _local_text(stay, "arrive_local", "arrive_offset"),
                _local_text(stay, "leave_local", "leave_offset"),
                stay.get("duration_minutes"),
                _stay_bound_text(stay, "arrival"),
                _stay_bound_text(stay, "departure"),
            )
            for stay in stays[:STAYS_LISTED]
        ],
    )
    note = (
        f"<p>The first {STAYS_LISTED} of {len(stays)} stays are listed; the stays_ CSV "
        "file lists every stay.</p>"
        if len(stays) > STAYS_LISTED
        else ""
    )
    return (
        "<h4>Stays</h4>\n"
        + table
        + "\n<p>Arrived: between the analysed record before the stay and its first record. "
        "Left: between its last record and the analysed record after it. Not bounded: that "
        "record could lie at the place, reports no accuracy, is a position jump or shares "
        "the instant (the device may have been there already, or still), or there is none. "
        "A bound may span a silence.</p>" + note
    )


def _source_title(source: Mapping[str, Any]) -> str:
    return f"Source {escape(_text(source.get('id')))}: {escape(_text(source.get('label')))}"


def _fill(text: str, parameters: Mapping[str, Any]) -> str:
    try:
        return text.format(**parameters)
    except (KeyError, IndexError, ValueError):
        return text


def _minutes_text(minutes: object) -> str | None:
    if not isinstance(minutes, int | float):
        return None
    return f"{minutes:.1f} min ({minutes / 60:.1f} h)"


def _kilometres_text(metres: object) -> str | None:
    if not isinstance(metres, int | float):
        return None
    return f"{metres / 1000:.1f} km"


def _left_out_text(segments: object, metres: object) -> str | None:
    """'12 steps, 3.4 km'; None for a summary without these figures."""
    if not isinstance(segments, int) or not isinstance(metres, int | float):
        return None
    return f"{segments} steps, {metres / 1000:.1f} km"


def _local_text(position: Mapping[str, Any], local_key: str, offset_key: str) -> str | None:
    """Wall-clock time, with its UTC offset when the record carries one (minutes east)."""
    local = position.get(local_key)
    if local is None:
        return None
    offset = position.get(offset_key)
    if isinstance(offset, int) and not isinstance(offset, bool):
        try:
            wall = datetime.fromisoformat(str(local))
        except ValueError:
            return str(local)
        record_zone = timezone(timedelta(minutes=offset))
        return _local_moment_text(wall.replace(tzinfo=record_zone))
    return str(local)


def _local_moment_text(moment: datetime) -> str:
    """An aware moment in its own offset, formatted like every other local time."""
    return display_local_text(moment, moment.tzinfo)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_of_iso(value: object) -> object:
    """metadata.json keeps the raw instant; the report shows it to the second."""
    moment = _aware_moment(value)
    return value if moment is None else _utc_text(moment)


def _local_of_iso(value: object) -> object:
    moment = _aware_moment(value)
    return value if moment is None else _local_moment_text(moment)


def _aware_moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    return [entry for entry in _list(value) if isinstance(entry, Mapping)]


def _read_optional_json(path: Path) -> Mapping[str, Any] | None:
    """A JSON document the run may not have written (failed run, online off)."""
    if not path.is_file():
        return None
    return _mapping(json.loads(path.read_text(encoding="utf-8")))
