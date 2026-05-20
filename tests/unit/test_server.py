"""
Unit tests for Rootly MCP Server core functionality.

Tests cover:
- Server creation with different configurations
- OpenAPI spec loading and filtering
- HTTP client configuration
- Tool generation from OpenAPI spec
"""

import json
import os
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, mock_open, patch

import mcp.types as mt
import pytest

import rootly_mcp_server.server as server_module
from rootly_mcp_server.server import (
    DEFAULT_ALLOWED_PATHS,
    DEFAULT_DELETE_ALLOWED_PATHS,
    AuthenticatedHTTPXClient,
    _auth_header_state,
    _current_tool_identity,
    _extract_client_ip,
    _extract_request_id,
    _extract_structured_tool_error,
    _filter_openapi_spec,
    _fingerprint_auth_header,
    _format_traceback_excerpt,
    _load_swagger_spec,
    _validate_bearer_auth_header,
    create_rootly_mcp_server,
)
from rootly_mcp_server.spec_transform import audit_openapi_spec, has_openapi_audit_findings
from rootly_mcp_server.utils import OAUTH_PROTECTED_RESOURCE_PATH


@pytest.mark.unit
class TestServerCreation:
    """Test server creation with various configurations."""

    def test_create_server_with_defaults(self, mock_httpx_client, mock_api_response):
        """Test creating server with default parameters."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Rootly API", "version": "1.0.0"},
                "paths": {"/incidents": {"get": {"operationId": "listIncidents"}}},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server()

            # Verify server was created
            assert server is not None
            assert hasattr(server, "list_tools")

            # Verify default parameters were used
            mock_load_spec.assert_called_once_with(None)

    def test_create_server_with_custom_name(self, mock_httpx_client):
        """Test server creation with custom name."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            custom_name = "CustomRootlyServer"
            server = create_rootly_mcp_server(name=custom_name)

            assert server is not None

    def test_create_server_hosted_mode(self, mock_httpx_client):
        """Test server creation in hosted mode."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server(hosted=True)

            assert server is not None

    def test_bundled_swagger_audit_passes(self):
        """Ensure the bundled swagger passes the full schema audit."""
        swagger_path = os.path.join(os.path.dirname(server_module.__file__), "data", "swagger.json")
        with open(swagger_path, encoding="utf-8") as f:
            spec = json.load(f)

        findings = audit_openapi_spec(spec)

        assert not has_openapi_audit_findings(findings), findings

    def test_filtered_bundled_swagger_audit_passes(self):
        """Ensure the shipped MCP-filtered spec passes the full schema audit."""
        swagger_path = os.path.join(os.path.dirname(server_module.__file__), "data", "swagger.json")
        with open(swagger_path, encoding="utf-8") as f:
            spec = json.load(f)

        filtered_spec = _filter_openapi_spec(
            spec,
            [
                f"/v1{path}" if not path.startswith("/v1") else path
                for path in DEFAULT_ALLOWED_PATHS
            ],
            delete_allowed_paths=[
                f"/v1{path}" if not path.startswith("/v1") else path
                for path in DEFAULT_DELETE_ALLOWED_PATHS
            ],
        )
        findings = audit_openapi_spec(filtered_spec)

        assert not has_openapi_audit_findings(findings), findings

    def test_filter_openapi_spec_disables_write_methods_by_default(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/teams": {
                    "get": {"operationId": "listTeams"},
                    "post": {"operationId": "createTeam"},
                },
                "/v1/workflows/123/workflow_tasks": {
                    "get": {"operationId": "listWorkflowTasks"},
                    "post": {"operationId": "createWorkflowTask"},
                },
                "/v1/workflow_tasks/456": {
                    "get": {"operationId": "getWorkflowTask"},
                    "put": {"operationId": "updateWorkflowTask"},
                    "delete": {"operationId": "deleteWorkflowTask"},
                },
            },
            "components": {"schemas": {}},
        }

        filtered = _filter_openapi_spec(
            spec,
            ["/v1/teams", "/v1/workflows/123/workflow_tasks", "/v1/workflow_tasks/456"],
            delete_allowed_paths=[],
            write_allowed_paths=["/v1/workflows/123/workflow_tasks", "/v1/workflow_tasks/456"],
            enable_write_tools=False,
        )

        assert "post" not in filtered["paths"]["/v1/teams"]
        assert "post" not in filtered["paths"]["/v1/workflows/123/workflow_tasks"]
        assert "put" not in filtered["paths"]["/v1/workflow_tasks/456"]
        assert "delete" not in filtered["paths"]["/v1/workflow_tasks/456"]

    def test_filter_openapi_spec_only_exposes_curated_write_methods_when_enabled(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/teams": {
                    "get": {"operationId": "listTeams"},
                    "post": {"operationId": "createTeam"},
                },
                "/v1/workflows/123/workflow_tasks": {
                    "get": {"operationId": "listWorkflowTasks"},
                    "post": {"operationId": "createWorkflowTask"},
                },
                "/v1/workflow_tasks/456": {
                    "get": {"operationId": "getWorkflowTask"},
                    "put": {"operationId": "updateWorkflowTask"},
                },
            },
            "components": {"schemas": {}},
        }

        filtered = _filter_openapi_spec(
            spec,
            ["/v1/teams", "/v1/workflows/123/workflow_tasks", "/v1/workflow_tasks/456"],
            write_allowed_paths=["/v1/workflows/123/workflow_tasks", "/v1/workflow_tasks/456"],
            enable_write_tools=True,
        )

        assert "post" not in filtered["paths"]["/v1/teams"]
        assert "post" in filtered["paths"]["/v1/workflows/123/workflow_tasks"]
        assert "put" in filtered["paths"]["/v1/workflow_tasks/456"]

    def test_filter_openapi_spec_can_allowlist_specific_operation_ids(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/teams": {
                    "get": {"operationId": "listTeams"},
                    "post": {"operationId": "createTeam"},
                },
                "/v1/users/me": {
                    "get": {"operationId": "getCurrentUser"},
                },
            },
            "components": {"schemas": {}},
        }

        filtered = _filter_openapi_spec(
            spec,
            ["/v1/teams", "/v1/users/me"],
            enabled_operation_ids={"listTeams"},
        )

        assert "get" in filtered["paths"]["/v1/teams"]
        assert "post" not in filtered["paths"]["/v1/teams"]
        assert "/v1/users/me" not in filtered["paths"]

    def test_create_server_with_bundled_swagger(self):
        """Ensure FastMCP can instantiate from the bundled swagger without schema errors."""
        swagger_path = os.path.join(os.path.dirname(server_module.__file__), "data", "swagger.json")

        server = create_rootly_mcp_server(swagger_path=swagger_path, hosted=False)

        assert server is not None

    def test_create_server_with_custom_paths(self, mock_httpx_client):
        """Test server creation with custom allowed paths."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {"/custom": {}},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            custom_paths = ["/custom"]
            server = create_rootly_mcp_server(allowed_paths=custom_paths)

            assert server is not None

    def test_create_server_with_custom_delete_allowed_paths(self, mock_httpx_client):
        """Test server creation passes custom delete allowlist through to spec filtering."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            with patch("rootly_mcp_server.server._filter_openapi_spec") as mock_filter_spec:
                mock_spec = {
                    "openapi": "3.0.0",
                    "info": {"title": "Test API", "version": "1.0.0"},
                    "paths": {},
                    "components": {"schemas": {}},
                }
                mock_load_spec.return_value = mock_spec
                mock_filter_spec.return_value = mock_spec

                custom_allowed_paths = ["/schedules/{schedule_id}"]
                custom_delete_allowed_paths = ["/schedules/{schedule_id}"]
                server = create_rootly_mcp_server(
                    allowed_paths=custom_allowed_paths,
                    delete_allowed_paths=custom_delete_allowed_paths,
                )

                assert server is not None
                assert mock_filter_spec.call_count == 1
                assert mock_filter_spec.call_args.args[1] == ["/v1/schedules/{schedule_id}"]
                assert mock_filter_spec.call_args.kwargs["delete_allowed_paths"] == [
                    "/v1/schedules/{schedule_id}"
                ]

    def test_create_server_with_swagger_path(self, mock_httpx_client):
        """Test server creation with explicit swagger file path."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            swagger_path = "/path/to/swagger.json"
            create_rootly_mcp_server(swagger_path=swagger_path)

            mock_load_spec.assert_called_once_with(swagger_path)


