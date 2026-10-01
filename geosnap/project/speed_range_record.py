# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Speed ranges recorded from the map's Speed tool.

The browser sends the two ends of a range, the records the examiner excluded with a reason
each, and the figures it computed. The server reads the payload of the map file it serves,
recomputes every figure with analysis.speed_range and refuses the record when the browser's
figures differ: the recorded figures are GEOSnap's own. The JSON record and a one-page sheet
are written to speed_ranges/ and chained in speed_ranges/records.jsonl like search areas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from geosnap import APP_NAME, __version__
from geosnap.analysis.speed_range import (
    SPEED_RANGE_METHOD_TEXT,
    SPEED_RANGE_PREMISE_TEXT,
    RangeRecord,
    SpeedRangeError,
    SpeedRangeFigures,
    speed_range_figures,
)
from geosnap.project.search_area import (
    FORBIDDEN_CHARACTERS,
    SEARCH_AREA_SHEET_CSS,
    CaseDetails,
    ChainedRecordDraft,
    append_chained_record,
)
from geosnap.project.verify import verify_command
from geosnap.project.workspace import (
    SEARCH_AREA_RECORDS_NAME,
    SPEED_RANGES_DIR_NAME,
    STAMP_FORMAT,
)
from geosnap.timezones import display_local_text, resolve_display_timezone

logger = logging.getLogger(__name__)

RECORD_TYPE = "geosnap-speed-range"
RECORD_VERSION = 1
EXCLUSION_REASONS = ("position outlier", "implausible jump", "duplicate position", "other")
MAX_EXCLUSIONS = 20_000
MAX_NOTE_LENGTH = 500
MAX_LABEL_LENGTH = 200
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PAYLOAD_START = '<script type="application/json" id="geosnap-data">'
PAYLOAD_END = "</script>"
# The browser's figures may differ from GEOSnap's by float noise only (GeographicLib in both).
FIGURE_TOLERANCE = 1e-6


class SpeedRangeRecordError(ValueError):
    """The request is refused; the message says why and goes back to the browser."""


@dataclass(frozen=True, slots=True)
class RangeEnd:
    sequence: int
    record_number: int
    utc: str


@dataclass(frozen=True, slots=True)
class Exclusion:
    sequence: int
    record_number: int
    utc: str
    reason: str
    note: str


@dataclass(frozen=True, slots=True)
class SpeedRangeRequest:
    source_id: int
    first: RangeEnd
    last: RangeEnd
    exclusions: tuple[Exclusion, ...]
    browser_figures: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RecordedSpeedRange:
    record_number: int
    files: dict[str, str]  # file name -> SHA-256
    last_line_sha256: str


# --- request ------------------------------------------------------------------------------


