"""Unit tests for on-call handoff tools."""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from rootly_mcp_server.tools.oncall import (
    DEFAULT_MAX_SHIFT_INCIDENT_RESULTS,
    SHIFT_INCIDENT_QUERY_FIELDS,
    register_oncall_tools,
)


class FakeMCP:
    """Small tool registry used for direct custom tool testing."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


class FakeMCPError:
    """Minimal error helper for custom tool tests."""

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        return (exception.__class__.__name__, str(exception))

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "error": True,
            "error_type": error_type,
            "message": error_message,
            "details": details or {},
        }


@pytest.mark.unit
@pytest.mark.asyncio
class TestGetOncallHandoffSummary:
    """Test get_oncall_handoff_summary tool."""

    async def test_tool_registered(self):
        """Test that get_oncall_handoff_summary is registered."""
        from rootly_mcp_server.server import create_rootly_mcp_server

        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()
            assert server is not None

            tools_list = await server.list_tools()
            tool_names = []
            for t in tools_list:
                if hasattr(t, "name"):
                    tool_names.append(t.name)  # type: ignore[attr-defined]
                else:
                    tool_names.append(str(t))

            assert "get_oncall_handoff_summary" in tool_names


@pytest.mark.unit
@pytest.mark.asyncio
class TestGetShiftIncidents:
    """Test get_shift_incidents tool."""

    async def test_tool_registered(self):
        """Test that get_shift_incidents is registered."""
        from rootly_mcp_server.server import create_rootly_mcp_server

        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()
            assert server is not None

            tools_list = await server.list_tools()
            tool_names = []
            for t in tools_list:
                if hasattr(t, "name"):
                    tool_names.append(t.name)  # type: ignore[attr-defined]
                else:
                    tool_names.append(str(t))

            assert "get_shift_incidents" in tool_names

    def _register_tools(self) -> tuple[dict[str, Any], AsyncMock]:
        mcp = FakeMCP()
        request = AsyncMock()
        register_oncall_tools(
            mcp=mcp,
            make_authenticated_request=request,
            mcp_error=FakeMCPError(),
        )
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_get_shift_incidents_fetches_up_to_shift_end_with_essential_fields(self):
        tools, request = self._register_tools()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"data": [], "meta": {"total_pages": 1}}
        request.return_value = response

        result = await tools["get_shift_incidents"](
            start_time="2026-03-17T15:00:00Z",
            end_time="2026-03-18T15:00:00Z",
        )

        request.assert_awaited_once()
        assert request.await_args is not None
        args, kwargs = request.await_args
        assert args == ("GET", "/v1/incidents")
        assert kwargs["params"]["filter[started_at][lte]"] == "2026-03-18T15:00:00Z"
        assert kwargs["params"]["fields[incidents]"] == SHIFT_INCIDENT_QUERY_FIELDS
        assert kwargs["params"]["page[number]"] == 1
        assert result["success"] is True
        assert result["summary"]["total_incidents"] == 0

    @pytest.mark.asyncio
    async def test_get_shift_incidents_keeps_incidents_resolved_during_shift(self):
        tools, request = self._register_tools()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "data": [
                {
                    "id": "inc-resolved-during-shift",
                    "attributes": {
                        "title": "Preexisting incident",
                        "severity": {"name": "SEV 2"},
                        "status": "resolved",
                        "created_at": "2026-03-17T12:00:00Z",
                        "started_at": "2026-03-17T12:30:00Z",
                        "resolved_at": "2026-03-17T16:00:00Z",
                        "summary": "Resolved during shift",
                        "customer_impact_summary": "Minor impact",
                        "mitigation": "Patched",
                        "url": "https://rootly.com/incidents/1",
                    },
                }
            ],
            "meta": {"total_pages": 1},
        }
        request.return_value = response

        result = await tools["get_shift_incidents"](
            start_time="2026-03-17T15:00:00Z",
            end_time="2026-03-18T15:00:00Z",
        )

        assert result["success"] is True
        assert result["summary"]["total_incidents"] == 1
        assert result["incidents"][0]["incident_id"] == "inc-resolved-during-shift"
        assert result["incidents"][0]["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_get_shift_incidents_truncates_large_results(self):
        tools, request = self._register_tools()
        response = Mock()
        response.status_code = 200
        long_summary = "x" * 500
        response.json.return_value = {
            "data": [
                {
                    "id": f"inc-{i}",
                    "attributes": {
                        "title": f"Incident {i}",
                        "severity": {"name": "SEV 2"},
                        "status": "started",
                        "created_at": "2026-03-17T16:00:00Z",
                        "started_at": "2026-03-17T16:00:00Z",
                        "resolved_at": None,
                        "summary": long_summary,
                        "customer_impact_summary": long_summary,
                        "mitigation": long_summary,
                        "url": f"https://rootly.com/incidents/{i}",
                    },
                }
                for i in range(DEFAULT_MAX_SHIFT_INCIDENT_RESULTS + 5)
            ],
            "meta": {"total_pages": 1},
        }
        request.return_value = response

        result = await tools["get_shift_incidents"](
            start_time="2026-03-17T15:00:00Z",
            end_time="2026-03-18T15:00:00Z",
        )

        assert result["success"] is True
        assert result["summary"]["total_incidents"] == DEFAULT_MAX_SHIFT_INCIDENT_RESULTS + 5
        assert result["returned_incidents"] == DEFAULT_MAX_SHIFT_INCIDENT_RESULTS
        assert result["truncated_incidents"] == 5
        assert result["results_truncated"] is True
        assert len(result["incidents"]) == DEFAULT_MAX_SHIFT_INCIDENT_RESULTS
        first_incident = result["incidents"][0]
        assert first_incident["summary"].endswith("…")
        assert first_incident["impact"].endswith("…")
        assert first_incident["mitigation"].endswith("…")
        assert first_incident["narrative"] is not None
        assert len(first_incident["narrative"]) <= 400


@pytest.mark.unit
@pytest.mark.asyncio
class TestListShifts:
    """Test list_shifts pagination and filtering behavior."""

    def _register_tools(self) -> tuple[dict[str, Any], AsyncMock]:
        mcp = FakeMCP()
        request = AsyncMock()
        register_oncall_tools(
            mcp=mcp,
            make_authenticated_request=request,
            mcp_error=FakeMCPError(),
        )
        return mcp.tools, request

    def _response(self, payload: dict[str, Any]) -> Mock:
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    async def test_list_shifts_returns_requested_page_with_meta(self):
        tools, request = self._register_tools()
        request.side_effect = [
            self._response(
                {
                    "data": [
                        {
                            "id": "2381",
                            "type": "users",
                            "attributes": {
                                "full_name": "Quentin Rousseau",
                                "email": "quentin@example.com",
                            },
                        },
                        {
                            "id": "94178",
                            "type": "users",
                            "attributes": {
                                "full_name": "Gideon Lapshun",
                                "email": "gideon@example.com",
                            },
                        },
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "schedule-1",
                            "type": "schedules",
                            "attributes": {
                                "name": "Infrastructure - Primary",
                                "owner_group_ids": ["team-1"],
                            },
                        }
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "team-1",
                            "type": "teams",
                            "attributes": {"name": "Infrastructure"},
                        }
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "shift-1",
                            "type": "shifts",
                            "attributes": {
                                "schedule_id": "schedule-1",
                                "starts_at": "2026-02-09T08:00:00.000-08:00",
                                "ends_at": "2026-02-09T16:00:00.000-08:00",
                                "is_override": False,
                            },
                            "relationships": {"user": {"data": {"id": "2381", "type": "users"}}},
                        },
                        {
                            "id": "shift-2",
                            "type": "shifts",
                            "attributes": {
                                "schedule_id": "schedule-1",
                                "starts_at": "2026-02-10T08:00:00.000-08:00",
                                "ends_at": "2026-02-10T16:00:00.000-08:00",
                                "is_override": False,
                            },
                            "relationships": {"user": {"data": {"id": "94178", "type": "users"}}},
                        },
                        {
                            "id": "shift-3",
                            "type": "shifts",
                            "attributes": {
                                "schedule_id": "schedule-1",
                                "starts_at": "2026-02-11T08:00:00.000-08:00",
                                "ends_at": "2026-02-11T16:00:00.000-08:00",
                                "is_override": False,
                            },
                            "relationships": {"user": {"data": {"id": "2381", "type": "users"}}},
                        },
                    ],
                    "included": [],
                    "meta": {"total_pages": 1},
                }
            ),
        ]

        result = await tools["list_shifts"](
            from_date="2026-02-09T00:00:00Z",
            to_date="2026-02-12T00:00:00Z",
            page_size=1,
            page_number=2,
        )

        assert result["total_shifts"] == 3
        assert result["returned_shifts"] == 1
        assert result["meta"] == {
            "page_size": 1,
            "page_number": 2,
            "total_matching_shifts": 3,
            "returned_shifts": 1,
            "has_more": True,
            "next_page": 3,
        }
        assert len(result["shifts"]) == 1
        assert result["shifts"][0]["shift_id"] == "shift-2"
        assert result["shifts"][0]["user_name"] == "Gideon Lapshun"
        assert request.await_count == 4

    async def test_list_shifts_filters_before_pagination(self):
        tools, request = self._register_tools()
        request.side_effect = [
            self._response(
                {
                    "data": [
                        {
                            "id": "2381",
                            "type": "users",
                            "attributes": {
                                "full_name": "Quentin Rousseau",
                                "email": "quentin@example.com",
                            },
                        },
                        {
                            "id": "94178",
                            "type": "users",
                            "attributes": {
                                "full_name": "Gideon Lapshun",
                                "email": "gideon@example.com",
                            },
                        },
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "schedule-1",
                            "type": "schedules",
                            "attributes": {
                                "name": "Infrastructure - Primary",
                                "owner_group_ids": ["team-1"],
                            },
                        }
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "team-1",
                            "type": "teams",
                            "attributes": {"name": "Infrastructure"},
                        }
                    ]
                }
            ),
            self._response(
                {
                    "data": [
                        {
                            "id": "shift-1",
                            "type": "shifts",
                            "attributes": {
                                "schedule_id": "schedule-1",
                                "starts_at": "2026-02-09T08:00:00.000-08:00",
                                "ends_at": "2026-02-09T16:00:00.000-08:00",
                                "is_override": False,
                            },
                            "relationships": {"user": {"data": {"id": "2381", "type": "users"}}},
                        },
                        {
                            "id": "shift-2",
                            "type": "shifts",
                            "attributes": {
                                "schedule_id": "schedule-1",
                                "starts_at": "2026-02-10T08:00:00.000-08:00",
                                "ends_at": "2026-02-10T16:00:00.000-08:00",
                                "is_override": False,
                            },
                            "relationships": {"user": {"data": {"id": "94178", "type": "users"}}},
                        },
                    ],
                    "included": [],
                    "meta": {"total_pages": 1},
                }
            ),
        ]

        result = await tools["list_shifts"](
            from_date="2026-02-09T00:00:00Z",
            to_date="2026-02-12T00:00:00Z",
            user_ids="2381",
            page_size=10,
            page_number=1,
        )

        assert result["total_shifts"] == 1
        assert result["returned_shifts"] == 1
        assert result["meta"]["has_more"] is False
        assert result["shifts"][0]["user_id"] == "2381"

    async def test_list_shifts_rejects_page_number_zero(self):
        tools, request = self._register_tools()

        result = await tools["list_shifts"](
            from_date="2026-02-09T00:00:00Z",
            to_date="2026-02-12T00:00:00Z",
            page_size=10,
            page_number=0,
        )

        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "page_number must be >= 1" in result["message"]
        request.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
class TestLookupMapsHelper:
    """Tests for the internal _fetch_users_and_schedules_maps helper.

    Exercised indirectly through list_shifts, which calls the helper to
    enrich its response. We assert on the call pattern observed on the
    mocked make_authenticated_request to verify pagination and caching
    behavior.
    """

    @staticmethod
    def _register() -> tuple[dict[str, Any], AsyncMock]:
        mcp = FakeMCP()
        request = AsyncMock()
        register_oncall_tools(
            mcp=mcp,
            make_authenticated_request=request,
            mcp_error=FakeMCPError(),
        )
        return mcp.tools, request

    @staticmethod
    def _ok(payload: dict[str, Any]) -> Mock:
        r = Mock()
        r.status_code = 200
        r.json.return_value = payload
        return r

    @staticmethod
    def _resource(kind: str, count: int, total_pages: int = 1) -> dict[str, Any]:
        return {
            "data": [
                {"id": f"{kind}-{i}", "type": kind, "attributes": {"name": f"{kind} {i}"}}
                for i in range(count)
            ],
            "meta": {"total_pages": total_pages},
        }

    async def test_fans_out_additional_pages_when_first_page_is_full(self):
        """When page 1 returns 100 items with total_pages > 1, the helper
        fetches the remaining pages — and merges them into the lookup."""
        tools, request = self._register()

        def responder(method, url, params=None, **_):
            assert params is not None
            page = params.get("page[number]", 1)
            if url == "/v1/users":
                # Page 1 full → must fan out; page 2 fills the rest.
                if page == 1:
                    return self._ok(self._resource("users", 100, total_pages=2))
                return self._ok(self._resource("users", 50, total_pages=2))
            if url in ("/v1/schedules", "/v1/teams"):
                return self._ok({"data": [], "meta": {"total_pages": 1}})
            if url == "/v1/shifts":
                return self._ok({"data": [], "included": [], "meta": {"total_pages": 1}})
            raise AssertionError(f"unexpected call: {url}")

        request.side_effect = responder
        await tools["list_shifts"](from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z")

        users_calls = [c for c in request.call_args_list if c.args[1] == "/v1/users"]
        pages_fetched = sorted(c.kwargs["params"]["page[number]"] for c in users_calls)
        assert pages_fetched == [1, 2]

    async def test_falls_back_to_max_pages_when_meta_total_pages_missing(self):
        """If page 1 is full but meta omits total_pages, we must still
        keep fetching — matches the legacy 'keep going until a short
        page' semantics so APIs without pagination metadata aren't
        silently truncated."""
        tools, request = self._register()

        def responder(method, url, params=None, **_):
            assert params is not None
            if url == "/v1/users":
                page = params["page[number]"]
                if page == 1:
                    # Full page, but no meta.total_pages — old behaviour
                    # would keep fetching; new behaviour must too.
                    return self._ok({"data": [{"id": f"u-{i}"} for i in range(100)]})
                # Later pages return short → real end of data.
                return self._ok({"data": [{"id": f"u-page{page}"}]})
            if url in ("/v1/schedules", "/v1/teams"):
                return self._ok({"data": [], "meta": {"total_pages": 1}})
            if url == "/v1/shifts":
                return self._ok({"data": [], "included": [], "meta": {"total_pages": 1}})
            raise AssertionError(f"unexpected call: {url}")

        request.side_effect = responder
        await tools["list_shifts"](from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z")

        users_calls = [c for c in request.call_args_list if c.args[1] == "/v1/users"]
        # Must have fetched more than just page 1.
        assert len(users_calls) > 1

    async def test_continues_when_one_page_fetch_raises(self):
        """A transient error on any non-first page must not bring down
        the whole resource fetch — surviving pages are still merged."""
        tools, request = self._register()

        def responder(method, url, params=None, **_):
            assert params is not None
            if url == "/v1/users":
                page = params["page[number]"]
                if page == 1:
                    return self._ok(
                        {"data": [{"id": f"u-{i}"} for i in range(100)], "meta": {"total_pages": 3}}
                    )
                if page == 2:
                    raise RuntimeError("upstream blip")
                # page 3
                return self._ok({"data": [{"id": "u-200"}]})
            if url in ("/v1/schedules", "/v1/teams"):
                return self._ok({"data": [], "meta": {"total_pages": 1}})
            if url == "/v1/shifts":
                return self._ok({"data": [], "included": [], "meta": {"total_pages": 1}})
            raise AssertionError(f"unexpected call: {url}")

        request.side_effect = responder
        # Should not raise even though page 2 errored.
        result = await tools["list_shifts"](
            from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z"
        )
        # Got a normal-shaped response, not a tool error.
        assert "error" not in result or result.get("error") is not True

    async def test_does_not_fetch_additional_pages_when_first_page_is_short(self):
        """A short first page (<100 items) must NOT trigger any followup
        fetches. Preserves the legacy termination signal."""
        tools, request = self._register()

        def responder(method, url, params=None, **_):
            if url == "/v1/users":
                # Short page → no need to fan out, even if meta lies.
                return self._ok(self._resource("users", 3, total_pages=5))
            if url in ("/v1/schedules", "/v1/teams"):
                return self._ok({"data": [], "meta": {"total_pages": 1}})
            if url == "/v1/shifts":
                return self._ok({"data": [], "included": [], "meta": {"total_pages": 1}})
            raise AssertionError(f"unexpected call: {url}")

        request.side_effect = responder
        await tools["list_shifts"](from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z")

        users_calls = [c for c in request.call_args_list if c.args[1] == "/v1/users"]
        assert len(users_calls) == 1

    async def test_caches_lookup_results_across_calls(self):
        """Second call within the cache TTL must not re-fetch any of the
        three resources."""
        tools, request = self._register()

        def responder(method, url, params=None, **_):
            if url in ("/v1/users", "/v1/schedules", "/v1/teams"):
                return self._ok({"data": [], "meta": {"total_pages": 1}})
            if url == "/v1/shifts":
                return self._ok({"data": [], "included": [], "meta": {"total_pages": 1}})
            raise AssertionError(f"unexpected call: {url}")

        request.side_effect = responder

        await tools["list_shifts"](from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z")
        first_lookup_calls = sum(
            1
            for c in request.call_args_list
            if c.args[1] in ("/v1/users", "/v1/schedules", "/v1/teams")
        )

        await tools["list_shifts"](from_date="2026-02-09T00:00:00Z", to_date="2026-02-12T00:00:00Z")
        total_lookup_calls = sum(
            1
            for c in request.call_args_list
            if c.args[1] in ("/v1/users", "/v1/schedules", "/v1/teams")
        )

        # Three resources fetched once on the first call, zero on the second.
        assert first_lookup_calls == 3
        assert total_lookup_calls == 3
