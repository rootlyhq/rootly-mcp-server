"""
Rootly MCP Server - A Model Context Protocol server for Rootly API integration.

This module implements a server that dynamically generates MCP tools based on
the Rootly API's OpenAPI (Swagger) specification using FastMCP's OpenAPI integration.
"""

import hashlib
import json
import logging
import os
import time
import traceback
from typing import Any

import fastmcp.server.middleware as fastmcp_middleware
import httpx
import mcp.types as mt
from fastmcp import FastMCP

from . import audit, legacy_server, payload_stripping, server_defaults, spec_transform, transport
from .exceptions import RootlyAuthenticationError
from .mcp_error import MCPError
from .security import mask_sensitive_data, sanitize_error_message
from .tools.alerts import register_alert_tools
from .tools.incidents import register_incident_tools
from .tools.oncall import register_oncall_tools
from .tools.resources import register_resource_handlers
from .utils import (
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OAUTH_PROTECTED_RESOURCE_PATH,
    auth_header_state,
    derive_oauth_server_url,
    is_mcp_server_url_static,
    resolve_mcp_server_url,
    sanitize_parameters_in_spec,
)

# Set up logger
logger = logging.getLogger(__name__)
_tool_usage_json_logger = logging.getLogger("rootly_mcp_server.tool_usage_json")

# Module-level storage for hosted auth middleware, set by create_rootly_mcp_server().
_hosted_auth_middleware: list | None = None


def get_hosted_auth_middleware() -> list | None:
    """Return the ASGI auth middleware list if in hosted mode, else None."""
    return _hosted_auth_middleware


# Re-export spec helpers for backward compatibility with existing tests/imports.
SWAGGER_URL = spec_transform.SWAGGER_URL
_load_swagger_spec = spec_transform._load_swagger_spec
_fetch_swagger_from_url = spec_transform._fetch_swagger_from_url
_filter_openapi_spec = spec_transform._filter_openapi_spec
_has_broken_references = spec_transform._has_broken_references
snakecase_operation_ids = spec_transform.snakecase_operation_ids

# Re-export transport/auth internals for backward compatibility with existing tests/imports.
ALERT_ESSENTIAL_ATTRIBUTES = transport.ALERT_ESSENTIAL_ATTRIBUTES
strip_heavy_alert_data = transport.strip_heavy_alert_data
AuthenticatedHTTPXClient = transport.AuthenticatedHTTPXClient
AuthCaptureMiddleware = transport.AuthCaptureMiddleware
_session_auth_token = transport._session_auth_token
_session_client_ip = transport._session_client_ip
_session_request_id = transport._session_request_id
_session_transport = transport._session_transport
_session_mcp_mode = transport._session_mcp_mode
_session_error_context = transport._session_error_context
_extract_client_ip = transport._extract_client_ip
_extract_request_id = transport._extract_request_id

# Re-export payload/default helpers for backward compatibility with existing tests/imports.
strip_heavy_nested_data = payload_stripping.strip_heavy_nested_data
_generate_recommendation = server_defaults._generate_recommendation
DEFAULT_ALLOWED_PATHS = server_defaults.DEFAULT_ALLOWED_PATHS
DEFAULT_DELETE_ALLOWED_PATHS = server_defaults.DEFAULT_DELETE_ALLOWED_PATHS
DEFAULT_WRITE_ALLOWED_PATHS = server_defaults.DEFAULT_WRITE_ALLOWED_PATHS
RootlyMCPServer = legacy_server.RootlyMCPServer


def _provider_tool_inventory(
    mcp: FastMCP,
) -> tuple[set[str], Any, set[str]]:
    """Return (curated_names, autogen_provider, autogen_names) across providers.

    Curated tools live on the LocalProvider (`_components` keyed `tool:<name>`);
    autogen OpenAPI tools live on the OpenAPIProvider (`_tools` keyed by name).
    """
    curated_names: set[str] = set()
    autogen_provider: Any = None
    autogen_names: set[str] = set()
    for provider in mcp.providers:
        components = getattr(provider, "_components", None)
        if components:
            for component_key, component in components.items():
                if component_key.startswith("tool:") and getattr(component, "name", None):
                    curated_names.add(component.name)
        autogen_tools = getattr(provider, "_tools", None)
        if isinstance(autogen_tools, dict):
            autogen_provider = provider
            autogen_names.update(autogen_tools.keys())
    return curated_names, autogen_provider, autogen_names


