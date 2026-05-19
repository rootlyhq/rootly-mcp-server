"""Unit tests for custom alert MCP tool functions."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from rootly_mcp_server.tools.alerts import register_alert_tools


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


def _alert(short_id: str, alert_id: str = "alert-uuid", **attrs: Any) -> dict:
    return {
        "id": alert_id,
        "type": "alerts",
        "attributes": {
            "short_id": short_id,
            "summary": attrs.get("summary", "test summary"),
            "status": attrs.get("status", "triggered"),
            "source": attrs.get("source", "datadog"),
            "description": attrs.get("description", ""),
            "started_at": attrs.get("started_at"),
            "ended_at": attrs.get("ended_at"),
            "noise": attrs.get("noise", False),
            "url": attrs.get("url"),
            "created_at": attrs.get("created_at"),
        },
    }


def _register() -> tuple[dict, AsyncMock]:
    mcp = FakeMCP()
    request = AsyncMock()
    register_alert_tools(mcp=mcp, make_authenticated_request=request, mcp_error=FakeMCPError())
    return mcp.tools, request


@pytest.mark.unit
class TestGetAlertByShortId:
    @pytest.mark.asyncio
    async def test_makes_single_api_call_with_search_filter(self):
        tools, request = _register()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [_alert(short_id="PhIQtP")]}
        request.return_value = response

        result = await tools["get_alert_by_short_id"]("PhIQtP")

        assert request.call_count == 1
        _, kwargs = request.call_args
        assert kwargs["params"]["filter[search]"] == "PhIQtP"
        assert "page[number]" not in kwargs["params"]
        assert result["short_id"] == "PhIQtP"

    @pytest.mark.asyncio
    async def test_extracts_short_id_from_url(self):
        tools, request = _register()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [_alert(short_id="PhIQtP")]}
        request.return_value = response

        result = await tools["get_alert_by_short_id"](
            "https://rootly.com/account/alerts/PhIQtP"
        )

        _, kwargs = request.call_args
        assert kwargs["params"]["filter[search]"] == "PhIQtP"
        assert result["short_id"] == "PhIQtP"

    @pytest.mark.asyncio
    async def test_filters_out_fuzzy_search_false_positives(self):
        """filter[search] is fuzzy across summary/description.
        We must verify the short_id exactly, not return the first hit.
        """
        tools, request = _register()
        response = Mock()
        response.raise_for_status.return_value = None
        # Search returns an alert whose summary contains "PhIQtP" but
        # whose short_id is different.
        response.json.return_value = {
            "data": [
                _alert(short_id="ABCxyz", summary="related to alert PhIQtP"),
                _alert(short_id="PhIQtP", summary="the actual one"),
            ]
        }
        request.return_value = response

        result = await tools["get_alert_by_short_id"]("PhIQtP")

        assert result["short_id"] == "PhIQtP"
        assert result["summary"] == "the actual one"

    @pytest.mark.asyncio
    async def test_returns_not_found_in_a_single_call(self):
        tools, request = _register()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": []}
        request.return_value = response

        result = await tools["get_alert_by_short_id"]("DOESNTEXIST")

        assert request.call_count == 1
        assert result == {
            "error": True,
            "error_type": "not_found",
            "message": "Alert with short_id 'DOESNTEXIST' not found",
        }

    @pytest.mark.asyncio
    async def test_validates_empty_short_id(self):
        tools, request = _register()

        result = await tools["get_alert_by_short_id"]("   ")

        assert request.call_count == 0
        assert result["error_type"] == "validation_error"
