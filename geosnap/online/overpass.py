# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Places of interest around the analysed positions, from the Overpass API."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from geosnap.analysis.geometry import haversine_distance_m
from geosnap.analysis.models import AnalysisReport
from geosnap.online.http_client import OnlineClient, OnlineResponse
from geosnap.online.place_catalogue import (
    PLACE_CATALOGUE,
    category_by_key,
    max_radius_m,
    selectors_for,
)

logger = logging.getLogger(__name__)

ItemT = TypeVar("ItemT")

OUTPUT_LIMIT = 2000
# A hit whose centre lies further than this many radii from every anchor is a large feature
# (forest, river, ...) whose centre falls outside the search area.
FAR_CENTRE_RADIUS_FACTOR = 3


@dataclass(frozen=True, slots=True)
class Anchor:
    latitude: float
    longitude: float
    label: str


@dataclass(frozen=True, slots=True)
class Place:
    category: str
    name: str | None
    latitude: float
    longitude: float
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    far_centre: bool = False


@dataclass(frozen=True, slots=True)
class FarCentreContext:
    """The anchors and radius a parsed place is measured against for ``far_centre``."""

    anchors: tuple[Anchor, ...]
    radius_m: int


@dataclass(frozen=True, slots=True)
class OverpassAnswer:
    """The parsed body of one Overpass response.

    ``truncated`` is set when the element count reached ``OUTPUT_LIMIT``: ``out center N``
    cuts the result silently, without a remark.
    """

    places: dict[str, list[Place]]
    remark: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class OverpassResult:
    query: str
    response: OnlineResponse
    places: dict[str, list[Place]]
    remark: str | None = None
    skipped_categories: tuple[str, ...] = ()
    truncated: bool = False


class OverpassResponseError(ValueError):
    """An answer arrived but could not be parsed; it carries the body so it can still be kept."""

    def __init__(self, message: str, response: OnlineResponse) -> None:
        super().__init__(message)
        self.response = response


def interleave_round_robin(groups: Sequence[Sequence[ItemT]]) -> list[ItemT]:
    """One item from each group in turn until every group is exhausted."""
    interleaved: list[ItemT] = []
    for position in range(max((len(group) for group in groups), default=0)):
        interleaved.extend(group[position] for group in groups if position < len(group))
    return interleaved


def _anchor_candidates(report: AnalysisReport, label_suffix: str) -> list[Anchor]:
    """Last known position first, then stays by duration."""
    candidates: list[Anchor] = []
    if report.last is not None:
        candidates.append(
            Anchor(
                report.last.latitude, report.last.longitude, f"Last known position{label_suffix}"
            )
        )
    for stay in sorted(report.stays, key=lambda stay: stay.duration_minutes, reverse=True):
        candidates.append(
            Anchor(stay.latitude, stay.longitude, f"Stay {stay.identifier}{label_suffix}")
        )
    return candidates


def _merge_close_anchors(
    candidates: Sequence[Anchor], radius_m: int, max_anchors: int
) -> list[Anchor]:
    """Keep candidates in order; one closer than radius/2 to a kept anchor is dropped."""
    chosen: list[Anchor] = []
    for candidate in candidates:
        if len(chosen) >= max_anchors:
            break
        if any(
            haversine_distance_m(
                candidate.latitude, candidate.longitude, kept.latitude, kept.longitude
            )
            < radius_m / 2
            for kept in chosen
        ):
            continue
        chosen.append(candidate)
    return chosen


def choose_anchors_for_sources(
    labelled_reports: Sequence[tuple[str, AnalysisReport]],
    radius_m: int,
    max_anchors: int = 10,
) -> list[Anchor]:
    """Anchors of every source taken in turn, so each source gets its share of the cap.

    Per source: last known position first, then stays by duration; an anchor closer than
    radius/2 to a kept one merges into it. Labels name the source only when there is
    more than one.
    """
    multiple_sources = len(labelled_reports) > 1
    candidate_groups = [
        _anchor_candidates(report, f" · {label}" if multiple_sources else "")
        for label, report in labelled_reports
    ]
    return _merge_close_anchors(interleave_round_robin(candidate_groups), radius_m, max_anchors)


def categories_within_radius(
    categories: Sequence[str], radius_m: int
) -> tuple[list[str], list[str]]:
    """Split the requested categories into (queried, skipped): a category is skipped when
    the radius exceeds the maximum its density class allows."""
    queried: list[str] = []
    skipped: list[str] = []
    for category in categories:
        if max_radius_m(category_by_key(category)) < radius_m:
            skipped.append(category)
        else:
            queried.append(category)
    return queried, skipped


