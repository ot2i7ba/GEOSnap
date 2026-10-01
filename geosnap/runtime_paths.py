# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Resolve the application root and bundled resources for source and frozen runs."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Directory holding the executable, or the dev/ directory when running from source.

    Input files, config.toml and the output/ tree all live here.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_resource(relative_path: str) -> Path:
    """Locate a read-only resource shipped with the application.

    Inside a bundle the resources are unpacked below sys._MEIPASS with the same
    relative layout as the source tree, so one relative path works in both modes.
    """
    if is_frozen():
        bundle_dir = Path(str(getattr(sys, "_MEIPASS")))  # noqa: B009
        return bundle_dir / relative_path
    return app_root() / relative_path
