# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Text formatting for the terminal UI."""

from __future__ import annotations

SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_file_size(size_bytes: int) -> str:
    """Human-readable size such as '512 B', '2.0 KB' or '5.0 GB'."""
    size = float(size_bytes)
    for unit in SIZE_UNITS:
        if size < 1024 or unit == SIZE_UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {SIZE_UNITS[-1]}"