def _remove_autogen_tools_shadowed_by_curated(mcp: FastMCP) -> None:
    """Drop autogen tools whose name a curated `@mcp.tool` registration provides.

    A curated tool and an autogen OpenAPI tool can resolve to the same name (the
    curated one is the richer implementation). Without this, both would surface
    as duplicate entries in `tools/list`. The collision set is derived from the
    actually-registered tool names, so adding a future curated tool that shadows
    an autogen operation is handled automatically — nothing to keep in sync.
    """
    curated_names, autogen_provider, autogen_names = _provider_tool_inventory(mcp)
    if autogen_provider is None:
        return
    collisions = curated_names & autogen_names
    for name in collisions:
        autogen_provider._tools.pop(name, None)
    if collisions:
        logger.info(
            "Removed %d autogen tool(s) shadowed by curated implementations: %s",
            len(collisions),
            ", ".join(sorted(collisions)),
        )


def _fingerprint_auth_header(auth_header: str) -> str:
    """Hash auth header token for non-reversible identity correlation."""
    if not auth_header:
        return ""
    token = auth_header.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _auth_header_state(auth_header: str) -> str:
    """Classify Authorization header shape without exposing token contents."""
    return auth_header_state(auth_header)


def _validate_bearer_auth_header(auth_header: str) -> str:
    """Validate hosted Authorization headers before forwarding them upstream."""
    state = _auth_header_state(auth_header)
    if state == "missing":
        raise RootlyAuthenticationError(
            "Missing Authorization header. Expected 'Authorization: Bearer <ROOTLY_API_TOKEN>'."
        )
    if state == "invalid_format":
        raise RootlyAuthenticationError(
            "Invalid Authorization header format. Expected 'Authorization: Bearer <ROOTLY_API_TOKEN>'."
        )
    if state == "missing_token":
        raise RootlyAuthenticationError(
            "Authorization header is missing a token. Expected 'Authorization: Bearer <ROOTLY_API_TOKEN>'."
        )
    return auth_header.strip()


def _tool_usage_logging_enabled() -> bool:
    """Return whether per-tool usage logging is enabled."""
    return os.getenv("ROOTLY_TOOL_USAGE_LOGGING", "true").lower() in ("1", "true", "yes")


def _configure_tool_usage_json_logger() -> None:
    """Configure a dedicated logger that emits raw JSON lines for Datadog parsing."""
    if getattr(_tool_usage_json_logger, "_rootly_configured", False):
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _tool_usage_json_logger.handlers = [handler]
    _tool_usage_json_logger.setLevel(logging.INFO)
    _tool_usage_json_logger.propagate = False
    _tool_usage_json_logger._rootly_configured = True  # type: ignore[attr-defined]


def _current_tool_identity() -> dict[str, str]:
    """Collect caller identity context for tool usage logs."""
    request_headers: dict[str, str] = {}
    try:
        from fastmcp.server.dependencies import get_http_headers

        request_headers = transport._normalize_headers(get_http_headers())
    except Exception:
        request_headers = {}

    auth_header = request_headers.get("authorization", "") or _session_auth_token.get("")
    client_ip = transport._extract_client_ip(request_headers) or _session_client_ip.get("")
    request_id = transport._extract_request_id(request_headers) or _session_request_id.get("")

    try:
        from fastmcp.server.context import _current_transport

        transport_runtime = str(_current_transport.get() or "")
    except Exception:
        transport_runtime = ""

    transport_effective = _session_transport.get("") or transport_runtime
    mcp_mode = _session_mcp_mode.get("") or "classic"

    return {
        "token_fingerprint": _fingerprint_auth_header(auth_header),
        "auth_header_state": _auth_header_state(auth_header),
        "client_ip": client_ip,
        "request_id": request_id,
        # Keep `transport` backward-compatible while introducing explicit fields.
        "transport": transport_effective,
        "transport_effective": transport_effective,
        "transport_runtime": transport_runtime,
        "mcp_mode": mcp_mode,
    }


