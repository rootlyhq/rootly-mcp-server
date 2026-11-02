"""
Custom exception classes for the Rootly MCP Server.

This module defines specific exception types for better error handling
and debugging throughout the application.
"""

from fastmcp.exceptions import ToolError


class GuidedToolError(ToolError):
    """A tool call the server rejected with instructions for retrying it.

    Raised where the server answers a mistake it anticipated -- an argument the
    tool does not take, a required one left empty -- with a message that tells
    the model what to send instead. The call failed as designed, so telemetry
    publishes it as a non-error (see telemetry_scrubber.mark_expected_tool_errors).
    """


class GuidedScriptError(ValueError):
    """A Code Mode script failure the server explained in place of the raw error.

    A ValueError, as the unexplained sandbox errors are, so the client sees the
    same shape either way.
    """


# agentcat records exception types by name only, so these are what telemetry
# matches against.
GUIDED_ERROR_TYPE_NAMES = frozenset({GuidedToolError.__name__, GuidedScriptError.__name__})


class RootlyMCPError(Exception):
    """Base exception for all Rootly MCP Server errors."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class RootlyAuthenticationError(RootlyMCPError):
    """Raised when API authentication fails."""

    pass


class RootlyAuthorizationError(RootlyMCPError):
    """Raised when the user lacks permissions for the requested resource."""

    pass


class RootlyNetworkError(RootlyMCPError):
    """Raised when network connectivity issues occur."""

    pass


class RootlyTimeoutError(RootlyNetworkError):
    """Raised when a request times out."""

    pass


class RootlyValidationError(RootlyMCPError):
    """Raised when input validation fails."""

    pass


class RootlyRateLimitError(RootlyMCPError):
    """Raised when API rate limits are exceeded."""

    def __init__(self, message: str, retry_after: int | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.retry_after = retry_after


class RootlyAPIError(RootlyMCPError):
    """Raised when the Rootly API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.status_code = status_code


class RootlyServerError(RootlyAPIError):
    """Raised when the Rootly API returns a 5xx server error."""

    pass


class RootlyClientError(RootlyAPIError):
    """Raised when the Rootly API returns a 4xx client error."""

    pass


class RootlyConfigurationError(RootlyMCPError):
    """Raised when there's a configuration error (e.g., missing API token)."""

    pass


class RootlyResourceNotFoundError(RootlyClientError):
    """Raised when a requested resource is not found (404)."""

    pass


def categorize_exception(exception: Exception) -> tuple[type[RootlyMCPError], str]:
    """
    Categorize a generic exception into a specific Rootly exception type.

    Args:
        exception: The exception to categorize

    Returns:
        Tuple of (exception_class, error_message)
    """
    error_str = str(exception).lower()
    exception_type = type(exception).__name__.lower()

    # Authentication errors (401)
    if any(
        keyword in error_str
        for keyword in ["401", "unauthorized", "authentication failed", "invalid token"]
    ):
        return RootlyAuthenticationError, f"Authentication failed: {exception}"

    # Authorization errors (403)
    if any(
        keyword in error_str
        for keyword in ["403", "forbidden", "permission denied", "access denied"]
    ):
        return RootlyAuthorizationError, f"Authorization failed: {exception}"

    # Rate limit errors (429)
    if any(keyword in error_str for keyword in ["429", "rate limit", "too many requests"]):
        return RootlyRateLimitError, f"Rate limit exceeded: {exception}"

    # Resource not found (404)
    if any(keyword in error_str for keyword in ["404", "not found"]):
        return RootlyResourceNotFoundError, f"Resource not found: {exception}"

    # Client errors (4xx)
    if any(keyword in error_str for keyword in ["400", "bad request", "invalid"]):
        return RootlyClientError, f"Client error: {exception}"

    # Server errors (5xx)
    if any(keyword in error_str for keyword in ["500", "502", "503", "504", "server error"]):
        return RootlyServerError, f"Server error: {exception}"

    # Timeout errors
    if any(keyword in exception_type for keyword in ["timeout", "timedout"]):
        return RootlyTimeoutError, f"Request timed out: {exception}"

    # Network/Connection errors
    if any(keyword in exception_type for keyword in ["connection", "network"]):
        return RootlyNetworkError, f"Network error: {exception}"

    # Validation errors
    if any(keyword in exception_type for keyword in ["validation", "pydantic", "field"]):
        return RootlyValidationError, f"Validation error: {exception}"

    # Configuration errors
    if any(keyword in error_str for keyword in ["not set", "missing", "configuration"]):
        return RootlyConfigurationError, f"Configuration error: {exception}"

    # Default to generic API error
    return RootlyAPIError, f"API error: {exception}"
