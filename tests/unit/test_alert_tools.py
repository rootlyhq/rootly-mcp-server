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


def _ok_response(alert: dict) -> Mock:
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": alert}
    return response


def _alert_payload(short_id: str = "PhIQtP", alert_id: str = "alert-uuid") -> dict:
    return {
        "id": alert_id,
        "type": "alerts",
        "attributes": {
            "short_id": short_id,
            "summary": "Disk full on web-01",
            "status": "triggered",
            "source": "datadog",
            "description": "free space below 5%",
            "started_at": "2026-05-19T17:00:00Z",
            "ended_at": None,
            "noise": False,
            "url": f"https://rootly.com/account/alerts/{short_id}",
            "created_at": "2026-05-19T17:00:00Z",
            "alert_urgency_id": "urgency-1",
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
    async def test_uses_direct_point_lookup_endpoint(self):
        tools, request = _register()
        request.return_value = _ok_response(_alert_payload(short_id="PhIQtP"))

        result = await tools["get_alert_by_short_id"]("PhIQtP")

        assert request.call_count == 1
        args, kwargs = request.call_args
        assert args == ("GET", "/v1/alerts/PhIQtP")
        # No list params, no filter — pure point GET.
        assert "params" not in kwargs or not kwargs.get("params")
        assert result["short_id"] == "PhIQtP"
        assert result["summary"] == "Disk full on web-01"

    @pytest.mark.asyncio
    async def test_extracts_short_id_from_url(self):
        tools, request = _register()
        request.return_value = _ok_response(_alert_payload(short_id="PhIQtP"))

        result = await tools["get_alert_by_short_id"]("https://rootly.com/account/alerts/PhIQtP")

        args, _ = request.call_args
        assert args == ("GET", "/v1/alerts/PhIQtP")
        assert result["short_id"] == "PhIQtP"

    @pytest.mark.asyncio
    async def test_url_encodes_short_id(self):
        """Defensive: even though short_ids are alphanumeric in practice,
        anything reaching the path segment must be URL-encoded."""
        tools, request = _register()
        request.return_value = _ok_response(_alert_payload(short_id="x/y"))

        await tools["get_alert_by_short_id"]("a b")

        args, _ = request.call_args
        assert args == ("GET", "/v1/alerts/a%20b")

    @pytest.mark.asyncio
    async def test_returns_not_found_on_404(self):
        tools, request = _register()
        response = Mock()
        response.status_code = 404
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
