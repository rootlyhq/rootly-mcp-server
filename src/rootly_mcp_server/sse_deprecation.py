"""Deprecation signalling for the legacy SSE transport.

SSE is deprecated in favour of Streamable HTTP (``/mcp``) and Code Mode
(``/mcp-codemode``) and is scheduled for removal after :data:`SSE_SUNSET`.
Nothing here changes SSE behaviour; it only tells clients and operators:

* ``Deprecation`` (RFC 9745), ``Sunset`` (RFC 8594) and a ``successor-version``
  ``Link`` header on every response from the SSE stream and its message
  endpoint, including the 401 an unauthenticated probe gets;
* one structured ``sse_deprecated_connection`` warning per opened SSE stream,
  so remaining SSE users can be found in Datadog;
* a note in the MCP ``instructions`` of SSE sessions only.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs

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

# RFC 9745: Deprecation is a Structured Field Date, "@<unix seconds>".
DEPRECATION_HEADER_VALUE = f"@{int(SSE_DEPRECATED_AT.timestamp())}"
# RFC 8594: Sunset is an HTTP-date.
SUNSET_HEADER_VALUE = format_datetime(SSE_SUNSET, usegmt=True)
LINK_HEADER_VALUE = f'<{SSE_SUCCESSOR_URL}>; rel="successor-version"'

SSE_DEPRECATION_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"deprecation", DEPRECATION_HEADER_VALUE.encode()),
    (b"sunset", SUNSET_HEADER_VALUE.encode()),
    (b"link", LINK_HEADER_VALUE.encode()),
)

SSE_DEPRECATION_NOTICE = (
    "Notice: this connection uses the deprecated SSE transport, which will be "
    f"removed after {SSE_SUNSET_DATE}. Reconfigure the MCP client to use "
    f"Streamable HTTP at {SSE_SUCCESSOR_URL} (or Code Mode at "
    "https://mcp.rootly.com/mcp-codemode)."
)

CLI_SSE_DEPRECATION_WARNING = (
    "The 'sse' transport is deprecated and will be removed after "
    f"{SSE_SUNSET_DATE}. Use '--transport streamable-http' instead."
)

SSE_CONNECTION_EVENT = "sse_deprecated_connection"

_MAX_USER_AGENT_CHARS = 256

_deprecation_json_logger = json_line_logger("rootly_mcp_server.sse_deprecation_json")


def _query_param(scope: dict[str, Any], name: str) -> str:
    raw = scope.get("query_string", b"") or b""
    values = parse_qs(raw.decode("latin-1")).get(name)
    return values[0] if values else ""


def log_sse_deprecated_connection(scope: dict[str, Any]) -> None:
    """Log one structured warning for a newly opened SSE stream."""
    headers = _normalize_headers(
        {key.decode("latin-1"): value.decode("latin-1") for key, value in scope.get("headers", [])}
    )
    event: dict[str, Any] = {
        "event": SSE_CONNECTION_EVENT,
        "level": "warning",
        "message": "SSE transport is deprecated",
        "transport": "sse",
        "path": str(scope.get("path", "")),
        "sunset": SSE_SUNSET_DATE,
        "successor": SSE_SUCCESSOR_URL,
        "user_agent": headers.get("user-agent", "")[:_MAX_USER_AGENT_CHARS],
        "client_ip": _extract_client_ip(headers),
        "request_id": _extract_request_id(headers),
        "token_fingerprint": _fingerprint_auth_header(headers.get("authorization", "")),
        "tool_profile": _query_param(scope, "tool_profile")
        or headers.get("x-rootly-tool-profile", ""),
    }
    _deprecation_json_logger.warning(
        json.dumps({k: v for k, v in event.items() if v != ""}, separators=(",", ":"))
    )


class SSEDeprecationMiddleware:
    """ASGI middleware adding deprecation headers to SSE responses.

    Headers are injected on ``http.response.start`` only; body messages pass
    straight through, so the event stream is never buffered. Place it outside
    the auth middleware so the 401 sent to unauthenticated requests carries the
    headers too.
    """

    def __init__(self, app, sse_path: str | None = None, message_path: str | None = None):
        self.app = app
        if not sse_path:
            sse_path = os.getenv("FASTMCP_SSE_PATH") or "/sse"
        if not message_path:
            message_path = os.getenv("FASTMCP_MESSAGE_PATH") or "/messages"
        self._sse_path = _normalize_path(sse_path)
        self._message_path = _normalize_path(message_path)

    def _is_sse_path(self, path: str) -> bool:
        normalized = _normalize_path(path)
        return normalized == self._sse_path or (
            normalized == self._message_path or normalized.startswith(self._message_path + "/")
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self._is_sse_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        is_stream_open = (
            _normalize_path(str(scope.get("path", ""))) == self._sse_path
            and scope.get("method") == "GET"
        )
        logged = False

        async def send_with_deprecation_headers(message):
            nonlocal logged
            if message.get("type") == "http.response.start":
                existing = list(message.get("headers", []))
                existing_names = {name.lower() for name, _ in existing}
                existing.extend(
                    header for header in SSE_DEPRECATION_HEADERS if header[0] not in existing_names
                )
                message = {**message, "headers": existing}
                if is_stream_open and not logged and message.get("status") == 200:
                    logged = True
                    try:
                        log_sse_deprecated_connection(scope)
                    except Exception:  # pragma: no cover - logging must never break SSE
                        logging.getLogger(__name__).debug(
                            "Failed to log SSE deprecation event", exc_info=True
                        )
            await send(message)

        await self.app(scope, receive, send_with_deprecation_headers)


def with_sse_deprecation_instructions(init_options: Any) -> Any:
    """Return MCP initialization options whose instructions carry the SSE notice."""
    existing = getattr(init_options, "instructions", None)
    instructions = (
        f"{existing.rstrip()}\n\n{SSE_DEPRECATION_NOTICE}" if existing else SSE_DEPRECATION_NOTICE
    )
    return init_options.model_copy(update={"instructions": instructions})
