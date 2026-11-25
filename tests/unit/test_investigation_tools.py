"""Unit tests for the curated AI SRE investigation tools."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from rootly_mcp_server.tools.investigations import register_investigation_tools


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


class FakeMCPError:
    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        return (exception.__class__.__name__, str(exception))

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"error": True, "error_type": error_type, "message": error_message}
        if details:
            result["details"] = details
        return result


class _HTTPError(Exception):
    def __init__(self, response: Mock):
        super().__init__("Client error '409 Conflict'")
        self.response = response


def _response(body: dict) -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _investigation(investigation_id: str = "inv-1", **attributes: Any) -> dict:
    return {
        "id": investigation_id,
        "type": "investigations",
        "attributes": {"status": "completed", "outcome": "root_cause_identified", **attributes},
    }


def _register(request: AsyncMock, enable_write_tools: bool = True) -> dict[str, Any]:
    mcp = FakeMCP()
    register_investigation_tools(
        mcp=mcp,
        make_authenticated_request=request,
        mcp_error=FakeMCPError(),
        enable_write_tools=enable_write_tools,
    )
    return mcp.tools


def test_write_tools_are_gated():
    read_only = _register(AsyncMock(), enable_write_tools=False)
    assert set(read_only) == {
        "list_investigations",
        "get_investigation",
    }

    all_tools = _register(AsyncMock())
    assert {"start_investigation", "cancel_investigation"} <= set(all_tools)


@pytest.mark.asyncio
async def test_list_investigations_sends_filters_and_flattens():
    request = AsyncMock(
        return_value=_response({"data": [_investigation()], "meta": {"total_count": 1}})
    )
    tools = _register(request)

    result = await tools["list_investigations"](alert_id="alert-1", status="completed")

    request.assert_awaited_once_with(
        "GET",
        "/v1/ai/investigations",
        params={
            "page[size]": 10,
            "page[number]": 1,
            "filter[alert_id]": "alert-1",
            "filter[status]": "completed",
        },
    )
    assert result["investigations"] == [
        {"id": "inv-1", "status": "completed", "outcome": "root_cause_identified"}
    ]
    assert result["meta"] == {"total_count": 1}


@pytest.mark.asyncio
async def test_get_investigation_resolves_latest_for_alert():
    request = AsyncMock(
        side_effect=[
            _response({"data": [_investigation("inv-9")]}),
            _response({"data": _investigation("inv-9", summary="Disk full")}),
        ]
    )
    tools = _register(request)

    result = await tools["get_investigation"](alert_id="alert-1")

    assert request.await_count == 2
    assert request.await_args_list[1].args == ("GET", "/v1/ai/investigations/inv-9")
    assert result["investigation"]["summary"] == "Disk full"


@pytest.mark.asyncio
async def test_get_investigation_needs_an_identifier():
    tools = _register(AsyncMock())

    result = await tools["get_investigation"]()

    assert result["error_type"] == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed", "cancelled"])
async def test_get_investigation_returns_current_snapshot(status):
    attributes = {"status": status, "summary": "Disk full", "root_cause": None}
    request = AsyncMock(return_value=_response({"data": _investigation(**attributes)}))
    tools = _register(request)

    result = await tools["get_investigation"](investigation_id="inv-1")

    request.assert_awaited_once_with("GET", "/v1/ai/investigations/inv-1")
    assert result["investigation"] == {
        "id": "inv-1",
        "outcome": "root_cause_identified",
        **attributes,
    }
    assert "get_investigation_progress" not in tools


@pytest.mark.asyncio
async def test_start_investigation_posts_json_api_payload():
    request = AsyncMock(return_value=_response({"data": _investigation("inv-2", status="pending")}))
    tools = _register(request)

    result = await tools["start_investigation"](incident_id="inc-1", rerun=True)

    request.assert_awaited_once_with(
        "POST",
        "/v1/ai/investigations",
        json={
            "data": {
                "type": "investigations",
                "attributes": {"incident_id": "inc-1", "rerun": True},
            }
        },
    )
    assert result["investigation"]["id"] == "inv-2"
    assert "get_investigation with investigation_id='inv-2'" in result["next_step"]


@pytest.mark.asyncio
async def test_start_investigation_requires_exactly_one_target():
    request = AsyncMock()
    tools = _register(request)

    both = await tools["start_investigation"](alert_id="a", incident_id="i")
    neither = await tools["start_investigation"]()

    assert both["error_type"] == neither["error_type"] == "validation_error"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_investigation_returns_tool_error_on_http_failure():
    request = AsyncMock(side_effect=RuntimeError("409 Conflict"))
    tools = _register(request)

    result = await tools["cancel_investigation"](investigation_id="inv-1")

    assert result == {"error": True, "error_type": "RuntimeError", "message": "409 Conflict"}


@pytest.mark.asyncio
async def test_cancel_investigation_surfaces_api_error_title_and_code():
    response = Mock(status_code=409)
    response.json.return_value = {
        "errors": [
            {
                "title": "The investigation has already finished.",
                "code": "not_cancellable",
                "status": "409",
            }
        ]
    }
    response.raise_for_status.side_effect = _HTTPError(response)
    tools = _register(AsyncMock(return_value=response))

    result = await tools["cancel_investigation"](investigation_id="inv-1")

    assert result["message"] == "The investigation has already finished. (not_cancellable)"
    assert result["details"]["upstream_status"] == 409


@pytest.mark.asyncio
async def test_get_investigation_error_without_code_shows_only_the_title():
    response = Mock(status_code=404)
    response.json.return_value = {
        "errors": [{"title": "Not found or unauthorized", "status": "404"}]
    }
    response.raise_for_status.side_effect = _HTTPError(response)
    tools = _register(AsyncMock(return_value=response))

    result = await tools["get_investigation"](investigation_id="missing")

    assert result["message"] == "Not found or unauthorized"
    assert result["details"]["upstream_status"] == 404
