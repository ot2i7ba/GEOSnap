# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Minimal HTTPS client on urllib with a fixed User-Agent, timeouts, size cap and logging."""

from __future__ import annotations

import hashlib
import http.client
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from geosnap import APP_NAME, __version__

logger = logging.getLogger(__name__)

Transport = Callable[[urllib.request.Request, float], bytes]
DEFAULT_MAX_RESPONSE_BYTES = 50_000_000


class OnlineServiceError(Exception):
    """Any failure talking to an online service; the pipeline logs it and carries on."""


@dataclass(frozen=True, slots=True)
class OnlineResponse:
    body: bytes
    elapsed_seconds: float
    sha256_hex: str


def default_user_agent() -> str:
    return f"{APP_NAME}/{__version__}"


class _RedirectRefusingHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the request must only ever reach the configured endpoint."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_opener = urllib.request.build_opener(_RedirectRefusingHandler)


def _urllib_transport(request: urllib.request.Request, timeout_seconds: float) -> bytes:
    # A 3xx answer surfaces as HTTPError here and becomes an OnlineServiceError in _send.
    with _opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
        return bytes(response.read(DEFAULT_MAX_RESPONSE_BYTES + 1))


class OnlineClient:
    """Sends requests through an injectable transport so tests never touch the network."""

    def __init__(
        self,
        user_agent: str,
        transport: Transport | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.user_agent = user_agent
        self._transport = transport or _urllib_transport
        self._max_response_bytes = max_response_bytes

    def post_form(self, url: str, fields: dict[str, str], timeout_seconds: float) -> OnlineResponse:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._send(request, timeout_seconds)

    def get(self, url: str, params: dict[str, str], timeout_seconds: float) -> OnlineResponse:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        return self._send(urllib.request.Request(full_url, method="GET"), timeout_seconds)

    def _send(self, request: urllib.request.Request, timeout_seconds: float) -> OnlineResponse:
        if not request.full_url.startswith("https://"):
            raise OnlineServiceError(f"Only https URLs are allowed: {request.full_url}")
        request.add_header("User-Agent", self.user_agent)
        started = time.monotonic()
        try:
            body = self._transport(request, timeout_seconds)
        except urllib.error.HTTPError as error:
            raise OnlineServiceError(
                f"{request.full_url}: HTTP {error.code} {error.reason}"
            ) from error
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
        ) as error:
            raise OnlineServiceError(f"{request.full_url}: {error}") from error
        elapsed = time.monotonic() - started
        if len(body) > self._max_response_bytes:
            raise OnlineServiceError(
                f"{request.full_url}: response larger than {self._max_response_bytes} bytes"
            )
        digest = hashlib.sha256(body).hexdigest()
        logger.info(
            "Online request %s %s -> %d bytes in %.2f s sha256=%s",
            request.get_method(),
            request.full_url,
            len(body),
            elapsed,
            digest,
        )
        return OnlineResponse(body=body, elapsed_seconds=elapsed, sha256_hex=digest)
