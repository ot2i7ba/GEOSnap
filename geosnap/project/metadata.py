# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Hashing of output files and the metadata.json record of one project run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_of_file(path: Path, chunk_bytes: int = 1_048_576) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metadata(path: Path, document: dict[str, object]) -> None:
    """Persist the run record as human-readable, key-sorted, strictly valid JSON."""
    serialized = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    path.write_text(serialized + "\n", encoding="utf-8")
