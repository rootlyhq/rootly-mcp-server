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
        return {"error": True, "error_type": error_type, "message": error_message}


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


def _progress(entries: list, done: bool, cursor: str | None, has_more: bool = False) -> dict:
    return {
        "data": {
            "id": "inv-1",
            "type": "investigation_progress",
            "attributes": {
                "status": "completed" if done else "running",
                "outcome": None,
                "done": done,
                "has_more": has_more,
                "phases": [{"key": "gather", "status": "running"}],
                "open_questions": [{"key": "pods", "question": "Were pods OOMKilled?"}],
                "entries": entries,
                "next_cursor": cursor,
            },
        }
    }


def _register(request: AsyncMock, enable_write_tools: bool = True) -> dict[str, Any]:
    mcp = FakeMCP()
    register_investigation_tools(
        mcp=mcp,
        make_authenticated_request=request,
        mcp_error=FakeMCPError(),
        enable_write_tools=enable_write_tools,
        poll_interval=0.01,
        sleep=AsyncMock(),
    )
    return mcp.tools


def test_write_tools_are_gated():
    read_only = _register(AsyncMock(), enable_write_tools=False)
    assert set(read_only) == {
        "list_investigations",
        "get_investigation",
        "get_investigation_progress",
    }

    all_tools = _register(AsyncMock())
    assert {"start_investigation", "ask_investigation", "cancel_investigation"} <= set(all_tools)


@pytest.mark.asyncio
async def test_list_investigations_sends_filters_and_flattens():
    request = AsyncMock(
        return_value=_response({"data": [_investigation()], "meta": {"total_count": 1}})
    )
    tools = _register(request)

    result = await tools["list_investigations"](alert_id="alert-1", status="completed")

    request.assert_awaited_once_with(
        "GET",
        "/v1/investigations",
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
async def test_get_investigation_resolves_latest_for_alert_and_includes_messages():
    request = AsyncMock(
        side_effect=[
            _response({"data": [_investigation("inv-9")]}),
            _response({"data": _investigation("inv-9", summary="Disk full")}),
            _response(
                {
                    "data": [{"id": "m1", "attributes": {"role": "user", "content": "why?"}}],
                    "meta": {"answering": False},
                }
            ),
        ]
    )
    tools = _register(request)

    result = await tools["get_investigation"](alert_id="alert-1", include_messages=True)

    assert request.await_args_list[1].args == ("GET", "/v1/investigations/inv-9")
    assert request.await_args_list[2].args == ("GET", "/v1/investigations/inv-9/messages")
    assert result["investigation"]["summary"] == "Disk full"
    assert result["conversation"]["messages"] == [{"id": "m1", "role": "user", "content": "why?"}]


@pytest.mark.asyncio
async def test_get_investigation_needs_an_identifier():
    tools = _register(AsyncMock())

    result = await tools["get_investigation"]()

    assert result["error_type"] == "validation_error"


@pytest.mark.asyncio
async def test_progress_polls_until_entries_arrive_and_says_to_continue():
    request = AsyncMock(
        side_effect=[
            _response(_progress([], done=False, cursor=None)),
            _response(_progress([{"kind": "step", "title": "Read logs"}], done=False, cursor="c1")),
        ]
    )
    tools = _register(request)

    result = await tools["get_investigation_progress"](investigation_id="inv-1", wait_seconds=5)

    assert request.await_count == 2
    assert request.await_args_list[0].kwargs == {"params": None}
    assert result["entries"] == [{"kind": "step", "title": "Read logs"}]
    assert result["next_cursor"] == "c1"
    assert result["done"] is False
    assert "after_cursor='c1'" in result["next_step"]
    assert result["open_questions"] == [{"key": "pods", "question": "Were pods OOMKilled?"}]


@pytest.mark.asyncio
async def test_progress_drains_pages_and_stops_when_done():
    request = AsyncMock(
        side_effect=[
            _response(_progress([{"title": "a"}], done=False, cursor="c1", has_more=True)),
            _response(_progress([{"title": "b"}], done=True, cursor="c2")),
        ]
    )
    tools = _register(request)

    result = await tools["get_investigation_progress"](investigation_id="inv-1", after_cursor="c0")

    assert request.await_args_list[0].kwargs == {"params": {"after": "c0"}}
    assert request.await_args_list[1].kwargs == {"params": {"after": "c1"}}
    assert [entry["title"] for entry in result["entries"]] == ["a", "b"]
    assert result["done"] is True
    assert "get_investigation" in result["next_step"]


@pytest.mark.asyncio
async def test_progress_returns_empty_batch_when_wait_expires():
    request = AsyncMock(return_value=_response(_progress([], done=False, cursor="c5")))
    tools = _register(request)

    result = await tools["get_investigation_progress"](
        investigation_id="inv-1", after_cursor="c5", wait_seconds=0
    )

    assert request.await_count == 1
    assert result["entries"] == []
    assert result["next_cursor"] == "c5"


@pytest.mark.asyncio
async def test_start_investigation_posts_json_api_payload():
    request = AsyncMock(return_value=_response({"data": _investigation("inv-2", status="pending")}))
    tools = _register(request)

    result = await tools["start_investigation"](incident_id="inc-1", rerun=True)

    request.assert_awaited_once_with(
        "POST",
        "/v1/investigations",
        json={
            "data": {
                "type": "investigations",
                "attributes": {"incident_id": "inc-1", "rerun": True},
            }
        },
    )
    assert result["investigation"]["id"] == "inv-2"
    assert "get_investigation_progress" in result["next_step"]


@pytest.mark.asyncio
async def test_start_investigation_requires_exactly_one_target():
    request = AsyncMock()
    tools = _register(request)

    both = await tools["start_investigation"](alert_id="a", incident_id="i")
    neither = await tools["start_investigation"]()

    assert both["error_type"] == neither["error_type"] == "validation_error"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_investigation_posts_message():
    request = AsyncMock(
        return_value=_response(
            {"data": [], "meta": {"answering": True, "pending_question": "pods OOMKilled"}}
        )
    )
    tools = _register(request)

    result = await tools["ask_investigation"](investigation_id="inv-1", message="pods OOMKilled")

    request.assert_awaited_once_with(
        "POST",
        "/v1/investigations/inv-1/messages",
        json={
            "data": {"type": "investigation_messages", "attributes": {"message": "pods OOMKilled"}}
        },
    )
    assert result["meta"]["answering"] is True
    assert "include_messages=true" in result["next_step"]


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
