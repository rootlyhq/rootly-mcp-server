"""
Unit tests for custom MCP tool functions.

Tests cover:
- list_incidents and search_incidents function logic
- scoped incident update tool behavior
- Parameter validation and defaults
- Pagination handling (single page vs multi-page)
- Error handling and response formatting
"""

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from rootly_mcp_server.mcp_error import MCPError
from rootly_mcp_server.server import DEFAULT_ALLOWED_PATHS, create_rootly_mcp_server
from rootly_mcp_server.server_defaults import _generate_recommendation
from rootly_mcp_server.tools.incidents import (
    INCIDENT_LIST_FIELDS,
    _augment_pagination_error,
    _normalize_incident_reference,
    _summarize_incident_record,
    register_incident_tools,
)
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
    """Test the scoped custom update_incident tool."""

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

        assert "create_incident" in tools
        assert "update_incident" in tools
        assert "get_incident" in tools

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

        assert "get_incident" in mcp.tools
        assert "create_incident" not in mcp.tools
        assert "update_incident" not in mcp.tools

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

        result = await tools["create_incident"](
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

        result = await tools["create_incident"](title="   ", summary=None)

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

        result = await tools["get_incident"](incident_id="11111111-1111-4111-8111-111111111111")

        request.assert_awaited_once_with(
            "GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"
        )
        assert result["data"]["id"] == "inc-123"
        assert result["data"]["attributes"]["retrospective_progress_status"] == "active"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("incident_reference"),
        [
            "4460",
            "#4460",
            "INC-4460",
            "inc-4460",
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

        result = await tools["get_incident"](incident_id=incident_reference)

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "filter[sequential_id]": 4460,
                    "page[size]": 1,
                    "fields[incidents]": "id,sequential_id",
                },
            ),
            call("GET", "/v1/incidents/11111111-1111-4111-8111-111111111111"),
        ]
        assert result["data"]["id"] == "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_get_incident_returns_clear_error_for_unknown_sequential_reference(self):
        tools, request = self._register_tools()

        # The filter[sequential_id] lookup returns no match.
        empty_response = Mock()
        empty_response.raise_for_status.return_value = None
        empty_response.json.return_value = {
            "data": [],
            "meta": {"current_page": 1, "total_pages": 1, "total_count": 0},
        }

        request.side_effect = [empty_response]

        result = await tools["get_incident"](incident_id="4460")

        assert result["error"] is True
        assert result["error_type"] == "not_found"
        assert "INC-4460" in result["message"]
        # A single direct filter lookup — no page walking (deep pagination is
        # rejected by the API with a 400).
        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "filter[sequential_id]": 4460,
                    "page[size]": 1,
                    "fields[incidents]": "id,sequential_id",
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_get_incident_rejects_sequential_mismatch_from_ignored_filter(self):
        # Defensive: if the API ever ignored filter[sequential_id] and returned a
        # non-matching incident, we must not resolve to the wrong UUID.
        tools, request = self._register_tools()

        mismatch_response = Mock()
        mismatch_response.raise_for_status.return_value = None
        mismatch_response.json.return_value = {
            "data": [
                {
                    "id": "99999999-9999-4999-8999-999999999999",
                    "type": "incidents",
                    "attributes": {"sequential_id": 9999},
                }
            ],
            "meta": {"current_page": 1, "total_pages": 1, "total_count": 1},
        }
        request.side_effect = [mismatch_response]

        result = await tools["get_incident"](incident_id="4460")

        assert result["error"] is True
        assert result["error_type"] == "not_found"
        assert "INC-4460" in result["message"]
        # Only the filter lookup happened; no incident fetch against a wrong UUID.
        assert request.await_count == 1

    @pytest.mark.asyncio
    async def test_list_incident_roles_tool_is_registered(self):
        tools, _ = self._register_tools()
        assert "list_incident_roles" in tools

    @pytest.mark.asyncio
    async def test_list_incident_roles_returns_flattened_assignments(self):
        """Happy path: incident_role_assignments in `included` get flattened to a table."""
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "id": "inc-uuid",
                "type": "incidents",
                "relationships": {
                    "roles": {
                        "data": [
                            {"id": "assign-1", "type": "incident_role_assignments"},
                            {"id": "assign-2", "type": "incident_role_assignments"},
                        ]
                    }
                },
            },
            "included": [
                {
                    "id": "assign-1",
                    "type": "incident_role_assignments",
                    "attributes": {
                        "incident_role": {
                            "data": {
                                "id": "role-commander",
                                "type": "incident_roles",
                                "attributes": {
                                    "slug": "commander",
                                    "name": "Commander",
                                    "summary": "Incident Commander",
                                },
                            }
                        },
                        "user": {
                            "data": {
                                "id": "109673",
                                "type": "users",
                                "attributes": {
                                    "email": "spencer.cheng@rootly.com",
                                    "full_name": "Spencer Cheng",
                                },
                            }
                        },
                        "created_at": "2026-06-05T09:57:17.213-07:00",
                        "updated_at": "2026-06-05T09:57:17.819-07:00",
                    },
                },
                {
                    "id": "assign-2",
                    "type": "incident_role_assignments",
                    "attributes": {
                        "incident_role": {
                            "data": {
                                "id": "role-postmortem",
                                "type": "incident_roles",
                                "attributes": {
                                    "slug": "postmortem-owner",
                                    # Trailing space mirrors real API payloads — must be stripped.
                                    "name": "Postmortem Owner ",
                                    "summary": "Postmortem Owner",
                                },
                            }
                        },
                        # Unassigned role: API returns user: None
                        "user": None,
                        "created_at": "2026-06-05T09:57:17.244-07:00",
                        "updated_at": "2026-06-05T09:57:17.244-07:00",
                    },
                },
            ],
        }
        request.return_value = response

        result = await tools["list_incident_roles"](incident_id="inc-uuid")

        request.assert_awaited_once_with(
            "GET", "/v1/incidents/inc-uuid", params={"include": "roles"}
        )
        assert result["meta"] == {
            "incident_id": "inc-uuid",
            "total_count": 2,
            "assigned_count": 1,
            "unassigned_count": 1,
        }
        assignments = result["data"]
        assert len(assignments) == 2

        commander = assignments[0]
        assert commander["role_slug"] == "commander"
        # Trailing space stripped.
        assert commander["role_name"] == "Commander"
        assert commander["user_id"] == "109673"
        assert commander["user_email"] == "spencer.cheng@rootly.com"
        assert commander["user_name"] == "Spencer Cheng"
        assert commander["assigned_at"] == "2026-06-05T09:57:17.213-07:00"

        postmortem = assignments[1]
        assert postmortem["role_slug"] == "postmortem-owner"
        assert postmortem["role_name"] == "Postmortem Owner"
        assert postmortem["user_id"] is None
        assert postmortem["user_email"] is None
        assert postmortem["user_name"] is None

    @pytest.mark.asyncio
    async def test_list_incident_roles_returns_empty_when_no_included(self):
        """Incident with no roles at all → empty data + zero counts, not an error."""
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {"id": "inc-empty", "type": "incidents", "attributes": {}},
        }
        request.return_value = response

        result = await tools["list_incident_roles"](incident_id="inc-empty")

        assert result["data"] == []
        assert result["meta"]["total_count"] == 0
        assert result["meta"]["assigned_count"] == 0
        assert result["meta"]["unassigned_count"] == 0

    @pytest.mark.asyncio
    async def test_list_incident_roles_resolves_sequential_reference(self):
        """`INC-4460` should be resolved to a UUID first, then include=roles fetched."""
        tools, request = self._register_tools()

        list_response = Mock()
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = {
            "data": [
                {
                    "id": "22222222-2222-4222-8222-222222222222",
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
        roles_response = Mock()
        roles_response.raise_for_status.return_value = None
        roles_response.json.return_value = {
            "data": {"id": "22222222-2222-4222-8222-222222222222", "type": "incidents"},
            "included": [],
        }
        request.side_effect = [list_response, roles_response]

        result = await tools["list_incident_roles"](incident_id="INC-4460")

        # Second call must be the include=roles fetch against the resolved UUID.
        assert request.await_args_list[-1] == call(
            "GET",
            "/v1/incidents/22222222-2222-4222-8222-222222222222",
            params={"include": "roles"},
        )
        assert result["meta"]["incident_id"] == "22222222-2222-4222-8222-222222222222"

    @pytest.mark.asyncio
    async def test_list_incident_roles_returns_validation_error_for_blank_reference(self):
        tools, request = self._register_tools()

        result = await tools["list_incident_roles"](incident_id="   ")

        # Specifically the ValueError → validation_error branch must be hit.
        # A loose `"error" in result` check would also pass for unrelated error
        # branches (e.g. a network failure) and silently mask the wrong code path.
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Incident reference is required" in result["message"]
        # No upstream HTTP call should have been attempted with a blank reference.
        request.assert_not_awaited()

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

        result = await tools["update_incident"](
            incident_id="11111111-1111-4111-8111-111111111111",
            retrospective_progress_status="active",
            summary="Updated PIR summary",
        )

        request.assert_awaited_once_with(
            "PUT",
            "/v1/incidents/11111111-1111-4111-8111-111111111111",
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

        result = await tools["update_incident"](
            incident_id="#4460",
            retrospective_progress_status="active",
        )

        assert request.await_args_list == [
            call(
                "GET",
                "/v1/incidents",
                params={
                    "filter[sequential_id]": 4460,
                    "page[size]": 1,
                    "fields[incidents]": "id,sequential_id",
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

        result = await tools["update_incident"](
            incident_id="11111111-1111-4111-8111-111111111111",
            retrospective_progress_status="skipped",
        )

        request.assert_awaited_once_with(
            "PUT",
            "/v1/incidents/11111111-1111-4111-8111-111111111111",
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

        result = await tools["update_incident"](incident_id="inc-123")

        request.assert_not_called()
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Must provide at least one" in result["message"]

    @pytest.mark.asyncio
    async def test_update_incident_rejects_invalid_retrospective_status(self):
        tools, request = self._register_tools()

        result = await tools["update_incident"](
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
            batch_size=10,
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
                    "page[size]": 10,
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
                    "page[size]": 10,
                    "page[number]": 2,
                },
            ),
        ]

        assert result["returned_incidents"] == 3
        assert result["collection"] == {
            "max_results": 3,
            "batch_size": 10,
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


@pytest.mark.unit
class TestPureHelpers:
    """Direct tests for module-level incident helpers."""

    def test_summarize_incident_record_tolerates_null_attributes(self):
        # API returning `"attributes": null` (present but null) must not crash.
        summary = _summarize_incident_record({"id": "abc", "attributes": None})
        assert summary["incident_id"] == "abc"
        assert summary["title"] is None
        assert summary["incident_number"] is None

    @pytest.mark.parametrize(
        "reference",
        ["../../v1/users", "foo/bar", "a b", "with\\slash", "..", "seg/../seg"],
    )
    def test_normalize_incident_reference_rejects_path_altering_direct_refs(self, reference):
        with pytest.raises(ValueError):
            _normalize_incident_reference(reference)

    def test_normalize_incident_reference_allows_plain_slug(self):
        assert _normalize_incident_reference("database-outage") == ("direct", "database-outage")

    def test_augment_pagination_error_appends_hint_on_deep_client_error(self):
        result = _augment_pagination_error(
            {"error": True, "error_type": "client_error", "message": "Client error: 400"},
            page_number=250,
        )
        assert "collect_incidents" in result["message"]

    def test_augment_pagination_error_noop_on_first_page(self):
        original = {"error": True, "error_type": "client_error", "message": "Client error: 400"}
        result = _augment_pagination_error(dict(original), page_number=1)
        assert result["message"] == original["message"]

    def test_augment_pagination_error_noop_on_non_client_error(self):
        original = {"error": True, "error_type": "server_error", "message": "Server error: 500"}
        result = _augment_pagination_error(dict(original), page_number=250)
        assert result["message"] == original["message"]


@pytest.mark.unit
class TestIncidentToolsHardening:
    """Error taxonomy, input hardening, and pagination-signal behavior.

    Uses the real MCPError so error_type categorization (e.g. client_error for
    4xx) matches production rather than the minimal FakeMCPError double.
    """

    def _register_tools(self):
        mcp = FakeMCP()
        request = AsyncMock()
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=MCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=True,
        )
        return mcp.tools, request

    def _empty_filter_response(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [], "meta": {"total_pages": 1, "total_count": 0}}
        return response

    @pytest.mark.asyncio
    async def test_get_incident_rejects_path_traversal_reference(self):
        tools, request = self._register_tools()

        result = await tools["get_incident"](incident_id="../../v1/users")

        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_incident_maps_unknown_sequential_to_not_found(self):
        tools, request = self._register_tools()
        request.side_effect = [self._empty_filter_response()]

        result = await tools["update_incident"](
            incident_id="4460", retrospective_progress_status="active"
        )

        assert result["error"] is True
        assert result["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_find_related_incidents_maps_unknown_sequential_to_not_found(self):
        mcp_tools, request = self._register_tools()
        request.side_effect = [self._empty_filter_response()]

        result = await mcp_tools["find_related_incidents"](incident_id="4460")

        assert result["error"] is True
        assert result["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_suggest_solutions_maps_unknown_sequential_to_not_found(self):
        mcp_tools, request = self._register_tools()
        request.side_effect = [self._empty_filter_response()]

        result = await mcp_tools["suggest_solutions"](incident_id="4460")

        assert result["error"] is True
        assert result["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_update_incident_treats_whitespace_summary_as_no_field(self):
        tools, request = self._register_tools()

        result = await tools["update_incident"](
            incident_id="11111111-1111-4111-8111-111111111111", summary="   "
        )

        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert "Must provide at least one" in result["message"]
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_incidents_tolerates_null_attributes_record(self):
        tools, request = self._register_tools()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {"id": "null-attrs", "attributes": None},
                {"id": "ok", "attributes": {"title": "Real", "sequential_id": 5}},
            ],
            "meta": {"current_page": 1, "total_pages": 1},
        }
        request.return_value = response

        result = await tools["list_incidents"]()

        assert result["returned_incidents"] == 2
        assert result["incidents"][0]["incident_id"] == "null-attrs"
        assert result["incidents"][1]["incident_number"] == "INC-5"

    @pytest.mark.asyncio
    async def test_list_incidents_appends_pagination_hint_on_deep_client_error(self):
        tools, request = self._register_tools()
        request.side_effect = Exception("400 Bad Request")

        result = await tools["list_incidents"](page_number=250)

        assert result["error"] is True
        assert result["error_type"] == "client_error"
        assert "collect_incidents" in result["message"]

    @pytest.mark.asyncio
    async def test_search_incidents_flags_partial_results_on_mid_page_error(self):
        tools, request = self._register_tools()

        # Full first page (== page_size, and < max_results) so the loop fetches
        # a second page, which then fails with a non-auth error mid-scan.
        first_page = Mock()
        first_page.raise_for_status.return_value = None
        first_page.json.return_value = {
            "data": [{"id": f"i-{n}", "attributes": {"title": f"t{n}"}} for n in range(5)],
            "meta": {"current_page": 1, "total_pages": 5},
        }
        second_page = Mock()
        second_page.raise_for_status.side_effect = Exception("500 Server Error")
        request.side_effect = [first_page, second_page]

        result = await tools["search_incidents"](page_number=0, page_size=5, max_results=10)

        assert result["meta"]["partial"] is True
        assert "error" in result["meta"]


def _incidents_page(payload=None):
    """A single incidents response with no further pages."""
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload or {
        "data": [],
        "meta": {"current_page": 1, "next_page": None, "total_pages": 1, "total_count": 0},
    }
    return response


@pytest.mark.unit
class TestNumericArgumentClamping:
    """Out-of-range numbers are clamped and reported, not refused.

    These bounds were pydantic constraints, so a caller asking for 20 results
    when the ceiling was 10 got a validation error instead of 10 results. That
    was the single highest-volume tool failure in production.
    """

    @staticmethod
    def _register():
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
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_search_page_size_above_the_cap_is_clamped(self):
        tools, request = self._register()
        request.return_value = _incidents_page()

        result = await tools["search_incidents"](page_size=50, page_number=1)

        assert request.await_args_list[0].kwargs["params"]["page[size]"] == 20
        assert result["argument_adjustments"] == [
            "page_size=50 is outside the supported range; used 20 instead."
        ]

    @pytest.mark.asyncio
    async def test_list_page_size_above_the_cap_is_clamped(self):
        tools, request = self._register()
        request.return_value = _incidents_page()

        result = await tools["list_incidents"](page_size=500)

        assert request.await_args_list[-1].kwargs["params"]["page[size]"] == 100
        assert "page_size=500" in result["argument_adjustments"][0]

    @pytest.mark.asyncio
    async def test_collect_batch_size_below_the_floor_is_clamped(self):
        tools, request = self._register()
        request.return_value = _incidents_page()

        result = await tools["collect_incidents"](batch_size=5)

        assert request.await_args_list[-1].kwargs["params"]["page[size]"] == 10
        assert result["collection"]["batch_size"] == 10
        assert "batch_size=5" in result["argument_adjustments"][0]

    @pytest.mark.asyncio
    async def test_collect_max_results_above_the_cap_is_clamped(self):
        tools, request = self._register()
        request.return_value = _incidents_page()

        result = await tools["collect_incidents"](max_results=200)

        assert result["collection"]["max_results"] == 100
        assert "max_results=200" in result["argument_adjustments"][0]

    @pytest.mark.asyncio
    async def test_an_in_range_call_carries_no_adjustment_key(self):
        tools, request = self._register()
        request.return_value = _incidents_page()

        result = await tools["list_incidents"](page_size=25)

        assert "argument_adjustments" not in result


@pytest.mark.unit
class TestResultCountArgumentAliases:
    """`limit` is the name callers reach for, and it was rejected everywhere.

    These go through a real FastMCP server because aliases live in argument
    validation -- calling the Python function directly bypasses them entirely,
    so a test on FakeMCP would pass whether or not the alias exists.
    """

    @staticmethod
    def _server(request):
        from fastmcp import FastMCP

        mcp = FastMCP("incident-alias-probe")
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )
        return mcp

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["page_size", "limit", "max_results"])
    async def test_list_incidents_accepts_every_result_count_spelling(self, name):
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        await mcp.call_tool("list_incidents", {name: 7})

        assert request.await_args_list[-1].kwargs["params"]["page[size]"] == 7

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["max_results", "limit"])
    async def test_collect_incidents_accepts_either_result_cap_spelling(self, name):
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        result = await mcp.call_tool("collect_incidents", {name: 40})

        payload = result.structured_content
        assert payload is not None
        assert payload["collection"]["max_results"] == 40

    @pytest.mark.asyncio
    async def test_collect_incidents_does_not_alias_page_size(self):
        # `page_size` is deliberately not an alias here. On list_incidents it is
        # effectively the result count, but collect_incidents' nearest argument
        # is the upstream batch size -- so honouring it would quietly mean
        # something else than the caller asked for. `limit` covers the intent.
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        with pytest.raises(Exception, match="page_size"):
            await mcp.call_tool("collect_incidents", {"page_size": 40})

    @pytest.mark.asyncio
    async def test_a_canonical_name_and_its_alias_together_are_refused(self):
        # AliasChoices binds the first name it finds and the rest become
        # unexpected. Worth pinning: two spellings of one argument in a single
        # call is ambiguous, and guessing which one wins would be worse.
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        with pytest.raises(Exception, match="limit"):
            await mcp.call_tool("list_incidents", {"page_size": 5, "limit": 9})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["max_results", "limit"])
    async def test_search_incidents_accepts_either_result_cap_spelling(self, name):
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        result = await mcp.call_tool("search_incidents", {name: 4, "page_number": 0})

        payload = result.structured_content
        assert payload is not None
        assert payload["meta"]["max_results"] == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["max_solutions", "max_results", "limit"])
    async def test_suggest_solutions_accepts_every_result_cap_spelling(self, name):
        # The tool was rejecting `max_results` outright; reaching the empty-set
        # branch is proof the argument validated rather than being refused.
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        result = await mcp.call_tool(
            "suggest_solutions", {"incident_title": "database timeouts", name: 2}
        )

        payload = result.structured_content
        assert payload is not None
        assert payload["solutions"] == []

    @pytest.mark.asyncio
    async def test_an_unknown_argument_is_still_refused(self):
        # Aliasing is not a licence to accept anything.
        request = AsyncMock(return_value=_incidents_page())
        mcp = self._server(request)

        with pytest.raises(Exception, match="not_a_real_argument"):
            await mcp.call_tool("list_incidents", {"not_a_real_argument": 1})


@pytest.mark.unit
class TestSearchIncidentsResultCap:
    """`limit` must limit on the default page, not only when fetching all pages.

    `max_results` applied only in multi-page mode, and `limit` aliases it, so
    `search_incidents(query=..., limit=5)` on the default page silently returned
    a full page_size of rows. `limit` was the most frequently rejected argument
    on this tool, so it was accepted and then ignored.

    These run through a real FastMCP server: `limit` is a validation alias, and
    calling the Python function directly bypasses it entirely.
    """

    @staticmethod
    def _params(request):
        """The params of the most recent call, asserted to exist."""
        call_args = request.await_args
        assert call_args is not None, "expected a request to have been made"
        return call_args.kwargs["params"]

    @staticmethod
    def _server():
        from fastmcp import FastMCP

        async def responder(method, url, params=None, **_kwargs):
            # The API returns at most page[size] rows; mirror that or the cap
            # under test cannot be observed.
            size = min(int((params or {}).get("page[size]", 10)), 10)
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "data": [
                    {"id": f"i{i}", "type": "incidents", "attributes": {"title": f"t{i}"}}
                    for i in range(size)
                ],
                "meta": {"current_page": 1, "next_page": None, "total_pages": 1},
            }
            return response

        request = AsyncMock(side_effect=responder)
        mcp = FastMCP("search-cap-probe")
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )
        return mcp, request

    @staticmethod
    async def _search(mcp, **arguments):
        result = await mcp.call_tool("search_incidents", {"query": "db", **arguments})
        payload = result.structured_content
        assert payload is not None
        return payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["limit", "max_results"])
    async def test_the_cap_applies_on_the_default_page(self, name):
        mcp, request = self._server()

        payload = await self._search(mcp, **{name: 5})

        assert self._params(request)["page[size]"] == 5
        assert len(payload["data"]) == 5

    @pytest.mark.asyncio
    async def test_the_smaller_of_the_two_wins(self):
        mcp, request = self._server()

        await self._search(mcp, limit=5, page_size=3)

        assert self._params(request)["page[size]"] == 3

    @pytest.mark.asyncio
    async def test_no_cap_leaves_the_page_size_alone(self):
        # The regression guard: an existing caller that never passed a cap must
        # keep receiving a full page.
        mcp, request = self._server()

        payload = await self._search(mcp)

        assert self._params(request)["page[size]"] == 10
        assert len(payload["data"]) == 10

    @pytest.mark.asyncio
    async def test_multi_page_mode_is_unchanged(self):
        mcp, _ = self._server()

        capped = await self._search(mcp, limit=5, page_number=0)
        defaulted = await self._search(mcp, page_number=0)

        assert len(capped["data"]) == 5
        # Still defaults to 5 across pages when no cap is given.
        assert len(defaulted["data"]) == 5

    @pytest.mark.asyncio
    async def test_an_over_large_cap_is_still_clamped_and_reported(self):
        mcp, request = self._server()

        payload = await self._search(mcp, limit=20)

        assert self._params(request)["page[size]"] == 10
        assert "max_results=20" in payload["argument_adjustments"][0]