def _log_tool_usage_event(
    *,
    tool_name: str,
    status: str,
    duration_ms: float,
    arg_keys: list[str],
    identity: dict[str, str],
    error_type: str | None = None,
    error_context: dict[str, Any] | None = None,
) -> None:
    """Emit structured per-tool usage events for analytics and observability."""
    if not _tool_usage_logging_enabled():
        return

    event: dict[str, Any] = {
        "event": "mcp_tool_call",
        "tool_name": tool_name,
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "tool_arg_count": len(arg_keys),
        "tool_arg_keys": arg_keys[:20],
        "token_fingerprint": identity.get("token_fingerprint", ""),
        "auth_header_state": identity.get("auth_header_state", ""),
        "client_ip": identity.get("client_ip", ""),
        "request_id": identity.get("request_id", ""),
        "transport": identity.get("transport", ""),
        "transport_effective": identity.get("transport_effective", ""),
        "transport_runtime": identity.get("transport_runtime", ""),
        "mcp_mode": identity.get("mcp_mode", ""),
    }
    if error_type:
        event["error_type"] = error_type
    if error_context:
        event.update(
            {key: value for key, value in error_context.items() if value not in ("", [], None, {})}
        )

    _configure_tool_usage_json_logger()
    _tool_usage_json_logger.info(
        json.dumps(
            {k: v for k, v in event.items() if v not in ("", [], None)}, separators=(",", ":")
        )
    )


def _normalize_error_details(value: Any) -> Any:
    """Trim nested tool error details into JSON-safe structured log values."""
    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return transport._sanitize_log_excerpt(value)

    if isinstance(value, dict):
        return mask_sensitive_data(
            {
                str(key): _normalize_error_details(subvalue)
                for key, subvalue in list(value.items())[:20]
            }
        )

    if isinstance(value, list | tuple):
        return [_normalize_error_details(item) for item in value[:20]]

    return transport._sanitize_log_excerpt(value)


def _format_traceback_excerpt(tb_text: str) -> str:
    """Keep a short traceback excerpt for structured logs without file-system noise."""
    if not tb_text:
        return ""
    return transport._sanitize_log_excerpt(tb_text, max_length=1500)


def _extract_structured_tool_error(result: Any) -> dict[str, Any]:
    """Extract structured tool error metadata from an MCP error result, if present."""
    structured = getattr(result, "structuredContent", None)
    is_structured_tool_error = isinstance(structured, dict) and structured.get("error") is True
    if not getattr(result, "isError", False) and not is_structured_tool_error:
        return {}

    error_event: dict[str, Any] = {"error_type": "ToolError"}

    if isinstance(structured, dict):
        if structured.get("error_type"):
            error_event["error_type"] = str(structured["error_type"])
        if structured.get("message"):
            error_event["error_message"] = sanitize_error_message(str(structured["message"]))

        details = structured.get("details")
        if isinstance(details, dict):
            normalized_details = _normalize_error_details(details)
            if normalized_details:
                error_event["error_details"] = normalized_details

            exception_type = details.get("exception_type")
            if exception_type:
                error_event["exception_type"] = str(exception_type)

            upstream_status = details.get("upstream_status", details.get("status_code"))
            if upstream_status is None:
                upstream_status = details.get("status")
            if upstream_status is not None:
                error_event["upstream_status"] = upstream_status

            for upstream_key in (
                "upstream_url",
                "upstream_path",
                "upstream_response_excerpt",
                "upstream_exception_type",
                "upstream_exception_message",
                "upstream_log_level",
            ):
                if details.get(upstream_key):
                    error_event[upstream_key] = _normalize_error_details(details[upstream_key])

            if details.get("traceback"):
                error_event["traceback_excerpt"] = _format_traceback_excerpt(
                    str(details["traceback"])
                )

    content = getattr(result, "content", None) or []
    if not error_event.get("error_message"):
        for item in content:
            text = getattr(item, "text", "")
            if text:
                error_event["error_message"] = sanitize_error_message(text)
                break

    error_event.update(transport._get_error_context())
    return {key: value for key, value in error_event.items() if value not in ("", [], None, {})}