@pytest.mark.unit
@pytest.mark.asyncio
class TestBundledIncidentFormFieldSelectionTools:
    """Verify the bundled swagger exposes the intended incident custom field tools."""

    async def test_default_server_shows_generated_write_tools(self, mock_environment_token):
        server = create_rootly_mcp_server(hosted=False)

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        # Read tools should be present
        assert "listIncidentActionItems" in tool_names
        assert "listIncidentFormFieldSelections" in tool_names
        assert "getIncidentFormFieldSelection" in tool_names

        # Write tools should also be present by default
        assert "createIncidentActionItem" in tool_names
        assert "createIncidentFormFieldSelection" in tool_names
        assert "updateIncidentFormFieldSelection" in tool_names
        # Delete operations are still restricted
        assert "deleteIncidentFormFieldSelection" not in tool_names

    async def test_default_server_shows_workflow_write_tools(self, mock_environment_token):
        server = create_rootly_mcp_server(hosted=False)

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        # Read tools should be present
        assert "listWorkflowTasks" in tool_names
        assert "getWorkflowTask" in tool_names

        # Write tools should also be present by default
        assert "createWorkflowTask" in tool_names
        assert "updateWorkflowTask" in tool_names
        # Delete operations are still restricted
        assert "deleteWorkflowTask" not in tool_names

    async def test_default_server_shows_additional_read_tools(self, mock_environment_token):
        server = create_rootly_mcp_server(hosted=False)

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        # Workflow observability reads
        assert "ListWorkflowRuns" in tool_names
        assert "listWorkflowGroups" in tool_names
        assert "getWorkflowGroup" in tool_names
        assert "listWorkflowFormFieldConditions" in tool_names
        assert "getWorkflowFormFieldCondition" in tool_names

        # Alert, status page, form metadata, and catalog reads
        assert "listAlertEvents" in tool_names
        assert "getAlertEvent" in tool_names
        assert "listStatusPages" in tool_names
        assert "getStatusPage" in tool_names
        assert "listStatusPageTemplates" in tool_names
        assert "getStatusPageTemplate" in tool_names
        assert "getTeamIncidentsChart" in tool_names
        assert "getServiceIncidentsChart" in tool_names
        assert "getServiceUptimeChart" in tool_names
        assert "getFunctionalityIncidentsChart" in tool_names
        assert "getFunctionalityUptimeChart" in tool_names
        assert "listAlertGroups" in tool_names
        assert "getAlertGroup" in tool_names
        assert "listAlertRoutingRules" in tool_names
        assert "getAlertRoutingRule" in tool_names
        assert "listAlertsSources" in tool_names
        assert "getAlertsSource" in tool_names
        assert "listAlertUrgencies" in tool_names
        assert "getAlertUrgency" in tool_names
        assert "listAllIncidentActionItems" in tool_names
        assert "getIncidentActionItems" in tool_names
        assert "listCustomForms" in tool_names
        assert "getCustomForm" in tool_names
        assert "listFormFields" in tool_names
        assert "getFormField" in tool_names
        assert "listFormFieldOptions" in tool_names
        assert "getFormFieldOption" in tool_names
        assert "listCatalogs" in tool_names
        assert "getCatalog" in tool_names
        assert "listCatalogEntities" in tool_names
        assert "getCatalogEntity" in tool_names
        assert "listCauses" in tool_names
        assert "getCause" in tool_names

        # Workflow creates now enabled, but workflow runs remain excluded
        assert "createWorkflowRun" not in tool_names
        # Alert configuration writes remain excluded (connects to external systems)
        assert "createAlertGroup" not in tool_names
        assert "updateAlertGroup" not in tool_names
        assert "createAlertRoutingRule" not in tool_names
        assert "updateAlertRoutingRule" not in tool_names
        assert "createAlertSource" not in tool_names
        assert "updateAlertSource" not in tool_names
        assert "createAlertUrgency" not in tool_names
        assert "updateAlertUrgency" not in tool_names
        # Custom form/field creation excluded (schema-level configuration)
        assert "createCustomForm" not in tool_names
        assert "updateCustomForm" not in tool_names
        assert "createFormField" not in tool_names
        assert "updateFormField" not in tool_names

    async def test_enable_write_tools_exposes_curated_generated_write_tools(
        self, mock_environment_token
    ):
        server = create_rootly_mcp_server(hosted=False, enable_write_tools=True)

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "createIncidentActionItem" in tool_names
        assert "createIncidentFormFieldSelection" in tool_names
        assert "updateIncidentFormFieldSelection" in tool_names
        assert "updateEnvironment" in tool_names
        assert "updateEscalationLevel" in tool_names
        assert "updateEscalationPath" in tool_names
        assert "updateEscalationPolicy" in tool_names
        assert "updateFunctionality" in tool_names
        assert "updateIncidentType" in tool_names
        assert "updateOnCallRole" in tool_names
        assert "updateOnCallShadow" in tool_names
        assert "updateOverrideShift" in tool_names
        assert "updateSchedule" in tool_names
        assert "updateScheduleRotation" in tool_names
        assert "updateService" in tool_names
        assert "updateSeverity" in tool_names
        assert "updateTeam" in tool_names
        assert "updateWorkflow" in tool_names
        assert "createWorkflowTask" in tool_names
        assert "updateWorkflowTask" in tool_names
        assert "updateAlert" not in tool_names
        assert "updateUser" not in tool_names
        assert "deleteSchedule" not in tool_names
        assert "deleteScheduleRotation" not in tool_names
        assert "deleteEscalationPolicy" not in tool_names
        assert "deleteEscalationPath" not in tool_names
        assert "deleteEscalationLevel" not in tool_names
        assert "deleteWorkflowTask" not in tool_names

    async def test_hosted_server_keeps_curated_write_tools_by_default(self, mock_environment_token):
        server = create_rootly_mcp_server(hosted=True)

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "createWorkflowTask" in tool_names
        assert "updateWorkflowTask" in tool_names
        assert "createIncidentActionItem" in tool_names
        assert "createIncidentFormFieldSelection" in tool_names
        assert "updateIncidentFormFieldSelection" in tool_names
        assert "deleteWorkflowTask" not in tool_names

    async def test_enabled_tools_allowlist_filters_generated_and_custom_tools(
        self, mock_environment_token
    ):
        server = create_rootly_mcp_server(
            hosted=False,
            enabled_tools={"listTeams", "listSeverities"},
        )

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert tool_names == {"listTeams", "listSeverities"}

    async def test_list_incidents_canonical_name_in_allowlist_does_not_raise(
        self, mock_environment_token
    ):
        """Regression: `ROOTLY_MCP_ENABLED_TOOLS=list_incidents` used to raise
        ValueError because validation ran against OpenAPI operationIds before the
        curated tool registered. The allowlist now validates against the full
        registry post-registration."""
        server = create_rootly_mcp_server(
            hosted=False,
            enabled_tools={"list_incidents"},
        )

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "list_incidents" in tool_names

    async def test_list_incidents_legacy_allowlist_exposes_both_names(self, mock_environment_token):
        """Posture A: legacy `listIncidents` in the allowlist must keep both
        the proxy and the canonical name exposed during the deprecation window."""
        server = create_rootly_mcp_server(
            hosted=False,
            enabled_tools={"listIncidents"},
        )

        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "list_incidents" in tool_names
        assert "listIncidents" in tool_names

    async def test_single_incident_list_tool_invariant(self, mock_environment_token):
        """Regression: only one canonical incident-list tool should exist.
        The autogen `listIncidents` is overwritten by the curated proxy; no
        third copy should sneak in. (`list_incidents` is a separately-named
        curated tool and is the canonical surface.)"""
        server = create_rootly_mcp_server(hosted=False)

        tools = await server.list_tools()
        tool_names = sorted(tool.name for tool in tools)

        # Expect exactly these two names targeting GET /incidents:
        list_variants = [n for n in tool_names if n in ("list_incidents", "listIncidents")]
        assert list_variants == ["listIncidents", "list_incidents"], (
            f"Unexpected incident-list tool surface: {list_variants}. "
            "Should be exactly ['listIncidents', 'list_incidents'] under posture A."
        )


