# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Process start: command line, configuration, logging, then the terminal application."""

from __future__ import annotations

import argparse
import io
import logging
import sys
from collections.abc import Sequence
from logging.handlers import MemoryHandler
from pathlib import Path

from geosnap import (
    APP_NAME,
    APPLICATION_LOG_NAME,
    CONFIG_FILE_NAME,
    INPUT_DIR_NAME,
    OUTPUT_DIR_NAME,
    __version__,
)
from geosnap.audit_log import configure_application_log
from geosnap.project.verify import EXIT_USAGE, run_verify
from geosnap.runtime_paths import app_root, bundled_resource
from geosnap.settings import SettingsError, ensure_config_file, load_settings
from geosnap.tui.app import GeoSnapApp

logger = logging.getLogger(__name__)


def ensure_input_directory(root: Path) -> Path:
    """Create the input/ directory below the application root when it is missing.

    A directory with the same name in other letter case (such as ``Input``)
    is renamed, so its files stay available; on Windows this only changes the case.
    """
    input_dir = root / INPUT_DIR_NAME
    entry_names = {entry.name for entry in root.iterdir()} if root.is_dir() else set()
    if INPUT_DIR_NAME not in entry_names:
        for name in sorted(entry_names):
            candidate = root / name
            if name.lower() == INPUT_DIR_NAME and candidate.is_dir():
                candidate.rename(input_dir)
                logger.info("Renamed the input directory %s to %s", candidate, input_dir)
                break
    input_dir.mkdir(exist_ok=True)
    return input_dir


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_NAME}: without arguments the terminal application starts.",
    )
    parser.add_argument(
        "--verify",
        metavar="PROJECT_DIR",
        help="check a project directory against its MANIFEST.sha256, the search-area hash "
        "chain and the recorded source files; exit 0 match, 1 deviation, 2 call error",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
    except SystemExit as parser_exit:  # --version, --help or a usage error
        return parser_exit.code if isinstance(parser_exit.code, int) else EXIT_USAGE
    if arguments.verify is not None:
        # Paths may hold characters the console code page lacks; never fail on printing.
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(errors="backslashreplace")
        return run_verify(arguments.verify, sys.stdout)
    return run_application()


def run_application() -> int:
    root = app_root()
    config_path = root / CONFIG_FILE_NAME
    # The log level comes from config.toml, so the log opens after it is read; the loader's
    # warnings (legacy values, implicit defaults) are held back until then.
    early_records = MemoryHandler(capacity=1000, flushLevel=logging.CRITICAL + 1)
    logging.getLogger().addHandler(early_records)
    try:
        try:
            ensure_config_file(config_path, bundled_resource(CONFIG_FILE_NAME))
            settings = load_settings(config_path)
        except (SettingsError, OSError) as error:
            print(f"{APP_NAME} cannot start: {error}", file=sys.stderr)
            return 2

        try:
            application_log = configure_application_log(
                root / OUTPUT_DIR_NAME / APPLICATION_LOG_NAME, settings.logging
            )
        except OSError as error:
            print(f"{APP_NAME} cannot open its log file: {error}", file=sys.stderr)
            return 2
        early_records.setTarget(application_log)
        early_records.flush()
    finally:
        logging.getLogger().removeHandler(early_records)
        early_records.close()

    try:
        input_dir = ensure_input_directory(root)
    except OSError as error:
        print(f"{APP_NAME} cannot create its {INPUT_DIR_NAME} directory: {error}", file=sys.stderr)
        return 2

    logger.info("%s %s starting; application root %s", APP_NAME, __version__, root)
    logger.info("Input files are read from %s", input_dir)
    try:
        GeoSnapApp(settings, root).run()
    except Exception:
        logger.exception("Unhandled error; application terminated")
        print(
            f"{APP_NAME} stopped because of an unexpected error; "
            f"see {OUTPUT_DIR_NAME}/{APPLICATION_LOG_NAME}",
            file=sys.stderr,
        )
        return 1
    logger.info("%s exited normally", APP_NAME)
    return 0
