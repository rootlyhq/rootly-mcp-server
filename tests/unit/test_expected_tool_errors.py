"""Guided tool errors are published to AgentCat as non-errors.

AgentCat's Sentry exporter opens an issue for every event with `is_error` set.
The server's guided answers -- raised as GuidedToolError / GuidedScriptError --
should not; everything else must keep opening issues.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from rootly_mcp_server.code_mode import execute_code_for_call_shape
from rootly_mcp_server.exceptions import (
    GUIDED_ERROR_TYPE_NAMES,
    GuidedScriptError,
    GuidedToolError,
)
from rootly_mcp_server.server import _drop_null_arguments
from rootly_mcp_server.telemetry_scrubber import (
    is_guided_error,
    mark_expected_tool_errors,
    prepare_telemetry_event,
)


def _captured(exc: BaseException) -> dict[str, Any]:
    """The fields of agentcat's capture_exception payload that the hook reads."""
    chain: list[dict[str, Any]] = []
    current = exc.__cause__ or exc.__context__
    while current is not None:
        chain.append({"message": str(current), "type": type(current).__name__})
        current = current.__cause__ or current.__context__
    payload: dict[str, Any] = {
        "message": str(exc),
        "type": type(exc).__name__,
        "platform": "python",
    }
    if chain:
        payload["chained_errors"] = chain
    return payload


def _wrapped(inner: BaseException, outer: type[BaseException] = ToolError) -> BaseException:
    """`inner` re-raised the way fastmcp and Code Mode wrap tool failures."""
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer(f"Error calling tool 'x': {exc}") from exc
    except BaseException as wrapped:
        return wrapped


def _event(error, is_error=True):
    return SimpleNamespace(is_error=is_error, error=error, parameters=None, user_intent=None)


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
    assert is_guided_error(_captured(exc))


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
    assert not is_guided_error(_captured(exc))


@pytest.mark.parametrize("payload", [None, "a string", {"message": "no type"}, {"type": None}])
def test_unreadable_payloads_are_not_recognized(payload):
    assert not is_guided_error(payload)


def test_guided_error_event_is_cleared_and_keeps_its_text():
    error = _captured(GuidedToolError("get_incident requires the argument 'incident_id'"))
    event = mark_expected_tool_errors(_event(error))
    assert event.is_error is False
    assert event.error == error


def test_real_failure_event_stays_an_error():
    event = mark_expected_tool_errors(_event(_captured(ToolError("HTTP error 500"))))
    assert event.is_error is True


def test_success_event_is_untouched():
    event = mark_expected_tool_errors(_event(None, is_error=False))
    assert event.is_error is False
    assert event.error is None


def test_prepare_hook_scrubs_and_marks():
    event = SimpleNamespace(
        is_error=True,
        error=_captured(GuidedToolError("x does not accept 'y'.")),
        parameters={"arguments": {"password": "hunter2", "title": "x"}},
        user_intent="find bugs",
    )
    result = prepare_telemetry_event(event)
    assert result is event
    assert result.is_error is False
    assert result.user_intent is None
    assert result.parameters["arguments"]["password"] == "[redacted]"
    assert result.parameters["arguments"]["title"] == "x"


# The raise sites themselves: a guided answer must be raised as a guided type.


def test_placeholder_required_argument_raises_guided_error():
    schema = {"properties": {"incident_id": {"type": "string"}}, "required": ["incident_id"]}
    with pytest.raises(GuidedToolError, match="a placeholder"):
        _drop_null_arguments("get_incident", {"incident_id": "null"}, schema)


def test_unreadable_execute_call_shape_raises_guided_error():
    with pytest.raises(GuidedToolError, match="execute takes one argument"):
        execute_code_for_call_shape({"foo": "bar"})


_PLAN_GATED_404 = (
    "Error calling tool 'list_alert_routes': HTTP error 404: Not Found - "
    '{"errors": [{"title": "Not found or unauthorized", "status": "404"}], '
    '"_plan_gating_hint": "This 404 most likely means the feature is not enabled"}'
)


@pytest.mark.parametrize(
    "message",
    [
        _PLAN_GATED_404,
        "Error calling tool 'get_user': HTTP error 404: Not Found - {\"errors\": []}",
        "Error calling tool 'list_ai_automations': HTTP error 403: Forbidden - {}",
        "Error calling tool 'list_incidents': HTTP error 401: Unauthorized",
        "Error calling tool 'create_team': HTTP error 409: Conflict - {}",
        "Error calling tool 'list_incidents': HTTP error 429: Too Many Requests",
    ],
)
def test_upstream_4xx_answers_open_no_issue(message):
    event = mark_expected_tool_errors(_event({"message": message, "type": "ToolError"}))

    assert event.is_error is False
    assert event.error["message"] == message


def test_code_mode_script_that_died_of_a_404_opens_no_issue():
    error = {
        "message": "call_tool('get_alert') failed: see cause",
        "type": "ToolError",
        "chained_errors": [{"message": _PLAN_GATED_404, "type": "ToolError"}],
    }

    assert mark_expected_tool_errors(_event(error)).is_error is False


@pytest.mark.parametrize(
    "message",
    [
        # Can mean the server built the request wrong; keep these as issues.
        "Error calling tool 'list_all_incident_action_items': HTTP error 400: Bad Request",
        "Error calling tool 'create_workflow_task': HTTP error 422: Unprocessable Entity - "
        '{"errors": [{"title": "unknown attribute \'channel\'"}]}',
        "Error calling tool 'list_incidents': HTTP error 500: Internal Server Error",
        "Error calling tool 'list_incidents': HTTP error 503: Service Unavailable",
        # A status quoted inside the body is not the response's own.
        "Error calling tool 'create_incident': HTTP error 422: Unprocessable Entity - "
        "{'detail': 'HTTP error 404'}",
        # A bare number elsewhere in a message is not an upstream status.
        "ValueError: page size 404 is too large",
    ],
)
def test_other_failures_still_open_issues(message):
    event = mark_expected_tool_errors(_event({"message": message, "type": "ToolError"}))

    assert event.is_error is True
