"""HTTP transport and auth-context helpers for Rootly MCP server."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .exceptions import RootlyValidationError
from .security import mask_sensitive_data
from .utils import OAUTH_PROTECTED_RESOURCE_PATH, auth_header_state, resolve_mcp_server_url

logger = logging.getLogger(__name__)

# Detects unfilled OpenAPI path templates like `{id}` or `{schedule_id}` that
# leaked into a URL because a required path argument was missing or misnamed.
# Without this check the literal `{id}` gets URL-encoded to `%7Bid%7D` and the
# Rootly API returns a misleading 404 instead of a clear param-missing error.
_UNFILLED_PATH_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|%7B[A-Za-z_][A-Za-z0-9_]*%7D")

# ContextVar to hold the auth token for the current hosted HTTP session/request.
# Set by AuthCaptureMiddleware on MCP transport paths (e.g. /sse, /mcp),
# then reused by outbound API requests when FastMCP request headers are not
# available in the current execution context.
_session_auth_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_auth_token", default=""
)
_session_client_ip: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_client_ip", default=""
)
_session_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_request_id", default=""
)
_session_transport: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_transport", default=""
)
_session_mcp_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_mcp_mode", default=""
)
_session_authenticated_user: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "_session_authenticated_user", default=None
)
_session_error_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_session_error_context", default=None
)

_MAX_LOG_EXCERPT_CHARS = 800
_MAX_LOG_LIST_ITEMS = 20


def _sanitize_log_excerpt(value: Any, max_length: int = _MAX_LOG_EXCERPT_CHARS) -> str:
    """Sanitize arbitrary text snippets for structured logs without losing context."""
    if value in (None, ""):
        return ""

    text = str(value).replace("\r\n", "\n").strip()
    text = re.sub(r"/[\w/.-]+\.py", "[file]", text)
    text = re.sub(r"C:\\[\w\\.-]+\.py", "[file]", text)
    text = re.sub(r'File "[^"]+"', 'File "[file]"', text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~\-]+", "Bearer ***REDACTED***", text, flags=re.I)
    text = re.sub(r"\brootly_[A-Za-z0-9]+\b", "rootly_***REDACTED***", text)

    if len(text) > max_length:
        text = text[:max_length] + "..."

    return text


def _sanitize_error_context_value(value: Any) -> Any:
    """Trim nested error context into JSON-safe, log-friendly values."""
    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                normalized = _sanitize_error_context_value(parsed)
                if isinstance(normalized, dict | list):
                    return json.dumps(normalized, separators=(",", ":"))
            except Exception:  # nosec B110 - Safe fallback for malformed JSON in log sanitization
                pass
        return _sanitize_log_excerpt(value)

    if isinstance(value, dict):
        sanitized = {
            str(key): _sanitize_error_context_value(subvalue)
            for key, subvalue in list(value.items())[:_MAX_LOG_LIST_ITEMS]
        }
        return mask_sensitive_data(sanitized)

    if isinstance(value, list | tuple):
        return [_sanitize_error_context_value(item) for item in value[:_MAX_LOG_LIST_ITEMS]]

    return _sanitize_log_excerpt(value)


def _clear_error_context() -> None:
    """Reset per-request upstream error context."""
    _session_error_context.set(None)


def _get_error_context() -> dict[str, Any]:
    """Return the captured upstream error context for the current request."""
    return dict(_session_error_context.get() or {})


def _merge_error_context(context: dict[str, Any] | None) -> None:
    """Merge sanitized error context into the current request-scoped state."""
    if not context:
        return

    current = _get_error_context()
    sanitized = {
        key: _sanitize_error_context_value(value)
        for key, value in context.items()
        if value not in ("", None, [], {})
    }
    current.update(mask_sensitive_data(sanitized))
    _session_error_context.set(current)


def get_hosted_authenticated_user() -> dict[str, str] | None:
    """Return the current hosted request's authenticated Rootly user, if available."""
    user = _session_authenticated_user.get()
    return dict(user) if user else None


def _extract_rootly_user_identity(payload: Any) -> dict[str, str] | None:
    """Extract a lightweight Rootly user identity from a JSON:API /users/me payload."""
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    user_id = data.get("id")
    if not isinstance(user_id, str) or not user_id:
        return None

    attributes = data.get("attributes")
    attrs = attributes if isinstance(attributes, dict) else {}

    user: dict[str, str] = {"id": user_id}
    email = attrs.get("email")
    if isinstance(email, str) and email:
        user["email"] = email

    full_name_with_team = attrs.get("full_name_with_team")
    if isinstance(full_name_with_team, str) and full_name_with_team:
        user["full_name_with_team"] = full_name_with_team

    name = attrs.get("full_name") or attrs.get("name")
    if isinstance(name, str) and name:
        user["name"] = name

    return user


def _extract_upstream_url_fields(url: Any) -> tuple[str, str]:
    """Return a queryless URL and path for structured upstream logging."""
    if isinstance(url, httpx.URL):
        sanitized_url = str(url.copy_with(query=None, fragment="")).rstrip("#")
        return sanitized_url, url.path or "/"

    url_text = str(url or "").strip()
    if not url_text:
        return "", ""

    try:
        parsed = httpx.URL(url_text)
        sanitized_url = str(parsed.copy_with(query=None, fragment="")).rstrip("#")
        return sanitized_url, parsed.path or "/"
    except Exception:
        path = url_text.split("?", 1)[0]
        return path, path


def _record_upstream_response_context(method: str, response: httpx.Response) -> None:
    """Capture structured context for upstream HTTP responses with error status."""
    request_url = response.request.url if response.request else response.url
    upstream_url, upstream_path = _extract_upstream_url_fields(request_url)
    _merge_error_context(
        {
            "upstream_method": method.upper(),
            "upstream_status": response.status_code,
            "upstream_url": upstream_url,
            "upstream_path": upstream_path,
            "upstream_response_excerpt": response.text,
            "upstream_log_level": "error" if response.status_code >= 500 else "warning",
        }
    )


