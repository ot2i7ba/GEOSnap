# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Loopback HTTP server that gives a project's map an http:// origin in the browser.

A map opened from disk sends no Referer, and overpass-api.de refuses such browser
requests (HTTP 406); served from 127.0.0.1 the browser sends its origin as Referer.
Each server delivers one map file under a random, unguessable path and nothing else, so
other local users cannot read the project's logs or exports. The two write routes,
POST /<token>/records/search-area (a search area from the map's crystal ball) and
POST /<token>/records/speed-range (a range of the Speed tool, recomputed from the served
map), record in the project directory; they need the same token, a loopback Host, the
server's own Origin and a JSON body of at most 2 MB.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from geosnap.project.search_area import (
    MAX_RECORD_BYTES,
    RecordedSearchArea,
    SearchAreaChainError,
    SearchAreaError,
    read_case_details,
    record_search_area,
)
from geosnap.project.speed_range_record import (
    RecordedSpeedRange,
    SpeedRangeRecordError,
    record_speed_range,
)

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
MAP_CONTENT_TYPE = "text/html; charset=utf-8"
JSON_CONTENT_TYPE = "application/json"
SEARCH_AREA_ROUTE_SUFFIX = "records/search-area"
SPEED_RANGE_ROUTE_SUFFIX = "records/speed-range"


class MapHTTPServer(ThreadingHTTPServer):
    """Serves one pre-resolved map file at one secret route."""

    # On Windows SO_REUSEADDR would let another process of the same user bind our port.
    allow_reuse_address = False

    def __init__(self, map_path: Path, map_route: str) -> None:
        super().__init__((LOOPBACK_HOST, 0), MapRequestHandler)
        self.map_path = map_path
        self.map_route = map_route
        self.secret_token = map_route.split("/")[1]
        self.search_area_route = f"/{self.secret_token}/{SEARCH_AREA_ROUTE_SUFFIX}"
        self.speed_range_route = f"/{self.secret_token}/{SPEED_RANGE_ROUTE_SUFFIX}"
        port = self.server_address[1]
        # Anything else in Host points to DNS rebinding.
        self.allowed_hosts = frozenset({f"{LOOPBACK_HOST}:{port}", f"localhost:{port}"})
        # Only the served page itself may write: a foreign page's request carries its Origin.
        self.allowed_origins = frozenset(f"http://{host}" for host in self.allowed_hosts)


