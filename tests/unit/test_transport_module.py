"""Focused tests for transport module."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from rootly_mcp_server import transport


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
        response = httpx.Response(
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
        response = httpx.Response(
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
        response = httpx.Response(
            502,
            request=httpx.Request("GET", "https://api.rootly.com/v1/incidents?page[size]=10"),
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
            client.client.request = AsyncMock(side_effect=httpx.ReadTimeout("request timed out"))

            with pytest.raises(httpx.ReadTimeout):
                await client.request("GET", "/v1/teams")

        error_context = transport._get_error_context()
        assert error_context["upstream_exception_type"] == "ReadTimeout"
        assert error_context["upstream_exception_message"] == "request timed out"
        assert error_context["upstream_path"] == "/v1/teams"
        assert error_context["upstream_log_level"] == "error"

    @pytest.mark.asyncio
    async def test_authenticated_client_preserves_failure_context_across_followup_success(self):
        responses = [
            httpx.Response(
                502,
                request=httpx.Request("GET", "https://api.rootly.com/v1/alerts"),
                content=b'{"error":"backend down"}',
            ),
            httpx.Response(
                200,
                request=httpx.Request("GET", "https://api.rootly.com/v1/users/me"),
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
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://api.rootly.com/v1/workflows"),
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
        response = httpx.Response(
            200,
            request=httpx.Request("PATCH", "https://api.rootly.com/v1/workflows/wf-1"),
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

    @pytest.mark.asyncio
    async def test_authenticated_client_send_drops_empty_query_parameters(self):
        response = httpx.Response(
            200,
            request=httpx.Request(
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

            request = httpx.Request(
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