@pytest.mark.unit
class TestAuthenticatedHTTPXClient:
    """Test the HTTP client wrapper functionality."""

    def test_client_initialization_local_mode(self, mock_environment_token):
        """Test client initialization in local mode with environment token."""
        client = AuthenticatedHTTPXClient(hosted=False)

        assert client.hosted is False
        assert client._api_token == mock_environment_token
        assert client.client is not None

        # Verify headers include authorization
        headers = client.client.headers
        assert "Authorization" in headers
        assert headers["Authorization"] == f"Bearer {mock_environment_token}"
        assert headers["Content-Type"] == "application/vnd.api+json"

    def test_client_initialization_hosted_mode(self):
        """Test client initialization in hosted mode without token loading."""
        client = AuthenticatedHTTPXClient(hosted=True)

        assert client.hosted is True
        assert client._api_token is None
        assert client.client is not None

        # Verify no authorization header in hosted mode
        headers = client.client.headers
        assert "Authorization" not in headers or not headers.get("Authorization")

    def test_client_with_custom_base_url(self):
        """Test client initialization with custom base URL."""
        custom_base = "https://custom.api.com"
        client = AuthenticatedHTTPXClient(base_url=custom_base, hosted=True)

        assert client._base_url == custom_base
        assert client.client.base_url == custom_base

    @patch.dict(os.environ, {}, clear=True)
    def test_client_without_token(self):
        """Test client behavior when no token is available."""
        client = AuthenticatedHTTPXClient(hosted=False)

        # Should handle missing token gracefully
        assert client._api_token is None

    def test_get_api_token_success(self, mock_environment_token):
        """Test successful API token retrieval."""
        client = AuthenticatedHTTPXClient(hosted=False)
        token = client._get_api_token()

        assert token == mock_environment_token
        assert token is not None and token.startswith("rootly_")

    @patch.dict(os.environ, {}, clear=True)
    def test_get_api_token_missing(self):
        """Test API token retrieval when token is missing."""
        client = AuthenticatedHTTPXClient(hosted=True)  # Won't try to get token
        token = client._get_api_token()

        assert token is None


