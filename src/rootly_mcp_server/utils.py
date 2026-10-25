"""
Shared utilities for Rootly MCP Server.
"""

import hashlib
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


def json_line_logger(name: str) -> logging.Logger:
    """A logger that writes each message as a bare line, so Datadog parses JSON messages."""
    json_logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    json_logger.handlers = [handler]
    json_logger.setLevel(logging.INFO)
    json_logger.propagate = False
    return json_logger


_MAX_LOG_EXCERPT_CHARS = 800


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


def _format_traceback_excerpt(tb_text: str) -> str:
    """Keep a short traceback excerpt for structured logs without file-system noise."""
    if not tb_text:
        return ""
    return _sanitize_log_excerpt(tb_text, max_length=1500)


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


# OAuth 2.0 Protected Resource Metadata (RFC 9728)
OAUTH_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
# OAuth 2.0 Authorization Server Metadata (RFC 8414)
OAUTH_AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"

# Cached at import time — static for the lifetime of the process.
_MCP_SERVER_URL = os.getenv("ROOTLY_MCP_SERVER_URL", "")


def resolve_mcp_server_url(request) -> str:
    """Derive the MCP server's public URL from env var or request headers."""
    if _MCP_SERVER_URL:
        return _MCP_SERVER_URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


def is_mcp_server_url_static() -> bool:
    """Whether the MCP server URL comes from a fixed env var (vs. request headers)."""
    return bool(_MCP_SERVER_URL)


def derive_oauth_server_url(api_base_url: str) -> str:
    """Derive the OAuth authorization server URL from the API base URL.

    OAuth endpoints (/oauth/authorize, /.well-known/openid-configuration) live
    on the main domain (e.g. rootly.com), not the API subdomain (api.rootly.com).
    """
    parsed = urlparse(api_base_url)
    host = parsed.hostname or ""
    if host.startswith("api."):
        host = host[4:]
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    else:
        netloc = host
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def auth_header_state(auth_header: str) -> str:
    """Classify Authorization header shape without exposing token contents."""
    raw = (auth_header or "").strip()
    if not raw:
        return "missing"
    parts = raw.split(None, 1)
    if not parts or parts[0].lower() != "bearer":
        return "invalid_format"
    if len(parts) == 1 or not parts[1].strip():
        return "missing_token"
    return "bearer"


def sanitize_parameter_name(name: str) -> str:
    """
    Sanitize parameter names to match MCP property key pattern ^[a-zA-Z0-9_.-]{1,64}$.

    Args:
        name: Original parameter name

    Returns:
        Sanitized parameter name
    """
    # Replace square brackets with underscores: filter[kind] -> filter_kind
    sanitized = re.sub(r"\[([^\]]+)\]", r"_\1", name)

    # Replace any remaining invalid characters with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", sanitized)

    # Remove multiple consecutive underscores
    sanitized = re.sub(r"_{2,}", "_", sanitized)

    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")

    # Ensure the name doesn't exceed 64 characters
    if len(sanitized) > 64:
        sanitized = sanitized[:64].rstrip("_")

    # Ensure the name is not empty and starts with a letter or underscore
    if not sanitized or sanitized[0].isdigit():
        sanitized = "param_" + sanitized if sanitized else "param"

    return sanitized


def sanitize_parameters_in_spec(spec: dict[str, Any]) -> dict[str, str]:
    """
    Sanitize all parameter names in an OpenAPI specification.

    This function modifies the spec in-place and builds a mapping
    of sanitized names to original names.

    Args:
        spec: OpenAPI specification dictionary

    Returns:
        Dictionary mapping sanitized names to original names
    """
    parameter_mapping = {}

    # Sanitize parameters in paths
    if "paths" in spec:
        for _path, path_item in spec["paths"].items():
            if not isinstance(path_item, dict):
                continue

            # Sanitize path-level parameters
            if "parameters" in path_item:
                for param in path_item["parameters"]:
                    if "name" in param:
                        original_name = param["name"]
                        sanitized_name = sanitize_parameter_name(original_name)
                        if sanitized_name != original_name:
                            logger.debug(
                                f"Sanitized path-level parameter: '{original_name}' -> '{sanitized_name}'"
                            )
                            param["name"] = sanitized_name
                            parameter_mapping[sanitized_name] = original_name

            # Sanitize operation-level parameters
            for method, operation in path_item.items():
                if method.lower() not in [
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "options",
                    "head",
                    "trace",
                ]:
                    continue
                if not isinstance(operation, dict):
                    continue

                if "parameters" in operation:
                    for param in operation["parameters"]:
                        if "name" in param:
                            original_name = param["name"]
                            sanitized_name = sanitize_parameter_name(original_name)
                            if sanitized_name != original_name:
                                logger.debug(
                                    f"Sanitized operation parameter: '{original_name}' -> '{sanitized_name}'"
                                )
                                param["name"] = sanitized_name
                                parameter_mapping[sanitized_name] = original_name

    # Sanitize parameters in components (OpenAPI 3.0)
    if "components" in spec and "parameters" in spec["components"]:
        for _param_name, param_def in spec["components"]["parameters"].items():
            if isinstance(param_def, dict) and "name" in param_def:
                original_name = param_def["name"]
                sanitized_name = sanitize_parameter_name(original_name)
                if sanitized_name != original_name:
                    logger.debug(
                        f"Sanitized component parameter: '{original_name}' -> '{sanitized_name}'"
                    )
                    param_def["name"] = sanitized_name
                    parameter_mapping[sanitized_name] = original_name

    return parameter_mapping
