"""Focused tests for transport module."""

import json
from unittest.mock import AsyncMock, patch

import httpx2
import pytest

from rootly_mcp_server import transport
from rootly_mcp_server.exceptions import RootlyValidationError


class TestTransportModule:
    """Direct tests for extracted transport/auth helpers."""

    @pytest.fixture(autouse=True)
    def _bypass_token_probe(self):
        with patch.object(
            transport.AuthCaptureMiddleware,
            "_validate_token_upstream",
            return_value={"id": "user_123", "email": "example.user@example.test"},
        ):
            yield

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_sets_token_for_sse(self):
        async def app(scope, receive, send):
            return None

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [(b"authorization", b"Bearer test-token")],
        }

        # Ensure a known baseline in this context.
        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer test-token"
        assert transport._session_transport.get() == "sse"
        assert transport._session_mcp_mode.get() == "classic"
        assert transport._session_authenticated_user.get() == {
            "id": "user_123",
            "email": "example.user@example.test",
        }

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_sets_token_for_streamable_http(self):
        async def app(scope, receive, send):
            return None

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer streamable-token")],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer streamable-token"
        assert transport._session_transport.get() == "streamable-http"
        assert transport._session_mcp_mode.get() == "classic"

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_sets_transport_for_messages_path(self):
        async def app(scope, receive, send):
            return None

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/messages",
            "headers": [(b"authorization", b"Bearer sse-message-token")],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer sse-message-token"
        assert transport._session_transport.get() == "sse"
        assert transport._session_mcp_mode.get() == "classic"

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_sets_transport_for_code_mode_path(self):
        async def app(scope, receive, send):
            return None

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp-codemode",
            "headers": [(b"authorization", b"Bearer codemode-token")],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer codemode-token"
        assert transport._session_transport.get() == "streamable-http"
        assert transport._session_mcp_mode.get() == "code-mode"

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_ignores_non_mcp_paths(self):
        async def app(scope, receive, send):
            return None

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/healthz",
            "headers": [(b"authorization", b"Bearer should-not-be-used")],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        assert transport._session_auth_token.get() == ""
        assert transport._session_transport.get() == ""
        assert transport._session_mcp_mode.get() == ""

    @pytest.mark.asyncio
    async def test_auth_capture_middleware_respects_custom_paths(self):
        async def app(scope, receive, send):
            return None

        with patch.dict(
            "os.environ",
            {
                "FASTMCP_SSE_PATH": "/custom-sse",
                "FASTMCP_MESSAGE_PATH": "/custom-messages",
                "FASTMCP_STREAMABLE_HTTP_PATH": "/custom-mcp",
                "ROOTLY_CODE_MODE_PATH": "/custom-codemode",
            },
        ):
            middleware = transport.AuthCaptureMiddleware(app)

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")
        transport._session_authenticated_user.set(None)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        custom_scope = {
            "type": "http",
            "path": "/custom-mcp",
            "headers": [(b"authorization", b"Bearer custom-token")],
        }
        await middleware(custom_scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer custom-token"
        assert transport._session_transport.get() == "streamable-http"
        assert transport._session_mcp_mode.get() == "classic"

        custom_message_scope = {
            "type": "http",
            "path": "/custom-messages",
            "headers": [(b"authorization", b"Bearer custom-message-token")],
        }
        await middleware(custom_message_scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer custom-message-token"
        assert transport._session_transport.get() == "sse"
        assert transport._session_mcp_mode.get() == "classic"

        custom_code_mode_scope = {
            "type": "http",
            "path": "/custom-codemode",
            "headers": [(b"authorization", b"Bearer custom-codemode-token")],
        }
        await middleware(custom_code_mode_scope, receive, send)
        assert transport._session_auth_token.get() == "Bearer custom-codemode-token"
        assert transport._session_transport.get() == "streamable-http"
        assert transport._session_mcp_mode.get() == "code-mode"

    def test_infer_transport_from_path(self):
        assert (
            transport._infer_transport_from_path(
                "/sse", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "sse"
        )
        assert (
            transport._infer_transport_from_path(
                "/messages", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "sse"
        )
        assert (
            transport._infer_transport_from_path(
                "/mcp", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "streamable-http"
        )
        assert (
            transport._infer_transport_from_path(
                "/mcp-codemode", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "streamable-http"
        )

    def test_extract_rootly_user_identity_returns_lightweight_user(self):
        payload = {
            "data": {
                "id": "user_123",
                "attributes": {
                    "email": "example.user@example.test",
                    "full_name": "Example User",
                    "full_name_with_team": "[Acme Reliability] Example User",
                },
            }
        }

        user = transport._extract_rootly_user_identity(payload)

        assert user == {
            "id": "user_123",
            "email": "example.user@example.test",
            "full_name_with_team": "[Acme Reliability] Example User",
            "name": "Example User",
        }

    def test_extract_rootly_user_identity_returns_none_without_id(self):
        payload = {"data": {"attributes": {"email": "example.user@example.test"}}}

        assert transport._extract_rootly_user_identity(payload) is None
        assert (
            transport._infer_transport_from_path(
                "/healthz", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == ""
        )

    def test_infer_mcp_mode_from_path(self):
        assert (
            transport._infer_mcp_mode_from_path(
                "/sse", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "classic"
        )
        assert (
            transport._infer_mcp_mode_from_path(
                "/messages", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "classic"
        )
        assert (
            transport._infer_mcp_mode_from_path(
                "/mcp", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "classic"
        )
        assert (
            transport._infer_mcp_mode_from_path(
                "/mcp-codemode", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == "code-mode"
        )
        assert (
            transport._infer_mcp_mode_from_path(
                "/healthz", "/sse", "/messages", "/mcp", "/mcp-codemode"
            )
            == ""
        )

    def test_normalize_incident_form_field_selection_payload_textarea(self):
        payload = {
            "data": {
                "id": "selection-1",
                "type": "incident_form_field_selections",
                "attributes": {
                    "value": "Decagon Chat API degraded",
                    "selected_group_ids": [],
                    "selected_option_ids": [],
                    "selected_service_ids": [],
                    "selected_functionality_ids": [],
                    "selected_catalog_entity_ids": [],
                    "selected_user_ids": [],
                    "selected_groups": {"id": None, "value": "Decagon Chat API degraded"},
                    "selected_options": {"id": None, "value": "Decagon Chat API degraded"},
                    "selected_services": {"id": None, "value": "Decagon Chat API degraded"},
                    "selected_functionalities": {
                        "id": None,
                        "value": "Decagon Chat API degraded",
                    },
                    "selected_catalog_entities": {
                        "id": None,
                        "value": "Decagon Chat API degraded",
                    },
                    "selected_users": {"id": None, "value": "Decagon Chat API degraded"},
                    "selected_environments": {
                        "id": None,
                        "value": "Decagon Chat API degraded",
                    },
                    "selected_causes": {"id": None, "value": "Decagon Chat API degraded"},
                    "selected_incident_types": {
                        "id": None,
                        "value": "Decagon Chat API degraded",
                    },
                    "form_field": {"input_kind": "textarea"},
                },
            }
        }

        normalized = (
            transport.AuthenticatedHTTPXClient._normalize_incident_form_field_selection_payload(
                payload
            )
        )
        attributes = normalized["data"]["attributes"]

        assert attributes["value"] == "Decagon Chat API degraded"
        assert attributes["selected_group_ids"] == []
        assert "selected_groups" not in attributes
        assert "selected_options" not in attributes
        assert "selected_services" not in attributes
        assert "selected_functionalities" not in attributes
        assert "selected_catalog_entities" not in attributes
        assert "selected_users" not in attributes
        assert "selected_environments" not in attributes
        assert "selected_causes" not in attributes
        assert "selected_incident_types" not in attributes

    def test_normalize_incident_form_field_selection_payload_select_unchanged(self):
        payload = {
            "data": {
                "id": "selection-2",
                "type": "incident_form_field_selections",
                "attributes": {
                    "selected_option_ids": ["opt-1"],
                    "selected_options": {"id": "opt-1", "value": "Database"},
                    "form_field": {"input_kind": "select"},
                },
            }
        }

        normalized = (
            transport.AuthenticatedHTTPXClient._normalize_incident_form_field_selection_payload(
                payload
            )
        )

        assert normalized == payload

    def test_normalize_incident_form_field_selection_list_payload(self):
        payload = {
            "data": [
                {
                    "id": "selection-3",
                    "type": "incident_form_field_selections",
                    "attributes": {
                        "value": "External user impact only",
                        "selected_groups": {"id": None, "value": "External user impact only"},
                        "selected_group_ids": [],
                        "form_field": {"input_kind": "textarea"},
                    },
                },
                {
                    "id": "selection-4",
                    "type": "incident_form_field_selections",
                    "attributes": {
                        "selected_options": {"id": "opt-1", "value": "Database"},
                        "selected_option_ids": ["opt-1"],
                        "form_field": {"input_kind": "select"},
                    },
                },
            ]
        }

        normalized = (
            transport.AuthenticatedHTTPXClient._normalize_incident_form_field_selection_payload(
                payload
            )
        )

        assert "selected_groups" not in normalized["data"][0]["attributes"]
        assert normalized["data"][1]["attributes"]["selected_options"] == {
            "id": "opt-1",
            "value": "Database",
        }

    def test_maybe_normalize_incident_form_field_selection_response(self):
        response = httpx2.Response(
            200,
            json={
                "data": {
                    "id": "selection-5",
                    "type": "incident_form_field_selections",
                    "attributes": {
                        "value": "External user impact only",
                        "selected_groups": {"id": None, "value": "External user impact only"},
                        "selected_group_ids": [],
                        "form_field": {"input_kind": "textarea"},
                    },
                }
            },
        )

        result = transport.AuthenticatedHTTPXClient._maybe_normalize_incident_form_field_selection_response(
            "PUT", "/v1/incident_form_field_selections/selection-5", response
        )
        parsed = result.json()

        assert parsed["data"]["attributes"]["value"] == "External user impact only"
        assert parsed["data"]["attributes"]["selected_group_ids"] == []
        assert "selected_groups" not in parsed["data"]["attributes"]

    def test_maybe_normalize_incident_form_field_selection_list_response(self):
        response = httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "id": "selection-6",
                        "type": "incident_form_field_selections",
                        "attributes": {
                            "value": "External user impact only",
                            "selected_groups": {"id": None, "value": "External user impact only"},
                            "selected_group_ids": [],
                            "form_field": {"input_kind": "textarea"},
                        },
                    }
                ]
            },
        )

        result = transport.AuthenticatedHTTPXClient._maybe_normalize_incident_form_field_selection_response(
            "GET", "/v1/incidents/inc-123/form_field_selections", response
        )
        parsed = result.json()

        assert parsed["data"][0]["attributes"]["selected_group_ids"] == []
        assert "selected_groups" not in parsed["data"][0]["attributes"]

    def test_authenticated_client_user_agent_contains_mode(self):
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            local_client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            hosted_client = transport.AuthenticatedHTTPXClient(hosted=True, transport="sse")

        local_ua = local_client.client.headers.get("User-Agent")
        hosted_ua = hosted_client.client.headers.get("User-Agent")

        assert local_ua is not None
        assert hosted_ua is not None
        assert "(stdio; self-hosted)" in local_ua
        assert "(sse; hosted)" in hosted_ua

    @pytest.mark.asyncio
    async def test_authenticated_client_records_upstream_error_response_context(self):
        response = httpx2.Response(
            502,
            request=httpx2.Request("GET", "https://api.rootly.com/v1/incidents?page[size]=10"),
            content=b'{"error":"backend down","api_token":"secret"}',
        )

        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(
                hosted=False,
                transport="stdio",
                parameter_mapping={
                    "filter_status": "filter[status]",
                    "filter_source": "filter[source]",
                    "filter_services": "filter[services]",
                    "page_number": "page[number]",
                    "page_size": "page[size]",
                },
            )
            client.client.request = AsyncMock(return_value=response)

            returned = await client.request("GET", "/v1/incidents")

        error_context = transport._get_error_context()

        assert returned.status_code == 502
        assert error_context["upstream_status"] == 502
        assert error_context["upstream_method"] == "GET"
        assert error_context["upstream_url"] == "https://api.rootly.com/v1/incidents"
        assert error_context["upstream_path"] == "/v1/incidents"
        assert "***REDACTED***" in error_context["upstream_response_excerpt"]

    @pytest.mark.asyncio
    async def test_authenticated_client_records_upstream_exception_context(self):
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock(side_effect=httpx2.ReadTimeout("request timed out"))

            with pytest.raises(httpx2.ReadTimeout):
                await client.request("GET", "/v1/teams")

        error_context = transport._get_error_context()
        assert error_context["upstream_exception_type"] == "ReadTimeout"
        assert error_context["upstream_exception_message"] == "request timed out"
        assert error_context["upstream_path"] == "/v1/teams"
        assert error_context["upstream_log_level"] == "error"

    @pytest.mark.asyncio
    async def test_authenticated_client_preserves_failure_context_across_followup_success(self):
        responses = [
            httpx2.Response(
                502,
                request=httpx2.Request("GET", "https://api.rootly.com/v1/alerts"),
                content=b'{"error":"backend down"}',
            ),
            httpx2.Response(
                200,
                request=httpx2.Request("GET", "https://api.rootly.com/v1/users/me"),
                content=b'{"data":{"id":"1"}}',
            ),
        ]

        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock(side_effect=responses)

            transport._clear_error_context()
            await client.request("GET", "/v1/alerts")
            await client.request("GET", "/v1/users/me")

        error_context = transport._get_error_context()
        assert error_context["upstream_status"] == 502
        assert error_context["upstream_path"] == "/v1/alerts"
        assert error_context["upstream_log_level"] == "error"

    @pytest.mark.asyncio
    async def test_authenticated_client_unwraps_body_envelope_on_write_requests(self):
        response = httpx2.Response(
            200,
            request=httpx2.Request("POST", "https://api.rootly.com/v1/workflows"),
            content=b'{"data":{"id":"wf-1"}}',
        )

        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock(return_value=response)

            await client.request(
                "POST",
                "/v1/workflows",
                json={"body": {"genius_workflow": {"name": "MCP verification workflow"}}},
            )

            _, kwargs = client.client.request.call_args
            assert kwargs["json"] == {"genius_workflow": {"name": "MCP verification workflow"}}

    @pytest.mark.asyncio
    async def test_authenticated_client_preserves_non_envelope_payload_on_update_requests(self):
        response = httpx2.Response(
            200,
            request=httpx2.Request("PATCH", "https://api.rootly.com/v1/workflows/wf-1"),
            content=b'{"data":{"id":"wf-1"}}',
        )

        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock(return_value=response)

            await client.request(
                "PATCH",
                "/v1/workflows/wf-1",
                json={"data": {"type": "workflows", "attributes": {"name": "Updated"}}},
            )

            _, kwargs = client.client.request.call_args
            assert kwargs["json"] == {
                "data": {"type": "workflows", "attributes": {"name": "Updated"}}
            }

    def test_authenticated_client_drops_empty_query_parameters(self):
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(
                hosted=False,
                transport="stdio",
                parameter_mapping={
                    "filter_status": "filter[status]",
                    "filter_source": "filter[source]",
                    "filter_services": "filter[services]",
                    "page_number": "page[number]",
                    "page_size": "page[size]",
                },
            )
        assert client._transform_params(
            {
                "filter_status": "",
                "filter_source": "   ",
                "filter_services": [
                    "svc-1",
                    "",
                    "svc-2",
                ],
                "page_number": 1,
                "page_size": 20,
                "include": "alert_urgency",
            }
        ) == {
            "filter[services]": ["svc-1", "svc-2"],
            "page[number]": 1,
            "page[size]": 20,
            "include": "alert_urgency",
        }

    def test_check_shift_date_range_blocks_over_31_day_schedule_query(self):
        with pytest.raises(RootlyValidationError) as exc_info:
            transport.AuthenticatedHTTPXClient._check_shift_date_range(
                "GET",
                "https://api.rootly.com/v1/schedules/abc/shifts",
                {"from": "2026-05-01", "to": "2026-07-31"},
            )
        msg = str(exc_info.value)
        # Derived from the constant: the cap is a measured upstream number, and
        # hardcoding it here once made this test the only thing objecting when
        # it was corrected.
        assert f"{transport.SCHEDULE_SHIFTS_LIMIT_DAYS}-day cap" in msg
        assert "Split the query" in msg

    def test_check_shift_date_range_blocks_over_cap_listShifts(self):
        with pytest.raises(RootlyValidationError):
            transport.AuthenticatedHTTPXClient._check_shift_date_range(
                "GET",
                "https://api.rootly.com/v1/shifts?from=2026-04-01T00:00:00Z&to=2026-07-01T00:00:00Z",
                None,
            )

    def test_check_shift_date_range_allows_within_limit(self):
        # 28 days on the schedule endpoint (limit 30) — must pass.
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "GET",
            "https://api.rootly.com/v1/schedules/abc/shifts",
            {"from": "2026-05-01", "to": "2026-05-29"},
        )

    def test_check_shift_date_range_ignores_non_get(self):
        # A POST on the same path must not be range-checked.
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "POST",
            "https://api.rootly.com/v1/schedules/abc/shifts",
            {"from": "2026-01-01", "to": "2026-12-31"},
        )

    def test_check_shift_date_range_ignores_unrelated_path(self):
        # Same params on a different endpoint must not be checked.
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "GET",
            "https://api.rootly.com/v1/incidents",
            {"from": "2026-01-01", "to": "2026-12-31"},
        )

    def test_check_shift_date_range_ignores_override_shifts_lookalike(self):
        # `/v1/schedules/{id}/override_shifts` and `/v1/override_shifts/{id}` both
        # contain the substring `_shifts` but do not END in `/shifts`. They must
        # not be range-checked — they have their own (different) date semantics.
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "GET",
            "https://api.rootly.com/v1/schedules/abc/override_shifts",
            {"from": "2026-01-01", "to": "2026-12-31"},
        )
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "GET",
            "https://api.rootly.com/v1/override_shifts/abc",
            {"from": "2026-01-01", "to": "2026-12-31"},
        )

    def test_check_for_unfilled_path_params_ignores_query_string_braces(self):
        # A legitimate query value containing `{...}` (or URL-encoded `%7B...%7D`)
        # must not trigger the path-template guard. Only braces in the URL path
        # indicate an unfilled OpenAPI substitution.
        transport.AuthenticatedHTTPXClient._check_for_unfilled_path_params(
            "GET", "https://api.rootly.com/v1/incidents?filter[search]={literal}"
        )
        transport.AuthenticatedHTTPXClient._check_for_unfilled_path_params(
            "GET",
            "https://api.rootly.com/v1/incidents?filter%5Bsearch%5D=%7Bliteral%7D",
        )

    def test_check_for_unfilled_path_params_catches_path_brace_with_query(self):
        # An unfilled placeholder in the PATH must still raise even when the
        # URL also has a query string.
        with pytest.raises(RootlyValidationError):
            transport.AuthenticatedHTTPXClient._check_for_unfilled_path_params(
                "GET", "https://api.rootly.com/v1/schedules/{id}/shifts?from=2026-01-01"
            )

    def test_check_shift_date_range_skips_on_malformed_dates(self):
        # Bad dates should fall through to the upstream's own error path.
        transport.AuthenticatedHTTPXClient._check_shift_date_range(
            "GET",
            "https://api.rootly.com/v1/shifts",
            {"from": "not-a-date", "to": "also-not"},
        )

    def test_alert_routing_rules_403_gets_use_tool_hint(self):
        """403 from /alert_routing_rules with advanced-routing message gets a _use_tool field."""
        response = httpx2.Response(
            403,
            request=httpx2.Request(
                "GET", "https://api.rootly.com/v1/alert_routing_rules?page%5Bsize%5D=20"
            ),
            content=(
                b'{"errors":[{"title":"Advanced alert routing features are enabled '
                b"for your team. Please use the Alert Routes endpoint instead for "
                b'enhanced alert routing functionality.","status":"403"}]}'
            ),
        )
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_alert_routing_deprecation(
            "GET",
            "https://api.rootly.com/v1/alert_routing_rules?page%5Bsize%5D=20",
            response,
        )
        body = annotated.json()
        assert body["_use_tool"]["use"] == "list_alert_routes"
        assert body["_use_tool"]["instead_of"] == "list_alert_routing_rules"
        assert "Call list_alert_routes instead" in body["_use_tool"]["reason"]

    @pytest.mark.parametrize(
        ("method", "path", "legacy", "replacement"),
        [
            ("GET", "/v1/alert_routing_rules/abc", "get_alert_routing_rule", "get_alert_route"),
            ("POST", "/v1/alert_routing_rules", "create_alert_routing_rule", "create_alert_route"),
            (
                "PUT",
                "/v1/alert_routing_rules/abc",
                "update_alert_routing_rule",
                "update_alert_route",
            ),
        ],
    )
    def test_alert_routing_rules_403_names_the_matching_alert_route_tool(
        self, method, path, legacy, replacement
    ):
        # ROOTLY-MCP-1X: the hint named the operationId `listAlertRoutes`, which
        # is not a tool the model can call.
        url = f"https://api.rootly.com{path}"
        response = httpx2.Response(
            403,
            request=httpx2.Request(method, url),
            content=(
                b'{"errors":[{"title":"Advanced alert routing features are enabled '
                b'for your team.","status":"403"}]}'
            ),
        )
        body = transport.AuthenticatedHTTPXClient._maybe_annotate_alert_routing_deprecation(
            method, url, response
        ).json()
        assert body["_use_tool"] == {
            "instead_of": legacy,
            "use": replacement,
            "reason": body["_use_tool"]["reason"],
        }

    @pytest.mark.asyncio
    async def test_alert_route_hint_names_real_tools(self, monkeypatch):
        from rootly_mcp_server.server import create_rootly_mcp_server

        monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
        server = create_rootly_mcp_server(
            swagger_path="src/rootly_mcp_server/data/swagger.json", hosted=False
        )
        names = {tool.name for tool in await server.list_tools()}
        for legacy, replacement in transport._ALERT_ROUTE_REPLACEMENTS.values():
            assert legacy in names
            assert replacement in names

    def test_alert_routing_rules_non_advanced_403_not_annotated(self):
        """Other 403 reasons (auth failure, etc.) must not get the use_tool hint."""
        response = httpx2.Response(
            403,
            request=httpx2.Request("GET", "https://api.rootly.com/v1/alert_routing_rules"),
            content=b'{"errors":[{"title":"Forbidden","status":"403"}]}',
        )
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_alert_routing_deprecation(
            "GET", "https://api.rootly.com/v1/alert_routing_rules", response
        )
        assert "_use_tool" not in annotated.json()

    def test_unrelated_403_not_annotated(self):
        """A 403 on an endpoint other than /alert_routing_rules is left alone."""
        response = httpx2.Response(
            403,
            request=httpx2.Request("GET", "https://api.rootly.com/v1/schedules"),
            content=b'{"errors":[{"title":"Forbidden","status":"403"}]}',
        )
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_alert_routing_deprecation(
            "GET", "https://api.rootly.com/v1/schedules", response
        )
        assert "_use_tool" not in annotated.json()

    @pytest.mark.asyncio
    async def test_authenticated_client_request_blocks_unfilled_path_template(self):
        """`{id}` left in the URL must raise before hitting the upstream API."""
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock()

            with pytest.raises(RootlyValidationError) as exc_info:
                await client.request("GET", "https://api.rootly.com/v1/schedules/{id}/shifts")
            assert "{id}" in str(exc_info.value)
            client.client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_authenticated_client_send_blocks_unfilled_path_template(self):
        """URL-encoded `%7Bid%7D` in a built request must also be blocked."""
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.send = AsyncMock()

            request = httpx2.Request(
                "GET",
                "https://api.rootly.com/v1/schedules/%7Bschedule_id%7D/shifts",
            )
            with pytest.raises(RootlyValidationError) as exc_info:
                await client.send(request)
            assert "{schedule_id}" in str(exc_info.value)
            client.client.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_authenticated_client_request_allows_filled_path(self):
        """A fully-substituted URL must pass the path-template guard."""
        response = httpx2.Response(
            200,
            request=httpx2.Request("GET", "https://api.rootly.com/v1/schedules/abc-123/shifts"),
            content=b'{"data":[]}',
        )
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")
            client.client.request = AsyncMock(return_value=response)

            result = await client.request(
                "GET", "https://api.rootly.com/v1/schedules/abc-123/shifts"
            )
            assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_authenticated_client_send_drops_empty_query_parameters(self):
        response = httpx2.Response(
            200,
            request=httpx2.Request(
                "GET",
                "https://api.rootly.com/v1/alerts?include=alert_urgency&page%5Bnumber%5D=1",
            ),
            content=b'{"data":[]}',
        )

        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            client = transport.AuthenticatedHTTPXClient(
                hosted=False,
                transport="stdio",
                parameter_mapping={
                    "filter_status": "filter[status]",
                    "filter_source": "filter[source]",
                    "page_number": "page[number]",
                    "page_size": "page[size]",
                },
            )
            client.client.send = AsyncMock(return_value=response)

            request = httpx2.Request(
                "GET",
                "https://api.rootly.com/v1/alerts"
                "?filter_status=&filter_source=%20%20%20&include=alert_urgency"
                "&page_number=1&page_size=20",
            )

            await client.send(request)

            sent_request = client.client.send.call_args.args[0]
            sent_params = dict(sent_request.url.params)
            assert "filter[status]" not in sent_params
            assert "filter[status]" not in str(sent_request.url)
            assert "filter[source]" not in sent_params
            assert sent_params["include"] == "alert_urgency"
            assert sent_params["page[number]"] == "1"
            # A valid page size must be forwarded untouched (not corrupted to 0).
            assert sent_params["page[size]"] == "20"

    def test_sanitize_log_excerpt_redacts_tokens_and_paths(self):
        excerpt = transport._sanitize_log_excerpt(
            'Bearer rootly_1234567890 File "/Users/spencercheng/app.py" failed'
        )
        assert "***REDACTED***" in excerpt
        assert "/Users/spencercheng" not in excerpt
        assert "[file]" in excerpt

    def test_strip_heavy_alert_data_keeps_whitelist_fields(self):
        data = {
            "data": [
                {
                    "id": "a-1",
                    "attributes": {
                        "short_id": "ABCD",
                        "summary": "CPU alarm",
                        "status": "triggered",
                        "source": "datadog",
                        "created_at": "2026-02-20T00:00:00Z",
                        "labels": [{"name": "prod"}],
                        "extra": "remove-me",
                    },
                    "relationships": {"alerts": {"data": [{"id": "x-1"}, {"id": "x-2"}]}},
                }
            ],
            "included": [{"id": "heavy"}],
        }

        result = transport.strip_heavy_alert_data(data)
        attrs = result["data"][0]["attributes"]
        assert attrs["short_id"] == "ABCD"
        assert attrs["summary"] == "CPU alarm"
        assert "extra" not in attrs
        assert "labels" not in attrs
        assert result["data"][0]["relationships"]["alerts"] == {"count": 2}
        assert "included" not in result

    def test_normalize_request_json_payload_unwraps_body_for_write_methods(self):
        payload = {"body": {"genius_workflow": {"name": "workflow-name"}}}

        result = transport.AuthenticatedHTTPXClient._normalize_request_json_payload("POST", payload)

        assert result == {"genius_workflow": {"name": "workflow-name"}}

    def test_normalize_request_json_payload_does_not_unwrap_for_get(self):
        payload = {"body": {"query": "database timeout"}}

        result = transport.AuthenticatedHTTPXClient._normalize_request_json_payload("GET", payload)

        assert result == payload

    def test_normalize_request_json_payload_keeps_non_envelope_dict(self):
        payload = {"data": {"type": "incidents"}, "body": {"ignored": True}}

        result = transport.AuthenticatedHTTPXClient._normalize_request_json_payload(
            "PATCH", payload
        )

        assert result == payload

    def test_strip_heavy_user_data_keeps_profile_essentials(self):
        data = {
            "data": [
                {
                    "id": "u-1",
                    "type": "users",
                    "attributes": {
                        "full_name": "Example User",
                        "email": "example.user@example.test",
                        "time_zone": "UTC",
                        "created_at": "2026-03-18T00:00:00Z",
                        "updated_at": "2026-03-18T01:00:00Z",
                        "avatar_url": "https://example.com/avatar.png",
                    },
                    "relationships": {
                        "email_addresses": {"data": [{"id": "e-1"}, {"id": "e-2"}]},
                        "role": {
                            "data": {"id": "r-1", "type": "roles", "attributes": {"name": "Admin"}}
                        },
                    },
                }
            ],
            "included": [
                {
                    "id": "r-1",
                    "type": "roles",
                    "attributes": {"name": "Admin", "permissions": ["all"]},
                    "relationships": {"teams": {"data": [{"id": "t-1"}]}},
                }
            ],
        }

        result = transport.strip_heavy_user_data(data)
        attrs = result["data"][0]["attributes"]
        assert attrs["full_name"] == "Example User"
        assert attrs["email"] == "example.user@example.test"
        assert "avatar_url" not in attrs
        assert result["data"][0]["relationships"]["email_addresses"] == {"count": 2}
        assert result["data"][0]["relationships"]["role"] == {
            "data": {"id": "r-1", "type": "roles"}
        }
        included_role = result["included"][0]
        assert included_role["attributes"] == {"name": "Admin"}
        assert "relationships" not in included_role

    def test_strip_heavy_service_data_keeps_operational_essentials(self):
        data = {
            "data": [
                {
                    "id": "svc-1",
                    "type": "services",
                    "attributes": {
                        "name": "API",
                        "slug": "api",
                        "status": "operational",
                        "description": "Core API",
                        "owner_group_ids": ["team-1"],
                        "incidents_count": 4,
                        "created_at": "2026-03-18T00:00:00Z",
                        "updated_at": "2026-03-18T01:00:00Z",
                        "pagerduty_id": "PD123",
                        "slack_channels": [{"id": "C1"}],
                    },
                    "relationships": {
                        "teams": {"data": [{"id": "team-1"}, {"id": "team-2"}]},
                        "alert_urgency": {"data": {"id": "urg-1", "type": "alert_urgencies"}},
                    },
                }
            ]
        }

        result = transport.strip_heavy_service_data(data)
        attrs = result["data"][0]["attributes"]
        assert attrs["name"] == "API"
        assert attrs["status"] == "operational"
        assert "pagerduty_id" not in attrs
        assert "slack_channels" not in attrs
        assert result["data"][0]["relationships"]["teams"] == {"count": 2}
        assert result["data"][0]["relationships"]["alert_urgency"] == {
            "data": {"id": "urg-1", "type": "alert_urgencies"}
        }

    def test_strip_heavy_shift_data_keeps_timing_and_minimal_user(self):
        data = {
            "data": [
                {
                    "id": "shift-1",
                    "type": "shifts",
                    "attributes": {
                        "schedule_id": "sched-1",
                        "rotation_id": "rot-1",
                        "starts_at": "2026-03-18T00:00:00Z",
                        "ends_at": "2026-03-18T08:00:00Z",
                        "is_override": False,
                        "notes": "extra",
                    },
                    "relationships": {
                        "user": {"data": {"id": "u-1", "type": "users"}},
                        "shift_override": {"data": None},
                        "schedule_rotation": {
                            "data": {"id": "rot-1", "type": "schedule_rotations"}
                        },
                    },
                }
            ],
            "included": [
                {
                    "id": "u-1",
                    "type": "users",
                    "attributes": {
                        "full_name": "Example User",
                        "email": "example.user@example.test",
                        "time_zone": "UTC",
                        "avatar_url": "https://example.com/avatar.png",
                    },
                }
            ],
        }

        result = transport.strip_heavy_shift_data(data)
        attrs = result["data"][0]["attributes"]
        assert attrs["schedule_id"] == "sched-1"
        assert attrs["starts_at"] == "2026-03-18T00:00:00Z"
        assert "notes" not in attrs
        assert sorted(result["data"][0]["relationships"]) == ["shift_override", "user"]
        included_user = result["included"][0]
        assert included_user["attributes"] == {
            "full_name": "Example User",
            "email": "example.user@example.test",
            "time_zone": "UTC",
        }


class TestAuthCaptureMiddlewareWWWAuthenticate:
    """Tests for WWW-Authenticate header injection on 401 responses."""

    @pytest.mark.asyncio
    async def test_401_response_includes_www_authenticate_header(self):
        """When downstream app returns 401, middleware injects WWW-Authenticate."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"Unauthorized"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        assert start_msg["status"] == 401
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" in header_dict
        assert (
            header_dict[b"www-authenticate"]
            == b'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
        )

    @pytest.mark.asyncio
    async def test_200_response_does_not_include_www_authenticate(self):
        """Non-401 responses with valid Bearer should not get WWW-Authenticate header."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer valid_token")],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with (
            patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"),
            patch.object(
                middleware,
                "_validate_token_upstream",
                return_value={"id": "user_123", "email": "example.user@example.test"},
            ),
        ):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" not in header_dict

    @pytest.mark.asyncio
    async def test_invalid_token_rejected_by_upstream_probe(self):
        """Bearer token with valid format but rejected by upstream should get 401."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer invalid_token_12345")],
        }

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with (
            patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"),
            patch.object(middleware, "_validate_token_upstream", return_value=None),
        ):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        assert start_msg["status"] == 401
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" in header_dict

    @pytest.mark.asyncio
    async def test_unauthenticated_mcp_request_returns_401(self):
        """Requests to MCP paths without Bearer token should get 401."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [],
        }

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        assert start_msg["status"] == 401
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" in header_dict

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_format_returns_401(self):
        """Requests with malformed auth header should get 401."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Token not_bearer_format")],
        }

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        assert start_msg["status"] == 401

    @pytest.mark.asyncio
    async def test_401_on_non_mcp_path_does_not_inject_header(self):
        """401 responses on non-MCP paths should not get WWW-Authenticate."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"Unauthorized"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/healthz",
            "headers": [],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" not in header_dict

    @pytest.mark.asyncio
    async def test_www_authenticate_derives_url_from_request_headers(self):
        """Without ROOTLY_MCP_SERVER_URL env var, URL is derived from request."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"Unauthorized"})

        middleware = transport.AuthCaptureMiddleware(app)
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"host", b"mcp.rootly.com"),
                (b"x-forwarded-proto", b"https"),
            ],
        }

        transport._session_auth_token.set("")
        transport._session_transport.set("")
        transport._session_mcp_mode.set("")

        sent_messages = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent_messages.append(message)

        with patch("rootly_mcp_server.utils._MCP_SERVER_URL", ""):
            await middleware(scope, receive, send)

        start_msg = sent_messages[0]
        header_dict = dict(start_msg["headers"])
        assert b"www-authenticate" in header_dict
        assert (
            header_dict[b"www-authenticate"]
            == b'Bearer resource_metadata="https://mcp.rootly.com/.well-known/oauth-protected-resource"'
        )


def _pagination_400(body: dict | list | str | None = None) -> httpx2.Response:
    """A 400 shaped like Rootly's offset-pagination rejection."""
    if body is None:
        body = {
            "errors": [
                {
                    "title": (
                        "Offset pagination is limited to 50000 records. Use "
                        "cursor-based pagination (page[after]) for deeper results. "
                        "The first page response includes a next_cursor value in "
                        "the meta object."
                    ),
                    "status": "400",
                }
            ]
        }
    content = body if isinstance(body, bytes | str) else json.dumps(body)
    if isinstance(content, str):
        content = content.encode()
    return httpx2.Response(
        status_code=400,
        content=content,
        request=httpx2.Request("GET", "https://api.rootly.com/v1/alerts?page%5Bnumber%5D=5000"),
    )