def _record_upstream_exception_context(method: str, url: Any, exc: Exception) -> None:
    """Capture structured context for network/transport exceptions before a response exists."""
    upstream_url, upstream_path = _extract_upstream_url_fields(url)
    _merge_error_context(
        {
            "upstream_method": method.upper(),
            "upstream_url": upstream_url,
            "upstream_path": upstream_path,
            "upstream_exception_type": type(exc).__name__,
            "upstream_exception_message": str(exc),
            "upstream_log_level": "error",
        }
    )


def _normalize_path(path: str) -> str:
    """Normalize HTTP path values for reliable comparisons."""
    if not path:
        return "/"
    normalized = path if path.startswith("/") else f"/{path}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _normalize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Normalize request headers to lowercase string keys/values."""
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _extract_client_ip(headers: dict[str, str]) -> str:
    """Extract best-effort client IP from common proxy headers."""
    for key in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
        value = headers.get(key, "").strip()
        if value:
            return value

    xff = headers.get("x-forwarded-for", "").strip()
    if xff:
        first_hop = xff.split(",")[0].strip()
        if first_hop:
            return first_hop
    return ""


def _extract_request_id(headers: dict[str, str]) -> str:
    """Extract request correlation identifier from common headers."""
    for key in ("x-request-id", "x-correlation-id", "cf-ray"):
        value = headers.get(key, "").strip()
        if value:
            return value
    return ""


def _infer_transport_from_path(
    path: str,
    sse_path: str,
    message_path: str,
    streamable_path: str,
    code_mode_path: str = "",
) -> str:
    """Infer effective transport name from the incoming MCP path."""
    normalized = _normalize_path(path)
    if normalized in {sse_path, message_path}:
        return "sse"
    if normalized in {streamable_path, code_mode_path}:
        return "streamable-http"
    return ""


def _infer_mcp_mode_from_path(
    path: str,
    sse_path: str,
    message_path: str,
    streamable_path: str,
    code_mode_path: str = "",
) -> str:
    """Infer whether a request is classic MCP or Code Mode."""
    normalized = _normalize_path(path)
    if normalized == code_mode_path and code_mode_path:
        return "code-mode"
    if normalized in {sse_path, message_path, streamable_path}:
        return "classic"
    return ""


