# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Decode a JSON document value by value from a chunked UTF-8 file.

The technique is that of the Google Records.json reader: json.JSONDecoder.raw_decode on
a sliding text buffer that holds only the unread rest plus the value being decoded, so a
document of any size needs memory only for its largest single value. Every byte is
hashed as it is read, the decoding is strict UTF-8 (a byte-order mark is skipped) and
JSON integers are bounded (reader_support.json_int).
"""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import BinaryIO

from geosnap.extraction.reader_support import json_int
from geosnap.extraction.source_reader import UTF8_BOM

JSON_WHITESPACE = " \t\r\n"


class Utf8ChunkSource:
    """Chunks of a binary file as text: strict UTF-8, hashed and counted as they are read.

    ``not_utf8`` builds the exception raised for a byte sequence that is not UTF-8; the
    message names the byte offset. ``on_chunk`` is called after every chunk (progress).
    """

    def __init__(
        self,
        source_file: BinaryIO,
        chunk_bytes: int,
        not_utf8: Callable[[str], Exception],
        on_chunk: Callable[[], None] | None = None,
    ) -> None:
        self._source_file = source_file
        self._chunk_bytes = chunk_bytes
        self._not_utf8 = not_utf8
        self._on_chunk = on_chunk
        self._decoder: codecs.IncrementalDecoder | None = None
        self.encoding = "utf-8"
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def next_text(self) -> str | None:
        """The next chunk as text; None once the file is exhausted."""
        chunk = self._source_file.read(self._chunk_bytes)
        if self._decoder is None:
            if chunk.startswith(UTF8_BOM):
                self.encoding = "utf-8-sig"
            self._decoder = codecs.getincrementaldecoder(self.encoding)("strict")
        if chunk:
            self.digest.update(chunk)
            self.bytes_read += len(chunk)
            if self._on_chunk is not None:
                self._on_chunk()
        try:
            text = self._decoder.decode(chunk, final=not chunk)
        except UnicodeDecodeError as error:
            # error.object holds the bytes still pending from earlier chunks plus this one.
            byte_offset = self.bytes_read - len(error.object) + error.start
            raise self._not_utf8(f"not UTF-8 (byte {byte_offset}): {error}") from error
        return text if chunk else None


class JsonValueStream:
    """One JSON value at a time from a Utf8ChunkSource, on a sliding buffer.

    ``malformed`` builds the exception for broken JSON; ``max_value_chars`` bounds the
    text one value may span (a larger one is refused, never buffered whole).
    ``repeated_names`` lists the member names that occurred twice in one object of the
    value read last (json itself would keep the last one silently).
    """

    def __init__(
        self, source: Utf8ChunkSource, max_value_chars: int, malformed: Callable[[str], Exception]
    ) -> None:
        self._source = source
        self._max_value_chars = max_value_chars
        self._malformed = malformed
        self._decoder = json.JSONDecoder(
            parse_int=json_int, object_pairs_hook=self._object_noting_repeated_names
        )
        self.repeated_names: list[str] = []
        self._buffer = ""
        self._position = 0
        self._exhausted = False

    def _fill(self) -> bool:
        """Append the next chunk to the unread rest; False at the end of the file."""
        if self._exhausted:
            return False
        text = self._source.next_text()
        if text is None:
            self._exhausted = True
            return False
        self._buffer = self._buffer[self._position :] + text
        self._position = 0
        return True

    def peek(self) -> str | None:
        """The next character that is not JSON whitespace; None at the end of the file."""
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position] in (
                JSON_WHITESPACE
            ):
                self._position += 1
            if self._position < len(self._buffer):
                return self._buffer[self._position]
            if not self._fill():
                return None

    def take(self, expected: str, context: str) -> None:
        """Consume the expected character, or raise malformed(...)."""
        if self.peek() != expected:
            raise self._malformed(f"expected '{expected}' {context}")
        self._position += 1

    def take_if(self, character: str) -> bool:
        """Consume the character when it is next; False otherwise."""
        if self.peek() != character:
            return False
        self._position += 1
        return True

    def read_value(self, context: str) -> object:
        """Decode the next value completely, reading further chunks as needed."""
        return self.read_value_and_text(context)[0]

    def read_value_and_text(self, context: str) -> tuple[object, str]:
        """The next value and its text exactly as written in the file (``context`` names
        it in messages: "feature 3", "top-level member 'name'")."""
        if self.peek() is None:
            raise self._malformed(f"unexpected end of file in {context}")
        while True:
            self.repeated_names = []
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except RecursionError as error:
                raise self._malformed(f"{context}: nested too deeply") from error
            except json.JSONDecodeError as error:
                if len(self._buffer) - self._position > self._max_value_chars:
                    raise self._malformed(f"{context} exceeds {self._limit_text()}") from error
                if not self._fill():
                    raise self._malformed(f"{context}: {error.msg}") from error
                continue
            if end == len(self._buffer) and self._fill():
                # A value touching the buffer end (a number) may continue in the next chunk.
                continue
            text = self._buffer[self._position : end]
            self._position = end
            if self._position > len(self._buffer) // 2:
                self._buffer, self._position = self._buffer[self._position :], 0
            return value, text

    def _object_noting_repeated_names(self, pairs: list[tuple[str, object]]) -> dict[str, object]:
        json_object = dict(pairs)
        if len(json_object) != len(pairs):
            self.repeated_names.extend(repeated_member_names(pairs))
        return json_object

    def _limit_text(self) -> str:
        if self._max_value_chars % 1_048_576 == 0:
            return f"{self._max_value_chars // 1_048_576} MiB"
        return f"{self._max_value_chars} characters"

    def read_key(self, context: str) -> str:
        """An object member's name followed by its colon."""
        key = self.read_value(context)
        if not isinstance(key, str):
            raise self._malformed(f"expected a member name in {context}")
        self.take(":", f"after the member name {key!r}")
        return key

    def at_end(self) -> bool:
        """True when only whitespace remains until the end of the file."""
        return self.peek() is None


def repeated_member_names(pairs: list[tuple[str, object]]) -> list[str]:
    """The member names of one JSON object that occur again after their first use."""
    seen: set[str] = set()
    repeated: list[str] = []
    for name, _ in pairs:
        if name in seen:
            repeated.append(name)
        seen.add(name)
    return repeated


def repeated_names_reason(names: Sequence[str]) -> str:
    """The rejection reason for repeated member names (each named once)."""
    return "member name occurs twice in one object: " + ", ".join(
        repr(name) for name in dict.fromkeys(names)
    )


def compact_json_text(text: str) -> str:
    """JSON text with the whitespace outside strings removed and nothing else changed:
    key order, repeated keys and number spellings stay as written."""
    parts: list[str] = []
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            parts.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
            parts.append(character)
        elif character not in JSON_WHITESPACE:
            parts.append(character)
    return "".join(parts)
