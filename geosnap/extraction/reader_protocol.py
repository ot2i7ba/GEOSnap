# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""The call signature every format reader shares."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from geosnap.extraction.extractor import ExtractionProgress
from geosnap.extraction.models import RejectedLine, SourceExtractionOutcome
from geosnap.settings import ExtractionSettings


class FormatReader(Protocol):
    """extract_points, extract_kml, extract_kmz, extract_gpx, extract_csv, extract_google
    and extract_geojson: one file in, the common outcome out, rejections streamed to
    ``on_rejected``, progress per chunk, cancellation polled. A reader may take further
    keyword arguments with defaults (a CSV mapping, a Google format, a zone for naive
    times). A file that is not acceptable in its format raises SourceFormatError."""

    def __call__(
        self,
        source_path: Path,
        settings: ExtractionSettings,
        on_rejected: Callable[[RejectedLine], None],
        on_progress: Callable[[ExtractionProgress], None] | None = ...,
        is_cancelled: Callable[[], bool] | None = ...,
        source_id: int = ...,
    ) -> SourceExtractionOutcome: ...