def _recording_record(rec_id, *, word_count=41, platform="google_meet"):
    return {
        "id": rec_id,
        "type": "meeting_recordings",
        "attributes": {
            "platform": platform,
            "status": "completed",
            "started_at": "2026-09-02T16:37:56-07:00",
            "ended_at": "2026-09-02T16:38:17-07:00",
            "duration_minutes": 0,
            "speaker_count": 1,
            "word_count": word_count,
            "transcript_summary": "Ten customers reported search was slow.",
            # ~2KB of presigned AWS query string in production.
            "video_url": "https://rootly-storage.s3.amazonaws.com/x?X-Amz-Credential=" + "A" * 400,
        },
    }


def _word_transcript(*turns):
    """The API's shape: one object per word, with confidence and timings."""
    return [
        {
            "speaker": speaker,
            "language": "en_us",
            "speaker_id": None,
            "words": [
                {
                    "text": word,
                    "language": None,
                    "confidence": 0.97,
                    "start_timestamp": i * 0.3,
                    "end_timestamp": i * 0.3 + 0.2,
                }
                for i, word in enumerate(text.split())
            ],
        }
        for speaker, text in turns
    ]


@pytest.mark.unit
class TestIncidentMeetingTranscripts:
    """Reading an incident's call transcripts used to take three calls.

    Resolve the incident, list its recordings, then fetch each one with
    `include=transcript` -- and the caller had to know that `include` value
    exists, since it appears nowhere in the bundled spec. The API also returns
    one object per spoken word, which is 125 bytes of JSON per word.
    """

    @staticmethod
    def _register(*, recordings=None, transcript=None, detail_error=False):
        mcp = FakeMCP()
        recordings = [] if recordings is None else recordings

        async def responder(method, url, params=None, **_kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if url == "/v1/incidents":  # sequential-id resolution
                response.json.return_value = {
                    "data": [{"id": "inc-uuid", "attributes": {"sequential_id": 5185}}]
                }
            elif url.endswith("/meeting_recordings"):
                response.json.return_value = {"data": recordings}
            elif "/v1/meeting_recordings/" in url:
                if detail_error:
                    raise RuntimeError("upstream exploded")
                response.json.return_value = {
                    "data": {"attributes": {"transcript": transcript or []}}
                }
            else:
                response.json.return_value = {"data": {}}
            return response

        request = AsyncMock(side_effect=responder)
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )
        return mcp.tools, request

    @pytest.mark.asyncio
    async def test_word_level_output_becomes_text_per_speaker_turn(self):
        tools, _ = self._register(
            recordings=[_recording_record("rec-1")],
            transcript=_word_transcript(
                ("Luca Tirelli", "we have about 10 customers reporting slow search"),
                ("Ada Lovelace", "starting the investigation now"),
            ),
        )

        result = await tools["get_incident_meeting_transcripts"](incident_id="INC-5185")

        assert result["returned_recordings"] == 1
        assert result["recordings"][0]["transcript"] == [
            {
                "text": "we have about 10 customers reporting slow search",
                "speaker": "Luca Tirelli",
                "language": "en_us",
            },
            {
                "text": "starting the investigation now",
                "speaker": "Ada Lovelace",
                "language": "en_us",
            },
        ]

    @pytest.mark.asyncio
    async def test_the_signed_video_url_is_not_returned(self):
        # ~2KB of AWS credential query string per record, expiring in an hour.
        tools, _ = self._register(
            recordings=[_recording_record("rec-1")],
            transcript=_word_transcript(("Luca", "hello")),
        )

        result = await tools["get_incident_meeting_transcripts"](incident_id="5185")

        assert "video_url" not in result["recordings"][0]
        assert "X-Amz-Credential" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_recordings_where_nobody_spoke_are_skipped(self):
        tools, request = self._register(
            recordings=[
                _recording_record("silent", word_count=0),
                _recording_record("spoken", word_count=12),
            ],
            transcript=_word_transcript(("Luca", "we are live")),
        )

        result = await tools["get_incident_meeting_transcripts"](incident_id="5185")

        assert result["returned_recordings"] == 1
        assert result["recordings"][0]["recording_id"] == "spoken"
        assert any("nobody spoke" in note for note in result["notes"])
        # And no request was wasted fetching the empty one.
        fetched = [
            c.args[1] for c in request.await_args_list if "/v1/meeting_recordings/" in c.args[1]
        ]
        assert fetched == ["/v1/meeting_recordings/spoken"]

    @pytest.mark.asyncio
    async def test_a_long_transcript_is_truncated_and_says_so(self):
        long_turns = [("Speaker", "word " * 400) for _ in range(60)]
        tools, _ = self._register(
            recordings=[_recording_record("rec-1", word_count=24000)],
            transcript=_word_transcript(*long_turns),
        )

        result = await tools["get_incident_meeting_transcripts"](incident_id="5185")

        recording = result["recordings"][0]
        assert recording["transcript_truncated"] is True
        assert len(recording["transcript"]) < 60
        assert any("truncated" in note for note in result["notes"])

    @pytest.mark.asyncio
    async def test_no_recordings_is_not_an_error(self):
        tools, _ = self._register(recordings=[])

        result = await tools["get_incident_meeting_transcripts"](incident_id="5185")

        assert result.get("error") is None
        assert result["returned_recordings"] == 0
        assert "No Meeting Scribe recordings" in result["notes"][0]

    @pytest.mark.asyncio
    async def test_one_unreadable_transcript_does_not_lose_the_response(self):
        tools, _ = self._register(recordings=[_recording_record("rec-1")], detail_error=True)

        result = await tools["get_incident_meeting_transcripts"](incident_id="5185")

        assert result["returned_recordings"] == 1
        assert result["recordings"][0]["transcript"] == []
        assert "Failed to read transcript" in result["recordings"][0]["error"]

    @pytest.mark.asyncio
    async def test_max_recordings_is_clamped_and_reported(self):
        tools, request = self._register(
            recordings=[_recording_record("rec-1")],
            transcript=_word_transcript(("Luca", "hello")),
        )

        result = await tools["get_incident_meeting_transcripts"](
            incident_id="5185", max_recordings=99
        )

        assert "max_recordings=99" in result["argument_adjustments"][0]
        # The cap bounds how many transcripts come back, not the list page size.
        # Asserting on page[size] would pin the bug where silent recordings ate
        # the requested count.
        assert result["returned_recordings"] <= 10

    @pytest.mark.asyncio
    async def test_an_unknown_incident_reference_is_a_validation_error(self):
        tools, _ = self._register()

        result = await tools["get_incident_meeting_transcripts"](incident_id="../../etc")

        assert result["error"] is True
        assert result["error_type"] == "validation_error"


