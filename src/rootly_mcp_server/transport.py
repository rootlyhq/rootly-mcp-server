"""HTTP transport and auth-context helpers for Rootly MCP server."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from typing import Any

import httpx

from .security import mask_sensitive_data
from .utils import OAUTH_PROTECTED_RESOURCE_PATH, auth_header_state, resolve_mcp_server_url

logger = logging.getLogger(__name__)

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

    _TOKEN_CACHE_TTL = 300

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
        self._validated_tokens: dict[str, float] = {}

    async def _validate_token_upstream(self, auth_header: str) -> bool:
        """Probe the Rootly API to verify the Bearer token is valid."""
        import hashlib
        import time

        token_hash = hashlib.sha256(auth_header.encode()).hexdigest()[:16]
        now = time.monotonic()

        cached_at = self._validated_tokens.get(token_hash)
        if cached_at is not None and (now - cached_at) < self._TOKEN_CACHE_TTL:
            return True

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/v1/users/me",
                    headers={
                        "Authorization": auth_header,
                        "Accept": "application/vnd.api+json",
                    },
                )
            if resp.is_success:
                self._validated_tokens[token_hash] = now
                if len(self._validated_tokens) > 10000:
                    cutoff = now - self._TOKEN_CACHE_TTL
                    self._validated_tokens = {
                        k: v for k, v in self._validated_tokens.items() if v > cutoff
                    }
                return True
        except Exception:
            logger.warning("Token validation probe failed, rejecting request")
        return False

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
            if auth_state != "bearer" or not await self._validate_token_upstream(auth):
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

            async def _send_with_www_authenticate(message):
                if message.get("type") == "http.response.start" and message.get("status") == 401:
                    response_headers = list(message.get("headers", []))
                    response_headers.append((b"www-authenticate", www_auth_value))
                    message = {**message, "headers": response_headers}
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

    def _transform_params(self, params: dict[str, Any] | None) -> dict[str, Any] | None:
        """Transform sanitized parameter names back to original names."""
        if not params or not self.parameter_mapping:
            return params

        transformed = {}
        for key, value in params.items():
            # Use the original name if we have a mapping, otherwise keep the sanitized name
            original_key = self.parameter_mapping.get(key, key)
            transformed[original_key] = value
            if original_key != key:
                logger.debug(f"Transformed parameter: '{key}' -> '{original_key}'")
        return transformed

    async def request(self, method: str, url: str, **kwargs):
        """Override request to transform parameters and ensure correct headers."""
        # Transform query parameters
        if "params" in kwargs:
            kwargs["params"] = self._transform_params(kwargs["params"])
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
                original_key = self.parameter_mapping.get(key, key)
                original_params[original_key] = value
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
