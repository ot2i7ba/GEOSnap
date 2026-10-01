# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Value objects shared by the extraction pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from geosnap.extraction.source_reader import SourceReadReport

SOURCE_KINDS = ("smartphone", "computer", "laptop", "tablet", "watch", "vehicle", "other")
SOURCE_FORMATS = (
    "text",
    "kml",
    "kmz",
    "gpx",
    "csv",
    "google-records",
    "google-timeline",
    "google-semantic",
    "geojson",
)
# A .json file that is neither a Google export nor GeoJSON; it is refused, never guessed.
UNRECOGNISED_FORMAT = "unrecognised"

# What a record number counts in each format (GeoPoint.line_number): one wording everywhere.
_RECORD_NOUNS = {
    "text": ("line", "lines"),
    "csv": ("line", "lines"),
    "kml": ("placemark", "placemarks"),
    "kmz": ("placemark", "placemarks"),
    "gpx": ("GPX point", "GPX points"),
    "google-records": ("record", "records"),
    "google-timeline": ("record", "records"),
    "google-semantic": ("record", "records"),
    "geojson": ("feature", "features"),
}


def record_noun(source_format: str, plural: bool = False) -> str:
    """'line', 'placemark', 'GPX point', 'feature' or 'record' for a source format."""
    single, several = _RECORD_NOUNS.get(source_format, ("record", "records"))
    return several if plural else single


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """One input file of a project: what it is, how it is named and coloured."""

    identifier: int
    label: str
    kind: str
    colour: str
    path: Path
    format: str
    # CSV only: column roles, time format, zone and delimiter (see csv_reader.CsvMapping).
    csv_mapping: dict[str, str] | None = field(default=None, hash=False)
    # Confidence level of the source's accuracy radii: "68", "95" or "unknown".
    accuracy_level: str = "unknown"
    # CSV only: where the mapping's zone came from ("examiner", "display_setting",
    # "host_system" or "header"); None when the mapping has no zone.
    zone_origin: str | None = None


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """One validated location record and the exact source line it came from.

    ``source_id`` names the input file (1-based, see SourceDescriptor). ``label`` and
    ``note`` carry the record's own name and description when the format has them (KML);
    ``accuracy_known`` is False when the format reports no accuracy (the accuracies are
    then 0.0 and are not drawn as circles).
    """

    line_number: int
    latitude: float
    longitude: float
    latitude_accuracy_m: float
    longitude_accuracy_m: float
    timestamp_utc: datetime
    original_line: str
    source_id: int = 1
    label: str | None = None
    note: str | None = None
    accuracy_known: bool = True
    # A local wall time that occurred twice (clocks going back) is read as its earlier
    # occurrence; this is how much later the true instant can be (0.0 when unambiguous).
    time_ambiguity_seconds: float = 0.0
    # How the position was determined: "gnss", "wifi", "cell", "network", or None (unknown).
    positioning_method: str | None = None
    # Factor from the reported radius to the radius every uncertainty computation uses (the
    # source's accuracy level and analysis.accuracy_confidence); set once per source.
    accuracy_scale: float = 1.0

    @property
    def accuracy_radius_m(self) -> float:
        """Radius drawn on the map: the larger of the two reported accuracies."""
        return max(self.latitude_accuracy_m, self.longitude_accuracy_m)

    @property
    def uncertainty_radius_m(self) -> float:
        """The reported radius at the confidence level the analyses use."""
        return self.accuracy_radius_m * self.accuracy_scale

    @property
    def duplicate_key(self) -> tuple[int, float, float, float, float, str | None]:
        """Source, position, accuracy and positioning method; records sharing this key count
        as duplicates (a satellite fix and a cell fix at one spot are different reports)."""
        return (
            self.source_id,
            self.latitude,
            self.longitude,
            self.latitude_accuracy_m,
            self.longitude_accuracy_m,
            self.positioning_method,
        )


# How many of a source's records carry an accuracy (metadata, report, map payload).
ACCURACY_REPORTED_BY_ALL = "all"
ACCURACY_REPORTED_BY_SOME = "some"
ACCURACY_REPORTED_BY_NONE = "none"


