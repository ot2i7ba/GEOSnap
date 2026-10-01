# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Reverse geocoding and address search through Nominatim with the mandatory
one-request-per-second pacing."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from geosnap.analysis.models import Address
from geosnap.online.http_client import OnlineClient, OnlineResponse

logger = logging.getLogger(__name__)


def parse_reverse_response(body: bytes, looked_up_utc: datetime) -> Address | None:
    """An Address, or None when Nominatim has no result for the position."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Nominatim response is not JSON: {error}") from error
    except RecursionError as error:
        raise ValueError("Nominatim response is not JSON: nested too deeply") from error
    if not isinstance(document, dict) or "display_name" not in document:
        return None
    osm_id = document.get("osm_id")
    return Address(
        display_name=str(document["display_name"]),
        osm_type=None if document.get("osm_type") is None else str(document["osm_type"]),
        osm_id=int(osm_id) if isinstance(osm_id, int) else None,
        looked_up_utc=looked_up_utc,
    )


@dataclass(frozen=True, slots=True)
class GeocodingAnswer:
    """The first Nominatim answer to an address search; not verified by anyone."""

    display_name: str
    latitude: float
    longitude: float
    osm_type: str | None
    osm_id: int | None


def parse_search_response(body: bytes) -> GeocodingAnswer | None:
    """The first answer, or None when Nominatim found nothing usable for the address."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Nominatim response is not JSON: {error}") from error
    except RecursionError as error:
        raise ValueError("Nominatim response is not JSON: nested too deeply") from error
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        return None
    first = document[0]
    try:
        latitude, longitude = float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if abs(latitude) > 90.0 or abs(longitude) > 180.0 or "display_name" not in first:
        return None
    osm_id = first.get("osm_id")
    return GeocodingAnswer(
        display_name=str(first["display_name"]),
        latitude=latitude,
        longitude=longitude,
        osm_type=None if first.get("osm_type") is None else str(first["osm_type"]),
        osm_id=osm_id if isinstance(osm_id, int) else None,
    )


class AddressLookup:
    """Paces Nominatim requests (reverse lookups and address searches share one pace) so
    the public usage policy is respected."""

    def __init__(
        self,
        client: OnlineClient,
        endpoint: str,
        search_endpoint: str = "",
        timeout_seconds: float = 15.0,
        min_interval_seconds: float = 1.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._search_endpoint = search_endpoint
        self._timeout_seconds = timeout_seconds
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._last_request_at: float | None = None
        self.lookups = 0
        # The queried position next to its answer, so a stored response stays attributable.
        self.lookups_log: list[tuple[float, float, OnlineResponse]] = []
        self.searches = 0
        self.searches_log: list[tuple[str, OnlineResponse]] = []

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            waited = self._clock() - self._last_request_at
            if waited < self._min_interval_seconds:
                self._sleep(self._min_interval_seconds - waited)
        self._last_request_at = self._clock()

    def search(self, address: str) -> GeocodingAnswer | None:
        """One forward search for a free-text address; only the first answer is used."""
        self._wait_for_turn()
        response = self._client.get(
            self._search_endpoint,
            {"q": address, "format": "jsonv2", "limit": "1"},
            self._timeout_seconds,
        )
        self.searches += 1
        self.searches_log.append((address, response))
        answer = parse_search_response(response.body)
        logger.info(
            "Nominatim search %r -> %s",
            address,
            "no result"
            if answer is None
            else f"{answer.display_name!r} ({answer.latitude}, {answer.longitude})",
        )
        return answer

    def reverse(self, latitude: float, longitude: float) -> Address | None:
        self._wait_for_turn()
        response = self._client.get(
            self._endpoint,
            {"lat": f"{latitude:.5f}", "lon": f"{longitude:.5f}", "format": "jsonv2", "zoom": "18"},
            self._timeout_seconds,
        )
        self.lookups += 1
        self.lookups_log.append((latitude, longitude, response))
        address = parse_reverse_response(response.body, self._now())
        logger.info(
            "Nominatim reverse %.5f,%.5f -> %s",
            latitude,
            longitude,
            "no result" if address is None else repr(address.display_name),
        )
        return address
