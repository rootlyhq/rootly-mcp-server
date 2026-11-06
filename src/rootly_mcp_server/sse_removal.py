"""The SSE transport was removed after its sunset date.

SSE was deprecated on 2026-10-21 and removed after :data:`SSE_SUNSET`. Clients
still configured for it keep calling ``GET /sse`` and ``POST /messages/``; a
bare 404 tells them nothing, so :class:`SSEGoneMiddleware` answers both paths
with ``410 Gone`` and a body naming the successor endpoints, before auth runs
(a removed endpoint has nothing to authenticate against).

Each answered request also logs one structured ``sse_removed_request``
warning, so the clients still pointed at SSE can be counted in Datadog and
reached.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from .transport import (
    _extract_client_ip,
    _extract_request_id,
    _normalize_headers,
    _normalize_path,
)
from .utils import _fingerprint_auth_header, json_line_logger

SSE_DEPRECATED_AT = datetime(2026, 10, 21, tzinfo=UTC)
SSE_SUNSET = datetime(2026, 11, 20, tzinfo=UTC)
SSE_SUNSET_DATE = SSE_SUNSET.date().isoformat()
SSE_SUCCESSOR_URL = "https://mcp.rootly.com/mcp"
CODE_MODE_URL = "https://mcp.rootly.com/mcp-codemode"

# Kept on the 410 so a client that reads them sees why: RFC 9745 / RFC 8594.
DEPRECATION_HEADER_VALUE = f"@{int(SSE_DEPRECATED_AT.timestamp())}"
SUNSET_HEADER_VALUE = format_datetime(SSE_SUNSET, usegmt=True)
LINK_HEADER_VALUE = f'<{SSE_SUCCESSOR_URL}>; rel="successor-version"'

SSE_REMOVED_MESSAGE = (
    f"The SSE transport was removed on {SSE_SUNSET_DATE}. Reconfigure your MCP "
    f"client to use Streamable HTTP at {SSE_SUCCESSOR_URL} (or Code Mode at "
    f"{CODE_MODE_URL}); for most clients only the URL changes, plus the "
    "transport type set to 'http' / 'streamable-http' where the config has one."
)

CLI_SSE_REMOVED_ERROR = (
    f"The 'sse' transport was removed on {SSE_SUNSET_DATE}. "
    "Use '--transport streamable-http' instead."
)

SSE_REMOVED_EVENT = "sse_removed_request"

_MAX_USER_AGENT_CHARS = 256

_removal_json_logger = json_line_logger("rootly_mcp_server.sse_removal_json")

_GONE_BODY = json.dumps(
    {
        "error": "sse_transport_removed",
        "message": SSE_REMOVED_MESSAGE,
        "successor": SSE_SUCCESSOR_URL,
        "code_mode": CODE_MODE_URL,
    },
    separators=(",", ":"),
).encode()

_GONE_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_GONE_BODY)).encode()),
    (b"cache-control", b"no-store"),
    (b"deprecation", DEPRECATION_HEADER_VALUE.encode()),
    (b"sunset", SUNSET_HEADER_VALUE.encode()),
    (b"link", LINK_HEADER_VALUE.encode()),
]


def log_sse_removed_request(scope: dict[str, Any]) -> None:
    """Log one structured warning for a request to a removed SSE path."""
    headers = _normalize_headers(
        {key.decode("latin-1"): value.decode("latin-1") for key, value in scope.get("headers", [])}
    )
    event: dict[str, Any] = {
        "event": SSE_REMOVED_EVENT,
        "level": "warning",
        "message": "Request to removed SSE transport",
        "transport": "sse",
        "method": str(scope.get("method", "")),
        "path": str(scope.get("path", "")),
        "successor": SSE_SUCCESSOR_URL,
        "user_agent": headers.get("user-agent", "")[:_MAX_USER_AGENT_CHARS],
        "client_ip": _extract_client_ip(headers),
        "request_id": _extract_request_id(headers),
        "token_fingerprint": _fingerprint_auth_header(headers.get("authorization", "")),
    }
    _removal_json_logger.warning(
        json.dumps({k: v for k, v in event.items() if v != ""}, separators=(",", ":"))
    )


class SSEGoneMiddleware:
    """ASGI middleware answering the removed SSE paths with ``410 Gone``.

    Place it outermost, ahead of the auth middleware: an unauthenticated
    client stuck on ``/sse`` should learn the endpoint is gone, not that its
    token is missing.

    ``live_paths`` are the MCP endpoints the server actually mounts. A path in
    it is never answered 410, so a self-hosted server that serves Streamable
    HTTP at ``/sse`` (``FASTMCP_STREAMABLE_HTTP_PATH=/sse``) keeps working.
    """

    def __init__(
        self,
        app,
        sse_path: str | None = None,
        message_path: str | None = None,
        live_paths: Iterable[str | None] = (),
    ):
        self.app = app
        self._sse_path = _normalize_path(sse_path or os.getenv("FASTMCP_SSE_PATH") or "/sse")
        self._message_path = _normalize_path(
            message_path or os.getenv("FASTMCP_MESSAGE_PATH") or "/messages"
        )
        self._live_paths = {_normalize_path(path) for path in live_paths if path}

    def _is_sse_path(self, path: str) -> bool:
        normalized = _normalize_path(path)
        if any(
            normalized == live or normalized.startswith(live + "/") for live in self._live_paths
        ):
            return False
        return normalized in {self._sse_path, self._message_path} or normalized.startswith(
            self._message_path + "/"
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self._is_sse_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        try:
            log_sse_removed_request(scope)
        except Exception:  # pragma: no cover - logging must never break the response
            logging.getLogger(__name__).debug("Failed to log SSE removal event", exc_info=True)
        await send({"type": "http.response.start", "status": 410, "headers": _GONE_HEADERS})
        await send({"type": "http.response.body", "body": _GONE_BODY})