def _extract_exception_error_context(exc: Exception) -> dict[str, Any]:
    """Build structured error metadata for raised tool exceptions."""
    error_context: dict[str, Any] = {
        "error_message": sanitize_error_message(str(exc)),
        "exception_type": type(exc).__name__,
    }

    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if traceback_text:
        error_context["traceback_excerpt"] = _format_traceback_excerpt(traceback_text)

    error_context.update(transport._get_error_context())
    return {key: value for key, value in error_context.items() if value not in ("", [], None, {})}


class CamelCaseAliasMiddleware(fastmcp_middleware.Middleware):
    """Routes deprecated camelCase tool names to their snake_case canonical.

    The tool surface is uniformly snake_case and only snake_case names are
    advertised in `tools/list`. This middleware keeps the historical camelCase
    names callable for clients with cached configs or in-flight sessions by
    rewriting an incoming `call_tool` name to its canonical snake_case form
    before dispatch. Aliases are intentionally never listed, so the visible
    surface stays free of duplicates.
    """

    def __init__(self, aliases: dict[str, str]) -> None:
        # `snakecase_operation_ids` only emits entries that actually changed, so
        # every key here is a camelCase name distinct from its snake_case value.
        self._aliases = dict(aliases)

    async def on_call_tool(
        self,
        context: fastmcp_middleware.MiddlewareContext[mt.CallToolRequestParams],
        call_next: fastmcp_middleware.CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        canonical = self._aliases.get(context.message.name)
        if canonical:
            logger.debug(
                "Routing deprecated camelCase tool %r to canonical %r",
                context.message.name,
                canonical,
            )
            context.message.name = canonical
        return await call_next(context)


class ToolUsageLoggingMiddleware(fastmcp_middleware.Middleware):
    """FastMCP middleware that logs per-tool usage with caller identity context."""

    async def on_call_tool(
        self,
        context: fastmcp_middleware.MiddlewareContext[mt.CallToolRequestParams],
        call_next: fastmcp_middleware.CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        tool_name = context.message.name
        arguments = context.message.arguments or {}
        arg_keys = sorted(arguments.keys()) if isinstance(arguments, dict) else []
        identity = _current_tool_identity()
        start = time.perf_counter()
        transport._clear_error_context()

        try:
            result = await call_next(context)
        except Exception as exc:
            _log_tool_usage_event(
                tool_name=tool_name,
                status="error",
                duration_ms=(time.perf_counter() - start) * 1000,
                arg_keys=arg_keys,
                identity=identity,
                error_type=type(exc).__name__,
                error_context=_extract_exception_error_context(exc),
            )
            raise

        structured_error = _extract_structured_tool_error(result)
        if structured_error:
            _log_tool_usage_event(
                tool_name=tool_name,
                status="error",
                duration_ms=(time.perf_counter() - start) * 1000,
                arg_keys=arg_keys,
                identity=identity,
                error_type=str(structured_error.get("error_type", "ToolError")),
                error_context=structured_error,
            )
            return result

        _log_tool_usage_event(
            tool_name=tool_name,
            status="success",
            duration_ms=(time.perf_counter() - start) * 1000,
            arg_keys=arg_keys,
            identity=identity,
        )
        return result


def create_rootly_mcp_server(
    swagger_path: str | None = None,
    name: str = "Rootly",
    allowed_paths: list[str] | None = None,
    hosted: bool = False,
    base_url: str | None = None,
    transport: str = "stdio",
    delete_allowed_paths: list[str] | None = None,
    enable_write_tools: bool | None = None,
    write_allowed_paths: list[str] | None = None,
    enabled_tools: set[str] | None = None,
) -> FastMCP:
    """
    Create a Rootly MCP Server using FastMCP's OpenAPI integration.

    Args:
        swagger_path: Path to the Swagger JSON file. If None, will fetch from URL.
        name: Name of the MCP server.
        allowed_paths: List of API paths to include. If None, includes default paths.
        delete_allowed_paths: Path templates where DELETE operations are exposed.
            If None, destructive delete tools remain disabled by default.
        hosted: Whether the server is hosted (affects authentication).
        base_url: Base URL for Rootly API. If None, uses ROOTLY_BASE_URL env var or default.
        transport: Transport protocol (stdio, sse, or streamable-http).
        enable_write_tools: Whether non-destructive write tools are exposed.
            If None, uses ROOTLY_MCP_ENABLE_WRITE_TOOLS.
        write_allowed_paths: Path templates where POST/PUT/PATCH operations are exposed
            when write tools are enabled. If None, uses DEFAULT_WRITE_ALLOWED_PATHS.
        enabled_tools: Optional allowlist of exact MCP tool names to expose.
            If None, uses ROOTLY_MCP_ENABLED_TOOLS when set.

    Returns:
        A FastMCP server instance.
    """
    # Set default allowed paths if none provided
    if allowed_paths is None:
        allowed_paths = DEFAULT_ALLOWED_PATHS
    if enable_write_tools is None:
        enable_write_tools = server_defaults.write_tools_enabled_from_env(default=True)
    if enabled_tools is None:
        enabled_tools = server_defaults.enabled_tools_from_env(hosted=hosted)
    if enabled_tools:
        enabled_tools = server_defaults.canonicalize_tool_names(enabled_tools)
    if delete_allowed_paths is None:
        delete_allowed_paths = []
    if write_allowed_paths is None:
        write_allowed_paths = DEFAULT_WRITE_ALLOWED_PATHS if enable_write_tools else []

    # Add /v1 prefix to paths if not present
    allowed_paths_v1 = [
        f"/v1{path}" if not path.startswith("/v1") else path for path in allowed_paths
    ]
    delete_allowed_paths_v1 = [
        f"/v1{path}" if not path.startswith("/v1") else path for path in delete_allowed_paths
    ]
    write_allowed_paths_v1 = [
        f"/v1{path}" if not path.startswith("/v1") else path for path in write_allowed_paths
    ]

    logger.info(f"Creating Rootly MCP Server with allowed paths: {allowed_paths_v1}")

    # Load the Swagger specification
    swagger_spec = _load_swagger_spec(swagger_path)
    logger.info(f"Loaded Swagger spec with {len(swagger_spec.get('paths', {}))} total paths")

    # Normalize every operationId to snake_case so the autogen tool surface is
    # uniformly snake_case (FastMCP derives tool names verbatim from operationIds).
    # Must run before operationId-based filtering, allowlist matching, and
    # curated-override stripping below — all of which now operate on snake names.
    # The returned camelCase->snake_case map feeds the alias middleware so the
    # historical camelCase names stay callable (but hidden from tools/list).
    camel_to_snake_aliases = snakecase_operation_ids(swagger_spec)
    logger.info(f"Normalized {len(camel_to_snake_aliases)} operationIds to snake_case")

    # When an allowlist is provided, build the subset that matches real OpenAPI
    # operationIds; curated tool names (registered later via @mcp.tool) won't appear
    # in the spec and are validated separately after registration.
    #
    # An empty subset is meaningful: it tells `_filter_openapi_spec` to drop every
    # autogen operation, which is the right behavior when the caller asked only for
    # curated tools (e.g. `ROOTLY_MCP_ENABLED_TOOLS=list_incidents`).
    autogen_allowlist: set[str] | None = None
    if enabled_tools:
        all_op_ids = server_defaults.collect_operation_ids(swagger_spec.get("paths", {}))
        autogen_allowlist = enabled_tools & all_op_ids

    # Filter the OpenAPI spec to only include allowed paths
    filtered_spec = _filter_openapi_spec(
        swagger_spec,
        allowed_paths_v1,
        delete_allowed_paths=delete_allowed_paths_v1,
        write_allowed_paths=write_allowed_paths_v1,
        enable_write_tools=enable_write_tools,
        enabled_operation_ids=autogen_allowlist,
    )

    # NOTE: autogen tools whose name collides with a curated `@mcp.tool` are
    # removed AFTER registration by `_remove_autogen_tools_shadowed_by_curated`,
    # which derives the collision set from the real registry instead of a
    # hand-maintained list.

    logger.info(f"Filtered spec to {len(filtered_spec.get('paths', {}))} allowed paths")

    # Log server configuration for audit trail
    config_info = {
        "enable_write_tools": enable_write_tools,
        "tool_count": len(filtered_spec.get("paths", {})),
        "hosted": hosted,
        "enabled_tools": list(enabled_tools) if enabled_tools else None,
        "transport": transport,
        "server_name": name,
    }
    audit.audit.log_server_start(config_info)

    # Log permission changes
    if enable_write_tools:
        audit.audit.log_permission_change(
            "write_tools_enabled",
            {
                "reason": "explicit_configuration",
                "write_paths_count": len(write_allowed_paths_v1),
                "hosted_mode": hosted,
            },
        )

    # Sanitize all parameter names in the filtered spec to be MCP-compliant
    parameter_mapping = sanitize_parameters_in_spec(filtered_spec)
    logger.info(
        f"Sanitized parameter names for MCP compatibility (mapped {len(parameter_mapping)} parameters)"
    )

    # Determine the base URL
    if base_url is None:
        base_url = os.getenv("ROOTLY_BASE_URL", "https://api.rootly.com")

    logger.info(f"Using Rootly API base URL: {base_url}")

    # Create the authenticated HTTP client with parameter mapping

    http_client = AuthenticatedHTTPXClient(
        base_url=base_url, hosted=hosted, parameter_mapping=parameter_mapping, transport=transport
    )

    # Create the MCP server using OpenAPI integration
    # By default, all routes become tools which is what we want
    # NOTE: We pass http_client (the wrapper) instead of http_client.client (the inner httpx client)
    # so that parameter transformation (e.g., filter_status -> filter[status]) is applied.
    # The wrapper implements the same interface as httpx.AsyncClient (duck typing).
    mcp = FastMCP.from_openapi(
        openapi_spec=filtered_spec,
        client=http_client,  # type: ignore[arg-type]
        name=name,
        tags={"rootly", "incident-management"},
    )
    # Alias middleware runs first so the historical camelCase names are rewritten
    # to snake_case before usage logging records the (canonical) tool name.
    mcp.add_middleware(CamelCaseAliasMiddleware(camel_to_snake_aliases))
    mcp.add_middleware(ToolUsageLoggingMiddleware())

    @mcp.custom_route("/healthz", methods=["GET"])
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("OK")

    # OAuth 2.0 Protected Resource Metadata (RFC 9728)
    # MCP clients fetch this to discover which authorization server to use.
    if hosted:
        from starlette.responses import JSONResponse

        async def _oauth_protected_resource_handler(request):
            mcp_server_url = resolve_mcp_server_url(request)

            cache = "max-age=3600" if is_mcp_server_url_static() else "no-store"
            return JSONResponse(
                {
                    "resource": mcp_server_url,
                    "authorization_servers": [derive_oauth_server_url(base_url)],
                    "scopes_supported": [
                        "openid",
                        "profile",
                        "email",
                        "all",
                    ],
                    "bearer_methods_supported": ["header"],
                },
                headers={"Cache-Control": cache},
            )

        # RFC 9728 §5: clients may request the path-suffixed variant first
        # (e.g. /.well-known/oauth-protected-resource/mcp for a resource at /mcp).
        @mcp.custom_route(OAUTH_PROTECTED_RESOURCE_PATH + "/{path:path}", methods=["GET"])
        async def oauth_protected_resource_suffixed(request):
            return await _oauth_protected_resource_handler(request)

        @mcp.custom_route(OAUTH_PROTECTED_RESOURCE_PATH, methods=["GET"])
        async def oauth_protected_resource(request):
            return await _oauth_protected_resource_handler(request)

        # OAuth 2.0 Authorization Server Metadata (RFC 8414)
        # Some MCP clients fetch this directly from the MCP server instead of
        # following the authorization_servers link from the protected resource
        # metadata. Proxy the response from the actual OAuth server.
        _auth_server_metadata_cache: dict[str, Any] = {}
        _AUTH_SERVER_CACHE_TTL = 3600

        # NOTE: The proxied response contains "issuer": "https://rootly.com" from
        # the upstream OAuth server, but clients fetch this from the MCP server
        # origin. RFC 8414 §3.3 requires issuer to match the request URL; strict
        # OAuth libraries may reject the mismatch. Most observed clients (node,
        # python-httpx, Cursor) do not enforce this check.
        async def _oauth_authorization_server_handler(request):
            oauth_server_url = derive_oauth_server_url(base_url)
            now = time.time()
            cached = _auth_server_metadata_cache.get("data")
            cached_at = _auth_server_metadata_cache.get("cached_at", 0)
            if cached and (now - cached_at) < _AUTH_SERVER_CACHE_TTL:
                return JSONResponse(cached, headers={"Cache-Control": "max-age=3600"})

            upstream_url = f"{oauth_server_url}/.well-known/oauth-authorization-server"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(upstream_url)
                    resp.raise_for_status()
                    metadata = resp.json()
                    _auth_server_metadata_cache["data"] = metadata
                    _auth_server_metadata_cache["cached_at"] = now
                    return JSONResponse(metadata, headers={"Cache-Control": "max-age=3600"})
            except Exception:
                logger.warning(
                    "Failed to proxy OAuth authorization server metadata from %s",
                    upstream_url,
                    exc_info=True,
                )
                if cached:
                    return JSONResponse(cached, headers={"Cache-Control": "max-age=60"})
                return JSONResponse(
                    {"error": "authorization_server_metadata_unavailable"},
                    status_code=502,
                    headers={"Cache-Control": "no-store"},
                )

        @mcp.custom_route(OAUTH_AUTHORIZATION_SERVER_PATH + "/{path:path}", methods=["GET"])
        async def oauth_authorization_server_suffixed(request):
            return await _oauth_authorization_server_handler(request)

        @mcp.custom_route(OAUTH_AUTHORIZATION_SERVER_PATH, methods=["GET"])
        async def oauth_authorization_server(request):
            return await _oauth_authorization_server_handler(request)

        # Some clients prepend the resource path before the well-known segment
        # (e.g. /mcp/.well-known/oauth-authorization-server).
        @mcp.custom_route(
            "/{resource_path:path}" + OAUTH_AUTHORIZATION_SERVER_PATH, methods=["GET"]
        )
        async def oauth_authorization_server_prefixed(request):
            return await _oauth_authorization_server_handler(request)

    # Add some custom tools for enhanced functionality

    @mcp.tool()
    def list_endpoints() -> list:
        """List all available Rootly API endpoints with their descriptions."""
        endpoints = []
        for path, path_item in filtered_spec.get("paths", {}).items():
            for method, operation in path_item.items():
                if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                    continue

                summary = operation.get("summary", "")
                description = operation.get("description", "")

                endpoints.append(
                    {
                        "path": path,
                        "method": method.upper(),
                        "summary": summary,
                        "description": description,
                    }
                )

        return endpoints

    @mcp.tool()
    def get_server_version() -> dict:
        """Get the Rootly MCP server version.

        Returns the current version of the deployed MCP server.
        Useful for checking if the server has been updated.
        """
        from rootly_mcp_server import __version__

        return {
            "version": __version__,
            "package": "rootly-mcp-server",
        }

    async def make_authenticated_request(method: str, url: str, **kwargs):
        """Make an authenticated request, extracting token from MCP headers in hosted mode."""
        # In hosted mode, get token from MCP request headers
        if hosted:
            request_headers: dict[str, str] = {}
            try:
                from fastmcp.server.dependencies import get_http_headers

                request_headers = get_http_headers()
                # Get client IP from headers (may be in x-forwarded-for or similar)
                client_ip = (
                    request_headers.get("x-forwarded-for", "unknown")
                    if request_headers
                    else "unknown"
                )
                logger.debug(
                    f"make_authenticated_request: client_ip={client_ip}, headers_keys={list(request_headers.keys()) if request_headers else []}"
                )
                direct_auth_header = (
                    request_headers.get("authorization", "") if request_headers else ""
                )
                effective_auth_header = direct_auth_header or _session_auth_token.get("")
                if direct_auth_header:
                    logger.debug("make_authenticated_request: Found auth header, adding to request")
                elif effective_auth_header:
                    logger.debug(
                        "make_authenticated_request: No direct MCP auth header; using captured session context"
                    )
                else:
                    logger.warning(
                        "make_authenticated_request: No authorization header found in MCP headers or session context"
                    )

                validated_auth_header = _validate_bearer_auth_header(effective_auth_header)
                if "headers" not in kwargs:
                    kwargs["headers"] = {}
                kwargs["headers"]["Authorization"] = validated_auth_header
            except RootlyAuthenticationError as e:
                effective_auth_header = (
                    request_headers.get("authorization", "") if request_headers else ""
                ) or _session_auth_token.get("")
                error_context = dict(_session_error_context.get() or {})
                error_context.update(
                    {
                        "auth_header_state": _auth_header_state(effective_auth_header),
                        "error_message": str(e),
                    }
                )
                _session_error_context.set(error_context)
                raise
            except Exception as e:
                logger.warning(f"make_authenticated_request: Failed to get headers: {e}")

        # Use our custom client with proper error handling instead of bypassing it
        return await http_client.request(method, url, **kwargs)

    register_incident_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        strip_heavy_nested_data=strip_heavy_nested_data,
        mcp_error=MCPError,
        generate_recommendation=_generate_recommendation,
        enable_write_tools=enable_write_tools,
    )

    register_oncall_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        mcp_error=MCPError,
    )

    register_resource_handlers(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        strip_heavy_nested_data=strip_heavy_nested_data,
        mcp_error=MCPError,
    )

    register_alert_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        mcp_error=MCPError,
    )

    # A curated tool and an autogen tool can resolve to the same name; drop the
    # autogen duplicate so only the richer curated implementation is surfaced.
    _remove_autogen_tools_shadowed_by_curated(mcp)

    # Validate the allowlist against the fully-registered tool set (autogen + curated).
    # This must happen after all register_*_tools() calls so curated tool names — which
    # aren't OpenAPI operationIds — are recognized as valid.
    if enabled_tools is not None:
        curated_names, _, autogen_names = _provider_tool_inventory(mcp)
        registered_names = curated_names | autogen_names

        valid_tools = enabled_tools & registered_names
        invalid_tools = sorted(enabled_tools - registered_names)

        if invalid_tools:
            audit.audit.log_configuration_error(
                "invalid_tool_names",
                f"Invalid tool names in allowlist: {', '.join(invalid_tools)}",
                {"invalid_tools": invalid_tools, "valid_tools": sorted(valid_tools)},
            )
            logger.warning(
                "Invalid tool names in allowlist (will be ignored): %s. "
                "Use --list-tools to see available options.",
                ", ".join(invalid_tools),
            )

        if not valid_tools:
            error_msg = "No valid tools found in allowlist"
            audit.audit.log_configuration_error(
                "no_valid_tools", error_msg, {"requested_tools": sorted(enabled_tools)}
            )
            raise ValueError(error_msg)

        audit.audit.log_tool_validation(enabled_tools, valid_tools, invalid_tools)

        # `remove_tool` operates on the LocalProvider only — curated tools live there.
        # Autogen OpenAPI tools were already constrained by `enabled_operation_ids` at
        # spec-filter time, so they shouldn't appear here outside `valid_tools`. If one
        # does (e.g. a future code path adds tools via another provider), skip it
        # rather than crash on KeyError from a provider that doesn't own the name.
        for tool_name in registered_names:
            if tool_name in valid_tools:
                continue
            try:
                mcp.local_provider.remove_tool(tool_name)
            except KeyError:
                logger.debug(
                    "Skipping removal of %r — not owned by LocalProvider",
                    tool_name,
                )
        logger.info(
            "Applied MCP tool allowlist: %s",
            ", ".join(sorted(valid_tools)),
        )

    # In hosted HTTP modes, configure ASGI middleware for auth token capture.
    # Callers retrieve via get_hosted_auth_middleware() and pass to server.run(middleware=...).
    global _hosted_auth_middleware
    if hosted:
        from starlette.middleware import Middleware

        _hosted_auth_middleware = [Middleware(AuthCaptureMiddleware)]
    else:
        _hosted_auth_middleware = None

    # Log server creation (tool count will be shown when tools are accessed)
    logger.info("Created Rootly MCP Server successfully")
    return mcp
