"""
Unit tests for custom MCP tool functions.

Tests cover:
- list_incidents and search_incidents function logic
- scoped incident update tool behavior
- Parameter validation and defaults
- Pagination handling (single page vs multi-page)
- Error handling and response formatting
"""

from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from rootly_mcp_server.server import DEFAULT_ALLOWED_PATHS, create_rootly_mcp_server
from rootly_mcp_server.server_defaults import _generate_recommendation
from rootly_mcp_server.tools.incidents import INCIDENT_LIST_FIELDS, register_incident_tools
from rootly_mcp_server.tools.resources import register_resource_handlers


class FakeMCP:
    """Small tool registry used for direct custom tool testing."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.resources: dict[str, Any] = {}

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator

    def resource(self, uri_template: str, **_: Any):
        def decorator(fn):
            self.resources[uri_template] = fn
            return fn

        return decorator


class FakeMCPError:
    """Minimal error helper for custom tool tests."""

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        return (exception.__class__.__name__, str(exception))

    @staticmethod
    def tool_error(message: str, error_type: str) -> dict[str, Any]:
        return {"error": True, "error_type": error_type, "message": message}


@pytest.mark.unit
class TestSearchIncidentsIntegration:
    """Test the search_incidents tool integration with the server."""

    def test_search_incidents_tool_availability(self):
        """Test that search_incidents tool is available in server."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {"/incidents": {"get": {"operationId": "listIncidents"}}},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()

            # Verify server was created successfully
            assert server is not None
            assert hasattr(server, "list_tools")

    def test_custom_tool_registration(self):
        """Test that custom tools are properly registered."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()

            # Server should have been created with custom tools
            assert server is not None


@pytest.mark.unit
class TestDefaultConfiguration:
    """Test default configuration and constants."""

    def test_default_allowed_paths_exist(self):
        """Test that default allowed paths are defined."""
        assert DEFAULT_ALLOWED_PATHS is not None
        assert isinstance(DEFAULT_ALLOWED_PATHS, list)
        assert len(DEFAULT_ALLOWED_PATHS) > 0

        # Verify some expected paths are included
        path_strings = str(DEFAULT_ALLOWED_PATHS)
        assert "incidents" in path_strings

    def test_server_creation_uses_defaults(self):
        """Test that server creation works with default paths."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()

            # Server should be created successfully with defaults
            assert server is not None

    def test_oncall_endpoints_in_defaults(self):
        """Test that on-call endpoints are included in default paths."""
        path_strings = [p.lower() for p in DEFAULT_ALLOWED_PATHS]

        # Verify on-call related paths are included
        assert any("schedule" in p for p in path_strings)
        assert any("shift" in p for p in path_strings)
        assert any("on_call" in p for p in path_strings)