@pytest.mark.unit
class TestHostedAuthRequestValidation:
    """Test hosted auth validation in the request path."""

    @pytest.mark.asyncio
    async def test_hosted_request_forwards_valid_bearer_header(self, mock_httpx_client):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.is_success = True
        mock_response.text = ""
        mock_httpx_client.request = AsyncMock(return_value=mock_response)

        captured: dict[str, Any] = {}

        def capture_alert_tools(**kwargs):
            captured["request"] = kwargs["make_authenticated_request"]

        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            with patch(
                "rootly_mcp_server.server.register_alert_tools", side_effect=capture_alert_tools
            ):
                with patch("rootly_mcp_server.server.register_incident_tools"):
                    with patch("rootly_mcp_server.server.register_oncall_tools"):
                        with patch("rootly_mcp_server.server.register_resource_handlers"):
                            mock_load_spec.return_value = {
                                "openapi": "3.0.0",
                                "info": {"title": "Test API", "version": "1.0.0"},
                                "paths": {},
                                "components": {"schemas": {}},
                            }
                            create_rootly_mcp_server(hosted=True)

        request = captured["request"]
        with patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"authorization": "Bearer rootly_valid_token"},
        ):
            await request("GET", "/v1/incidents")

        mock_httpx_client.request.assert_awaited_once()
        call_headers = mock_httpx_client.request.call_args.kwargs["headers"]
        assert call_headers["Authorization"] == "Bearer rootly_valid_token"

    @pytest.mark.asyncio
    async def test_hosted_request_uses_session_auth_fallback(self, mock_httpx_client):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.is_success = True
        mock_response.text = ""
        mock_httpx_client.request = AsyncMock(return_value=mock_response)

        captured: dict[str, Any] = {}

        def capture_alert_tools(**kwargs):
            captured["request"] = kwargs["make_authenticated_request"]

        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            with patch(
                "rootly_mcp_server.server.register_alert_tools", side_effect=capture_alert_tools
            ):
                with patch("rootly_mcp_server.server.register_incident_tools"):
                    with patch("rootly_mcp_server.server.register_oncall_tools"):
                        with patch("rootly_mcp_server.server.register_resource_handlers"):
                            mock_load_spec.return_value = {
                                "openapi": "3.0.0",
                                "info": {"title": "Test API", "version": "1.0.0"},
                                "paths": {},
                                "components": {"schemas": {}},
                            }
                            create_rootly_mcp_server(hosted=True)

        request = captured["request"]
        token_ctx = server_module._session_auth_token.set("Bearer rootly_session_token")
        try:
            with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
                await request("GET", "/v1/incidents")
        finally:
            server_module._session_auth_token.reset(token_ctx)

        mock_httpx_client.request.assert_awaited_once()
        call_headers = mock_httpx_client.request.call_args.kwargs["headers"]
        assert call_headers["Authorization"] == "Bearer rootly_session_token"

    @pytest.mark.asyncio
    async def test_hosted_request_rejects_malformed_auth_before_upstream_call(
        self, mock_httpx_client
    ):
        mock_httpx_client.request = AsyncMock()

        captured: dict[str, Any] = {}

        def capture_alert_tools(**kwargs):
            captured["request"] = kwargs["make_authenticated_request"]

        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            with patch(
                "rootly_mcp_server.server.register_alert_tools", side_effect=capture_alert_tools
            ):
                with patch("rootly_mcp_server.server.register_incident_tools"):
                    with patch("rootly_mcp_server.server.register_oncall_tools"):
                        with patch("rootly_mcp_server.server.register_resource_handlers"):
                            mock_load_spec.return_value = {
                                "openapi": "3.0.0",
                                "info": {"title": "Test API", "version": "1.0.0"},
                                "paths": {},
                                "components": {"schemas": {}},
                            }
                            create_rootly_mcp_server(hosted=True)

        request = captured["request"]
        error_ctx = server_module._session_error_context.set({})
        try:
            with patch(
                "fastmcp.server.dependencies.get_http_headers",
                return_value={"authorization": "rootly_malformed_token"},
            ):
                with pytest.raises(
                    server_module.RootlyAuthenticationError,
                    match="Invalid Authorization header format",
                ):
                    await request("GET", "/v1/incidents")
                error_context = server_module._session_error_context.get()
                assert error_context is not None
                assert error_context["auth_header_state"] == "invalid_format"
                assert "Invalid Authorization header format" in error_context["error_message"]
        finally:
            server_module._session_error_context.reset(error_ctx)

        mock_httpx_client.request.assert_not_awaited()


