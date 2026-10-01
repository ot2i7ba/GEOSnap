# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Logging setup: a rotating application log plus a complete per-project log."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from geosnap.settings import LoggingSettings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
# Loggers of the map server threads: they write for any project whose map is open, so their
# records belong in the application log only, never in the log of the project running now.
LOGGERS_OUTSIDE_PROJECT_RUNS = frozenset(
    {"geosnap.project.map_server", "geosnap.project.search_area"}
)


class UtcIsoFormatter(logging.Formatter):
    """Formatter writing ISO 8601 timestamps in UTC with millisecond precision."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        moment = datetime.fromtimestamp(record.created, tz=UTC)
        return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def configure_application_log(log_path: Path, settings: LoggingSettings) -> logging.Handler:
    """Attach the rotating application log to the root logger and apply the log level."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(UtcIsoFormatter(LOG_FORMAT))
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.level)
    root_logger.addHandler(handler)
    return handler


class ProjectRunFilter(logging.Filter):
    """Accepts only records of the thread that runs the project, and none of the loggers
    that act for other projects (a search area recorded from another project's map)."""

    def __init__(self) -> None:
        super().__init__()
        self.run_thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.thread == self.run_thread_id and record.name not in LOGGERS_OUTSIDE_PROJECT_RUNS
        )


def attach_project_log(log_path: Path) -> logging.Handler:
    """Add a non-rotating log file for one project run; detach it when the run ends.

    Call it from the thread that runs the project: only that thread's records are kept.
    """
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(UtcIsoFormatter(LOG_FORMAT))
    handler.addFilter(ProjectRunFilter())
    logging.getLogger().addHandler(handler)
    return handler


def detach_log_handler(handler: logging.Handler) -> None:
    """Remove a handler from the root logger and close its file."""
    logging.getLogger().removeHandler(handler)
    handler.close()
