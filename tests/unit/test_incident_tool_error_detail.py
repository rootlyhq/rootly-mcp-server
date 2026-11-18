"""Curated incident tools pass the API's reason and status through on failure.

httpx names only the status and URL, so a rejected update reached the model
as "Client error '422 Unprocessable Entity'" with nothing to correct, and it
retried blind. A sequential number that resolves to nothing is the API's
answer, recorded as a 404.
"""

from unittest.mock import AsyncMock

import httpx2
import pytest

from rootly_mcp_server.mcp_error import MCPError as mcp_error
from rootly_mcp_server.payload_stripping import strip_heavy_nested_data
from rootly_mcp_server.tools.incidents import _upstream_error_detail, register_incident_tools

UUID = "db078c3d-f31e-413a-9d38-5e738a1b3857"


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, name=None, **kwargs):
        def register(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return register


def response(data, status=200, method="GET"):
    return httpx2.Response(
        status,
        json=data,
        request=httpx2.Request(method, f"https://api.rootly.com/v1/incidents/{UUID}"),
    )


def incident_tools(request):
    registry = Registry()
    register_incident_tools(registry, request, strip_heavy_nested_data, mcp_error, lambda _: "")
    return registry.tools


async def test_update_rejection_carries_the_api_reason_and_status():
    body = {"errors": [{"title": "Status is not included in the list", "status": "422"}]}
    request = AsyncMock(return_value=response(body, 422, "PUT"))
    result = await incident_tools(request)["update_incident"](incident_id=UUID, status="mitigated")
    assert result["error_type"] == "client_error"
    assert "Status is not included in the list" in result["message"]
    assert result["details"] == {"upstream_status": 422}


async def test_unknown_incident_number_is_a_404_answer():
    request = AsyncMock(return_value=response({"data": []}))
    result = await incident_tools(request)["get_incident"](incident_id="INC-993")
    assert result["error_type"] == "not_found"
    assert result["details"] == {"upstream_status": 404}
    assert "search_incidents" in result["message"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {
                "errors": [
                    {
                        "title": "Invalid Parameter",
                        "detail": "Only is_one_of",
                        "source": {"pointer": "/data/attributes/status"},
                    }
                ]
            },
            "Invalid Parameter: Only is_one_of (/data/attributes/status)",
        ),
        ({"base": ["You need at least 1 path condition."]}, "You need at least 1 path condition."),
        (
            {"repeat_count": ["must be greater than or equal to 1"]},
            "repeat_count must be greater than or equal to 1",
        ),
        ({"errors": [{"title": "Same", "detail": "Same"}]}, "Same"),
    ],
)
def test_reason_reads_both_error_shapes(body, expected):
    exc = httpx2.HTTPStatusError(
        "x", request=httpx2.Request("PUT", "https://x"), response=response(body, 422, "PUT")
    )
    assert _upstream_error_detail(exc) == (422, expected)


def test_non_json_body_reports_status_only():
    resp = httpx2.Response(
        500, text="<html>oops</html>", request=httpx2.Request("GET", "https://x")
    )
    exc = httpx2.HTTPStatusError("x", request=resp.request, response=resp)
    assert _upstream_error_detail(exc) == (500, None)


def test_exception_without_response_has_no_detail():
    assert _upstream_error_detail(RuntimeError("boom")) == (None, None)
