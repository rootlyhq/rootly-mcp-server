"""The caller session PostHog MCP analytics groups a hosted caller's events by.

The hosted transport is stateless, so without this posthog.mcp gives nearly
every call its own `$session_id`.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from posthog.mcp.session import derive_session_id_from_mcp_session

from .transport import _session_auth_token, get_hosted_authenticated_user

MCP_SESSION_ID_HEADER = "mcp-session-id"
# Without a transport session, a caller's calls are grouped by UTC hour. The
# hosted transport runs more than one replica with no shared store, so the
# only task boundary every replica agrees on is the clock. Two tasks by one
# caller inside an hour share a session, and a task spanning the top of the
# hour is split in two. Clients that keep a transport session (the
# `Mcp-Session-Id` that posthog_session_middleware mints) get task-level
# boundaries from the header.
_SESSION_BUCKET_FORMAT = "%Y%m%d%H"


def _caller_key(user: Mapping[str, str] | None, auth_header: str) -> str | None:
    """Identify the caller without exposing the credential.

    Prefers the authenticated Rootly user; falls back to a digest of the
    bearer token so two tokens never share a key and the token never leaves
    the process.
    """
    if user and user.get("id"):
        return f"user:{user['id']}"
    token = auth_header.strip()
    if token:
        return f"token:{hashlib.sha256(token.encode()).hexdigest()[:32]}"
    return None


def derive_session_key(
    headers: Mapping[str, str] | None,
    user: Mapping[str, str] | None,
    auth_header: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Derive the caller's session key from request state.

    The key always starts with the caller, so no two callers can share a
    session: not through a colliding or replayed ``Mcp-Session-Id``, and not
    through the time bucket. Within one caller the client's MCP transport
    session is the boundary when it sends one, otherwise the UTC hour.
    ``None`` (no caller identity at all) leaves the session to posthog.mcp.
    """
    caller = _caller_key(user, auth_header)
    if caller is None:
        return None

    if headers:
        transport_session = headers.get(MCP_SESSION_ID_HEADER, "").strip()
        if transport_session:
            return f"{caller}:transport:{transport_session}"

    bucket = (now or datetime.now(UTC)).strftime(_SESSION_BUCKET_FORMAT)
    return f"{caller}:hour:{bucket}"


def request_headers() -> dict[str, str]:
    """The current HTTP request's headers, lowercased; empty outside one."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        return {str(k).lower(): str(v) for k, v in get_http_headers().items()}
    except Exception:  # nosec B110 - no HTTP request in scope (stdio, tests)
        return {}


def current_session_key() -> str | None:
    """The session key for the current request, from the ContextVars the
    hosted auth middleware fills."""
    return derive_session_key(
        request_headers(), get_hosted_authenticated_user(), _session_auth_token.get("")
    )


def current_session_id() -> str | None:
    """The PostHog `$session_id` for the current caller, or None to keep the SDK's."""
    key = current_session_key()
    return derive_session_id_from_mcp_session(key) if key else None
