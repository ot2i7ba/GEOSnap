# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Drive the reader and parser over one source file with progress and cancellation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from geosnap.extraction.line_parser import parse_line_records
from geosnap.extraction.models import (
    ExtractionCounters,
    GeoPoint,
    RejectedLine,
    SourceExtractionOutcome,
)
from geosnap.extraction.source_reader import SourceReader
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

CANCEL_CHECK_INTERVAL_LINES = 1000


@dataclass(frozen=True, slots=True)
class ExtractionProgress:
    """Counters of the source being read; the pipeline names the source (1-based index)."""

    bytes_read: int
    size_bytes: int
    accepted: int
    invalid: int
    unrelated: int
    source_index: int = 1
    source_count: int = 1
    source_label: str = ""


def report_extraction_progress(
    on_progress: Callable[[ExtractionProgress], None] | None,
    bytes_read: int,
    size_bytes: int,
    counters: ExtractionCounters,
) -> None:
    """Hand the counters of the source being read to the optional progress callback; the
    readers call it once per read chunk and once more when they finish."""
    if on_progress is None:
        return
    on_progress(
        ExtractionProgress(
            bytes_read=bytes_read,
            size_bytes=size_bytes,
            accepted=counters.accepted,
            invalid=counters.invalid,
            unrelated=counters.unrelated,
        )
    )


def extract_points(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
) -> SourceExtractionOutcome:
    """Read the whole file once, classify every line and collect the valid points.

    Every point is stamped with source_id, the 1-based number of this file in its project.

    Rejected lines are handed to on_rejected as they occur so they can be streamed to
    disk. Progress is reported once per chunk. Cancellation is checked every
    CANCEL_CHECK_INTERVAL_LINES lines and whenever a new chunk has been read, so that
    a file with very few line breaks stays cancellable; a cancelled run returns the
    points seen so far.
    """
    reader = SourceReader(source_path, settings.read_chunk_bytes, settings.fallback_encoding)
    points: list[GeoPoint] = []
    counters = ExtractionCounters()
    cancelled = False
    last_reported_bytes = -1
    logger.info("Extraction started: %s (%d bytes)", source_path.name, reader.size_bytes)

    def report_progress() -> None:
        report_extraction_progress(on_progress, reader.bytes_read, reader.size_bytes, counters)

    for source_line in reader.lines():
        records = parse_line_records(source_line.text, source_line.number)
        if not records:
            counters.unrelated += 1
        for parsed in records:
            if isinstance(parsed, GeoPoint):
                points.append(parsed if source_id == 1 else replace(parsed, source_id=source_id))
                counters.accepted += 1
            else:
                counters.invalid += 1
                on_rejected(parsed)
                logger.warning("Line %d rejected: %s", parsed.line_number, parsed.reason)

        chunk_boundary_crossed = reader.bytes_read != last_reported_bytes
        if chunk_boundary_crossed:
            last_reported_bytes = reader.bytes_read
            report_progress()

        if (
            is_cancelled is not None
            and (chunk_boundary_crossed or source_line.number % CANCEL_CHECK_INTERVAL_LINES == 0)
            and is_cancelled()
        ):
            cancelled = True
            logger.warning("Extraction cancelled after line %d", source_line.number)
            break

    counters.decode_replacements = reader.decode_replacements
    # The text format has no untimed records: no saved places.
    outcome = SourceExtractionOutcome(points, [], counters, reader.report(), cancelled)
    report_progress()
    logger.info(
        "Extraction finished: accepted=%d invalid=%d unrelated=%d lines=%d cancelled=%s",
        counters.accepted,
        counters.invalid,
        counters.unrelated,
        outcome.read_report.line_count,
        outcome.cancelled,
    )
    return outcome