@pytest.mark.unit
class TestOffsetPaginationHint:
    """The offset cap is reachable through page_number, and the cap is terminal.

    One caller produced 1,271 of these against list_alerts in six days, retrying
    the same shape because nothing in the response told it to switch.
    """

    def _annotate(self, response: httpx2.Response) -> httpx2.Response:
        return transport.AuthenticatedHTTPXClient._maybe_annotate_offset_pagination_limit(
            "GET", "https://api.rootly.com/v1/alerts", response
        )

    def test_offset_cap_gets_a_cursor_hint(self):
        body = self._annotate(_pagination_400()).json()

        hint = body["_use_cursor_pagination"]
        assert hint["instead_of"] == "page_number"
        assert hint["use"] == "page_after"
        # The remedy has to name next_cursor, which is what the caller reads
        # from the response to make progress.
        assert "next_cursor" in hint["how"]

    def test_hint_rules_out_retrying_the_same_shape(self):
        # Retrying page_number produced 1,271 failures: the observed callers sat
        # at pages 5,003-5,159 and reissued each roughly six times.
        reason = self._annotate(_pagination_400()).json()["_use_cursor_pagination"]["reason"]
        assert "page_number" in reason
        assert "cannot reach any further" in reason or "keep failing" in reason

    def test_hint_offers_narrowing_before_a_cursor_walk(self):
        # The affected callers were walking a filtered range and crossed the cap
        # at page 5,001. A cursor walk restarts from the beginning, so re-walking
        # 50,000 records is worse advice than narrowing the range.
        how = self._annotate(_pagination_400()).json()["_use_cursor_pagination"]["how"]
        assert "filter" in how.lower(), "expected narrowing to be offered"
        assert how.lower().index("narrow") < how.lower().index("cursor"), (
            "narrowing should be offered before the cursor walk"
        )

    def test_hint_is_honest_that_a_cursor_walk_restarts(self):
        # Without this the caller may believe page_after can resume where
        # page_number stopped, which it cannot.
        how = self._annotate(_pagination_400()).json()["_use_cursor_pagination"]["how"]
        assert "restarts from the beginning" in how or "cannot resume" in how

    def test_unrelated_400_is_left_alone(self):
        other = _pagination_400({"errors": [{"title": "Page size exceeds maximum of 1000"}]})
        assert "_use_cursor_pagination" not in self._annotate(other).json()

    def test_non_400_is_left_alone(self):
        ok = httpx2.Response(
            status_code=200,
            content=json.dumps({"data": []}).encode(),
            request=httpx2.Request("GET", "https://api.rootly.com/v1/alerts"),
        )
        assert "_use_cursor_pagination" not in self._annotate(ok).json()

    def test_non_dict_body_does_not_raise(self):
        assert self._annotate(_pagination_400(["not", "a", "dict"])).status_code == 400

    def test_non_json_body_does_not_raise(self):
        assert self._annotate(_pagination_400("<html>gateway</html>")).status_code == 400

    def test_existing_hint_is_not_overwritten(self):
        body = {
            "errors": [{"title": "Offset pagination is limited to 50000 records."}],
            "_use_cursor_pagination": {"use": "already-set"},
        }
        got = self._annotate(_pagination_400(body)).json()
        assert got["_use_cursor_pagination"] == {"use": "already-set"}


