# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Read the KML entry of a KMZ archive as a stream: nothing is unpacked to disk.

The archive is hashed completely first (the source SHA-256 is that of the .kmz file),
then one entry is decompressed chunk by chunk into the KML reader. The entry read is the
root-level doc.kml, else the first .kml entry in archive order; further .kml entries are
reported, not read. Decompression is bounded while streaming (zip bomb guard).
"""

from __future__ import annotations

import hashlib
import logging
import lzma
import os
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast

from geosnap.extraction.extractor import ExtractionProgress
from geosnap.extraction.kml_reader import KmlFormatError, extract_kml_stream
from geosnap.extraction.models import (
    ExtractionCounters,
    KmzEntryReport,
    RejectedLine,
    SourceExtractionOutcome,
)
from geosnap.extraction.source_reader import SourceReadReport
from geosnap.settings import ExtractionSettings

logger = logging.getLogger(__name__)

ZIP_LOCAL_HEADER_MAGIC = b"PK\x03\x04"
ROOT_KML_ENTRY_NAME = "doc.kml"
ENCRYPTED_ENTRY_FLAG = 0x1
MAX_DECOMPRESSED_BYTES = 4 * 1024**3
MAX_COMPRESSION_RATIO = 200
# The ratio is judged only after this much output: small, repetitive KML compresses well.
RATIO_CHECK_FROM_BYTES = 64 * 1024**2
ENCODING_BEFORE_KML_READ = "unknown"
# What zipfile and the decompressors raise for damaged or crafted data.
# OSError included: bz2 reports damaged data with it, zipfile a seek to a false offset.
UNREADABLE_DIRECTORY_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    struct.error,
    ValueError,
    NotImplementedError,
    OSError,
)
DAMAGED_ENTRY_ERRORS = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError)
MAX_OTHER_ENTRIES_NAMED = 20


class KmzFormatError(KmlFormatError):
    """The archive is refused: no ZIP, no or an encrypted KML entry, damaged, a zip bomb,
    or its KML entry is not acceptable KML."""


# The KMZ reader's result is the common reader outcome with kmz_entry set.
KmzOutcome = SourceExtractionOutcome


def is_zip_archive(path: Path) -> bool:
    """True when the file starts with the ZIP local file header magic."""
    with path.open("rb") as source_file:
        return source_file.read(len(ZIP_LOCAL_HEADER_MAGIC)) == ZIP_LOCAL_HEADER_MAGIC


def _declared_size(entry: zipfile.ZipInfo) -> int:
    return entry.file_size


class _GuardedEntryStream:
    """read() of one archive entry with the zip bomb limits and archive damage turned
    into KmzFormatError, so the KML reader attaches its partial outcome."""

    def __init__(
        self, entry_stream: BinaryIO, entry: zipfile.ZipInfo, archive_size_bytes: int
    ) -> None:
        self._entry_stream = entry_stream
        self._entry_name = entry.filename
        # No entry is larger than its archive: a falsified compress_size in the header
        # cannot raise the allowance of the ratio guard.
        self._compressed_bytes = max(min(entry.compress_size, archive_size_bytes), 1)
        self.bytes_produced = 0
        self.read_completely = False

    def read(self, size: int = -1) -> bytes:
        try:
            chunk = self._entry_stream.read(size)
        except DAMAGED_ENTRY_ERRORS as error:
            raise KmzFormatError(
                f"damaged archive: entry {self._entry_name!r} cannot be decompressed ({error})"
            ) from error
        if not chunk:
            # zipfile checks the CRC-32 when the entry ends: only now is the read complete.
            self.read_completely = True
            return chunk
        self.bytes_produced += len(chunk)
        if self.bytes_produced > MAX_DECOMPRESSED_BYTES:
            raise KmzFormatError(
                f"entry {self._entry_name!r} exceeds the decompressed size limit of "
                f"{MAX_DECOMPRESSED_BYTES} bytes"
            )
        if (
            self.bytes_produced > RATIO_CHECK_FROM_BYTES
            and self.bytes_produced > MAX_COMPRESSION_RATIO * self._compressed_bytes
        ):
            raise KmzFormatError(
                f"entry {self._entry_name!r} exceeds the compression ratio limit of "
                f"{MAX_COMPRESSION_RATIO}:1 (zip bomb guard)"
            )
        return chunk


def _choose_kml_entry(
    entries: list[zipfile.ZipInfo],
) -> tuple[zipfile.ZipInfo | None, tuple[str, ...]]:
    """The entry to read (root doc.kml, else the first .kml) and the other .kml names."""
    kml_entries = [
        entry for entry in entries if not entry.is_dir() and entry.filename.lower().endswith(".kml")
    ]
    chosen = next(
        (entry for entry in kml_entries if entry.filename == ROOT_KML_ENTRY_NAME),
        kml_entries[0] if kml_entries else None,
    )
    others = tuple(entry.filename for entry in kml_entries if entry is not chosen)
    return chosen, others


def _names_text(named_entries: tuple[str, ...], entry_count: int) -> str:
    """The quoted names, and how many more there are when not all are named."""
    names = ", ".join(repr(name) for name in named_entries)
    unnamed = entry_count - len(named_entries)
    return f"{names} and {unnamed} more" if unnamed else names


def extract_kmz(
    source_path: Path,
    settings: ExtractionSettings,
    on_rejected: Callable[[RejectedLine], None],
    on_progress: Callable[[ExtractionProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    source_id: int = 1,
) -> KmzOutcome:
    """Hash the archive, then stream its KML entry through the KML reader.

    The read report carries the archive's SHA-256 and size; kmz_entry names the entry and
    the SHA-256 of its decompressed bytes. Progress counts decompressed bytes against the
    entry's declared size. Raises KmzFormatError with the partial outcome (the archive
    hash included) for every refusal; all KML rules apply to the entry unchanged.
    """
    with source_path.open("rb") as archive_file:
        archive_size = os.fstat(archive_file.fileno()).st_size
        archive_digest = hashlib.sha256()
        while chunk := archive_file.read(settings.read_chunk_bytes):
            archive_digest.update(chunk)
        archive_sha256 = archive_digest.hexdigest()
        logger.info(
            "KMZ archive %s: %d bytes, sha256=%s", source_path.name, archive_size, archive_sha256
        )

        def with_archive_report(
            kml_outcome: SourceExtractionOutcome | None, kmz_entry: KmzEntryReport | None
        ) -> KmzOutcome:
            """The KML outcome with the archive as the hashed source."""
            archive_report = SourceReadReport(
                sha256_hex=archive_sha256,
                size_bytes=archive_size,
                bytes_read=archive_size,
                encoding=(
                    ENCODING_BEFORE_KML_READ
                    if kml_outcome is None
                    else kml_outcome.read_report.encoding
                ),
                line_count=0 if kml_outcome is None else kml_outcome.read_report.line_count,
                decode_replacements=0,
            )
            if kml_outcome is None:
                return KmzOutcome([], [], ExtractionCounters(), archive_report, False, kmz_entry)
            return replace(kml_outcome, read_report=archive_report, kmz_entry=kmz_entry)

        def refuse(reason: str) -> KmzFormatError:
            return KmzFormatError(f"{source_path.name}: {reason}", with_archive_report(None, None))

        archive_file.seek(0)
        if archive_file.read(len(ZIP_LOCAL_HEADER_MAGIC)) != ZIP_LOCAL_HEADER_MAGIC:
            raise refuse("not a ZIP archive (KMZ files are ZIP archives)")
        try:
            archive = zipfile.ZipFile(archive_file)
        except UNREADABLE_DIRECTORY_ERRORS as error:
            raise refuse(
                f"damaged archive: not a ZIP archive that can be read ({error})"
            ) from error
        with archive:
            entry, other_kml_entries = _choose_kml_entry(archive.infolist())
            if entry is None:
                raise refuse("no .kml entry in the archive")
            if other_kml_entries:
                logger.warning(
                    "KMZ archive %s: reading %r only; further .kml entries are not read: %s",
                    source_path.name,
                    entry.filename,
                    _names_text(
                        other_kml_entries[:MAX_OTHER_ENTRIES_NAMED], len(other_kml_entries)
                    ),
                )
            if entry.flag_bits & ENCRYPTED_ENTRY_FLAG:
                raise refuse(f"entry {entry.filename!r} is encrypted")
            if _declared_size(entry) > MAX_DECOMPRESSED_BYTES:
                raise refuse(
                    f"entry {entry.filename!r} exceeds the decompressed size limit of "
                    f"{MAX_DECOMPRESSED_BYTES} bytes"
                )
            try:
                entry_stream = archive.open(entry)
            except UNREADABLE_DIRECTORY_ERRORS as error:
                raise refuse(
                    f"damaged archive: entry {entry.filename!r} cannot be opened ({error})"
                ) from error
            with entry_stream:
                guarded_stream = _GuardedEntryStream(
                    cast(BinaryIO, entry_stream), entry, archive_size
                )

                def entry_report(kml_outcome: SourceExtractionOutcome | None) -> KmzEntryReport:
                    return KmzEntryReport(
                        name=entry.filename,
                        sha256_hex=(
                            hashlib.sha256(b"").hexdigest()
                            if kml_outcome is None
                            else kml_outcome.read_report.sha256_hex
                        ),
                        bytes_read=guarded_stream.bytes_produced,
                        read_completely=guarded_stream.read_completely,
                        other_kml_entries=other_kml_entries[:MAX_OTHER_ENTRIES_NAMED],
                        other_kml_entry_count=len(other_kml_entries),
                    )

                try:
                    kml_outcome = extract_kml_stream(
                        cast(BinaryIO, guarded_stream),
                        # repr keeps control characters of the entry name out of log lines.
                        f"{source_path.name}!{entry.filename!r}",
                        _declared_size(entry),
                        settings,
                        on_rejected,
                        on_progress,
                        is_cancelled,
                        source_id,
                    )
                except KmlFormatError as error:
                    raise KmzFormatError(
                        f"{source_path.name}: {error}",
                        with_archive_report(
                            error.partial_outcome, entry_report(error.partial_outcome)
                        ),
                    ) from error
    return with_archive_report(kml_outcome, entry_report(kml_outcome))