def accuracy_reporting(points: Sequence[GeoPoint]) -> str:
    """'all', 'some' or 'none': CSV and Google records carry an accuracy only optionally."""
    known = sum(1 for point in points if point.accuracy_known)
    if points and known == len(points):
        return ACCURACY_REPORTED_BY_ALL
    return ACCURACY_REPORTED_BY_SOME if known else ACCURACY_REPORTED_BY_NONE


@dataclass(frozen=True, slots=True)
class ReferencePlace:
    """A record without a timestamp: a position like any other, minus the time (KML
    placemark without TimeStamp, GPX point without time, GeoJSON feature without a time
    candidate, CSV row with an empty timestamp cell or from a mapping without time
    column). ``accuracy_known`` is False when the record reports no accuracy."""

    source_id: int
    record_number: int
    name: str
    note: str | None
    latitude: float
    longitude: float
    original_record: str
    accuracy_m: float = 0.0
    accuracy_known: bool = False
    # How the position was determined, as for GeoPoint.positioning_method.
    positioning_method: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedLine:
    """A line that looked like a location record but failed validation."""

    line_number: int
    reason: str
    original_line: str


@dataclass(slots=True)
class ExtractionCounters:
    """Running totals per line category."""

    accepted: int = 0
    invalid: int = 0
    unrelated: int = 0
    decode_replacements: int = 0
    # Accepted (GPX, KML, CSV) or rejected (Google) timestamps that carried no offset.
    naive_timestamps: int = 0
    # Records without a timestamp (SourceExtractionOutcome.reference_places).
    undated: int = 0


@dataclass(frozen=True, slots=True)
class KmzEntryReport:
    """The KML entry read from a KMZ archive; the source hash covers the archive itself."""

    name: str
    # SHA-256 of the decompressed bytes read (all of them when read_completely).
    sha256_hex: str
    bytes_read: int
    read_completely: bool
    # Further .kml entries of the archive, in archive order; they are not read. Only the
    # first names are kept (kmz_reader.MAX_OTHER_ENTRIES_NAMED); the count covers all.
    other_kml_entries: tuple[str, ...]
    other_kml_entry_count: int = 0


@dataclass(slots=True)
class SourceExtractionOutcome:
    """What a reader returns: dated points, undated records, counters, read report,
    cancel flag."""

    points: list[GeoPoint]
    reference_places: list[ReferencePlace]
    counters: ExtractionCounters
    read_report: SourceReadReport
    cancelled: bool
    kmz_entry: KmzEntryReport | None = None


class SourceFormatError(ValueError):
    """The file is not acceptable in its format (DOCTYPE, malformed XML/JSON/CSV, ...).

    ``partial_outcome`` holds what was read before the failure (counters, records and the
    digest of the bytes read), so the caller can document the partial read.
    """

    def __init__(self, message: str, partial_outcome: SourceExtractionOutcome | None = None):
        super().__init__(message)
        self.partial_outcome = partial_outcome


def naive_timestamp_handling(
    source_format: str,
    csv_mapping: dict[str, str] | None,
    naive_time_zone: str = "",
    display_zone_origin: str = "display_setting",
) -> str:
    """How the reader of a format treats a timestamp without offset (for metadata/report);
    ``naive_time_zone`` is the zone GeoJSON times are read in (empty: none set), and
    ``display_zone_origin`` says why none is set when the display zone is not "local"."""
    if source_format == "csv":
        zone = (csv_mapping or {}).get("zone", "")
        return f"read in {zone} (column mapping)" if zone else "rejected (no zone chosen)"
    if source_format == "gpx":
        return "assumed UTC (GPX times are UTC by definition)"
    if source_format in ("kml", "kmz"):
        return "assumed UTC (warned in the log)"
    if source_format.startswith("google-"):
        return "rejected (Google exports carry offsets)"
    if source_format == "geojson":
        if naive_time_zone:
            return f"read in {naive_time_zone} (display zone)"
        if display_zone_origin == "host_system":
            return "rejected (display zone comes from the host system, not confirmed)"
        return "rejected (display zone is local, no zone to read them in)"
    return "not applicable (every record carries an offset)"