@pytest.mark.unit
class TestToolUsageIdentityHelpers:
    """Test helper utilities used for tool usage observability."""

    def test_extract_client_ip_prefers_cloudflare_header(self):
        headers = {
            "x-forwarded-for": "10.0.0.1, 10.0.0.2",
            "cf-connecting-ip": "203.0.113.10",
        }
        assert _extract_client_ip(headers) == "203.0.113.10"

    def test_extract_client_ip_falls_back_to_x_forwarded_for(self):
        headers = {"x-forwarded-for": "198.51.100.7, 10.0.0.2"}
        assert _extract_client_ip(headers) == "198.51.100.7"

    def test_extract_request_id_uses_preferred_headers(self):
        headers = {"cf-ray": "abc123", "x-request-id": "req-42"}
        assert _extract_request_id(headers) == "req-42"

    def test_fingerprint_auth_header_hashes_token_without_exposing_secret(self):
        fingerprint = _fingerprint_auth_header("Bearer rootly_secret_token")
        assert fingerprint
        assert len(fingerprint) == 16
        assert "rootly_secret_token" not in fingerprint

    def test_auth_header_state_classifies_common_cases(self):
        assert _auth_header_state("") == "missing"
        assert _auth_header_state("rootly_token_only") == "invalid_format"
        assert _auth_header_state("Bearer   ") == "missing_token"
        assert _auth_header_state("Bearer rootly_secret_token") == "bearer"

    def test_validate_bearer_auth_header_accepts_valid_bearer_format(self):
        assert (
            _validate_bearer_auth_header("Bearer rootly_secret_token")
            == "Bearer rootly_secret_token"
        )

    @pytest.mark.parametrize(
        ("header", "expected_fragment"),
        [
            ("", "Missing Authorization header"),
            ("rootly_secret_token", "Invalid Authorization header format"),
            ("Bearer   ", "Authorization header is missing a token"),
        ],
    )
    def test_validate_bearer_auth_header_rejects_bad_formats(
        self, header: str, expected_fragment: str
    ):
        with pytest.raises(server_module.RootlyAuthenticationError, match=expected_fragment):
            _validate_bearer_auth_header(header)

    def test_current_tool_identity_uses_session_fallback(self):
        token_ctx = server_module._session_auth_token.set("Bearer rootly_session_token")
        ip_ctx = server_module._session_client_ip.set("192.0.2.8")
        req_ctx = server_module._session_request_id.set("req-session-1")
        transport_ctx = server_module._session_transport.set("sse")
        try:
            with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
                identity = _current_tool_identity()
        finally:
            server_module._session_auth_token.reset(token_ctx)
            server_module._session_client_ip.reset(ip_ctx)
            server_module._session_request_id.reset(req_ctx)
            server_module._session_transport.reset(transport_ctx)

        assert identity["token_fingerprint"] == _fingerprint_auth_header(
            "Bearer rootly_session_token"
        )
        assert identity["client_ip"] == "192.0.2.8"
        assert identity["request_id"] == "req-session-1"
        assert identity["transport"] == "sse"
        assert identity["transport_effective"] == "sse"
        assert identity["auth_header_state"] == "bearer"

    def test_current_tool_identity_reports_invalid_auth_header_shape(self):
        token_ctx = server_module._session_auth_token.set("rootly_session_token")
        try:
            with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
                identity = _current_tool_identity()
        finally:
            server_module._session_auth_token.reset(token_ctx)

        assert identity["token_fingerprint"] == _fingerprint_auth_header("rootly_session_token")
        assert identity["auth_header_state"] == "invalid_format"

    def test_current_tool_identity_prefers_session_transport_over_runtime(self):
        token_ctx = server_module._session_auth_token.set("Bearer rootly_session_token")
        transport_ctx = server_module._session_transport.set("streamable-http")
        mode_ctx = server_module._session_mcp_mode.set("code-mode")
        try:
            with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
                with patch("fastmcp.server.context._current_transport") as mock_transport:
                    mock_transport.get.return_value = "both"
                    identity = _current_tool_identity()
        finally:
            server_module._session_auth_token.reset(token_ctx)
            server_module._session_transport.reset(transport_ctx)
            server_module._session_mcp_mode.reset(mode_ctx)

        assert identity["transport_runtime"] == "both"
        assert identity["transport_effective"] == "streamable-http"
        assert identity["transport"] == "streamable-http"
        assert identity["mcp_mode"] == "code-mode"

    def test_current_tool_identity_defaults_mcp_mode_to_classic(self):
        with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
            identity = _current_tool_identity()

        assert identity["mcp_mode"] == "classic"

    def test_log_tool_usage_event_emits_json_line(self):
        with patch.object(server_module, "_configure_tool_usage_json_logger") as mock_configure:
            with patch.object(server_module._tool_usage_json_logger, "info") as mock_info:
                server_module._log_tool_usage_event(
                    tool_name="search_incidents",
                    status="success",
                    duration_ms=123.456,
                    arg_keys=["page_size", "page_number"],
                    identity={
                        "auth_header_state": "bearer",
                        "token_fingerprint": "abc123",
                        "client_ip": "203.0.113.10",
                        "request_id": "req-1",
                        "transport": "sse",
                        "transport_effective": "sse",
                        "transport_runtime": "both",
                        "mcp_mode": "classic",
                    },
                )

        mock_configure.assert_called_once()
        mock_info.assert_called_once()
        payload = json.loads(mock_info.call_args[0][0])
        assert payload["event"] == "mcp_tool_call"
        assert payload["tool_name"] == "search_incidents"
        assert payload["status"] == "success"
        assert payload["duration_ms"] == 123.46
        assert payload["transport"] == "sse"
        assert payload["transport_effective"] == "sse"
        assert payload["transport_runtime"] == "both"
        assert payload["mcp_mode"] == "classic"
        assert payload["auth_header_state"] == "bearer"

    def test_log_tool_usage_event_includes_error_context(self):
        with patch.object(server_module, "_configure_tool_usage_json_logger"):
            with patch.object(server_module._tool_usage_json_logger, "info") as mock_info:
                server_module._log_tool_usage_event(
                    tool_name="listAlerts",
                    status="error",
                    duration_ms=42.0,
                    arg_keys=["page_size"],
                    identity={
                        "token_fingerprint": "abc123",
                        "client_ip": "203.0.113.10",
                        "request_id": "req-1",
                        "transport": "sse",
                        "transport_effective": "sse",
                        "transport_runtime": "both",
                        "mcp_mode": "classic",
                    },
                    error_type="ToolError",
                    error_context={
                        "error_message": "boom",
                        "upstream_status": 502,
                        "traceback_excerpt": "Traceback... trimmed",
                    },
                )

        payload = json.loads(mock_info.call_args[0][0])
        assert payload["error_message"] == "boom"
        assert payload["upstream_status"] == 502
        assert payload["traceback_excerpt"] == "Traceback... trimmed"

    def test_extract_structured_tool_error_from_call_tool_result(self):
        result = mt.CallToolResult(
            content=[],
            structuredContent={
                "error": True,
                "error_type": "validation_error",
                "message": "Bad input at /Users/spencercheng/file.py",
                "details": {
                    "status_code": 422,
                    "exception_type": "ValidationError",
                    "traceback": 'Traceback (most recent call last):\n  File "/tmp/app.py", line 1',
                    "api_token": "secret-token",
                },
            },
            isError=True,
        )

        error_context = _extract_structured_tool_error(result)

        assert error_context["error_type"] == "validation_error"
        assert error_context["error_message"].startswith("Bad input")
        assert "[file]" in error_context["error_message"]
        assert error_context["upstream_status"] == 422
        assert error_context["exception_type"] == "ValidationError"
        assert "[file]" in error_context["traceback_excerpt"]
        assert error_context["error_details"]["api_token"] == "***REDACTED***"

    def test_extract_structured_tool_error_from_structured_content_error_flag(self):
        result = mt.CallToolResult(
            content=[],
            structuredContent={
                "error": True,
                "message": "Tool failed",
                "error_type": "client_error",
            },
            isError=False,
        )

        error_context = _extract_structured_tool_error(result)

        assert error_context["error_type"] == "client_error"
        assert error_context["error_message"] == "Tool failed"

    def test_format_traceback_excerpt_sanitizes_paths(self):
        excerpt = _format_traceback_excerpt(
            'Traceback (most recent call last):\n  File "/Users/spencercheng/app.py", line 10, in test'
        )
        assert "[file]" in excerpt
        assert "/Users/spencercheng" not in excerpt

    @pytest.mark.asyncio
    async def test_tool_usage_middleware_logs_returned_tool_errors(self):
        middleware = server_module.ToolUsageLoggingMiddleware()
        context = SimpleNamespace(
            message=SimpleNamespace(name="listAlerts", arguments={"page_size": 10})
        )
        result = mt.CallToolResult(
            content=[],
            structuredContent={
                "error": True,
                "error_type": "execution_error",
                "message": "Failed to fetch alerts",
                "details": {"status_code": 502, "exception_type": "HTTPStatusError"},
            },
            isError=True,
        )

        async def call_next(context: Any):
            return result

        with patch.object(
            server_module, "_current_tool_identity", return_value={"mcp_mode": "classic"}
        ):
            with patch.object(server_module, "_log_tool_usage_event") as mock_log:
                returned = await middleware.on_call_tool(cast(Any, context), cast(Any, call_next))

        assert returned is result
        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["status"] == "error"
        assert kwargs["error_type"] == "execution_error"
        assert kwargs["error_context"]["upstream_status"] == 502
        assert kwargs["error_context"]["exception_type"] == "HTTPStatusError"

    @pytest.mark.asyncio
    async def test_tool_usage_middleware_logs_exception_context(self):
        middleware = server_module.ToolUsageLoggingMiddleware()
        context = SimpleNamespace(
            message=SimpleNamespace(name="listTeams", arguments={"page_size": 10})
        )

        async def call_next(context: Any):
            server_module._session_error_context.set(
                {
                    "upstream_status": 503,
                    "upstream_url": "https://api.rootly.com/v1/teams",
                    "upstream_response_excerpt": "service unavailable",
                }
            )
            raise RuntimeError("boom")

        with patch.object(
            server_module, "_current_tool_identity", return_value={"mcp_mode": "classic"}
        ):
            with patch.object(server_module, "_log_tool_usage_event") as mock_log:
                with pytest.raises(RuntimeError):
                    await middleware.on_call_tool(cast(Any, context), cast(Any, call_next))

        kwargs = mock_log.call_args.kwargs
        assert kwargs["status"] == "error"
        assert kwargs["error_type"] == "RuntimeError"
        assert kwargs["error_context"]["exception_type"] == "RuntimeError"
        assert kwargs["error_context"]["upstream_status"] == 503
        assert kwargs["error_context"]["upstream_url"] == "https://api.rootly.com/v1/teams"