class MapRequestHandler(BaseHTTPRequestHandler):
    """GET/HEAD of the one map route and POST of the two record routes; other paths 404,
    foreign Host headers 400."""

    server: MapHTTPServer
    timeout = 15

    def do_GET(self) -> None:  # noqa: N802
        self._send_map(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_map(include_body=False)

    def _send_map(self, include_body: bool) -> None:
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if urlsplit(self.path).path != self.server.map_route:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            map_bytes = self.server.map_path.read_bytes()
        except OSError as error:
            logger.warning("Map server cannot read %s: %s", self.server.map_path, error)
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MAP_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(map_bytes)))
        self.end_headers()
        if include_body:
            self.wfile.write(map_bytes)

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "wrong Host header"})
            return
        route = urlsplit(self.path).path
        if route not in (self.server.search_area_route, self.server.speed_range_route):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get("Origin") not in self.server.allowed_origins:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "request from a foreign origin"})
            return
        media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media_type != JSON_CONTENT_TYPE:
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be JSON"}
            )
            return
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length missing"})
            return
        if not (length_text.isascii() and length_text.isdigit()):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        length = int(length_text)
        if length > MAX_RECORD_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "the record exceeds 2 MB"}
            )
            return
        try:
            body = self.rfile.read(length)
        except OSError as error:  # includes the socket timeout
            logger.warning("Map server: record body not received: %s", error)
            self.close_connection = True
            return
        try:
            document = json.loads(body.decode("utf-8"), parse_constant=_refuse_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "the body is not valid JSON"})
            return
        project_directory = self.server.map_path.parent
        kind = "search area" if route == self.server.search_area_route else "speed range"
        recorded: RecordedSearchArea | RecordedSpeedRange
        try:
            case = read_case_details(project_directory)
            if route == self.server.search_area_route:
                recorded = record_search_area(project_directory, document, case)
            else:
                recorded = record_speed_range(
                    project_directory, self.server.map_path, document, case
                )
        except (SearchAreaError, SpeedRangeRecordError) as error:
            logger.warning("Map server: %s refused: %s", kind, error)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except Exception as error:  # never a traceback without an answer
            logger.error("Map server: %s not recorded: %s", kind, error, exc_info=True)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"the {kind} could not be written: {_short_reason(error)}"},
            )
            return
        # The browser shows last_line_sha256 for the case file: records.jsonl itself cannot
        # reveal that its last lines were cut off (search_area logs the same value).
        self._send_json(
            HTTPStatus.OK,
            {
                "record_number": recorded.record_number,
                "files": recorded.files,
                "last_line_sha256": recorded.last_line_sha256,
            },
        )

    def _send_json(self, status: HTTPStatus, answer: dict[str, object]) -> None:
        body = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{JSON_CONTENT_TYPE}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # path is unset when the request line itself was rejected (e.g. 414).
        logger.info(
            "Map server %s %r -> %s",
            getattr(self, "command", "-") or "-",
            self._without_secret(str(getattr(self, "path", "-"))),
            code,
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug(
            "Map server %s: %r", self.address_string(), self._without_secret(format % args)
        )

    def _without_secret(self, text: str) -> str:
        """The token is the only protection of the address: keep it out of every log."""
        server = self.server
        token = server.secret_token if isinstance(server, MapHTTPServer) else ""
        return text.replace(token, "<secret>") if token else text


def _short_reason(error: Exception) -> str:
    """A reason the browser may show: never a traceback or a path (OSError.filename)."""
    if isinstance(error, SearchAreaChainError):
        return str(error)
    return (error.strerror if isinstance(error, OSError) else None) or type(error).__name__


def _refuse_constant(name: str) -> object:
    """json.loads accepts NaN and Infinity; a record must not."""
    raise ValueError(f"{name} is not valid JSON")


class MapServer:
    """One running loopback server for one map file."""

    def __init__(self, map_path: Path) -> None:
        self.map_path = map_path.resolve(strict=True)
        route = f"/{secrets.token_urlsafe(24)}/{quote(self.map_path.name)}"
        self._http_server = MapHTTPServer(self.map_path, route)
        self.port: int = self._http_server.server_address[1]
        self.url = f"http://{LOOPBACK_HOST}:{self.port}{route}"
        self.url_for_log = self.url.replace(self._http_server.secret_token, "<secret>")
        self._thread = threading.Thread(
            target=self._http_server.serve_forever, name=f"map-server-{self.port}", daemon=True
        )
        self._thread.start()
        logger.info("Map server for %s listening on %s:%s", self.map_path, LOOPBACK_HOST, self.port)

    def stop(self) -> None:
        self._http_server.shutdown()
        self._http_server.server_close()
        self._thread.join(timeout=5)
        logger.info("Map server for %s on port %s stopped", self.map_path, self.port)


def serve_map_file(map_path: Path) -> MapServer:
    """Serve one map file on 127.0.0.1 (OS-assigned port) under a secret address."""
    return MapServer(map_path)


class MapServerPool:
    """One server per map file, reused while the application runs."""

    def __init__(self) -> None:
        self._servers: dict[Path, MapServer] = {}

    def serve(self, map_path: Path) -> MapServer:
        key = map_path.resolve(strict=True)
        server = self._servers.get(key)
        if server is None:
            server = serve_map_file(key)
            self._servers[key] = server
        return server

    def stop_all(self) -> None:
        for server in self._servers.values():
            server.stop()
        self._servers.clear()