def _field(document: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in document:
        raise SpeedRangeRecordError(f"{path}{key} is missing")
    return document[key]


def _integer(value: object, path: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SpeedRangeRecordError(f"{path} must be a whole number of at least {minimum}")
    return value


def _text(value: object, path: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or FORBIDDEN_CHARACTERS.search(value):
        raise SpeedRangeRecordError(f"{path} must be text of at most {maximum} characters")
    return value


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SpeedRangeRecordError(f"{path} must be an object")
    return value


def _range_end(value: object, path: str) -> RangeEnd:
    end = _mapping(value, path)
    return RangeEnd(
        _integer(_field(end, "seq", path + "."), path + ".seq"),
        _integer(_field(end, "record_number", path + "."), path + ".record_number", 0),
        _text(_field(end, "utc", path + "."), path + ".utc", 64),
    )


def parse_request(document: object) -> SpeedRangeRequest:
    """Check the shape of the browser's request; the values are checked against the map."""
    request = _mapping(document, "the record")
    if request.get("record_type") != RECORD_TYPE or request.get("record_version") != RECORD_VERSION:
        raise SpeedRangeRecordError(
            f"the record is not a {RECORD_TYPE} of version {RECORD_VERSION}"
        )
    source = _mapping(_field(request, "source", ""), "source")
    exclusions_value = _field(request, "excluded", "")
    if not isinstance(exclusions_value, list) or len(exclusions_value) > MAX_EXCLUSIONS:
        raise SpeedRangeRecordError(f"excluded must be a list of at most {MAX_EXCLUSIONS} records")
    exclusions: list[Exclusion] = []
    for index, value in enumerate(exclusions_value):
        path = f"excluded[{index}]"
        entry = _mapping(value, path)
        reason = _field(entry, "reason", path + ".")
        if reason not in EXCLUSION_REASONS:
            raise SpeedRangeRecordError(
                f"{path}.reason must be one of: {', '.join(EXCLUSION_REASONS)}"
            )
        end = _range_end(entry, path)
        note = _text(_field(entry, "note", path + "."), path + ".note", MAX_NOTE_LENGTH)
        if reason == "other" and not note.strip():
            raise SpeedRangeRecordError(f"{path}.note must say why when the reason is other")
        exclusions.append(
            Exclusion(
                end.sequence,
                end.record_number,
                end.utc,
                reason,
                note,
            )
        )
    if len({exclusion.sequence for exclusion in exclusions}) != len(exclusions):
        raise SpeedRangeRecordError("a record is excluded twice")
    return SpeedRangeRequest(
        _integer(_field(source, "id", "source."), "source.id"),
        _range_end(_field(request, "first", ""), "first"),
        _range_end(_field(request, "last", ""), "last"),
        tuple(sorted(exclusions, key=lambda exclusion: exclusion.sequence)),
        _mapping(_field(request, "browser_figures", ""), "browser_figures"),
    )


# --- recomputation from the served map ----------------------------------------------------


def read_map_payload(map_text: str) -> dict[str, Any]:
    """The JSON payload embedded in a GEOSnap map file."""
    start = map_text.find(PAYLOAD_START)
    end = map_text.find(PAYLOAD_END, start + len(PAYLOAD_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise SpeedRangeRecordError("the served map carries no GEOSnap payload")
    try:
        payload = json.loads(map_text[start + len(PAYLOAD_START) : end])
    except (ValueError, RecursionError) as error:
        raise SpeedRangeRecordError("the payload of the served map is not readable") from error
    if not isinstance(payload, dict):
        raise SpeedRangeRecordError("the payload of the served map is not readable")
    return payload


@dataclass(frozen=True, slots=True)
class SourceRecords:
    label: str
    sha256: str
    time_resolution_seconds: float
    implausible_speed_kmh: float
    records: list[RangeRecord]
    # False for a map whose records carry neither asc nor cmb_rep: its radii were used as
    # reported and its browser block shows no figures as reported.
    reported_figures_shown: bool = True


def source_records(payload: Mapping[str, Any], source_id: int) -> SourceRecords:
    """The analysed records of one source as the map payload carries them."""
    source = next(
        (entry for entry in payload.get("sources") or [] if entry.get("id") == source_id), None
    )
    if source is None:
        raise SpeedRangeRecordError(f"the map has no source {source_id}")
    resolution = source.get("time_resolution_s")
    parameters = (payload.get("analysis") or {}).get("parameters") or {}
    threshold = parameters.get("implausible_speed_kmh")
    if not isinstance(resolution, int | float) or not isinstance(threshold, int | float):
        raise SpeedRangeRecordError(
            "the served map carries no time resolution or speed threshold; generate the "
            "project again"
        )
    provenance: Mapping[str, Any] = next(
        (
            entry
            for entry in (payload.get("provenance") or {}).get("sources") or []
            if entry.get("id") == source_id
        ),
        {},
    )
    records: list[RangeRecord] = []
    reported_figures_shown = False
    for point in payload.get("points") or []:
        if point.get("s") != source_id or not isinstance(point.get("seq"), int):
            continue
        reported_figures_shown = reported_figures_shown or "asc" in point or "cmb_rep" in point
        records.append(
            RangeRecord(
                sequence=point["seq"],
                record_number=point["n"],
                latitude=float(point["lat"]),
                longitude=float(point["lon"]),
                accuracy_m=None if point.get("acc_known") is False else float(point["radius"]),
                utc=datetime.fromisoformat(point["utc"]),
                path_m=float(point["cum"]),
                minimum_path_m=float(point["cmb"]),
                speed_kmh=None if point.get("spd") is None else float(point["spd"]),
                time_ambiguity_seconds=float(point.get("tam") or 0.0),
                # A map without them used the radii as reported.
                accuracy_scale=float(point.get("asc") or 1.0),
                minimum_path_reported_m=None
                if point.get("cmb_rep") is None
                else float(point["cmb_rep"]),
            )
        )
    records.sort(key=lambda record: record.sequence)
    return SourceRecords(
        str(source.get("label") or ""),
        str(provenance.get("sha256") or ""),
        float(resolution),
        float(threshold),
        records,
        reported_figures_shown,
    )


def _check_named_record(by_sequence: Mapping[int, RangeRecord], end: RangeEnd, role: str) -> None:
    record = by_sequence.get(end.sequence)
    if (
        record is None
        or record.record_number != end.record_number
        or record.utc != datetime.fromisoformat(end.utc)
    ):
        raise SpeedRangeRecordError(
            f"the {role} (record {end.record_number}) is not an analysed record of this source "
            "on the served map"
        )


def recompute(
    payload: Mapping[str, Any], request: SpeedRangeRequest
) -> tuple[SourceRecords, SpeedRangeFigures]:
    """GEOSnap's own figures for the request, from the map payload."""
    source = source_records(payload, request.source_id)
    by_sequence = {record.sequence: record for record in source.records}
    try:
        _check_named_record(by_sequence, request.first, "first record")
        _check_named_record(by_sequence, request.last, "last record")
        for exclusion in request.exclusions:
            _check_named_record(
                by_sequence,
                RangeEnd(exclusion.sequence, exclusion.record_number, exclusion.utc),
                "excluded record",
            )
    except ValueError as error:
        if isinstance(error, SpeedRangeRecordError):
            raise
        raise SpeedRangeRecordError(f"a time of the record is not readable: {error}") from error
    try:
        figures = speed_range_figures(
            source.records,
            request.first.sequence,
            request.last.sequence,
            [exclusion.sequence for exclusion in request.exclusions],
            source.time_resolution_seconds,
            source.implausible_speed_kmh,
        )
    except SpeedRangeError as error:
        raise SpeedRangeRecordError(str(error)) from error
    return source, figures


# The figures as reported, which a map without asc and cmb_rep does not show (they equal
# the others).
REPORTED_FIGURE_KEYS = frozenset(
    {"minimum_average_reported_kmh", "stepwise_minimum_average_reported_kmh"}
)


def compare_figures(
    own: SpeedRangeFigures, browser: Mapping[str, Any], reported_figures_shown: bool = True
) -> None:
    """Refuse when the browser showed other figures than GEOSnap computes. Without
    reported_figures_shown the browser need not give the figures as reported."""
    for key, value in asdict(own).items():
        if not reported_figures_shown and key in REPORTED_FIGURE_KEYS and key not in browser:
            continue
        shown = browser.get(key)
        if (
            isinstance(value, float)
            and isinstance(shown, int | float)
            and not isinstance(shown, bool)
        ):
            try:
                shown_float = float(shown)  # a huge JSON integer overflows here
            except OverflowError:
                shown_float = math.inf
            if math.isfinite(shown_float) and abs(shown_float - value) <= FIGURE_TOLERANCE * max(
                1.0, abs(value)
            ):
                continue
        elif shown == value and type(shown) is type(value):
            continue
        raise SpeedRangeRecordError(
            f"the figures shown in the browser differ from GEOSnap's ({key}); nothing was "
            "recorded; reload the map and try again"
        )


# --- files ---------------------------------------------------------------------------------


def _figure_rows(figures: SpeedRangeFigures) -> list[tuple[str, str]]:
    """The sheet's figures. Lower bounds are rounded down (speeds to 0.1 km/h, distances
    to the millimetre), never to the nearest value."""

    def speed(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} km/h"

    def lower_speed(value: float) -> str:
        return f"{math.floor(value * 10 + 1e-9) / 10:.1f} km/h"

    def lower_distance(metres: float) -> str:
        return f"{math.floor(metres * 1000 + 1e-6) / 1000:.3f} m"

    def as_reported(value: float) -> str:
        return f"; as reported: at least {lower_speed(value)}"

    return [
        (
            "Records in the range",
            f"{figures.records_in_range} ({figures.excluded_records} excluded)",
        ),
        ("Elapsed time", f"{figures.elapsed_seconds:.6f} s"),
        (
            "Time uncertainty",
            f"{figures.time_uncertainty_seconds:g} s (resolution "
            f"{figures.time_resolution_seconds:g} s plus ambiguous local times of the ends)",
        ),
        (
            "Minimum average speed",
            f"at least {lower_speed(figures.minimum_average_kmh)} (straight line, at least "
            f"{lower_distance(figures.straight_minimum_m)}; premise: the 2 end records within "
            f"their uncertainty circles{as_reported(figures.minimum_average_reported_kmh)})",
        ),
        (
            "Stepwise minimum",
            f"at least {lower_speed(figures.stepwise_minimum_average_kmh)} (at least "
            f"{lower_distance(figures.stepwise_minimum_m)}; premise: all "
            f"{figures.stepwise_premise_records} records within their uncertainty circles"
            f"{as_reported(figures.stepwise_minimum_average_reported_kmh)})",
        ),
        ("Straight line", f"{figures.straight_m:.3f} m"),
        ("Path over the records (estimate)", f"{figures.path_m:.3f} m"),
        ("Average over the path (estimate)", speed(figures.path_average_kmh)),
        ("Average over the straight line", speed(figures.straight_average_kmh)),
        ("Slowest step", speed(figures.slowest_step_kmh)),
        ("Fastest step", speed(figures.fastest_step_kmh)),
        ("Implausible steps", f"{figures.implausible_steps} of {figures.known_steps}"),
        (
            "Every record of the range on the map",
            "yes" if figures.complete else "no (thinned: sums from the full list)",
        ),
    ]


def _pairs_html(rows: list[tuple[str, str]]) -> str:
    return (
        '<table class="pairs">'
        + "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in rows)
        + "</table>"
    )


def render_sheet(
    document: Mapping[str, Any],
    figures: SpeedRangeFigures,
    record_number: int,
    recorded_local: str,
    json_file: tuple[str, str],
    verify_command_text: str,
) -> str:
    """One self-contained page without scripts; every value is escaped."""
    request = document["request"]
    source = request["source"]
    header = [
        ("Case reference", document["generation"]["case"]["reference"] or "not given"),
        ("Examiner", document["generation"]["case"]["examiner"] or "not given"),
        ("Project", document["generation"]["project"]),
        ("Source", f"{source['label']} (id {source['id']})"),
        ("Source SHA-256", source["sha256"] or "not recorded"),
        ("Map file", f"{document['map_file']['name']} (SHA-256 {document['map_file']['sha256']})"),
        ("First record", f"{request['first']['record_number']} · {request['first']['utc']}"),
        ("Last record", f"{request['last']['record_number']} · {request['last']['utc']}"),
    ]
    exclusions = request["excluded"]
    exclusion_html = (
        "<table><tr><th>Record</th><th>UTC</th><th>Reason</th><th>Note</th></tr>"
        + "".join(
            f"<tr><td>{entry['record_number']}</td><td>{escape(entry['utc'])}</td>"
            f"<td>{escape(entry['reason'])}</td><td>{escape(entry['note'])}</td></tr>"
            for entry in exclusions
        )
        + "</table>"
        if exclusions
        else "<p>None.</p>"
    )
    title = f"Speed range {record_number} – {document['generation']['project']}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n<style>{SEARCH_AREA_SHEET_CSS}</style>\n</head>\n<body>\n"
        f"<h1>{escape(title)}</h1>\n"
        f'<p class="subtitle">Recorded {escape(recorded_local)} from the map\'s Speed tool; '
        f"figures recomputed by {escape(APP_NAME)} {escape(__version__)} from the map file.</p>\n"
        f"{_pairs_html(header)}\n"
        f"<h2>Figures</h2>\n{_pairs_html(_figure_rows(figures))}\n"
        f"<h2>Excluded records</h2>\n{exclusion_html}\n"
        f"<h2>Method</h2>\n<p>{escape(SPEED_RANGE_METHOD_TEXT)}</p>\n"
        f"<p>{escape(SPEED_RANGE_PREMISE_TEXT)}</p>\n"
        "<h2>Integrity</h2>\n"
        f"<p>The SHA-256 of this sheet and of {escape(json_file[0])} "
        f'(<span class="hash">{escape(json_file[1])}</span>) is recorded in '
        f"{SPEED_RANGES_DIR_NAME}/{SEARCH_AREA_RECORDS_NAME} (record {record_number}, hash "
        f"chain); check them with <code>{escape(verify_command_text)}</code>.</p>\n"
        "</body>\n</html>\n"
    )


def _end_document(end: RangeEnd, by_sequence: Mapping[int, RangeRecord]) -> dict[str, Any]:
    return {
        "seq": end.sequence,
        "record_number": end.record_number,
        "utc": by_sequence[end.sequence].utc.isoformat(),
    }


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def record_speed_range(
    project_directory: Path,
    map_path: Path,
    document: object,
    case: CaseDetails,
    recorded_at: datetime | None = None,
) -> RecordedSpeedRange:
    """Check the request against the served map, recompute it and record it in the chain."""
    request = parse_request(document)
    map_bytes = map_path.read_bytes()
    payload = read_map_payload(map_bytes.decode("utf-8"))
    source, figures = recompute(payload, request)
    compare_figures(figures, request.browser_figures, source.reported_figures_shown)
    by_sequence = {record.sequence: record for record in source.records}
    moment = recorded_at or datetime.now().astimezone()
    moment_local = moment.astimezone(resolve_display_timezone(case.display_timezone))
    recorded_utc = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The browser's own field names: seq, record_number, utc.
    request_document: dict[str, Any] = {
        "source": {"id": request.source_id, "label": source.label, "sha256": source.sha256},
        # Times as the map records them, not as the browser spelled them.
        "first": _end_document(request.first, by_sequence),
        "last": _end_document(request.last, by_sequence),
        "excluded": [
            {
                "seq": exclusion.sequence,
                "record_number": exclusion.record_number,
                "utc": by_sequence[exclusion.sequence].utc.isoformat(),
                "reason": exclusion.reason,
                "note": exclusion.note,
            }
            for exclusion in request.exclusions
        ],
    }
    digests: dict[str, str] = {}

    def compose(record_number: int, previous_line_sha256: str) -> ChainedRecordDraft:
        base_name = f"speed_range_{record_number}_{moment.strftime(STAMP_FORMAT)}"
        json_name, html_name = f"{base_name}.json", f"{base_name}.html"
        document_out: dict[str, Any] = {
            "generation": {
                "record_type": RECORD_TYPE,
                "record_version": RECORD_VERSION,
                "record_number": record_number,
                "recorded_utc": recorded_utc,
                "recorded_local": moment_local.isoformat(timespec="seconds"),
                "application": f"{APP_NAME} {__version__}",
                "case": {"reference": case.reference, "examiner": case.examiner},
                "project": case.project_name,
            },
            "map_file": {"name": map_path.name, "sha256": hashlib.sha256(map_bytes).hexdigest()},
            "request": request_document,
            "figures": asdict(figures),
            "method": SPEED_RANGE_METHOD_TEXT,
            "premise": SPEED_RANGE_PREMISE_TEXT,
        }
        json_bytes = _json_bytes(document_out)
        digests[json_name] = hashlib.sha256(json_bytes).hexdigest()
        sheet = render_sheet(
            document_out,
            figures,
            record_number,
            display_local_text(moment_local, moment_local.tzinfo),
            (json_name, digests[json_name]),
            verify_command(project_directory),
        ).encode("utf-8")
        digests[html_name] = hashlib.sha256(sheet).hexdigest()
        return ChainedRecordDraft(
            files={json_name: json_bytes, html_name: sheet},
            line_document={
                "record_number": record_number,
                "recorded_utc": recorded_utc,
                "recorded_local": document_out["generation"]["recorded_local"],
                "source": request_document["source"],
                "first_utc": request_document["first"]["utc"],
                "last_utc": request_document["last"]["utc"],
                "excluded_records": len(request.exclusions),
                "files": dict(sorted(digests.items())),
                "previous_line_sha256": previous_line_sha256,
            },
        )

    appended = append_chained_record(project_directory, SPEED_RANGES_DIR_NAME, compose)
    logger.info(
        "Speed range %d recorded in %s: %s; records.jsonl last line sha256 %s",
        appended.record_number,
        project_directory / SPEED_RANGES_DIR_NAME,
        ", ".join(f"{name} sha256 {digest}" for name, digest in sorted(digests.items())),
        appended.last_line_sha256,
    )
    return RecordedSpeedRange(
        appended.record_number, dict(sorted(digests.items())), appended.last_line_sha256
    )