@pytest.mark.unit
class TestSwaggerSpecLoading:
    """Test OpenAPI/Swagger specification loading functionality."""

    def test_load_spec_from_file(self):
        """Test loading OpenAPI spec from local file."""
        mock_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }

        with patch("os.path.isfile", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_spec))):
                spec = _load_swagger_spec("/path/to/swagger.json")

                assert spec == mock_spec
                assert spec["openapi"] == "3.0.0"

    def test_load_spec_from_url(self):
        """Test loading OpenAPI spec from remote URL."""
        mock_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Remote API", "version": "1.0.0"},
            "paths": {},
        }

        with patch("pathlib.Path.is_file", return_value=False):
            with patch("requests.get") as mock_get:
                mock_response = Mock()
                mock_response.json.return_value = mock_spec
                mock_response.raise_for_status.return_value = None
                mock_get.return_value = mock_response

                spec = _load_swagger_spec(None)

                assert spec == mock_spec
                mock_get.assert_called_once()

    def test_load_spec_file_not_found(self):
        """Test behavior when swagger file is not found."""
        mock_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }

        # Mock all the path checking methods to return False
        with patch("os.path.isfile", return_value=False):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch("requests.get") as mock_get:
                    mock_response = Mock()
                    mock_response.json.return_value = mock_spec
                    mock_response.raise_for_status.return_value = None
                    mock_get.return_value = mock_response

                    # Should fall back to URL loading when no local files found
                    spec = _load_swagger_spec(None)

                    assert spec == mock_spec
                    mock_get.assert_called_once()


