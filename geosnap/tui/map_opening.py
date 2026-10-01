# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Serve a project's map on the loopback server and open it in the browser.

Shared by the summary screen (the run just finished) and the projects screen (a finished
project reopened later), so both show the same texts and apply the same browser guard.
"""

from __future__ import annotations

import logging
import os
import sys
import webbrowser
from pathlib import Path

from geosnap.project.map_server import MapServerPool

logger = logging.getLogger(__name__)


def graphical_browser_possible() -> bool:
    """False on Linux/BSD without a display: webbrowser would fall back to a text browser
    (lynx, w3m) that takes over the terminal the TUI is running in."""
    if sys.platform.startswith(("win", "darwin")):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def serve_and_open_map(
    map_servers: MapServerPool, map_html_path: Path, local_tiles_used: bool
) -> str:
    """Serve the map file on 127.0.0.1 under a secret address and open it from there.

    Returns the status line for the screen; problems are logged and named in it.
    """
    try:
        server = map_servers.serve(map_html_path)
    except (OSError, ValueError) as error:
        logger.warning("Cannot start the local map server for %s: %s", map_html_path, error)
        return f"Cannot start the local map server: {error}"
    map_url = server.url
    logger.info("Opening the map %s in the browser", server.url_for_log)
    served_line = (
        f"Map served at {map_url} (a secret address, reachable only from this computer "
        "while GEOSnap runs). Search areas and speed ranges are recorded only through this "
        "address. After a restart, press P on the file list to serve the map again."
    )
    if local_tiles_used:
        served_line += " Local tiles are not served: open the file from disk to see them."
    try:
        if graphical_browser_possible():
            browser_started = webbrowser.open(map_url)
            browser_problem = "" if browser_started else " (none found)"
        else:
            browser_started = False
            browser_problem = " (no graphical session)"
    except webbrowser.Error as error:
        browser_started = False
        browser_problem = f" ({error})"
    if browser_started:
        return served_line
    logger.warning("No browser could be started for %s%s", server.url_for_log, browser_problem)
    return f"{served_line} No browser could be started{browser_problem}: open the address yourself."