@pytest.mark.unit
class TestIncidentMeetingTranscriptsArgumentNames:
    """`limit` and `id` are the names callers reach for.

    Through a real FastMCP server: aliases live in argument validation, so
    calling the function directly bypasses them entirely.
    """

    @staticmethod
    def _server(spoken=0):
        from fastmcp import FastMCP

        records = [_recording_record(f"spoken-{i}", word_count=100) for i in range(spoken)]

        async def responder(method, url, params=None, **_kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if url.endswith("/meeting_recordings"):
                response.json.return_value = {"data": records, "meta": {"next_page": None}}
            else:
                response.json.return_value = {
                    "data": {
                        "attributes": {"transcript": [{"speaker": "A", "words": [{"text": "hi"}]}]}
                    }
                }
            return response

        request = AsyncMock(side_effect=responder)
        mcp = FastMCP("transcript-alias-probe")
        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=request,
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )
        return mcp, request

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["incident_id", "id"])
    async def test_the_incident_can_be_named_either_way(self, name):
        mcp, _ = self._server()

        result = await mcp.call_tool(
            "get_incident_meeting_transcripts",
            {name: "2b0dd1f2-6c9c-4a2f-9e4e-0b1d5a9f9d11"},
        )

        payload = result.structured_content
        assert payload is not None
        assert payload["returned_recordings"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["max_recordings", "limit", "max_results"])
    async def test_the_recording_cap_accepts_every_spelling(self, name):
        # Five recordings available, two asked for under each spelling. Counting
        # the transcripts returned proves the alias reached max_recordings
        # without pinning how the listing is paged.
        mcp, _ = self._server(spoken=5)

        result = await mcp.call_tool(
            "get_incident_meeting_transcripts",
            {"incident_id": "2b0dd1f2-6c9c-4a2f-9e4e-0b1d5a9f9d11", name: 2},
        )

        payload = result.structured_content
        assert payload is not None
        assert payload["returned_recordings"] == 2