@pytest.mark.unit
class TestScopedIncidentUpdateTool:
    """Test the scoped custom updateIncident tool."""

    def _register_tools(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=True,
        )
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_update_incident_tool_is_registered_with_customer_facing_name(self):
        tools, _ = self._register_tools()

        assert "createIncident" in tools
        assert "updateIncident" in tools
        assert "getIncident" in tools

    @pytest.mark.asyncio
    async def test_write_tools_are_hidden_when_write_gating_is_disabled(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )

        assert "getIncident" in mcp.tools
        assert "createIncident" not in mcp.tools
        assert "updateIncident" not in mcp.tools

    @pytest.mark.asyncio
    async def test_create_incident_sends_only_allowed_fields(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "id": "inc-123",
                "type": "incidents",
                "attributes": {
                    "title": "Database latency spike",
                    "summary": "Primary API requests timing out",
                    "severity_id": "sev-1",
                    "service_ids": ["svc-1", "svc-2"],
                    "group_ids": ["team-1", "team-2"],
                    "environment_ids": ["env-1"],
                    "incident_type_ids": ["type-1"],
                },
            }
        }
        request.return_value = response

        result = await tools["createIncident"](
            title="  Database latency spike  ",
            summary=" Primary API requests timing out ",
            severity_id=" sev-1 ",
            service_ids="svc-1, svc-2",
            team_ids="team-1, team-2",
            environment_ids="env-1",
            incident_type_ids="type-1",
        )

        request.assert_awaited_once_with(
            "POST",
            "/v1/incidents",
            json={
                "data": {
                    "type": "incidents",
                    "attributes": {
                        "title": "Database latency spike",
                        "summary": "Primary API requests timing out",
                        "severity_id": "sev-1",
                        "service_ids": ["svc-1", "svc-2"],
                        "group_ids": ["team-1", "team-2"],
                        "environment_ids": ["env-1"],
                        "incident_type_ids": ["type-1"],
                    },
                }
            },
        )
        assert result["data"]["id"] == "inc-123"
        assert result["data"]["attributes"]["title"] == "Database latency spike"

    @pytest.mark.asyncio
    async def test_create_incident_requires_title_or_summary(self):
        tools, request = self._register_tools()

        result = await tools["createIncident"](title="   ", summary=None)

        request.assert_not_called()
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Must provide at least one of title or summary" in result["message"]

    @pytest.mark.asyncio
    async def test_get_incident_fetches_single_incident(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "id": "inc-123",
                "type": "incidents",
                "attributes": {
                    "summary": "Updated PIR summary",
                    "retrospective_progress_status": "active",
                },
            }
        }
        request.return_value = response

        result = await tools["getIncident"](incident_id="inc-123")

        request.assert_awaited_once_with("GET", "/v1/incidents/inc-123")
        assert result["data"]["id"] == "inc-123"
        assert result["data"]["attributes"]["retrospective_progress_status"] == "active"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("incident_reference"),
        [
            "4460",
            "#4460",
            "INC-4460",
        ],
    )
    async def test_get_incident_resolves_sequential_references(self, incident_reference: str):
        tools, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4460},
                }
            ],
            "meta": {
                "current_page": 1,
                "next_page": None,
                "prev_page": None,
                "total_pages": 1,
                "total_count": 1,
            },
        }

        incident_response = Mock()
        incident_response.raise_for_status.return_value = None
        incident_response.json.return_value = {
            "data": {
                "id": "11111111-1111-4111-8111-111111111111",
                "type": "incidents",
                "attributes": {
                    "summary": "Updated PIR summary",
                    "retrospective_progress_status": "active",
                },
            }
        }

        request.side_effect = [list_response, incident_response]

        result = await tools["getIncident"](incident_id=incident_reference)

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "fields[incidents]": "id,sequential_id",
                    "include": "",
                    "sort": "-created_at",
                },
            ),
            call("GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"),
        ]
        assert result["data"]["id"] == "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_get_incident_returns_clear_error_for_unknown_sequential_reference(self):
        tools, request = self._register_tools()

        first_page_response = Mock()
        first_page_response.raise_for_status.return_value = None
        first_page_response.json.return_value = {
            "data": [
                {
                    "id": "uuid-page-1",
                    "type": "incidents",
                    "attributes": {"sequential_id": 5000},
                },
                {
                    "id": "uuid-page-1-last",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4901},
                },
            ],
            "meta": {
                "current_page": 1,
                "next_page": 2,
                "prev_page": None,
                "total_pages": 8,
                "total_count": 800,
            },
        }

        mid_page_response = Mock()
        mid_page_response.raise_for_status.return_value = None
        mid_page_response.json.return_value = {
            "data": [
                {
                    "id": "uuid-page-4",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4700},
                },
                {
                    "id": "uuid-page-4-last",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4601},
                },
            ],
            "meta": {
                "current_page": 4,
                "next_page": 5,
                "prev_page": 3,
                "total_pages": 8,
                "total_count": 800,
            },
        }

        target_range_response = Mock()
        target_range_response.raise_for_status.return_value = None
        target_range_response.json.return_value = {
            "data": [
                {
                    "id": "uuid-page-6",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4500},
                },
                {
                    "id": "uuid-page-6-last",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4401},
                },
            ],
            "meta": {
                "current_page": 6,
                "next_page": 7,
                "prev_page": 5,
                "total_pages": 8,
                "total_count": 800,
            },
        }

        request.side_effect = [first_page_response, mid_page_response, target_range_response]

        result = await tools["getIncident"](incident_id="4460")

        assert result["error"] is True
        assert result["error_type"] == "not_found"
        assert "INC-4460" in result["message"]
        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "fields[incidents]": "id,sequential_id",
                    "include": "",
                    "sort": "-created_at",
                },
            ),
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 100,
                    "page[number]": 4,
                    "fields[incidents]": "id,sequential_id",
                    "include": "",
                    "sort": "-created_at",
                },
            ),
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 100,
                    "page[number]": 6,
                    "fields[incidents]": "id,sequential_id",
                    "include": "",
                    "sort": "-created_at",
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_update_incident_sends_only_allowed_fields(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "id": "inc-123",
                "type": "incidents",
                "attributes": {
                    "summary": "Updated PIR summary",
                    "retrospective_progress_status": "active",
                    "title": "Should stay untouched on server",
                },
            }
        }
        request.return_value = response

        result = await tools["updateIncident"](
            incident_id="inc-123",
            retrospective_progress_status="active",
            summary="Updated PIR summary",
        )

        request.assert_awaited_once_with(
            "PUT",
            "/v1/incidents/inc-123",
            json={
                "data": {
                    "type": "incidents",
                    "attributes": {
                        "retrospective_progress_status": "active",
                        "summary": "Updated PIR summary",
                    },
                }
            },
        )
        assert result["data"]["attributes"]["retrospective_progress_status"] == "active"
        assert result["data"]["attributes"]["summary"] == "Updated PIR summary"

    @pytest.mark.asyncio
    async def test_update_incident_resolves_sequential_reference(self):
        tools, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4460},
                }
            ],
            "meta": {"current_page": 1, "next_page": None, "prev_page": None, "total_pages": 1},
        }

        update_response = Mock()
        update_response.raise_for_status.return_value = None
        update_response.json.return_value = {
            "data": {
                "id": "11111111-1111-4111-8111-111111111111",
                "type": "incidents",
                "attributes": {
                    "summary": "Updated PIR summary",
                    "retrospective_progress_status": "active",
                },
            }
        }

        request.side_effect = [list_response, update_response]

        result = await tools["updateIncident"](
            incident_id="#4460",
            retrospective_progress_status="active",
        )

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "fields[incidents]": "id,sequential_id",
                    "include": "",
                    "sort": "-created_at",
                },
            ),
            call(
                "PUT",
                "/v1/incidents/11111111-1111-4111-8111-111111111111",
                json={
                    "data": {
                        "type": "incidents",
                        "attributes": {
                            "retrospective_progress_status": "active",
                        },
                    }
                },
            ),
        ]
        assert result["data"]["id"] == "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_update_incident_allows_skipped_status(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "id": "inc-123",
                "type": "incidents",
                "attributes": {
                    "retrospective_progress_status": "skipped",
                },
            }
        }
        request.return_value = response

        result = await tools["updateIncident"](
            incident_id="inc-123",
            retrospective_progress_status="skipped",
        )

        request.assert_awaited_once_with(
            "PUT",
            "/v1/incidents/inc-123",
            json={
                "data": {
                    "type": "incidents",
                    "attributes": {
                        "retrospective_progress_status": "skipped",
                    },
                }
            },
        )
        assert result["data"]["attributes"]["retrospective_progress_status"] == "skipped"

    @pytest.mark.asyncio
    async def test_update_incident_requires_at_least_one_supported_field(self):
        tools, request = self._register_tools()

        result = await tools["updateIncident"](incident_id="inc-123")

        request.assert_not_called()
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Must provide at least one" in result["message"]

    @pytest.mark.asyncio
    async def test_update_incident_rejects_invalid_retrospective_status(self):
        tools, request = self._register_tools()

        result = await tools["updateIncident"](
            incident_id="inc-123",
            retrospective_progress_status="paused",
        )

        request.assert_not_called()
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "retrospective_progress_status must be one of" in result["message"]

    @pytest.mark.asyncio
    async def test_search_incidents_requests_retrospective_progress_status_field(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": []}
        request.return_value = response

        await tools["search_incidents"](query="pir", page_size=5, page_number=1)

        request.assert_awaited_once()
        await_args = request.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert "retrospective_progress_status" in kwargs["params"]["fields[incidents]"]


@pytest.mark.unit
class TestStructuredListIncidentsTool:
    """Test the structured list_incidents tool."""

    def _register_tools(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
        )
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_list_incidents_tool_is_registered(self):
        tools, _ = self._register_tools()

        assert "list_incidents" in tools

    @pytest.mark.asyncio
    async def test_list_incidents_passes_structured_filters_and_returns_compact_results(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {
                    "id": "inc-123",
                    "type": "incidents",
                    "attributes": {
                        "sequential_id": 829,
                        "title": "Database timeout in production",
                        "summary": "Primary database connection pool exhausted",
                        "status": "resolved",
                        "severity": {
                            "data": {
                                "attributes": {
                                    "name": "Critical",
                                    "slug": "critical",
                                }
                            }
                        },
                        "started_at": "2026-04-10T15:00:00Z",
                        "resolved_at": "2026-04-10T15:45:00Z",
                        "created_at": "2026-04-10T15:00:10Z",
                        "updated_at": "2026-04-10T15:46:00Z",
                        "retrospective_progress_status": "active",
                        "url": "https://rootly.com/account/incidents/inc-123",
                    },
                }
            ],
            "meta": {
                "current_page": 2,
                "next_page": 3,
                "prev_page": 1,
                "total_pages": 4,
                "total_count": 70,
            },
        }
        request.return_value = response

        result = await tools["list_incidents"](
            query="database timeout",
            team_ids="123,456",
            service_ids="svc-1",
            severity="critical",
            status="resolved",
            started_after="2026-04-01T00:00:00Z",
            started_before="2026-04-13T23:59:59Z",
            custom_field_selected_option_ids="opt-1,opt-2",
            sort="-updated_at",
            page_size=25,
            page_number=2,
        )

        request.assert_awaited_once_with(
            "GET",
            "/v1/incidents",
            params={
                "page[size]": 25,
                "page[number]": 2,
                "fields[incidents]": INCIDENT_LIST_FIELDS,
                "include": "",
                "sort": "-updated_at",
                "filter[search]": "database timeout",
                "filter[team_ids]": "123,456",
                "filter[service_ids]": "svc-1",
                "filter[severity]": "critical",
                "filter[status]": "resolved",
                "filter[started_at][gte]": "2026-04-01T00:00:00Z",
                "filter[started_at][lte]": "2026-04-13T23:59:59Z",
                "filter[custom_field_selected_option_ids]": "opt-1,opt-2",
            },
        )

        assert result["returned_incidents"] == 1
        assert result["pagination"]["has_more"] is True
        assert result["pagination"]["total_count"] == 70
        assert result["filters"]["team_ids"] == "123,456"
        assert result["incidents"] == [
            {
                "incident_id": "inc-123",
                "incident_number": "INC-829",
                "title": "Database timeout in production",
                "summary": "Primary database connection pool exhausted",
                "status": "resolved",
                "severity": "critical",
                "started_at": "2026-04-10T15:00:00Z",
                "resolved_at": "2026-04-10T15:45:00Z",
                "created_at": "2026-04-10T15:00:10Z",
                "updated_at": "2026-04-10T15:46:00Z",
                "retrospective_progress_status": "active",
                "url": "https://rootly.com/account/incidents/inc-123",
            }
        ]

    @pytest.mark.asyncio
    async def test_list_incidents_resolves_team_names_to_ids(self):
        tools, request = self._register_tools()

        slug_response = Mock()
        slug_response.raise_for_status.return_value = None
        slug_response.json.return_value = {"data": []}

        name_response = Mock()
        name_response.raise_for_status.return_value = None
        name_response.json.return_value = {
            "data": [
                {
                    "id": "team-123",
                    "type": "teams",
                    "attributes": {
                        "name": "Infrastructure",
                        "slug": "infrastructure",
                    },
                }
            ]
        }

        incidents_response = Mock()
        incidents_response.raise_for_status.return_value = None
        incidents_response.json.return_value = {
            "data": [],
            "meta": {"current_page": 1, "next_page": None, "total_pages": 1, "total_count": 0},
        }

        request.side_effect = [slug_response, name_response, incidents_response]

        result = await tools["list_incidents"](
            teams="Infrastructure",
            page_size=10,
            page_number=1,
        )

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/teams",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "filter[slug]": "Infrastructure",
                },
            ),
            call(
                "GET",
                "/v1/teams",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "filter[name]": "Infrastructure",
                },
            ),
            call(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 10,
                    "page[number]": 1,
                    "fields[incidents]": INCIDENT_LIST_FIELDS,
                    "include": "",
                    "sort": "-created_at",
                    "filter[team_ids]": "team-123",
                },
            ),
        ]
        assert result["filters"]["teams"] == "Infrastructure"
        assert result["filters"]["resolved_team_ids"] == "team-123"
        assert result["filters"]["resolved_team_lookup"] == {"Infrastructure": "team-123"}

    @pytest.mark.asyncio
    async def test_list_incidents_returns_validation_error_when_team_name_cannot_be_resolved(self):
        tools, request = self._register_tools()

        slug_response = Mock()
        slug_response.raise_for_status.return_value = None
        slug_response.json.return_value = {"data": []}

        name_response = Mock()
        name_response.raise_for_status.return_value = None
        name_response.json.return_value = {"data": []}

        request.side_effect = [slug_response, name_response]

        result = await tools["list_incidents"](teams="Infrastructure")

        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Could not resolve team names/slugs" in result["message"]