@pytest.mark.unit
class TestHintSurvivesToolErrorFormatting:
    """Attaching the hint is worthless if the caller never sees it.

    Auto-generated tools raise on 4xx. FastMCP builds the message from
    e.response.json(), and e.response is the same object the annotator
    mutated, so the hint travels inside the ValueError the model receives.
    This pins that path rather than assuming it.
    """

    def test_hint_appears_in_the_error_message_the_caller_receives(self):
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_offset_pagination_limit(
            "GET", "https://api.rootly.com/v1/alerts", _pagination_400()
        )

        # Mirrors fastmcp/server/providers/openapi/components.py: raise_for_status
        # then rebuild the message from the response body.
        try:
            annotated.raise_for_status()
            raise AssertionError("expected a 400 to raise")
        except httpx2.HTTPStatusError as exc:
            message = f"HTTP error {exc.response.status_code}: {exc.response.reason_phrase}"
            message += f" - {exc.response.json()}"

        assert "_use_cursor_pagination" in message
        assert "page_after" in message


@pytest.mark.unit
class TestAnnotatorsReachBothTransportPaths:
    """Annotating in isolation is not enough; it has to be wired into both paths.

    Auto-generated tools reach the API through send(); curated tools go through
    request(). The 404 plan-gating hint was wired only into request(), so the
    tools it was written for never received it, and the existing tests did not
    catch that because they call the annotators directly.
    """

    def _client(self):
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            return transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")

    @pytest.mark.asyncio
    async def test_request_path_annotates_the_offset_cap(self):
        client = self._client()
        client.client.request = AsyncMock(return_value=_pagination_400())

        returned = await client.request("GET", "/v1/alerts")

        assert "_use_cursor_pagination" in returned.json()

    @pytest.mark.asyncio
    async def test_send_path_annotates_the_offset_cap(self):
        # send() is the path auto-generated tools use, including list_alerts.
        client = self._client()
        client.client.send = AsyncMock(return_value=_pagination_400())

        returned = await client.send(httpx2.Request("GET", "https://api.rootly.com/v1/alerts"))

        assert "_use_cursor_pagination" in returned.json()

    def test_response_strippers_leave_error_bodies_for_the_annotators(self):
        # The strippers run before the annotators and rewrite _content. If one
        # ever stopped skipping error responses it would replace the errors
        # array the annotators match on, silently disabling every hint.
        body = json.dumps({"errors": [{"title": "Offset pagination is limited to 50000 records."}]})
        for stripper in (
            transport.AuthenticatedHTTPXClient._maybe_strip_alert_response,
            transport.AuthenticatedHTTPXClient._maybe_strip_collection_response,
        ):
            response = httpx2.Response(
                status_code=400,
                content=body.encode(),
                request=httpx2.Request("GET", "https://api.rootly.com/v1/alerts"),
            )
            out = stripper("GET", "https://api.rootly.com/v1/alerts", response)
            assert out.json() == json.loads(body), f"{stripper.__name__} altered an error body"