class AuthCaptureMiddleware:
    """ASGI middleware that captures the Authorization header into a ContextVar.

    In hosted HTTP transports, this middleware captures auth headers for MCP
    paths (SSE and Streamable HTTP) before request handling, so downstream
    tool execution can still authenticate Rootly API calls when request headers
    are unavailable in async child contexts.
    """

    _POSITIVE_CACHE_TTL = 300
    _NEGATIVE_CACHE_TTL = 60
    _MAX_CACHE_SIZE = 10_000

    def __init__(self, app):
        self.app = app
        self._sse_path = _normalize_path(os.getenv("FASTMCP_SSE_PATH", "/sse"))
        self._message_path = _normalize_path(os.getenv("FASTMCP_MESSAGE_PATH", "/messages"))
        self._streamable_path = _normalize_path(os.getenv("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp"))
        self._code_mode_path = _normalize_path(os.getenv("ROOTLY_CODE_MODE_PATH", "/mcp-codemode"))
        self._capture_paths = {
            self._sse_path,
            self._message_path,
            self._streamable_path,
            self._code_mode_path,
        }
        self._base_url = os.getenv("ROOTLY_BASE_URL", "https://api.rootly.com")
        # Maps token_hash -> (timestamp, authenticated user or None if invalid)
        self._token_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
        # In-flight probes keyed by token_hash to prevent cache stampede
        self._inflight: dict[str, asyncio.Future[dict[str, str] | None]] = {}

    async def _validate_token_upstream(self, auth_header: str) -> dict[str, str] | None:
        """Probe the Rootly API to verify the Bearer token and extract user identity."""
        token_hash = hashlib.sha256(auth_header.encode()).hexdigest()
        now = time.monotonic()

        cached = self._token_cache.get(token_hash)
        if cached is not None:
            cached_at, authenticated_user = cached
            ttl = (
                self._POSITIVE_CACHE_TTL
                if authenticated_user is not None
                else self._NEGATIVE_CACHE_TTL
            )
            if (now - cached_at) < ttl:
                return dict(authenticated_user) if authenticated_user is not None else None

        existing = self._inflight.get(token_hash)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str] | None] = loop.create_future()
        self._inflight[token_hash] = future

        try:
            authenticated_user = await self._probe_upstream(auth_header)
            self._token_cache[token_hash] = (time.monotonic(), authenticated_user)
            self._evict_cache(time.monotonic())
            future.set_result(authenticated_user)
            return authenticated_user
        except Exception:
            future.set_result(None)
            return None
        finally:
            self._inflight.pop(token_hash, None)

    async def _probe_upstream(self, auth_header: str) -> dict[str, str] | None:
        """Make the actual upstream validation request."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/v1/users/me",
                    headers={
                        "Authorization": auth_header,
                        "Accept": "application/vnd.api+json",
                    },
                )
            if not resp.is_success:
                return None
            return _extract_rootly_user_identity(resp.json())
        except Exception:
            logger.warning("Token validation probe failed, rejecting request")
            return None

    def _evict_cache(self, now: float) -> None:
        """Remove expired entries then enforce hard size cap."""
        if len(self._token_cache) <= self._MAX_CACHE_SIZE:
            return
        self._token_cache = {
            k: (ts, authenticated_user)
            for k, (ts, authenticated_user) in self._token_cache.items()
            if (now - ts)
            < (
                self._POSITIVE_CACHE_TTL
                if authenticated_user is not None
                else self._NEGATIVE_CACHE_TTL
            )
        }
        if len(self._token_cache) > self._MAX_CACHE_SIZE:
            sorted_entries = sorted(self._token_cache.items(), key=lambda x: x[1][0])
            self._token_cache = dict(sorted_entries[-self._MAX_CACHE_SIZE :])

    async def __call__(self, scope, receive, send):
        path = _normalize_path(str(scope.get("path", "")))
        if scope["type"] == "http" and path in self._capture_paths:
            from starlette.requests import Request

            request = Request(scope)
            headers = _normalize_headers(dict(request.headers))
            effective_transport = _infer_transport_from_path(
                path,
                self._sse_path,
                self._message_path,
                self._streamable_path,
                self._code_mode_path,
            )
            mcp_mode = _infer_mcp_mode_from_path(
                path,
                self._sse_path,
                self._message_path,
                self._streamable_path,
                self._code_mode_path,
            )
            if effective_transport:
                _session_transport.set(effective_transport)
            if mcp_mode:
                _session_mcp_mode.set(mcp_mode)
            _session_authenticated_user.set(None)
            auth = request.headers.get("authorization", "")
            if auth:
                _session_auth_token.set(auth)
            client_ip = _extract_client_ip(headers)
            if client_ip:
                _session_client_ip.set(client_ip)
            request_id = _extract_request_id(headers)
            if request_id:
                _session_request_id.set(request_id)
            if auth or client_ip or request_id or effective_transport or mcp_mode:
                logger.debug(
                    "Captured hosted auth/identity context from path: %s "
                    "(transport=%s, mcp_mode=%s)",
                    path,
                    effective_transport or "unknown",
                    mcp_mode or "unknown",
                )
            # Pre-compute the WWW-Authenticate value so the send wrapper
            # only needs a cheap status check per ASGI message.
            resource_metadata_url = (
                f"{resolve_mcp_server_url(request)}{OAUTH_PROTECTED_RESOURCE_PATH}"
            )
            www_auth_value = f'Bearer resource_metadata="{resource_metadata_url}"'.encode()

            # Reject unauthenticated, malformed, or invalid tokens on MCP
            # transport paths before FastMCP processes the protocol message.
            auth_state = auth_header_state(auth)
            authenticated_user = (
                await self._validate_token_upstream(auth) if auth_state == "bearer" else None
            )
            if auth_state != "bearer" or not authenticated_user:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", www_auth_value),
                        ],
                    }
                )
                body = {
                    "error": "unauthorized",
                    "message": "Authorization header with a valid Bearer token is required.",
                }
                await send(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(body).encode(),
                    }
                )
                return
            _session_authenticated_user.set(authenticated_user)

            response_started = False

            async def _send_with_www_authenticate(message):
                nonlocal response_started
                if message.get("type") == "http.response.start":
                    if response_started:
                        return
                    response_started = True
                    if message.get("status") == 401:
                        response_headers = list(message.get("headers", []))
                        response_headers.append((b"www-authenticate", www_auth_value))
                        message = {**message, "headers": response_headers}
                elif message.get("type") == "http.response.body" and not response_started:
                    return
                await send(message)

            await self.app(scope, receive, _send_with_www_authenticate)
            return

        await self.app(scope, receive, send)


# Essential alert attributes to keep (whitelist approach).
# Everything else is stripped to reduce payload size.
ALERT_ESSENTIAL_ATTRIBUTES = {
    "source",
    "status",
    "summary",
    "description",
    "noise",
    "alert_urgency_id",
    "short_id",
    "url",
    "external_url",
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
}
USER_ESSENTIAL_ATTRIBUTES = {
    "name",
    "email",
    "phone",
    "phone_2",
    "first_name",
    "last_name",
    "full_name",
    "full_name_with_team",
    "slack_id",
    "time_zone",
    "created_at",
    "updated_at",
}
SERVICE_ESSENTIAL_ATTRIBUTES = {
    "name",
    "slug",
    "description",
    "public_description",
    "color",
    "status",
    "show_uptime",
    "show_uptime_last_days",
    "environment_ids",
    "service_ids",
    "owner_group_ids",
    "owners_group_ids",
    "owner_user_ids",
    "owners_user_ids",
    "incidents_count",
    "alert_urgency_id",
    "created_at",
    "updated_at",
    "external_id",
    "backstage_id",
    "cortex_id",
    "opslevel_id",
}
SHIFT_ESSENTIAL_ATTRIBUTES = {
    "schedule_id",
    "rotation_id",
    "starts_at",
    "ends_at",
    "is_override",
}
MINIMAL_USER_INCLUDED_ATTRIBUTES = {
    "name",
    "email",
    "full_name",
    "time_zone",
}
MINIMAL_ROLE_INCLUDED_ATTRIBUTES = {"name", "slug"}
MINIMAL_SHIFT_OVERRIDE_INCLUDED_ATTRIBUTES = {
    "starts_at",
    "ends_at",
    "created_at",
    "updated_at",
}


def _collapse_relationship_data(relationship: Any) -> Any:
    """Reduce relationship payloads to ids/counts while keeping structure predictable."""
    if not isinstance(relationship, dict) or "data" not in relationship:
        return relationship

    data = relationship.get("data")
    if isinstance(data, list):
        return {"count": len(data)}
    if isinstance(data, dict):
        return {"data": {"id": data.get("id"), "type": data.get("type")}}
    return {"data": data}


def _strip_resource_attributes(resource: Any, allowed_attributes: set[str]) -> None:
    """Trim a resource's attributes to the allowed subset."""
    if not isinstance(resource, dict):
        return
    attrs = resource.get("attributes")
    if not isinstance(attrs, dict):
        return
    for key in [attr_key for attr_key in attrs if attr_key not in allowed_attributes]:
        del attrs[key]