@pytest.mark.unit
class TestTranscriptBudgetIgnoresSilentRecordings:
    """Silent recordings must not take a share of the character budget.

    The budget is divided across the calls actually read. Dividing by the
    number listed would leave one real call among nine silent ones with a
    tenth of the allowance, truncating a transcript that fits comfortably.

    Uses many speaker turns deliberately: a single turn is always kept whole
    regardless of budget, so one turn cannot tell the two behaviours apart.
    """

    @pytest.mark.asyncio
    async def test_one_spoken_call_among_many_silent_keeps_every_turn(self):
        mcp = FakeMCP()
        # 20 turns of 200 words: ~1k chars each, ~20k total. Fits the full
        # 40k budget; a tenth of it (4k) would drop most of them.
        turns = [
            {
                "speaker": f"Speaker {i}",
                "language": "en_us",
                "words": [{"text": "word", "confidence": 1} for _ in range(200)],
            }
            for i in range(20)
        ]
        records = [_recording_record(f"silent-{i}", word_count=0) for i in range(9)]
        records.append(_recording_record("spoken", word_count=4000))

        async def responder(method, url, params=None, **_kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if url.endswith("/meeting_recordings"):
                response.json.return_value = {"data": records}
            else:
                response.json.return_value = {"data": {"attributes": {"transcript": turns}}}
            return response

        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=AsyncMock(side_effect=responder),
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )

        result = await mcp.tools["get_incident_meeting_transcripts"](
            incident_id="2b0dd1f2-6c9c-4a2f-9e4e-0b1d5a9f9d11", max_recordings=10
        )

        recording = result["recordings"][0]
        assert recording["recording_id"] == "spoken"
        assert len(recording["transcript"]) == 20, "every turn should survive"
        assert recording.get("transcript_truncated") is None