@pytest.mark.unit
class TestCollectIncidentsTool:
    """Test the bounded bulk incident collection tool."""

    def _register_tools(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
        )
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_collect_incidents_tool_is_registered(self):
        tools, _ = self._register_tools()

        assert "collect_incidents" in tools

    @pytest.mark.asyncio
    async def test_collect_incidents_resolves_team_names_and_collects_across_pages(self):
        tools, request = self._register_tools()

        slug_response = Mock()
        slug_response.raise_for_status.return_value = None
        slug_response.json.return_value = {"data": []}

        name_response = Mock()
        name_response.raise_for_status.return_value = None
        name_response.json.return_value = {
            "data": [
                {
                    "id": "team-123",
                    "type": "teams",
                    "attributes": {
                        "name": "Infrastructure",
                        "slug": "infrastructure",
                    },
                }
            ]
        }

        incidents_page_one = Mock()
        incidents_page_one.raise_for_status.return_value = None
        incidents_page_one.json.return_value = {
            "data": [
                {
                    "id": "inc-1",
                    "type": "incidents",
                    "attributes": {
                        "sequential_id": 101,
                        "title": "Database saturation",
                        "summary": "Primary database maxed out",
                        "status": "resolved",
                        "severity": "critical",
                        "started_at": "2026-04-10T10:00:00Z",
                        "resolved_at": "2026-04-10T10:20:00Z",
                        "created_at": "2026-04-10T10:00:05Z",
                        "updated_at": "2026-04-10T10:21:00Z",
                        "retrospective_progress_status": "active",
                        "url": "https://rootly.com/account/incidents/inc-1",
                    },
                },
                {
                    "id": "inc-2",
                    "type": "incidents",
                    "attributes": {
                        "sequential_id": 102,
                        "title": "Cache cluster degraded",
                        "summary": "Redis failover took too long",
                        "status": "resolved",
                        "severity": "high",
                        "started_at": "2026-04-10T11:00:00Z",
                        "resolved_at": "2026-04-10T11:15:00Z",
                        "created_at": "2026-04-10T11:00:05Z",
                        "updated_at": "2026-04-10T11:16:00Z",
                        "retrospective_progress_status": "not_started",
                        "url": "https://rootly.com/account/incidents/inc-2",
                    },
                },
            ],
            "meta": {
                "current_page": 1,
                "next_page": 2,
                "prev_page": None,
                "total_pages": 3,
                "total_count": 5,
            },
        }

        incidents_page_two = Mock()
        incidents_page_two.raise_for_status.return_value = None
        incidents_page_two.json.return_value = {
            "data": [
                {
                    "id": "inc-3",
                    "type": "incidents",
                    "attributes": {
                        "sequential_id": 103,
                        "title": "Service mesh instability",
                        "summary": "Ingress latency spiked",
                        "status": "resolved",
                        "severity": "medium",
                        "started_at": "2026-04-10T12:00:00Z",
                        "resolved_at": "2026-04-10T12:10:00Z",
                        "created_at": "2026-04-10T12:00:05Z",
                        "updated_at": "2026-04-10T12:11:00Z",
                        "retrospective_progress_status": "completed",
                        "url": "https://rootly.com/account/incidents/inc-3",
                    },
                },
                {
                    "id": "inc-4",
                    "type": "incidents",
                    "attributes": {
                        "sequential_id": 104,
                        "title": "Background job backlog",
                        "summary": "Queue depth kept rising",
                        "status": "investigating",
                        "severity": "medium",
                        "started_at": "2026-04-10T13:00:00Z",
                        "resolved_at": None,
                        "created_at": "2026-04-10T13:00:05Z",
                        "updated_at": "2026-04-10T13:05:00Z",
                        "retrospective_progress_status": "not_started",
                        "url": "https://rootly.com/account/incidents/inc-4",
                    },
                },
            ],
            "meta": {
                "current_page": 2,
                "next_page": 3,
                "prev_page": 1,
                "total_pages": 3,
                "total_count": 5,
            },
        }

        request.side_effect = [slug_response, name_response, incidents_page_one, incidents_page_two]

        result = await tools["collect_incidents"](
            teams="Infrastructure",
            max_results=3,
            batch_size=2,
        )

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/teams",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "filter[slug]": "Infrastructure",
                },
            ),
            call(
                "GET",
                "/v1/teams",
                params={
                    "page[size]": 100,
                    "page[number]": 1,
                    "filter[name]": "Infrastructure",
                },
            ),
            call(
                "GET",
                "/v1/incidents",
                params={
                    "fields[incidents]": INCIDENT_LIST_FIELDS,
                    "include": "",
                    "sort": "-created_at",
                    "filter[team_ids]": "team-123",
                    "page[size]": 2,
                    "page[number]": 1,
                },
            ),
            call(
                "GET",
                "/v1/incidents",
                params={
                    "fields[incidents]": INCIDENT_LIST_FIELDS,
                    "include": "",
                    "sort": "-created_at",
                    "filter[team_ids]": "team-123",
                    "page[size]": 2,
                    "page[number]": 2,
                },
            ),
        ]

        assert result["returned_incidents"] == 3
        assert result["collection"] == {
            "max_results": 3,
            "batch_size": 2,
            "pages_fetched": 2,
            "total_matching_count": 5,
            "results_truncated": True,
        }
        assert result["filters"]["teams"] == "Infrastructure"
        assert result["filters"]["resolved_team_lookup"] == {"Infrastructure": "team-123"}
        assert [incident["incident_number"] for incident in result["incidents"]] == [
            "INC-101",
            "INC-102",
            "INC-103",
        ]