def _strip_minimal_included_resource(resource: Any) -> None:
    """Trim sideloaded resources for user/shift/service list endpoints."""
    if not isinstance(resource, dict):
        return

    resource_type = resource.get("type")
    if resource_type == "users":
        _strip_resource_attributes(resource, MINIMAL_USER_INCLUDED_ATTRIBUTES)
    elif resource_type in {"roles", "on_call_roles"}:
        _strip_resource_attributes(resource, MINIMAL_ROLE_INCLUDED_ATTRIBUTES)
    elif resource_type == "shift_overrides":
        _strip_resource_attributes(resource, MINIMAL_SHIFT_OVERRIDE_INCLUDED_ATTRIBUTES)
    else:
        resource.pop("attributes", None)
        resource.pop("relationships", None)
        return

    resource.pop("relationships", None)


def strip_heavy_user_data(data: dict[str, Any]) -> dict[str, Any]:
    """Trim user list responses to profile essentials and compact relationship data."""
    if not isinstance(data, dict) or "data" not in data:
        return data

    def _strip_single_user(user: Any) -> None:
        if not isinstance(user, dict):
            return
        _strip_resource_attributes(user, USER_ESSENTIAL_ATTRIBUTES)
        if "relationships" in user and isinstance(user["relationships"], dict):
            user["relationships"] = {
                rel_key: _collapse_relationship_data(rel_value)
                for rel_key, rel_value in user["relationships"].items()
            }

    if isinstance(data["data"], list):
        for user in data["data"]:
            _strip_single_user(user)
    elif isinstance(data["data"], dict):
        _strip_single_user(data["data"])

    included = data.get("included")
    if isinstance(included, list):
        for resource in included:
            _strip_minimal_included_resource(resource)

    return data


def strip_heavy_service_data(data: dict[str, Any]) -> dict[str, Any]:
    """Trim service list responses to operational essentials."""
    if not isinstance(data, dict) or "data" not in data:
        return data

    def _strip_single_service(service: Any) -> None:
        if not isinstance(service, dict):
            return
        _strip_resource_attributes(service, SERVICE_ESSENTIAL_ATTRIBUTES)
        if "relationships" in service and isinstance(service["relationships"], dict):
            service["relationships"] = {
                rel_key: _collapse_relationship_data(rel_value)
                for rel_key, rel_value in service["relationships"].items()
            }

    if isinstance(data["data"], list):
        for service in data["data"]:
            _strip_single_service(service)
    elif isinstance(data["data"], dict):
        _strip_single_service(data["data"])

    included = data.get("included")
    if isinstance(included, list):
        for resource in included:
            _strip_minimal_included_resource(resource)

    return data


def strip_heavy_shift_data(data: dict[str, Any]) -> dict[str, Any]:
    """Trim shift list responses to schedule/timing essentials plus minimal user refs."""
    if not isinstance(data, dict) or "data" not in data:
        return data

    def _strip_single_shift(shift: Any) -> None:
        if not isinstance(shift, dict):
            return
        _strip_resource_attributes(shift, SHIFT_ESSENTIAL_ATTRIBUTES)
        relationships = shift.get("relationships")
        if isinstance(relationships, dict):
            kept_relationships = {}
            for rel_key in ("user", "shift_override"):
                if rel_key in relationships:
                    kept_relationships[rel_key] = _collapse_relationship_data(
                        relationships[rel_key]
                    )
            shift["relationships"] = kept_relationships

    if isinstance(data["data"], list):
        for shift in data["data"]:
            _strip_single_shift(shift)
    elif isinstance(data["data"], dict):
        _strip_single_shift(data["data"])

    included = data.get("included")
    if isinstance(included, list):
        for resource in included:
            _strip_minimal_included_resource(resource)

    return data


def strip_heavy_alert_data(data: dict[str, Any]) -> dict[str, Any]:
    """
    Strip heavy nested data from alert responses to reduce payload size.
    Uses a whitelist approach: only essential attributes are kept.
    Handles both list responses (data: [...]) and single-resource responses (data: {...}).
    """
    if not isinstance(data, dict) or "data" not in data:
        return data

    def _strip_single_alert(alert: Any) -> None:
        if not isinstance(alert, dict):
            return
        if "attributes" in alert:
            attrs = alert["attributes"]
            keys_to_remove = [k for k in attrs if k not in ALERT_ESSENTIAL_ATTRIBUTES]
            for k in keys_to_remove:
                del attrs[k]
        # Collapse relationships to counts
        if "relationships" in alert:
            rels = alert["relationships"]
            for rel_key in list(rels.keys()):
                if (
                    isinstance(rels[rel_key], dict)
                    and "data" in rels[rel_key]
                    and isinstance(rels[rel_key]["data"], list)
                ):
                    rels[rel_key] = {"count": len(rels[rel_key]["data"])}

    if isinstance(data["data"], list):
        for alert in data["data"]:
            _strip_single_alert(alert)
    elif isinstance(data["data"], dict):
        _strip_single_alert(data["data"])

    # Remove sideloaded relationship data
    data.pop("included", None)

    return data