@pytest.mark.unit
class TestOpenAPISpecFiltering:
    """Test OpenAPI specification filtering functionality."""

    def test_filter_spec_with_allowed_paths(self):
        """Test filtering OpenAPI spec to include only allowed paths."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/incidents": {"get": {"operationId": "listIncidents"}},
                "/teams": {"get": {"operationId": "listTeams"}},
                "/forbidden": {"get": {"operationId": "forbiddenEndpoint"}},
            },
            "components": {"schemas": {}},
        }

        allowed_paths = ["/incidents", "/teams"]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        assert len(filtered_spec["paths"]) == 2
        assert "/incidents" in filtered_spec["paths"]
        assert "/teams" in filtered_spec["paths"]
        assert "/forbidden" not in filtered_spec["paths"]

        # Verify pagination parameters were added to /incidents endpoint
        incidents_get = filtered_spec["paths"]["/incidents"]["get"]
        assert "parameters" in incidents_get
        param_names = [p["name"] for p in incidents_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

        # Verify /teams endpoint does not get pagination (doesn't contain "incidents" or "alerts")
        teams_get = filtered_spec["paths"]["/teams"]["get"]
        if "parameters" in teams_get:
            param_names = [p["name"] for p in teams_get["parameters"]]
            assert "page[size]" not in param_names

        # Verify other properties are preserved
        assert filtered_spec["openapi"] == original_spec["openapi"]
        assert filtered_spec["info"] == original_spec["info"]

    def test_filter_spec_no_paths_match(self):
        """Test filtering when no paths match allowed list."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {"/other": {"get": {}}},
            "components": {"schemas": {}},
        }

        allowed_paths = ["/incidents"]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        assert len(filtered_spec["paths"]) == 0

    def test_filter_spec_preserve_structure(self):
        """Test that filtering preserves OpenAPI spec structure."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {"/incidents": {"get": {"operationId": "listIncidents"}}},
            "components": {
                "schemas": {"Incident": {"type": "object"}},
                "securitySchemes": {"bearer": {"type": "http"}},
            },
        }

        filtered_spec = _filter_openapi_spec(original_spec, ["/incidents"])

        # Verify all sections are preserved
        assert "openapi" in filtered_spec
        assert "info" in filtered_spec
        assert "servers" in filtered_spec
        assert "components" in filtered_spec
        assert filtered_spec["servers"] == original_spec["servers"]

        # Verify pagination parameters were added to /incidents endpoint
        incidents_get = filtered_spec["paths"]["/incidents"]["get"]
        assert "parameters" in incidents_get
        param_names = [p["name"] for p in incidents_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

    def test_filter_spec_adds_pagination_to_alerts(self):
        """Test that pagination parameters are added to alerts endpoints."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/alerts": {"get": {"operationId": "listAlerts"}},
                "/incidents/123/alerts": {"get": {"operationId": "listIncidentAlerts"}},
                "/users": {"get": {"operationId": "listUsers"}},
            },
            "components": {"schemas": {}},
        }

        allowed_paths = ["/alerts", "/incidents/123/alerts", "/users"]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        # Verify pagination was added to alerts endpoints
        alerts_get = filtered_spec["paths"]["/alerts"]["get"]
        assert "parameters" in alerts_get
        param_names = [p["name"] for p in alerts_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

        incident_alerts_get = filtered_spec["paths"]["/incidents/123/alerts"]["get"]
        assert "parameters" in incident_alerts_get
        param_names = [p["name"] for p in incident_alerts_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

        # Verify pagination was NOT added to /users (no "incident" or "alerts" in path)
        users_get = filtered_spec["paths"]["/users"]["get"]
        if "parameters" in users_get:
            param_names = [p["name"] for p in users_get["parameters"]]
            assert "page[size]" not in param_names

    def test_filter_spec_adds_filter_params_to_alerts(self):
        """Test that filter parameters are added to alerts endpoints."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/alerts": {"get": {"operationId": "listAlerts"}},
            },
            "components": {"schemas": {}},
        }

        allowed_paths = ["/alerts"]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        # Verify filter params were added to alerts endpoints
        alerts_get = filtered_spec["paths"]["/alerts"]["get"]
        assert "parameters" in alerts_get
        param_names = [p["name"] for p in alerts_get["parameters"]]

        # Check for the new filter parameters
        assert "filter[status]" in param_names
        assert "filter[groups]" in param_names
        assert "filter[services]" in param_names
        assert "filter[environments]" in param_names
        assert "filter[labels]" in param_names
        assert "filter[source]" in param_names
        assert "filter[started_at][gte]" in param_names
        assert "filter[started_at][lte]" in param_names
        assert "filter[ended_at][gte]" in param_names
        assert "filter[ended_at][lte]" in param_names
        assert "filter[created_at][gte]" in param_names
        assert "filter[created_at][lte]" in param_names

    def test_filter_spec_adds_pagination_to_incident_types(self):
        """Test that pagination parameters are added to incident-related endpoints."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/incident_types": {"get": {"operationId": "listIncidentTypes"}},
                "/incident_action_items": {"get": {"operationId": "listIncidentActionItems"}},
                "/services": {"get": {"operationId": "listServices"}},
            },
            "components": {"schemas": {}},
        }

        allowed_paths = ["/incident_types", "/incident_action_items", "/services"]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        # Verify pagination was added to incident-related endpoints
        incident_types_get = filtered_spec["paths"]["/incident_types"]["get"]
        assert "parameters" in incident_types_get
        param_names = [p["name"] for p in incident_types_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

        incident_action_items_get = filtered_spec["paths"]["/incident_action_items"]["get"]
        assert "parameters" in incident_action_items_get
        param_names = [p["name"] for p in incident_action_items_get["parameters"]]
        assert "page[size]" in param_names
        assert "page[number]" in param_names

        # Verify pagination was NOT added to /services (no "incident" or "alerts" in path)
        services_get = filtered_spec["paths"]["/services"]["get"]
        if "parameters" in services_get:
            param_names = [p["name"] for p in services_get["parameters"]]
            assert "page[size]" not in param_names

    def test_filter_spec_keeps_exact_path_matches(self):
        """Test exact allowlist path matching still works."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/schedules/{schedule_id}": {"get": {"operationId": "getSchedule"}},
                "/v1/ignored": {"get": {"operationId": "ignored"}},
            },
            "components": {"schemas": {}},
        }

        filtered_spec = _filter_openapi_spec(original_spec, ["/v1/schedules/{schedule_id}"])

        assert "/v1/schedules/{schedule_id}" in filtered_spec["paths"]
        assert "/v1/ignored" not in filtered_spec["paths"]

    def test_filter_spec_matches_parameter_name_variants(self):
        """Test allowlist entries match OpenAPI paths with different path parameter names."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/schedules/{id}": {"get": {"operationId": "getSchedule"}},
                "/v1/escalation_policies/{id}": {"get": {"operationId": "getEscalationPolicy"}},
                "/v1/teams/{id}": {"get": {"operationId": "getTeam"}},
            },
            "components": {"schemas": {}},
        }

        allowed_paths = [
            "/v1/schedules/{schedule_id}",
            "/v1/escalation_policies/{escalation_policy_id}",
        ]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)

        assert "/v1/schedules/{id}" in filtered_spec["paths"]
        assert "/v1/escalation_policies/{id}" in filtered_spec["paths"]
        assert "/v1/teams/{id}" not in filtered_spec["paths"]

    def test_filter_spec_excludes_non_allowlisted_normalized_paths(self):
        """Test normalized matching does not include non-allowlisted sibling paths."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/schedules/{id}": {"get": {"operationId": "getSchedule"}},
                "/v1/schedules/{id}/shifts": {"get": {"operationId": "getScheduleShifts"}},
            },
            "components": {"schemas": {}},
        }

        filtered_spec = _filter_openapi_spec(original_spec, ["/v1/schedules/{schedule_id}"])

        assert "/v1/schedules/{id}" in filtered_spec["paths"]
        assert "/v1/schedules/{id}/shifts" not in filtered_spec["paths"]

    def test_filter_spec_includes_full_screenshot_coverage_with_delete_allowlist(self):
        """Test screenshot families include full coverage, including allowed delete operations."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/schedules/{schedule_id}/schedule_rotations": {
                    "get": {"operationId": "listScheduleRotations"},
                    "post": {"operationId": "createScheduleRotation"},
                },
                "/v1/escalation_policies": {
                    "get": {"operationId": "listEscalationPolicies"},
                    "post": {"operationId": "createEscalationPolicy"},
                },
                "/v1/escalation_policies/{id}": {
                    "get": {"operationId": "getEscalationPolicy"},
                    "put": {"operationId": "updateEscalationPolicy"},
                    "delete": {"operationId": "deleteEscalationPolicy"},
                },
                "/v1/escalation_policies/{escalation_policy_id}/escalation_paths": {
                    "get": {"operationId": "listEscalationPaths"},
                    "post": {"operationId": "createEscalationPath"},
                },
                "/v1/escalation_paths/{id}": {
                    "get": {"operationId": "getEscalationPath"},
                    "put": {"operationId": "updateEscalationPath"},
                    "delete": {"operationId": "deleteEscalationPath"},
                },
                "/v1/escalation_paths/{escalation_policy_path_id}/escalation_levels": {
                    "get": {"operationId": "listEscalationLevelsPaths"},
                    "post": {"operationId": "createEscalationLevelPaths"},
                },
                "/v1/escalation_levels/{id}": {
                    "get": {"operationId": "getEscalationLevel"},
                    "put": {"operationId": "updateEscalationLevel"},
                    "delete": {"operationId": "deleteEscalationLevel"},
                },
            },
            "components": {"schemas": {}},
        }

        allowed_paths = [
            "/v1/schedules/{schedule_id}/schedule_rotations",
            "/v1/escalation_policies",
            "/v1/escalation_policies/{escalation_policy_id}",
            "/v1/escalation_policies/{escalation_policy_id}/escalation_paths",
            "/v1/escalation_paths/{escalation_policy_path_id}",
            "/v1/escalation_paths/{escalation_policy_path_id}/escalation_levels",
            "/v1/escalation_levels/{escalation_level_id}",
        ]
        delete_allowed_paths = [
            "/v1/escalation_policies/{escalation_policy_id}",
            "/v1/escalation_paths/{escalation_policy_path_id}",
            "/v1/escalation_levels/{escalation_level_id}",
        ]
        filtered_spec = _filter_openapi_spec(
            original_spec,
            allowed_paths,
            delete_allowed_paths=delete_allowed_paths,
            enable_write_tools=True,
        )
        filtered_paths = filtered_spec["paths"]

        assert set(filtered_paths["/v1/schedules/{schedule_id}/schedule_rotations"]) >= {
            "get",
            "post",
        }
        assert set(filtered_paths["/v1/escalation_policies"]) >= {"get", "post"}
        assert set(filtered_paths["/v1/escalation_policies/{id}"]) >= {"get", "put", "delete"}
        assert set(
            filtered_paths["/v1/escalation_policies/{escalation_policy_id}/escalation_paths"]
        ) >= {
            "get",
            "post",
        }
        assert set(filtered_paths["/v1/escalation_paths/{id}"]) >= {"get", "put", "delete"}
        assert set(
            filtered_paths["/v1/escalation_paths/{escalation_policy_path_id}/escalation_levels"]
        ) >= {
            "get",
            "post",
        }
        assert set(filtered_paths["/v1/escalation_levels/{id}"]) >= {"get", "put", "delete"}

    def test_filter_spec_strips_delete_operations(self):
        """Test that delete methods are removed from MCP-exposed operations."""
        original_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/v1/schedules/{id}": {
                    "get": {"operationId": "getSchedule"},
                    "delete": {"operationId": "deleteSchedule"},
                },
                "/v1/delete_only_resource/{id}": {
                    "delete": {"operationId": "deleteOnlyResource"},
                },
            },
            "components": {"schemas": {}},
        }

        allowed_paths = [
            "/v1/schedules/{schedule_id}",
            "/v1/delete_only_resource/{resource_id}",
        ]
        filtered_spec = _filter_openapi_spec(original_spec, allowed_paths)
        filtered_paths = filtered_spec["paths"]

        assert "/v1/schedules/{id}" in filtered_paths
        assert "get" in filtered_paths["/v1/schedules/{id}"]
        assert "delete" not in filtered_paths["/v1/schedules/{id}"]

        # Path with only delete should be removed from exposed paths entirely.
        assert "/v1/delete_only_resource/{id}" not in filtered_paths


