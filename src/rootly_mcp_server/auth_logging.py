"""Structured log events for MCP requests rejected by the auth check."""

from __future__ import annotations

import contextvars
import json
import logging
import traceback
from typing import Any

from .utils import (
    _fingerprint_auth_header,
    _format_traceback_excerpt,
    _sanitize_log_excerpt,
    json_line_logger,
)

AUTH_REJECTED_EVENT = "mcp_auth_rejected"

_FAILURE_REASONS = frozenset(
    {"probe_exception", "validation_exception", "upstream_error", "unreadable_identity"}
)

# Why the current request's token was rejected, set during validation and read
# when the 401 is logged.
auth_rejection: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "auth_rejection", default=None
)

_auth_json_logger = json_line_logger("rootly_mcp_server.auth_json")


def exception_details(reason: str, exc: BaseException) -> dict[str, Any]:
    return {
        "reason": reason,
        "exception_type": type(exc).__name__,
        "exception_message": _sanitize_log_excerpt(exc),
        "traceback_excerpt": _format_traceback_excerpt("".join(traceback.format_exception(exc))),
    }


def _rejection_level(reason: str) -> int:
    if reason in _FAILURE_REASONS:
        return logging.WARNING
    if reason == "missing":
        # Clients send one header-less request before starting OAuth; that 401 is
        # routine, so it stays below the logger's INFO threshold.
        return logging.DEBUG
    return logging.INFO


def log_auth_rejection(
    *,
    auth_header: str,
    auth_state: str,
    path: str,
    transport: str,
    mcp_mode: str,
    client_ip: str,
    request_id: str,
    rejection: dict[str, Any] | None,
) -> None:
    """Log one structured event for a request answered with 401."""
    details = rejection or {"reason": auth_state if auth_state != "bearer" else "rejected"}
    level = _rejection_level(details["reason"])
    if not _auth_json_logger.isEnabledFor(level):
        return
    event: dict[str, Any] = {
        "event": AUTH_REJECTED_EVENT,
        "level": logging.getLevelName(level).lower(),
        "message": "MCP request rejected by auth check",
        "path": path,
        "transport": transport,
        "mcp_mode": mcp_mode,
        "auth_header_state": auth_state,
        "token_fingerprint": _fingerprint_auth_header(auth_header),
        "client_ip": client_ip,
        "request_id": request_id,
        **details,
    }
    _auth_json_logger.log(
        level,
        json.dumps({k: v for k, v in event.items() if v not in ("", None)}, separators=(",", ":")),
    )
