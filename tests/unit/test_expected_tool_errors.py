"""Guided tool errors are recognized as expected failures.

The server's guided answers -- raised as GuidedToolError / GuidedScriptError --
are telemetry's expected failures (no Error Tracking issue); everything else
must stay a real failure. The PostHog side is in test_posthog_mcp_error_detail.
"""

import pytest
from fastmcp.exceptions import ToolError

from rootly_mcp_server.code_mode import execute_code_for_call_shape
from rootly_mcp_server.exceptions import (
    GUIDED_ERROR_TYPE_NAMES,
    GuidedScriptError,
    GuidedToolError,
    is_guided_exception,
)
from rootly_mcp_server.server import _drop_null_arguments


def _wrapped(inner: BaseException, outer: type[BaseException] = ToolError) -> BaseException:
    """`inner` re-raised the way fastmcp and Code Mode wrap tool failures."""
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer(f"Error calling tool 'x': {exc}") from exc
    except BaseException as wrapped:
        return wrapped


def test_type_names_match_the_classes():
    assert {GuidedToolError.__name__, GuidedScriptError.__name__} == GUIDED_ERROR_TYPE_NAMES


def test_guided_errors_keep_their_client_facing_base_types():
    # Callers and fastmcp still see a ToolError / ValueError.
    assert issubclass(GuidedToolError, ToolError)
    assert issubclass(GuidedScriptError, ValueError)


@pytest.mark.parametrize(
    "exc",
    [
        GuidedToolError("list_incidents does not accept 'filter_title_cont'."),
        GuidedScriptError("No module named 'zoneinfo'. Code Mode runs in a sandbox ..."),
        _wrapped(GuidedToolError("update_incident does not accept 'group_ids'.")),
        _wrapped(GuidedScriptError("KeyError: value. call_tool returns ..."), ValueError),
    ],
)
def test_guided_errors_are_recognized(exc):
    assert is_guided_exception(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ToolError("Rate limited by upstream API, please retry later"),
        ToolError("Error calling tool 'create_service': HTTP error 422: Unprocessable Entity"),
        ValueError("ZeroDivisionError: division by zero"),
        _wrapped(RuntimeError("HTTP error 500")),
        # Wording alone does not count: only the type does.
        ToolError("list_incidents does not accept 'filter_title_cont'."),
    ],
)
def test_other_errors_are_not_recognized(exc):
    assert not is_guided_exception(exc)


# The raise sites themselves: a guided answer must be raised as a guided type.


def test_placeholder_required_argument_raises_guided_error():
    schema = {"properties": {"incident_id": {"type": "string"}}, "required": ["incident_id"]}
    with pytest.raises(GuidedToolError, match="a placeholder"):
        _drop_null_arguments("get_incident", {"incident_id": "null"}, schema)


def test_unreadable_execute_call_shape_raises_guided_error():
    with pytest.raises(GuidedToolError, match="execute takes one argument"):
        execute_code_for_call_shape({"foo": "bar"})