class AuthenticatedHTTPXClient:
    """An HTTPX client wrapper that handles Rootly API authentication and parameter transformation."""

    def __init__(
        self,
        base_url: str = "https://api.rootly.com",
        hosted: bool = False,
        parameter_mapping: dict[str, str] | None = None,
        transport: str = "stdio",
    ):
        self._base_url = base_url
        self.hosted = hosted
        self._api_token = None
        self.parameter_mapping = parameter_mapping or {}

        if not self.hosted:
            self._api_token = self._get_api_token()

        # Create the HTTPX client
        from rootly_mcp_server import __version__

        mode = "hosted" if hosted else "self-hosted"
        headers = {
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
            "User-Agent": f"rootly-mcp-server/{__version__} ({transport}; {mode})",
        }
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"

        logger.info(
            f"AuthenticatedHTTPXClient init: hosted={hosted}, has_api_token={bool(self._api_token)}"
        )

        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            event_hooks={"request": [self._enforce_jsonapi_headers]},
        )

    @staticmethod
    async def _enforce_jsonapi_headers(request: httpx.Request):
        """Event hook to enforce JSON-API Content-Type and Accept on every outgoing request.

        This runs on ALL requests regardless of how they are initiated (request(), send(), etc.),
        ensuring the Rootly API always receives the correct Content-Type header.
        """
        has_auth = "authorization" in request.headers
        if has_auth:
            logger.debug(f"Outgoing request to {request.url} - has authorization: True")
        else:
            logger.warning(f"Outgoing request to {request.url} - has authorization: False")
        request.headers["content-type"] = "application/vnd.api+json"
        request.headers["accept"] = "application/vnd.api+json"

    def _get_api_token(self) -> str | None:
        """Get the API token from environment variables."""
        api_token = os.getenv("ROOTLY_API_TOKEN")
        if not api_token:
            logger.warning("ROOTLY_API_TOKEN environment variable is not set")
            return None
        return api_token

    # Date-range caps enforced by the Rootly shift endpoints. The upstream API
    # returns 422 with "Datetime range exceeds N month(s)" when a request
    # exceeds these limits; pre-flighting the check here turns a confusing
    # upstream error into an actionable client-side validation error.
    #
    # `/v1/shifts`         → listShifts: 2 months cap
    # `/v1/schedules/{id}/shifts` → getScheduleShifts: 1 month cap
    _LIST_SHIFTS_LIMIT_DAYS = 62
    _SCHEDULE_SHIFTS_LIMIT_DAYS = 31

    @staticmethod
    def _parse_iso_date(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        # httpx/upstream accept both date-only (`2026-05-28`) and ISO datetime.
        normalized = candidate.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.strptime(candidate, "%Y-%m-%d")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @classmethod
    def _check_shift_date_range(cls, method: str, url: Any, params: dict[str, Any] | None) -> None:
        """Reject shift queries whose `from`/`to` span exceeds the API's cap."""
        if method.upper() != "GET":
            return
        url_str = str(url)
        path = cls._path_for_url(url_str).rstrip("/")
        # The path must END in `/shifts` to be a real shift-list endpoint, which
        # excludes lookalikes like `/v1/override_shifts/{id}` or
        # `/v1/schedules/{id}/override_shifts` (no `/shifts` suffix).
        if not path.endswith("/shifts"):
            return
        if path == "/v1/shifts":
            limit_days = cls._LIST_SHIFTS_LIMIT_DAYS
        else:
            # Anything else ending in `/shifts` is the per-schedule endpoint.
            limit_days = cls._SCHEDULE_SHIFTS_LIMIT_DAYS
        # Pull `from`/`to` from either explicit kwargs or the URL query string.
        from_raw: Any = None
        to_raw: Any = None
        if params:
            from_raw = params.get("from")
            to_raw = params.get("to")
        if from_raw is None or to_raw is None:
            try:
                query_params = httpx.URL(url_str).params
                if from_raw is None:
                    from_raw = query_params.get("from")
                if to_raw is None:
                    to_raw = query_params.get("to")
            except Exception:
                return
        from_dt = cls._parse_iso_date(from_raw)
        to_dt = cls._parse_iso_date(to_raw)
        if from_dt is None or to_dt is None:
            return  # Let upstream handle malformed dates with its own 422.
        span_days = (to_dt - from_dt).total_seconds() / 86400
        if span_days <= limit_days:
            return
        raise RootlyValidationError(
            f"Date range from={from_raw} to={to_raw} spans {span_days:.1f} days, "
            f"which exceeds the Rootly API's {limit_days}-day cap for {path}. "
            f"Split the query into smaller chunks (each <= {limit_days} days) and "
            f"combine the results."
        )

    @staticmethod
    def _check_for_unfilled_path_params(method: str, url: Any) -> None:
        """Block requests whose URL PATH still contains unfilled `{param}` templates.

        FastMCP's OpenAPI integration substitutes path parameters from tool
        arguments. When the model calls a tool with the wrong parameter name
        (e.g. `schedule_id` instead of `id`), the placeholder remains in the URL
        and gets sent upstream, producing a misleading 404. This guard catches
        that earlier with a clear, actionable error.

        The check is restricted to the URL path (not the query string or
        fragment) so that legitimate query values containing literal braces or
        URL-encoded braces don't trigger a false positive.
        """
        url_str = str(url)
        # Extract the path portion only; everything from `?` onward is query.
        path = AuthenticatedHTTPXClient._path_for_url(url_str)
        matches = _UNFILLED_PATH_PARAM_RE.findall(path)
        if not matches:
            return
        # Normalize URL-encoded matches (`%7Bid%7D`) back to `{id}` for the message.
        missing = sorted({m.replace("%7B", "{").replace("%7D", "}") for m in matches})
        raise RootlyValidationError(
            f"Cannot call {method} {url_str}: the upstream URL still contains "
            f"unfilled path parameter(s) {missing}. This usually means a required "
            f"path argument was missing or passed under the wrong name. "
            f"Check the tool's input schema for the exact parameter names."
        )

    @staticmethod
    def _normalize_query_param_value(value: Any) -> Any:
        """Drop blank query values while preserving meaningful falsey values.

        Rootly endpoints may return 500s when passed empty-string filters such as
        ``filter[status]=``. We treat blank strings and empty collections as
        omitted query parameters, while keeping values like ``0`` and ``False``.
        """
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        if isinstance(value, list | tuple):
            normalized_items = [
                normalized
                for normalized in (
                    AuthenticatedHTTPXClient._normalize_query_param_value(item) for item in value
                )
                if normalized is not None
            ]
            return normalized_items or None
        return value

    def _transform_params(self, params: dict[str, Any] | None) -> dict[str, Any] | None:
        """Transform sanitized parameter names back to original names.

        Empty-string and null values are dropped regardless of whether a name
        mapping exists, so that only meaningful values are forwarded upstream
        (Rootly returns 500s for blank filters like ``filter[status]=``).
        """
        if not params:
            return params

        transformed = {}
        for key, value in params.items():
            normalized_value = self._normalize_query_param_value(value)
            if normalized_value is None:
                logger.debug(f"Dropping empty query parameter: '{key}'")
                continue
            # Use the original name if we have a mapping, otherwise keep the sanitized name
            original_key = self.parameter_mapping.get(key, key)
            transformed[original_key] = normalized_value
            if original_key != key:
                logger.debug(f"Transformed parameter: '{key}' -> '{original_key}'")
        return transformed

    async def request(self, method: str, url: str, **kwargs):
        """Override request to transform parameters and ensure correct headers."""
        self._check_for_unfilled_path_params(method, url)
        # Transform query parameters
        if "params" in kwargs:
            kwargs["params"] = self._transform_params(kwargs["params"])
        self._check_shift_date_range(method, url, kwargs.get("params"))
        if "json" in kwargs:
            kwargs["json"] = self._normalize_request_json_payload(method, kwargs["json"])

        # Log incoming headers for debugging (before transformation)
        incoming_headers = kwargs.get("headers", {})
        if incoming_headers:
            logger.debug(f"Incoming headers for {method} {url}: {list(incoming_headers.keys())}")

        # ALWAYS ensure Content-Type and Accept headers are set correctly for Rootly API
        # This is critical because:
        # 1. FastMCP's get_http_headers() returns LOWERCASE header keys (e.g., "content-type")
        # 2. We must remove any existing content-type/accept and set the correct JSON-API values
        # 3. Handle both lowercase and mixed-case variants to be safe
        headers = dict(kwargs.get("headers") or {})

        # In hosted mode, ensure Authorization header is present.
        # The _session_auth_token ContextVar is set by AuthCaptureMiddleware
        # on MCP transport paths (/sse, /mcp) and propagates to tool handlers
        # via Python's async context inheritance.
        if self.hosted:
            has_auth = any(k.lower() == "authorization" for k in headers)
            if not has_auth:
                session_token = _session_auth_token.get("")
                if session_token:
                    headers["authorization"] = session_token
                    logger.debug("Injected auth from session ContextVar")
                else:
                    logger.warning(f"No authorization header available for {method} {url}")

        # Remove any existing content-type and accept headers (case-insensitive)
        headers_to_remove = [k for k in headers if k.lower() in ("content-type", "accept")]
        for key in headers_to_remove:
            logger.debug(f"Removing header '{key}' with value '{headers[key]}'")
            del headers[key]
        # Set the correct JSON-API headers
        headers["Content-Type"] = "application/vnd.api+json"
        headers["Accept"] = "application/vnd.api+json"
        kwargs["headers"] = headers

        # Log outgoing request
        logger.debug(f"Request: {method} {url}")

        try:
            response = await self.client.request(method, url, **kwargs)
        except Exception as exc:
            _record_upstream_exception_context(method, url, exc)
            raise

        logger.debug(f"Response: {method} {url} -> {response.status_code}")

        # Log error responses (4xx/5xx)
        if response.is_error:
            _record_upstream_response_context(method, response)
            log_message = (
                f"HTTP {response.status_code} error for {method} {url}: "
                f"{response.text[:500] if response.text else 'No response body'}"
            )
            if response.status_code >= 500:
                logger.error(log_message)
            else:
                logger.warning(log_message)

        # Post-process alert GET responses to reduce payload size.
        # Modifies response._content (private httpx attr) because FastMCP's
        # OpenAPITool.run() calls response.json() after this returns, and
        # there is no other interception point for auto-generated tools.
        response = self._maybe_strip_alert_response(method, url, response)
        response = self._maybe_strip_collection_response(method, url, response)
        response = self._maybe_normalize_incident_form_field_selection_response(
            method, url, response
        )
        response = self._maybe_annotate_404_response(method, url, response)
        response = self._maybe_annotate_alert_routing_deprecation(method, url, response)

        return response

    @staticmethod
    def _normalize_request_json_payload(method: str, payload: Any) -> Any:
        """Normalize JSON payloads forwarded by generated tools.

        Some generated write tools expose a top-level `body` parameter, which can
        result in JSON payloads of shape `{"body": {...}}`. Rootly API write
        endpoints expect the inner object as the actual request body.
        """
        if method.upper() not in {"POST", "PUT", "PATCH"}:
            return payload
        if not isinstance(payload, dict):
            return payload
        body = payload.get("body")
        if len(payload) == 1 and isinstance(body, dict):
            return body
        return payload

    # Matches UUID v4 format: 8-4-4-4-12 lowercase hex chars
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    )

    @staticmethod
    def _path_has_id_segment(url: str) -> bool:
        """Return True if the URL path ends with what looks like an ID segment.

        Collection paths (/v1/heartbeats, /v1/status-pages) return False.
        Individual-resource paths (/v1/heartbeats/123) return True.

        Hyphenated resource names like 'status-pages' must not be confused with
        UUID-style IDs — the UUID check requires strict hex-only segments.
        """
        path = AuthenticatedHTTPXClient._path_for_url(url)
        last = path.rstrip("/").rsplit("/", 1)[-1]
        return bool(last and (last.isdigit() or AuthenticatedHTTPXClient._UUID_RE.match(last)))

    @staticmethod
    def _maybe_annotate_404_response(
        method: str, url: str, response: httpx.Response
    ) -> httpx.Response:
        """Append a plan-gating hint to 404 responses.

        Rootly returns 404 for endpoints that are locked to a specific subscription
        tier, even when the request is valid. The response body uses the generic
        title "Not found or unauthorized" with no plan-specific discriminator.

        Heuristic: a 404 on a collection path (no trailing ID) is almost certainly
        plan gating regardless of method.  A 404 on an ID path during a write is
        ambiguous — the resource may simply not exist — so the hint is softened.
        """
        if response.status_code != 404:
            return response
        has_id = AuthenticatedHTTPXClient._path_has_id_segment(url)
        is_write = method.upper() in {"POST", "PUT", "PATCH"}
        # Skip GET on ID paths — those are ordinary "resource not found" responses
        if has_id and not is_write:
            return response
        try:
            body = response.json()
            if not has_id:
                hint = (
                    "This 404 most likely means the feature is not enabled on your Rootly plan. "
                    "Contact Rootly support to enable it for your organisation."
                )
            else:
                hint = (
                    "This 404 may mean the resource does not exist, or that the feature is not "
                    "enabled on your Rootly plan. Contact Rootly support if the resource exists "
                    "and the error persists."
                )
            if isinstance(body, dict):
                body.setdefault("_plan_gating_hint", hint)
            else:
                body = {"original_response": body, "_plan_gating_hint": hint}
            response._content = json.dumps(body).encode()  # noqa: SLF001
        except Exception:  # nosec B110 - Safe fallback; annotation is best-effort
            pass
        return response

    @staticmethod
    def _maybe_annotate_alert_routing_deprecation(
        method: str, url: str, response: httpx.Response
    ) -> httpx.Response:
        """Translate the 403 from `/alert_routing_rules` into a model-actionable hint.

        Tenants with the Advanced Alert Routing feature enabled get a 403 on the
        legacy endpoint with a long-form message pointing to `/alert_routes`.
        We surface a structured `_use_tool` field so the calling model can
        switch tools without re-reading the prose.
        """
        if response.status_code != 403:
            return response
        path = AuthenticatedHTTPXClient._path_for_url(url)
        # Match `/v1/alert_routing_rules` and `/v1/alert_routing_rules/{id}` only;
        # a hypothetical `/v1/alert_routing_rules_v2` would slip through a plain
        # startswith. Anchor on a `/` boundary instead.
        if path != "/v1/alert_routing_rules" and not path.startswith("/v1/alert_routing_rules/"):
            return response
        try:
            body = response.json()
        except Exception:  # nosec B110 - best-effort annotation
            return response
        if not isinstance(body, dict):
            return response
        errors = body.get("errors")
        first_title = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first_title = str(errors[0].get("title", ""))
        if "advanced alert routing" not in first_title.lower():
            return response
        replacement = "listAlertRoutes" if method.upper() == "GET" else "the Alert Routes endpoint"
        body.setdefault(
            "_use_tool",
            {
                "instead_of": "listAlertRoutingRules",
                "use": replacement,
                "reason": (
                    "This tenant has Advanced Alert Routing enabled. The legacy "
                    "/v1/alert_routing_rules endpoint is locked; use /v1/alert_routes "
                    "(operationId listAlertRoutes / getAlertRoute) instead."
                ),
            },
        )
        response._content = json.dumps(body).encode()  # noqa: SLF001
        return response

    @staticmethod
    def _is_alert_endpoint(url: str) -> bool:
        """Check if a URL is an alert endpoint (but not alert sub-resources like events)."""
        url_str = str(url)
        # Match /alerts or /alerts/{id} but not /alert_urgencies, /alert_events, etc.
        # Also matches /incidents/{id}/alerts
        return "/alerts" in url_str and not any(
            sub in url_str
            for sub in ["/alert_urgencies", "/alert_events", "/alert_sources", "/alert_routing"]
        )

    @staticmethod
    def _path_for_url(url: str) -> str:
        """Return the path portion of a URL or path-like string."""
        try:
            return httpx.URL(str(url)).path
        except Exception:
            return str(url).split("?", 1)[0]

    @staticmethod
    def _is_incident_form_field_selection_endpoint(path: str) -> bool:
        """Return whether a path targets incident form field selection resources."""
        return path.startswith("/v1/incident_form_field_selections/") or (
            path.startswith("/v1/incidents/") and path.endswith("/form_field_selections")
        )

    @staticmethod
    def _maybe_strip_alert_response(
        method: str, url: str, response: httpx.Response
    ) -> httpx.Response:
        """Strip heavy data from alert GET responses."""
        if method.upper() != "GET":
            return response
        if not response.is_success:
            return response
        if not AuthenticatedHTTPXClient._is_alert_endpoint(url):
            return response
        try:
            data = response.json()
            stripped = strip_heavy_alert_data(data)
            response._content = json.dumps(stripped).encode()  # noqa: SLF001
        except Exception:
            logger.debug(f"Could not strip alert response for {url}", exc_info=True)
        return response

    @classmethod
    def _maybe_strip_collection_response(
        cls, method: str, url: str, response: httpx.Response
    ) -> httpx.Response:
        """Trim known high-volume collection endpoints while keeping essential fields."""
        if method.upper() != "GET" or not response.is_success:
            return response

        path = cls._path_for_url(url)
        if path not in {"/v1/users", "/v1/services", "/v1/shifts"}:
            return response

        try:
            data = response.json()
            if path == "/v1/users":
                stripped = strip_heavy_user_data(data)
            elif path == "/v1/services":
                stripped = strip_heavy_service_data(data)
            else:
                stripped = strip_heavy_shift_data(data)
            response._content = json.dumps(stripped).encode()  # noqa: SLF001
        except Exception:
            logger.debug(f"Could not strip collection response for {url}", exc_info=True)
        return response

    @staticmethod
    def _normalize_incident_form_field_selection_item(item: Any) -> Any:
        """Prune redundant selected_* objects for text-like form field selections."""
        if not isinstance(item, dict):
            return item

        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            return item

        form_field = attributes.get("form_field")
        if not isinstance(form_field, dict):
            return item

        if form_field.get("input_kind") not in {"text", "textarea"}:
            return item

        normalized_attributes = dict(attributes)
        for key in (
            "selected_groups",
            "selected_options",
            "selected_services",
            "selected_functionalities",
            "selected_catalog_entities",
            "selected_users",
            "selected_environments",
            "selected_causes",
            "selected_incident_types",
        ):
            normalized_attributes.pop(key, None)

        normalized_item = dict(item)
        normalized_item["attributes"] = normalized_attributes
        return normalized_item

    @classmethod
    def _normalize_incident_form_field_selection_payload(cls, payload: Any) -> Any:
        """Normalize form field selection payloads that may contain one item or many."""
        if not isinstance(payload, dict):
            return payload

        data = payload.get("data")
        if isinstance(data, dict):
            normalized_payload = dict(payload)
            normalized_payload["data"] = cls._normalize_incident_form_field_selection_item(data)
            return normalized_payload

        if isinstance(data, list):
            normalized_payload = dict(payload)
            normalized_payload["data"] = [
                cls._normalize_incident_form_field_selection_item(item) for item in data
            ]
            return normalized_payload

        return payload

    @classmethod
    def _maybe_normalize_incident_form_field_selection_response(
        cls, method: str, url: str, response: httpx.Response
    ) -> httpx.Response:
        """Normalize noisy incident form field selection responses."""
        if method.upper() not in {"GET", "POST", "PUT", "PATCH"} or not response.is_success:
            return response

        path = cls._path_for_url(url)
        if not cls._is_incident_form_field_selection_endpoint(path):
            return response

        try:
            payload = response.json()
            normalized = cls._normalize_incident_form_field_selection_payload(payload)
            response._content = json.dumps(normalized).encode()  # noqa: SLF001
        except Exception:
            logger.debug(
                f"Could not normalize incident form field selection response for {url}",
                exc_info=True,
            )
        return response

    async def get(self, url: str, **kwargs):
        """Proxy to request with GET method."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs):
        """Proxy to request with POST method."""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs):
        """Proxy to request with PUT method."""
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs):
        """Proxy to request with PATCH method."""
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs):
        """Proxy to request with DELETE method."""
        return await self.request("DELETE", url, **kwargs)

    async def send(self, request: httpx.Request, **kwargs):
        """Proxy send() for newer fastmcp versions that build requests and call send() directly.

        Headers are enforced by the event hook, so we just delegate to the inner client.
        Alert response stripping is also applied here for forward compatibility.
        """
        self._check_for_unfilled_path_params(request.method, request.url)
        # In hosted mode, ensure Authorization header is present in the request.
        # The _session_auth_token ContextVar is set by AuthCaptureMiddleware
        # on MCP transport paths (/sse, /mcp).
        if self.hosted:
            has_auth = any(k.lower() == "authorization" for k in request.headers)
            if not has_auth:
                session_token = _session_auth_token.get("")
                if session_token:
                    request.headers["authorization"] = session_token
                    logger.debug("Injected auth from session ContextVar in send()")
                else:
                    logger.warning(
                        f"No authorization header available for {request.method} {request.url}"
                    )

        # Transform URL query parameters from sanitized names to original names
        # FastMCP builds requests with sanitized parameter names (e.g., filter_status)
        # but the API expects original names (e.g., filter[status])
        if request.url.params:
            original_params = {}
            for key, value in request.url.params.items():
                normalized_value = self._normalize_query_param_value(value)
                if normalized_value is None:
                    logger.debug(f"Dropping empty URL query parameter: '{key}'")
                    continue
                original_key = self.parameter_mapping.get(key, key)
                original_params[original_key] = normalized_value
            # Rebuild URL with transformed parameters
            new_url = str(request.url).split("?")[0]
            if original_params:
                from urllib.parse import urlencode

                new_url += "?" + urlencode(original_params, doseq=True)
            # Create new request with transformed URL
            new_request = httpx.Request(
                method=request.method,
                url=httpx.URL(new_url),
                headers=request.headers,
                content=request.content,
            )
            request = new_request

        self._check_shift_date_range(request.method, request.url, None)

        try:
            response = await self.client.send(request, **kwargs)
        except Exception as exc:
            _record_upstream_exception_context(request.method, request.url, exc)
            raise

        if response.is_error:
            _record_upstream_response_context(request.method, response)

        response = self._maybe_strip_alert_response(request.method, str(request.url), response)
        response = self._maybe_strip_collection_response(request.method, str(request.url), response)
        response = self._maybe_normalize_incident_form_field_selection_response(
            request.method, str(request.url), response
        )
        response = self._maybe_annotate_alert_routing_deprecation(
            request.method, str(request.url), response
        )
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def __getattr__(self, name):
        # Delegate all other attributes to the underlying client, except for request methods
        if name in ["request", "get", "post", "put", "patch", "delete"]:
            # Use our overridden methods instead
            return getattr(self, name)
        return getattr(self.client, name)

    @property
    def base_url(self):
        return self.client.base_url

    @property
    def headers(self):
        return self.client.headers
