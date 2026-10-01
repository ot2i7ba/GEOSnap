# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""CSV exports: every accepted point, and every rejected line as it is found."""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping, Sequence
from datetime import tzinfo
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from geosnap.extraction.duplicates import AnnotatedPoint
from geosnap.extraction.models import ReferencePlace, RejectedLine
from geosnap.timezones import display_zone_label, to_display_time

CSV_ENCODING = "utf-8-sig"

POINTS_CSV_COLUMNS = (
    "line_number",
    "latitude",
    "longitude",
    "latitude_accuracy_m",
    "longitude_accuracy_m",
    "accuracy_radius_m",
    "timestamp_utc",
    "timestamp_local",
    "local_timezone",
    "duplicate_count",
    "source_id",
    "source_label",
    "record_label",
    "record_note",
    "accuracy_known",
    "positioning_method",
    "original_line",
)

REJECTED_CSV_COLUMNS = ("source_id", "source_label", "line_number", "reason", "original_line")


def write_points_csv(
    path: Path,
    annotated_points: Sequence[AnnotatedPoint],
    display_tz: tzinfo | None,
    display_zone: str,
    source_labels: Mapping[int, str] | None = None,
    undated_records: Sequence[ReferencePlace] = (),
) -> int:
    """Write all dated points in chronological order, then the undated records in source
    and record order with empty time and duplicate columns (no time orders them, and
    reports per position are counted over dated records only); returns the number of
    rows written."""
    labels = source_labels or {}
    with path.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(POINTS_CSV_COLUMNS)
        for entry in annotated_points:
            point = entry.point
            local_time = to_display_time(point.timestamp_utc, display_tz)
            writer.writerow(
                [
                    point.line_number,
                    point.latitude,
                    point.longitude,
                    point.latitude_accuracy_m,
                    point.longitude_accuracy_m,
                    point.accuracy_radius_m,
                    point.timestamp_utc.isoformat(),
                    local_time.isoformat(timespec="seconds"),
                    display_zone_label(local_time, display_zone),
                    entry.duplicate_count,
                    point.source_id,
                    labels.get(point.source_id, ""),
                    point.label or "",
                    point.note or "",
                    "true" if point.accuracy_known else "false",
                    point.positioning_method or "",
                    point.original_line,
                ]
            )
        for record in sorted(undated_records, key=lambda r: (r.source_id, r.record_number)):
            writer.writerow(
                [
                    record.record_number,
                    record.latitude,
                    record.longitude,
                    record.accuracy_m,
                    record.accuracy_m,
                    record.accuracy_m,
                    "",
                    "",
                    "",
                    "",
                    record.source_id,
                    labels.get(record.source_id, ""),
                    record.name,
                    record.note or "",
                    "true" if record.accuracy_known else "false",
                    record.positioning_method or "",
                    record.original_record,
                ]
            )
    return len(annotated_points) + len(undated_records)


class RejectedLineWriter:
    """Streams rejected lines to CSV while extraction runs, so nothing is held in memory."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None
        self._writer: Any | None = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def __enter__(self) -> RejectedLineWriter:
        self._file = self._path.open("w", encoding=CSV_ENCODING, newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(REJECTED_CSV_COLUMNS)
        return self

    def write(self, rejected: RejectedLine, source_id: int = 1, source_label: str = "") -> None:
        if self._writer is None:
            raise RuntimeError("RejectedLineWriter must be used as a context manager")
        self._writer.writerow(
            [source_id, source_label, rejected.line_number, rejected.reason, rejected.original_line]
        )
        self._count += 1

    def for_source(self, source_id: int, source_label: str) -> Callable[[RejectedLine], None]:
        """The per-source callback the extractors stream their rejected lines to."""

        def write_for_source(rejected: RejectedLine) -> None:
            self.write(rejected, source_id, source_label)

        return write_for_source

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None
