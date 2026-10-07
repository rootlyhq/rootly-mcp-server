"""MCP protocol/tool error helpers for Rootly MCP server."""

from __future__ import annotations

import re
from typing import Any


class MCPError:
    """Enhanced error handling for MCP protocol compliance."""

    @staticmethod
    def protocol_error(
        code: int, message: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a JSON-RPC protocol-level error response."""
        error_response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
        }
        if data:
            error_response["error"]["data"] = data
        return error_response

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a tool-level error response (returned as successful tool result)."""
        error_response: dict[str, Any] = {
            "error": True,
            "error_type": error_type,
            "message": error_message,
        }
        if details:
            error_response["details"] = details
        return error_response

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        """Categorize an exception into error type and appropriate message."""
        error_str = str(exception)
        exception_type = type(exception).__name__

        # Authentication/Authorization errors
        if any(
            keyword in error_str.lower()
            for keyword in ["401", "unauthorized", "authentication", "token", "forbidden"]
        ):
            return "authentication_error", f"Authentication failed: {error_str}"

        # Network/Connection errors
        if any(
            keyword in exception_type.lower() for keyword in ["connection", "timeout", "network"]
        ):
            return "network_error", f"Network error: {error_str}"

        # HTTP errors. Read the status off the response when the exception
        # carries one: httpx renders a 400 as "Client error '400 Bad Request'
        # for url ...", whose first ten characters are "Client err", so looking
        # for a status in the text missed every real HTTP failure and sent them
        # all to execution_error.
        status_code = getattr(getattr(exception, "response", None), "status_code", None)
        if not isinstance(status_code, int):
            # Otherwise the number has to identify itself as a status: any
            # standalone three-digit number swept up "invalid page size 500"
            # and called it a server error, ahead of the validation branch
            # below. Matches "404 Not Found", "HTTP error 500", "status: 503".
            # A number only counts as a status when it says so: named by a
            # keyword ("HTTP error 500", "status: 503"), leading the message
            # ("404 not found"), or followed by a reason phrase ("400 Bad
            # Request"). A bare number mid-sentence -- "invalid page size 500",
            # "422 things" -- is left to the validation branch below.
            match = re.search(
                r"(?i:\b(?:HTTP|status)(?:\s+(?:error|code|status))?)\W{0,3}([1-5]\d{2})\b"
                r"|^\s*([1-5]\d{2})\b"
                r"|\b([1-5]\d{2})\s+(?=[A-Z][a-z])",
                error_str,
            )
            status_code = int(next(g for g in match.groups() if g)) if match else None
        if isinstance(status_code, int):
            if 400 <= status_code < 500:
                return "client_error", f"Client error: {error_str}"
            if 500 <= status_code < 600:
                return "server_error", f"Server error: {error_str}"

        # Validation errors
        if any(
            keyword in exception_type.lower() for keyword in ["validation", "pydantic", "field"]
        ):
            return "validation_error", f"Input validation error: {error_str}"

        # Generic execution errors
        return "execution_error", f"Tool execution error: {error_str}"