@pytest.mark.unit
class TestPlanGatingHintReachesAutogenTools:
    """The 404 plan-gating hint existed but never reached the tools it was for.

    Responses travel two paths. Curated tools call request(); auto-generated
    tools reach the API through send(), because FastMCP's OpenAPI executor
    calls client.send(). The hint was wired only into request(), so the 231
    auto-generated tools that actually hit plan gating never received it.

    The existing annotator tests could not catch this: they call the static
    methods directly and never assert a pipeline applies them.
    """

    def _client(self):
        with patch.object(
            transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="token"
        ):
            return transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")

    def _plan_gated_404(self) -> httpx2.Response:
        return httpx2.Response(
            status_code=404,
            content=json.dumps({"errors": [{"title": "Not found or unauthorized"}]}).encode(),
            request=httpx2.Request("GET", "https://api.rootly.com/v1/pulses"),
        )

    @pytest.mark.asyncio
    async def test_send_path_annotates_plan_gated_404(self):
        client = self._client()
        client.client.send = AsyncMock(return_value=self._plan_gated_404())

        returned = await client.send(httpx2.Request("GET", "https://api.rootly.com/v1/pulses"))

        assert "_plan_gating_hint" in returned.json()

    @pytest.mark.asyncio
    async def test_request_path_still_annotates_plan_gated_404(self):
        client = self._client()
        client.client.request = AsyncMock(return_value=self._plan_gated_404())

        returned = await client.request("GET", "/v1/pulses")

        assert "_plan_gating_hint" in returned.json()

    def test_both_paths_apply_the_same_annotators(self):
        # A guard against the next annotator being added to one path only,
        # which is the mistake this PR corrects.
        import inspect
        import re

        def applied_in(fn) -> set[str]:
            return set(re.findall(r"self\.(_maybe_annotate_[a-z0-9_]+)\(", inspect.getsource(fn)))

        in_request = applied_in(transport.AuthenticatedHTTPXClient.request)
        in_send = applied_in(transport.AuthenticatedHTTPXClient.send)

        assert in_request, "expected request() to apply annotators"
        assert in_request == in_send, (
            f"annotators differ between transport paths; "
            f"only in request(): {sorted(in_request - in_send)}; "
            f"only in send(): {sorted(in_send - in_request)}"
        )