@pytest.mark.unit
class TestDefaultConfiguration:
    """Test default configuration values."""

    def test_default_allowed_paths_exist(self):
        """Test that default allowed paths are defined."""
        assert DEFAULT_ALLOWED_PATHS is not None
        assert isinstance(DEFAULT_ALLOWED_PATHS, list)
        assert len(DEFAULT_ALLOWED_PATHS) > 0

        # Verify some expected paths are included
        path_strings = str(DEFAULT_ALLOWED_PATHS)
        assert "incidents" in path_strings
        assert "teams" in path_strings
        assert "/schedules/{schedule_id}/schedule_rotations" in DEFAULT_ALLOWED_PATHS
        assert "/escalation_policies" in DEFAULT_ALLOWED_PATHS
        assert "/escalation_paths/{escalation_policy_path_id}" in DEFAULT_ALLOWED_PATHS
        assert "/escalation_policies/{escalation_policy_id}" in DEFAULT_DELETE_ALLOWED_PATHS
        assert "/escalation_paths/{escalation_policy_path_id}" in DEFAULT_DELETE_ALLOWED_PATHS
        assert "/escalation_levels/{escalation_level_id}" in DEFAULT_DELETE_ALLOWED_PATHS

    def test_default_swagger_url(self):
        """Test that default swagger URL is properly defined."""
        from rootly_mcp_server.server import SWAGGER_URL

        assert SWAGGER_URL is not None
        assert isinstance(SWAGGER_URL, str)
        assert SWAGGER_URL.startswith("https://")
        assert "swagger" in SWAGGER_URL.lower()


@pytest.mark.unit
class TestOAuthProtectedResourceRoute:
    """Tests for OAuth protected resource metadata route."""

    def test_oauth_route_registered_in_hosted_mode(self, mock_httpx_client):
        """In hosted mode, /.well-known/oauth-protected-resource route is registered."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server(hosted=True)

            # Check that the route is in additional HTTP routes
            routes = server._get_additional_http_routes()
            route_paths = [getattr(r, "path", None) for r in routes]
            assert OAUTH_PROTECTED_RESOURCE_PATH in route_paths
            # RFC 9728 §5: path-suffixed variant
            assert OAUTH_PROTECTED_RESOURCE_PATH + "/{path:path}" in route_paths

    def test_oauth_route_not_registered_in_non_hosted_mode(self, mock_httpx_client):
        """In non-hosted mode, /.well-known/oauth-protected-resource route is NOT registered."""
        with patch("rootly_mcp_server.server._load_swagger_spec") as mock_load_spec:
            mock_spec = {
                "openapi": "3.0.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            mock_load_spec.return_value = mock_spec

            server = create_rootly_mcp_server(hosted=False)

            routes = server._get_additional_http_routes()
            route_paths = [getattr(r, "path", None) for r in routes]
            assert OAUTH_PROTECTED_RESOURCE_PATH not in route_paths