@pytest.mark.unit
class TestTranscriptPaginationFindsSpokenRecordings:
    """Silent recordings must not consume the requested transcript count.

    Most recordings in production have no speech -- Scribe joined a call that
    never happened. Fetching exactly `max_recordings` list entries and then
    filtering silent ones returned nothing whenever the first entries were
    silent, while reporting that nobody spoke. Roughly one recording in eight
    sampled had speech, so that was the common case rather than an edge.
    """

    @staticmethod
    def _register(all_records):
        mcp = FakeMCP()
        pages_requested: list[int] = []

        async def responder(method, url, params=None, **_kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if url.endswith("/meeting_recordings"):
                size = int((params or {}).get("page[size]", 50))
                number = int((params or {}).get("page[number]", 1))
                pages_requested.append(number)
                page = all_records[(number - 1) * size : number * size]
                has_next = number * size < len(all_records)
                response.json.return_value = {
                    "data": page,
                    "meta": {"next_page": number + 1 if has_next else None},
                }
            else:
                response.json.return_value = {
                    "data": {
                        "attributes": {
                            "transcript": [
                                {
                                    "speaker": "Luca",
                                    "language": "en_us",
                                    "words": [{"text": "hello"}],
                                }
                            ]
                        }
                    }
                }
            return response

        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=AsyncMock(side_effect=responder),
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )
        return mcp.tools, pages_requested

    @staticmethod
    async def _run(tools, asked):
        return await tools["get_incident_meeting_transcripts"](
            incident_id="2b0dd1f2-6c9c-4a2f-9e4e-0b1d5a9f9d11", max_recordings=asked
        )

    @pytest.mark.asyncio
    async def test_silent_recordings_first_do_not_hide_the_spoken_ones(self):
        records = [_recording_record(f"silent-{i}", word_count=0) for i in range(5)]
        records += [_recording_record(f"spoken-{i}", word_count=100) for i in range(7)]
        tools, _ = self._register(records)

        result = await self._run(tools, 3)

        assert result["returned_recordings"] == 3
        assert all(r["recording_id"].startswith("spoken") for r in result["recordings"])

    @pytest.mark.asyncio
    async def test_it_turns_the_page_when_a_whole_page_is_silent(self):
        records = [_recording_record(f"silent-{i}", word_count=0) for i in range(60)]
        records += [_recording_record(f"spoken-{i}", word_count=100) for i in range(4)]
        tools, pages = self._register(records)

        result = await self._run(tools, 3)

        assert result["returned_recordings"] == 3
        assert pages == [1, 2], "should read a second page to reach spoken recordings"

    @pytest.mark.asyncio
    async def test_paging_is_bounded_when_everything_is_silent(self):
        tools, pages = self._register(
            [_recording_record(f"silent-{i}", word_count=0) for i in range(500)]
        )

        result = await self._run(tools, 3)

        assert result["returned_recordings"] == 0
        assert len(pages) == 5, "must stop at the page cap rather than walk 500 records"
        assert any("Stopped after scanning" in note for note in result["notes"])

    @pytest.mark.asyncio
    async def test_fewer_spoken_than_asked_is_not_an_error(self):
        tools, _ = self._register(
            [_recording_record(f"spoken-{i}", word_count=100) for i in range(2)]
        )

        result = await self._run(tools, 3)

        assert result.get("error") is None
        assert result["returned_recordings"] == 2

    @pytest.mark.asyncio
    async def test_a_listing_failure_mid_walk_keeps_what_was_found(self):
        mcp = FakeMCP()
        records = [_recording_record(f"spoken-{i}", word_count=100) for i in range(60)]

        async def responder(method, url, params=None, **_kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if url.endswith("/meeting_recordings"):
                number = int((params or {}).get("page[number]", 1))
                if number > 1:
                    raise RuntimeError("listing exploded")
                response.json.return_value = {
                    "data": records[:50],
                    "meta": {"next_page": 2},
                }
            else:
                response.json.return_value = {
                    "data": {
                        "attributes": {"transcript": [{"speaker": "A", "words": [{"text": "hi"}]}]}
                    }
                }
            return response

        register_incident_tools(
            mcp=mcp,
            make_authenticated_request=AsyncMock(side_effect=responder),
            strip_heavy_nested_data=lambda data: data,
            mcp_error=FakeMCPError(),
            generate_recommendation=_generate_recommendation,
            enable_write_tools=False,
        )

        result = await mcp.tools["get_incident_meeting_transcripts"](
            incident_id="2b0dd1f2-6c9c-4a2f-9e4e-0b1d5a9f9d11", max_recordings=3
        )

        # The first page satisfied the request, so the failure never happens.
        assert result["returned_recordings"] == 3
        assert result.get("error") is None