@pytest.mark.unit
class TestPlanGatingHintDoesNotMisdiagnoseNestedCollections:
    """A nested collection's 404 usually means the parent id is wrong.

    `_path_has_id_segment` only inspects the trailing segment, so
    `/v1/incidents/{id}/action_items` ends in `action_items` and reads as a
    top-level collection. Claiming plan gating there is wrong: incident events
    and action items are not gated, the incident simply does not exist.

    Over 30 days of production 404s this shape was 760 of 1,631 -- more than
    twice the 317 genuine top-level collection 404s -- so getting it wrong
    would misdiagnose the majority of them.
    """

    CONFIDENT = "most likely means the feature is not enabled"

    def _hint(self, path: str, method: str = "GET") -> str | None:
        url = "https://api.rootly.com" + path
        response = httpx2.Response(
            404,
            content=json.dumps({"errors": [{"title": "Not found or unauthorized"}]}).encode(),
            request=httpx2.Request(method, url),
        )
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_404_response(
            method, url, response
        )
        return annotated.json().get("_plan_gating_hint")

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/incidents/4846/action_items",  # numeric parent
            "/v1/incidents/INC-1742/events",  # prefixed parent
            "/v1/incidents/a95520fa-f45d-401b-a6d2-9098af345ca1/alerts",  # uuid parent
            "/v1/catalogs/9f8e/entities",
        ],
    )
    def test_nested_collection_is_not_blamed_on_the_plan(self, path):
        # All four parent-id shapes appear in production; the check keys on
        # depth so it does not have to recognise each one.
        hint = self._hint(path)
        assert hint is not None, "a nested collection should still be annotated"
        assert self.CONFIDENT not in hint
        assert "may mean the resource does not exist" in hint

    @pytest.mark.parametrize("path", ["/v1/schedules", "/v1/alert_routes", "/v1/alert_sources"])
    def test_top_level_collection_still_gets_the_confident_hint(self, path):
        # These are the genuine plan-gating 404s and must not be softened.
        hint = self._hint(path)
        assert hint is not None
        assert self.CONFIDENT in hint

    @pytest.mark.parametrize(
        "path", ["/v1/users/124306", "/v1/causes/a95520fa-f45d-401b-a6d2-9098af345ca1"]
    )
    def test_single_resource_get_is_still_left_alone(self, path):
        assert self._hint(path) is None

    def test_write_to_a_single_resource_is_still_softened(self):
        hint = self._hint("/v1/incidents/4846", method="PATCH")
        assert hint is not None
        assert self.CONFIDENT not in hint


@pytest.mark.unit
class TestNestedCollectionDetection:
    """Depth, not id shape, is what distinguishes the two collection kinds."""

    @pytest.mark.parametrize(
        ("path", "nested"),
        [
            ("/v1/schedules", False),
            ("/v1/alert_routes", False),
            ("/v1/incidents/4846", False),
            ("/v1/incidents/4846/action_items", True),
            ("/v1/incidents/INC-1742/events", True),
            ("/v1/schedules/abc/shifts", True),
            # trailing slashes and the version prefix must not shift the count
            ("/v1/schedules/", False),
            ("/v1/incidents/4846/action_items/", True),
        ],
    )
    def test_depth_classification(self, path, nested):
        url = "https://api.rootly.com" + path
        assert transport.AuthenticatedHTTPXClient._path_is_nested_collection(url) is nested

    def test_version_prefix_is_not_counted_as_a_segment(self):
        # Without dropping the v1 prefix, /v1/schedules would count as 2 and a
        # single-resource path as 3, inverting the whole classification.
        C = transport.AuthenticatedHTTPXClient
        assert C._path_is_nested_collection("https://api.rootly.com/v1/schedules") is False
        assert C._path_is_nested_collection("https://api.rootly.com/v1/incidents/1") is False