@pytest.mark.unit
class TestIncidentReferenceResolutionAcrossTools:
    def _register_tools(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=True,
        )
        register_resource_handlers(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
        )
        return mcp, request

    @pytest.mark.asyncio
    async def test_find_related_incidents_resolves_sequential_reference(self):
        mcp, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4460},
                }
            ],
            "meta": {"current_page": 1, "next_page": None, "prev_page": None, "total_pages": 1},
        }

        incident_response = Mock()
        incident_response.raise_for_status.return_value = None
        incident_response.json.return_value = {
            "data": {
                "id": "11111111-1111-4111-8111-111111111111",
                "attributes": {
                    "title": "Database timeout",
                    "summary": "Connection pool exhausted",
                },
            }
        }

        historical_response = Mock()
        historical_response.raise_for_status.return_value = None
        historical_response.json.return_value = {
            "data": [
                {
                    "id": "other-1",
                    "attributes": {
                        "title": "Database timeout",
                        "summary": "Connection pool exhausted",
                        "status": "resolved",
                        "created_at": "2026-04-01T00:00:00Z",
                        "url": "https://example.com/incidents/other-1",
                    },
                }
            ]
        }

        request.side_effect = [list_response, incident_response, historical_response]

        result = await mcp.tools["find_related_incidents"](incident_id="INC-4460")

        assert request.await_args_list[1] == call(
            "GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"
        )
        assert result["target_incident"]["resolved_incident_id"] == (
            "11111111-1111-4111-8111-111111111111"
        )

    @pytest.mark.asyncio
    async def test_suggest_solutions_resolves_sequential_reference(self):
        mcp, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4460},
                }
            ],
            "meta": {"current_page": 1, "next_page": None, "prev_page": None, "total_pages": 1},
        }

        incident_response = Mock()
        incident_response.raise_for_status.return_value = None
        incident_response.json.return_value = {
            "data": {
                "id": "11111111-1111-4111-8111-111111111111",
                "attributes": {
                    "title": "Database timeout",
                    "summary": "Connection pool exhausted",
                },
            }
        }

        historical_response = Mock()
        historical_response.raise_for_status.return_value = None
        historical_response.json.return_value = {
            "data": [
                {
                    "id": "other-1",
                    "attributes": {
                        "title": "Database timeout",
                        "summary": "Connection pool exhausted",
                        "status": "resolved",
                        "created_at": "2026-04-01T00:00:00Z",
                        "resolved_at": "2026-04-01T01:00:00Z",
                    },
                }
            ]
        }

        request.side_effect = [list_response, incident_response, historical_response]

        result = await mcp.tools["suggest_solutions"](incident_id="4460")

        assert request.await_args_list[1] == call(
            "GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"
        )
        assert result["target_incident"]["resolved_incident_id"] == (
            "11111111-1111-4111-8111-111111111111"
        )

    @pytest.mark.asyncio
    async def test_incident_resource_resolves_sequential_reference(self):
        mcp, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "type": "incidents",
                    "attributes": {"sequential_id": 4460},
                }
            ],
            "meta": {"current_page": 1, "next_page": None, "prev_page": None, "total_pages": 1},
        }

        incident_response = Mock()
        incident_response.raise_for_status.return_value = None
        incident_response.json.return_value = {
            "data": {
                "id": "11111111-1111-4111-8111-111111111111",
                "attributes": {
                    "title": "Database timeout",
                    "status": "resolved",
                    "severity": "critical",
                    "created_at": "2026-04-01T00:00:00Z",
                    "updated_at": "2026-04-01T00:05:00Z",
                    "summary": "Connection pool exhausted",
                    "url": "https://example.com/incidents/4460",
                },
            }
        }

        request.side_effect = [list_response, incident_response]

        result = await mcp.resources["incident://{incident_id}"]("#4460")

        assert request.await_args_list[1] == call(
            "GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"
        )
        assert "Resolved Incident ID: 11111111-1111-4111-8111-111111111111" in result["text"]
