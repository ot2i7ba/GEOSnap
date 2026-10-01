# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Stream a text file line by line with constant memory while hashing its raw bytes."""

from __future__ import annotations

import codecs
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"
REPLACEMENT_CHARACTER = "�"
BOM_SNIFF_BYTES = 4


@dataclass(frozen=True, slots=True)
class SourceLine:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class SourceReadReport:
    sha256_hex: str
    size_bytes: int
    bytes_read: int
    encoding: str
    line_count: int
    decode_replacements: int


def detect_encoding(sample: bytes, fallback_encoding: str) -> str:
    """Pick the codec for a file from its first chunk.

    Byte-order marks win; otherwise strict UTF-8 is probed and the configured
    fallback is used when the probe fails. Incomplete trailing multibyte
    sequences at the chunk boundary are not treated as errors.
    """
    if sample.startswith(UTF8_BOM):
        return "utf-8-sig"
    if sample.startswith((UTF16_LE_BOM, UTF16_BE_BOM)):
        return "utf-16"
    probe = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        probe.decode(sample, final=False)
    except UnicodeDecodeError:
        return fallback_encoding
    return "utf-8"


class SourceReader:
    """Reads a file in fixed-size chunks, yielding numbered lines and a SHA-256 digest."""

    def __init__(
        self,
        path: Path,
        chunk_bytes: int,
        fallback_encoding: str,
        encoding: str | None = None,
    ) -> None:
        """``encoding`` names the codec to use instead of detecting one from the first
        chunk (the CSV reader passes what its preview decided and showed)."""
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        self.path = path
        self.chunk_bytes = chunk_bytes
        self.fallback_encoding = fallback_encoding
        self.chosen_encoding = encoding
        self.size_bytes = path.stat().st_size
        self.bytes_read = 0
        self.line_count = 0
        self.decode_replacements = 0
        self.encoding: str | None = None
        self._digest = hashlib.sha256()
        self._pending = ""

    def lines(self) -> Iterator[SourceLine]:
        """Yield every line without its line ending; \\n, \\r\\n and \\r all terminate a line."""
        with self.path.open("rb") as source_file:
            chunk = source_file.read(max(self.chunk_bytes, BOM_SNIFF_BYTES))
            self.encoding = self.chosen_encoding or detect_encoding(chunk, self.fallback_encoding)
            logger.info("Reading %s as %s", self.path.name, self.encoding)
            decoder = codecs.getincrementaldecoder(self.encoding)("replace")
            while chunk:
                self._digest.update(chunk)
                self.bytes_read += len(chunk)
                yield from self._emit_lines(decoder.decode(chunk, final=False), final=False)
                chunk = source_file.read(self.chunk_bytes)
            yield from self._emit_lines(decoder.decode(b"", final=True), final=True)

    def report(self) -> SourceReadReport:
        return SourceReadReport(
            sha256_hex=self._digest.hexdigest(),
            size_bytes=self.size_bytes,
            bytes_read=self.bytes_read,
            encoding=self.encoding or "unknown",
            line_count=self.line_count,
            decode_replacements=self.decode_replacements,
        )

    def _emit_lines(self, text: str, final: bool) -> Iterator[SourceLine]:
        buffer = self._pending + text
        held_back = ""
        if not final and buffer.endswith("\r"):
            # A trailing CR may be the first half of CRLF; wait for the next chunk.
            buffer, held_back = buffer[:-1], "\r"
        parts = buffer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if final:
            self._pending = ""
            if parts and parts[-1] == "":
                parts.pop()
        else:
            self._pending = parts.pop() + held_back
        for part in parts:
            self.line_count += 1
            replacements = part.count(REPLACEMENT_CHARACTER)
            if replacements:
                self.decode_replacements += replacements
                logger.warning(
                    "Line %d: %d undecodable byte(s) replaced", self.line_count, replacements
                )
            yield SourceLine(self.line_count, part)
