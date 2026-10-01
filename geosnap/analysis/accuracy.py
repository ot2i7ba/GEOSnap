# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Confidence level of accuracy radii: the factor from a reported radius to the radius the
analyses treat as the uncertainty of a position."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace

from geosnap.extraction.models import GeoPoint

# Ratio of the 95 % to the 68 % radius of a circular normal position error (Rayleigh
# distribution: P(r > R) = exp(-R^2 / 2 sigma^2), so R_p = sigma * sqrt(-2 ln(1 - p))).
RAYLEIGH_68_TO_95 = math.sqrt(math.log(0.05) / math.log(0.32))

ACCURACY_LEVEL_95 = "95"
CONFIDENCE_P95 = "p95"
CONFIDENCE_REPORTED = "reported"
# SourceDescriptor.accuracy_level -> the wording of report, map and setup.
ACCURACY_LEVEL_TEXTS = {
    "68": "68 %",
    ACCURACY_LEVEL_95: "95 %",
    "unknown": "unknown (treated as 68 %)",
}


def accuracy_scale_for(accuracy_level: str, accuracy_confidence: str) -> float:
    """1 when radii are used as reported or already are 95 % radii; otherwise the Rayleigh
    factor, so a source of unknown level counts as 68 %."""
    if accuracy_confidence == CONFIDENCE_REPORTED or accuracy_level == ACCURACY_LEVEL_95:
        return 1.0
    return RAYLEIGH_68_TO_95


def accuracy_level_text(level: object, confidence: object) -> str | None:
    """'68 %; radii scaled to 95 % for the analysis (factor 1.6215)', '95 %; radii used as
    reported', or the level alone without a known confidence; None for an unknown level."""
    level_text = ACCURACY_LEVEL_TEXTS.get(str(level))
    if level_text is None:
        return None
    if confidence == CONFIDENCE_P95 and level != ACCURACY_LEVEL_95:
        return (
            f"{level_text}; radii scaled to 95 % for the analysis (factor {RAYLEIGH_68_TO_95:.4f})"
        )
    if confidence in (CONFIDENCE_P95, CONFIDENCE_REPORTED):
        return f"{level_text}; radii used as reported"
    return level_text


def apply_accuracy_scales(points: list[GeoPoint], scale_by_source: Mapping[int, float]) -> None:
    """Give every point the scale of its source, in place: a replaced point is released at
    once, so the points are never held twice. A point whose scale is already right stays
    the same object."""
    for index, point in enumerate(points):
        scale = scale_by_source.get(point.source_id, 1.0)
        if point.accuracy_scale != scale:
            points[index] = replace(point, accuracy_scale=scale)