def build_overpass_query(
    anchors: Sequence[Anchor], radius_m: int, categories: Sequence[str], timeout_seconds: int
) -> str:
    """Every selector of every category around every anchor; the caller has already applied
    ``categories_within_radius``."""
    lines = [f"[out:json][timeout:{timeout_seconds}];", "("]
    for anchor in anchors:
        around = f"(around:{radius_m},{anchor.latitude},{anchor.longitude})"
        for category in categories:
            for selector in selectors_for(category_by_key(category)):
                lines.append(f"  nwr{selector}{around};")
    lines.append(");")
    lines.append(f"out center {OUTPUT_LIMIT};")
    return "\n".join(lines)


def classify_place(tags: dict[str, str], categories: Sequence[str]) -> str | None:
    """First matching category in canonical catalogue order, restricted to the requested set."""
    requested = set(categories)
    for category in PLACE_CATALOGUE:
        if category.key not in requested:
            continue
        for key, allowed in category.rules:
            if tags.get(key) in allowed:
                return category.key
    return None


def _is_far_from_every_anchor(
    latitude: float, longitude: float, far_centre_context: FarCentreContext | None
) -> bool:
    if far_centre_context is None or not far_centre_context.anchors:
        return False
    nearest_m = min(
        haversine_distance_m(latitude, longitude, anchor.latitude, anchor.longitude)
        for anchor in far_centre_context.anchors
    )
    return nearest_m > FAR_CENTRE_RADIUS_FACTOR * far_centre_context.radius_m


def parse_places(
    body: bytes,
    categories: Sequence[str],
    far_centre_context: FarCentreContext | None = None,
) -> OverpassAnswer:
    """Group response elements by category; elements without a position are skipped.

    With a ``far_centre_context``, every place is marked ``far_centre`` when its centre lies
    further than ``FAR_CENTRE_RADIUS_FACTOR`` radii from the nearest anchor.
    """
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Overpass response is not JSON: {error}") from error
    except RecursionError as error:
        raise ValueError("Overpass response is not JSON: nested too deeply") from error
    elements = document.get("elements") if isinstance(document, dict) else None
    if not isinstance(elements, list):
        raise ValueError("Overpass response has no 'elements' list")
    remark = document.get("remark")
    if not isinstance(remark, str) or not remark.strip():
        remark = None
    places: dict[str, list[Place]] = {}
    malformed_count = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        string_tags = {str(key): str(value) for key, value in tags.items()}
        category = classify_place(string_tags, categories)
        if category is None:
            continue
        position = element if "lat" in element else element.get("center")
        if not isinstance(position, dict) or "lat" not in position or "lon" not in position:
            continue
        try:
            latitude = float(position["lat"])
            longitude = float(position["lon"])
            if not (math.isfinite(latitude) and math.isfinite(longitude)):
                raise ValueError("non-finite coordinate")
            place = Place(
                category=category,
                name=string_tags.get("name"),
                latitude=latitude,
                longitude=longitude,
                osm_type=str(element.get("type", "unknown")),
                osm_id=int(element.get("id", 0)),
                tags=string_tags,
                far_centre=_is_far_from_every_anchor(latitude, longitude, far_centre_context),
            )
        except (TypeError, ValueError, OverflowError):  # OverflowError: 1e400-sized numbers
            malformed_count += 1
            continue
        places.setdefault(category, []).append(place)
    if malformed_count:
        logger.warning("Overpass response: %d malformed element(s) skipped", malformed_count)
    return OverpassAnswer(places=places, remark=remark, truncated=len(elements) >= OUTPUT_LIMIT)


def fetch_places(
    client: OnlineClient,
    endpoint: str,
    anchors: Sequence[Anchor],
    radius_m: int,
    categories: Sequence[str],
    timeout_seconds: int,
) -> OverpassResult:
    """One request for the categories the radius allows.

    Raises ValueError before any request when the density rule skips every category.
    """
    queried, skipped = categories_within_radius(categories, radius_m)
    if not queried:
        raise ValueError("no category can be searched at this radius")
    query = build_overpass_query(anchors, radius_m, queried, timeout_seconds)
    logger.info(
        "Overpass query: %d anchor(s), radius %d m, categories %s",
        len(anchors),
        radius_m,
        ",".join(queried),
    )
    logger.info("Overpass QL:\n%s", query)
    response = client.post_form(endpoint, {"data": query}, timeout_seconds + 10)
    try:
        answer = parse_places(response.body, queried, FarCentreContext(tuple(anchors), radius_m))
    except ValueError as error:
        raise OverpassResponseError(str(error), response) from error
    places = answer.places
    logger.info(
        "Overpass returned %d place(s): %s",
        sum(len(entries) for entries in places.values()),
        ", ".join(f"{category}={len(entries)}" for category, entries in sorted(places.items())),
    )
    return OverpassResult(
        query=query,
        response=response,
        places=places,
        remark=answer.remark,
        skipped_categories=tuple(skipped),
        truncated=answer.truncated,
    )
