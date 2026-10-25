"""Regressions from the October 2026 MCP directory review session."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx2
import pytest

from rootly_mcp_server.mcp_error import MCPError as mcp_error
from rootly_mcp_server.payload_stripping import strip_heavy_nested_data
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.tools.incidents import register_incident_tools
from rootly_mcp_server.tools.oncall import register_oncall_tools
from rootly_mcp_server.transport import AuthenticatedHTTPXClient


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, name=None, **kwargs):
        def register(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return register


def response(data, status=200):
    return httpx2.Response(
        status, json=data, request=httpx2.Request("GET", "https://api.rootly.com/v1/test")
    )


async def transport_response(monkeypatch, method, path, payload, *, via_send=False):
    monkeypatch.setattr(AuthenticatedHTTPXClient, "_get_api_token", lambda _: "test-token")
    client = AuthenticatedHTTPXClient()
    client.client.request = AsyncMock(return_value=response(payload))
    client.client.send = AsyncMock(return_value=response(payload))
    try:
        if via_send:
            return await client.send(httpx2.Request(method, "https://api.rootly.com" + path))
        return await client.request(method, path)
    finally:
        await client.client.aclose()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/heartbeats"),
        ("GET", "https://api.rootly.com/v1/heartbeats/?page[number]=2"),
        ("GET", "/v1/heartbeats/hb-1"),
        ("PUT", "/v1/heartbeats/hb-1"),
    ],
)
async def test_heartbeat_credentials_never_reach_tool_response(monkeypatch, method, path):
    payload = {
        "data": [
            {
                "id": "hb-1",
                "type": "heartbeats",
                "attributes": {
                    "secret": "live-ping-credential",
                    "email_address": "heartbeat-test-credential@example.com",
                    "ping_url": "https://api.rootly.com/v1/heartbeats/hb-1/ping",
                    "name": "Nightly backup",
                    "status": "active",
                },
            },
            {
                "id": "hb-2",
                "type": "heartbeats",
                "attributes": {"secret": None, "email_address": None},
            },
        ],
        "meta": {"total_pages": 2},
    }
    result = await transport_response(monkeypatch, method, path, payload)
    assert "live-ping-credential" not in result.text
    assert "heartbeat-test-credential@example.com" not in result.text
    assert result.json()["data"][0]["attributes"] == {
        "ping_url": "https://api.rootly.com/v1/heartbeats/hb-1/ping",
        "name": "Nightly backup",
        "status": "active",
    }
    assert "secret" not in result.json()["data"][1]["attributes"]
    assert "email_address" not in result.json()["data"][1]["attributes"]
    assert result.json()["meta"]["total_pages"] == 2


async def test_heartbeat_response_without_credentials_is_unchanged(monkeypatch):
    payload = {
        "data": [
            {
                "id": "hb-1",
                "type": "heartbeats",
                "attributes": {
                    "ping_url": "https://api.rootly.com/v1/heartbeats/hb-1/ping",
                    "name": "Nightly backup",
                    "status": "active",
                },
            }
        ],
        "meta": {"total_pages": 1},
    }
    result = await transport_response(monkeypatch, "GET", "/v1/heartbeats", payload)
    assert result.json() == payload


@pytest.mark.parametrize("via_send", [False, True])
async def test_creation_can_return_new_heartbeat_credential(monkeypatch, via_send):
    payload = {
        "data": {
            "type": "heartbeats",
            "attributes": {
                "secret": "new-credential",
                "email_address": "heartbeat-new-credential@example.com",
            },
        }
    }
    result = await transport_response(
        monkeypatch, "POST", "/v1/heartbeats", payload, via_send=via_send
    )
    assert result.json() == payload


async def test_generated_list_heartbeats_omits_credentials_from_text_and_structured_output(
    monkeypatch,
):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    monkeypatch.setattr(AuthenticatedHTTPXClient, "_get_api_token", lambda _: "test-token")
    monkeypatch.setattr(
        httpx2.AsyncClient,
        "send",
        AsyncMock(
            return_value=response(
                {
                    "data": [
                        {
                            "id": "hb-1",
                            "type": "heartbeats",
                            "attributes": {
                                "name": "Backup",
                                "secret": "list-credential",
                                "email_address": "heartbeat-list-credential@example.com",
                            },
                        }
                    ]
                }
            )
        ),
    )
    server = create_rootly_mcp_server(enabled_tools={"list_heartbeats"})
    result = await server.call_tool("list_heartbeats", {})
    assert "list-credential" not in str(result.content)
    assert "list-credential" not in str(result.structured_content)
    assert result.structured_content is not None
    attributes = result.structured_content["data"][0]["attributes"]
    assert "secret" not in attributes
    assert "email_address" not in attributes


async def test_heartbeat_secret_omitted_when_embedded_in_alert_detail(monkeypatch):
    payload = {
        "data": {
            "type": "alerts",
            "attributes": {
                "heartbeat": {"secret": "embedded-credential", "status": "expired"},
            },
        }
    }
    result = await transport_response(monkeypatch, "GET", "/v1/alerts/a-1", payload)
    assert "embedded-credential" not in result.text
    heartbeat = result.json()["data"]["attributes"]["heartbeat"]
    assert "secret" not in heartbeat
    assert heartbeat["status"] == "expired"


@pytest.mark.parametrize("via_send", [False, True])
@pytest.mark.parametrize("single_resource", [False, True])
async def test_attach_alert_compacts_real_nested_incident_shape(
    monkeypatch, via_send, single_resource
):
    payload = {
        "data": [
            {
                "id": "a-1",
                "type": "alerts",
                "attributes": {
                    "summary": "Test alert",
                    "status": "open",
                    "incidents": [
                        {
                            "id": "inc-1",
                            "title": "Test incident",
                            "embedding_small": [0.123456789] * 1536,
                            "previous_attributes": {"summary": "previous"},
                            "ability_cache": {"internal": True},
                            "zoom_meeting_password": "meeting-credential",
                            "zoom_meeting_start_url": "host-credential",
                        }
                    ],
                },
            }
        ],
        "meta": {"total_count": 1},
        "included": [{"type": "incidents", "attributes": {"embedding_small": [1] * 1536}}],
    }
    if single_resource:
        payload["data"] = payload["data"][0]
    result = await transport_response(
        monkeypatch, "POST", "/v1/incidents/inc-1/alerts", payload, via_send=via_send
    )
    for field in (
        "embedding_small",
        "previous_attributes",
        "ability_cache",
        "meeting-credential",
        "host-credential",
    ):
        assert field not in result.text
    assert len(result.content) < 500
    alert = result.json()["data"] if single_resource else result.json()["data"][0]
    assert alert["id"] == "a-1"
    assert alert["attributes"]["summary"] == "Test alert"
    assert result.json()["meta"]["total_count"] == 1


async def test_heartbeat_delete_empty_success_stays_successful(monkeypatch):
    monkeypatch.setattr(AuthenticatedHTTPXClient, "_get_api_token", lambda _: "test-token")
    client = AuthenticatedHTTPXClient()
    client.client.send = AsyncMock(return_value=httpx2.Response(204))
    try:
        result = await client.send(
            httpx2.Request("DELETE", "https://api.rootly.com/v1/heartbeats/hb-1")
        )
        assert result.status_code == 204
    finally:
        await client.client.aclose()


def incident_tools(request):
    registry = Registry()
    register_incident_tools(registry, request, strip_heavy_nested_data, mcp_error, lambda _: "")
    return registry.tools


@pytest.mark.parametrize("severity_id", ["not-a-severity", "11111111-1111-4111-8111-111111111111"])
async def test_unknown_severity_cannot_clear_existing_value(severity_id):
    request = AsyncMock(return_value=response({"errors": [{"title": "Not found"}]}, 404))
    tools = incident_tools(request)
    result = await tools["update_incident"](
        incident_id="INC-13", severity_id=severity_id, status="resolved"
    )
    assert result["error_type"] == "validation_error"
    assert all(call.args[0] == "GET" for call in request.await_args_list)
    assert request.await_count == 1


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_severity_lookup_failure_prevents_write(status):
    request = AsyncMock(return_value=response({"errors": [{"title": "Unavailable"}]}, status))
    tools = incident_tools(request)
    result = await tools["update_incident"](incident_id="INC-13", severity_id="sev-1")
    assert result["error"]
    assert all(call.args[0] == "GET" for call in request.await_args_list)


async def test_severity_lookup_mismatched_id_prevents_write():
    request = AsyncMock(return_value=response({"data": {"id": "different-severity"}}))
    tools = incident_tools(request)
    result = await tools["update_incident"](incident_id="INC-13", severity_id="sev-1")
    assert result["error_type"] == "validation_error"
    assert request.await_count == 1
    assert request.await_args is not None
    assert request.await_args.args[0] == "GET"


def oncall_tools(responder):
    registry = Registry()
    request = AsyncMock(side_effect=responder)
    register_oncall_tools(registry, request, mcp_error)
    return registry.tools, request


def shift(start, end, user_id="1001", shift_id="shift-1"):
    return {
        "id": shift_id,
        "type": "shifts",
        "attributes": {
            "schedule_id": "sched-1",
            "user_id": int(user_id),
            "starts_at": start,
            "ends_at": end,
        },
        "relationships": {"user": {"data": {"id": user_id, "type": "users"}}},
    }


def schedule():
    return {"id": "sched-1", "attributes": {"name": "Primary", "owner_group_ids": []}}


async def test_handoff_recognizes_long_running_active_shift():
    now = datetime.now(UTC)
    active = shift((now - timedelta(days=9)).isoformat(), (now + timedelta(days=40)).isoformat())

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        assert path == "/v1/schedules/sched-1/shifts"
        assert params is not None
        assert "from" in params and "to" in params
        assert not any(k.startswith("filter[") for k in params)
        # Shifts have no on_call_role relationship (Api::V1::ShiftSerializer);
        # the role is the user's, sideloaded through the nested include.
        assert params["include"].split(",") == ["user", "assignee", "user.on_call_role"]
        return response(
            {
                "data": [active],
                "included": [
                    {
                        "type": "users",
                        "id": "1001",
                        "attributes": {"full_name": "Test Responder"},
                        "relationships": {
                            "on_call_role": {"data": {"type": "on_call_roles", "id": "primary"}}
                        },
                    },
                    {"type": "on_call_roles", "id": "primary", "attributes": {"name": "Primary"}},
                ],
            }
        )

    tools, _ = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"](timezone="America/Los_Angeles")
    assert result["summary"]["schedules_with_current_oncall"] == 1
    assert result["schedules"][0]["current_oncall"]["user_id"] == "1001"
    assert result["schedules"][0]["current_oncall"]["user_name"] == "Test Responder"
    assert result["schedules"][0]["current_oncall"]["role"] == "Primary"


async def test_handoff_role_without_sideloaded_role_is_unknown_not_an_error():
    """A user whose on-call role was not sideloaded still gets a name."""
    now = datetime.now(UTC)
    active = shift((now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat())

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        return response(
            {
                "data": [active],
                "included": [
                    {"type": "users", "id": "1001", "attributes": {"email": "r@example.com"}}
                ],
            }
        )

    tools, _ = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"]()
    current = result["schedules"][0]["current_oncall"]
    assert current["user_name"] == "r@example.com"
    assert current["role"] == "Unknown Role"


def _incident(incident_id, started_at):
    return {
        "id": incident_id,
        "type": "incidents",
        "attributes": {
            "title": f"Incident {incident_id}",
            "status": "resolved",
            "created_at": started_at,
            "started_at": started_at,
            "resolved_at": started_at,
        },
    }


@pytest.mark.parametrize("tags", ["db,network", " db "])
async def test_shift_incidents_tags_are_not_sent_and_warn(tags):
    """/v1/incidents permits no tags filter; the argument must not pretend to filter."""
    seen_params = []

    async def responder(method, path, params=None):
        assert path == "/v1/incidents"
        seen_params.append(dict(params or {}))
        return response(
            {
                "data": [_incident("inc-1", "2026-10-01T02:00:00Z")],
                "meta": {"total_pages": 1},
            }
        )

    tools, _ = oncall_tools(responder)
    result = await tools["get_shift_incidents"](
        start_time="2026-10-01T00:00:00Z", end_time="2026-10-01T23:59:59Z", tags=tags
    )
    assert result["success"] is True
    assert not any("tag" in key for params in seen_params for key in params)
    assert result["summary"]["total_incidents"] == 1
    assert len(result["warnings"]) == 1
    assert "tags argument was ignored" in result["warnings"][0]
    assert "filter[labels]" in result["warnings"][0]


@pytest.mark.parametrize("tags", ["", " , "])
async def test_shift_incidents_without_tags_have_no_warning(tags):
    async def responder(method, path, params=None):
        return response({"data": [], "meta": {"total_pages": 1}})

    tools, _ = oncall_tools(responder)
    result = await tools["get_shift_incidents"](
        start_time="2026-10-01T00:00:00Z", end_time="2026-10-01T23:59:59Z", tags=tags
    )
    assert result["success"] is True
    assert "warnings" not in result


async def test_availability_includes_shift_starting_before_window_and_clips_hours():
    active = shift("2026-10-04T17:00:00-07:00", "2026-11-30T09:00:00-08:00")
    outside = shift("2026-10-21T00:00:00Z", "2026-10-22T00:00:00Z", shift_id="outside")

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        if path == "/v1/schedules/sched-1/shifts":
            return response({"data": [active, outside]})
        assert path in {"/v1/users", "/v1/teams"}
        return response({"data": []})

    tools, _ = oncall_tools(responder)
    result = await tools["check_responder_availability"](
        start_date="2026-10-13", end_date="2026-10-20", user_ids="1001,1002"
    )
    assert result["scheduled"][0]["user_id"] == 1001
    assert result["scheduled"][0]["total_hours"] == 192
    assert result["scheduled"][0]["shifts"][0]["schedule_name"] == "Primary"
    assert len(result["scheduled"][0]["shifts"]) == 1
    assert result["not_scheduled"][0]["user_id"] == 1002


@pytest.mark.parametrize("severity", ["critical", "SEV0", "sev0", "sev-critical"])
async def test_shift_incidents_resolve_severity_level_name_slug_or_id(severity):
    async def responder(method, path, params=None):
        if path == "/v1/severities":
            return response(
                {
                    "data": [
                        {
                            "id": "sev-critical",
                            "attributes": {"name": "SEV0", "slug": "sev0", "severity": "critical"},
                        }
                    ]
                }
            )
        assert path == "/v1/incidents"
        assert params is not None
        assert params["filter[severity_id]"] == "sev-critical"
        assert "filter[severity]" not in params
        return response(
            {
                "data": [
                    {
                        "id": "inc-1",
                        "attributes": {
                            "title": "Outage",
                            "status": "started",
                            "started_at": "2026-10-06T12:00:00Z",
                            "severity": {
                                "data": {
                                    "id": "sev-critical",
                                    "type": "severities",
                                    "attributes": {"name": "SEV0"},
                                }
                            },
                        },
                    }
                ]
            }
        )

    tools, _ = oncall_tools(responder)
    result = await tools["get_shift_incidents"](
        start_time="2026-10-06T00:00:00Z", end_time="2026-10-07T00:00:00Z", severity=severity
    )
    assert result["summary"]["total_incidents"] == 1
    assert result["incidents"][0]["severity"] == "SEV0"


@pytest.mark.parametrize(
    "start,end", [("bad", "2026-10-20"), ("2026-10-21", "2026-10-20"), ("2025-01-01", "2026-10-20")]
)
async def test_availability_rejects_invalid_window_without_fetching(start, end):
    tools, request = oncall_tools(None)
    result = await tools["check_responder_availability"](
        start_date=start, end_date=end, user_ids="1001"
    )
    assert result["error"]
    request.assert_not_awaited()


async def test_availability_long_range_does_not_double_count_spanning_shift():
    active = shift("2026-10-04T00:00:00Z", "2026-12-15T00:00:00Z")

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        if path.endswith("/shifts"):
            return response({"data": [active]})
        return response({"data": []})

    tools, request = oncall_tools(responder)
    result = await tools["check_responder_availability"](
        start_date="2026-10-13T00:00:00Z", end_date="2026-12-12T00:00:00Z", user_ids="1001"
    )
    assert result["scheduled"][0]["total_hours"] == 60 * 24
    assert len(result["scheduled"][0]["shifts"]) == 1
    calls = [c for c in request.await_args_list if c.args[1].endswith("/shifts")]
    assert len(calls) == 2


async def test_handoff_handles_calculated_start_after_request_began():
    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        now = datetime.now(UTC)
        return response({"data": [shift(now.isoformat(), (now + timedelta(days=7)).isoformat())]})

    tools, _ = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"]()
    assert result["summary"]["schedules_with_current_oncall"] == 1


async def test_handoff_skips_coverage_gaps_and_recognizes_user_assignee():
    now = datetime.now(UTC)
    active = shift((now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat())
    active["attributes"].pop("user_id")
    active["relationships"] = {"assignee": {"data": {"id": "1001", "type": "users"}}}
    gap = {
        "id": "gap",
        "attributes": {
            "starts_at": (now + timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=2)).isoformat(),
        },
        "relationships": {
            "user": {"data": None},
            "assignee": {"data": {"type": "schedules", "id": "nested-schedule"}},
        },
    }
    upcoming = shift(
        (now + timedelta(hours=2)).isoformat(),
        (now + timedelta(days=1)).isoformat(),
        user_id="1002",
    )

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        return response({"data": [active, gap, upcoming]})

    tools, _ = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"]()
    assert result["schedules"][0]["current_oncall"]["user_id"] == "1001"
    assert result["schedules"][0]["next_oncall"]["user_id"] == "1002"


async def test_handoff_rejects_truncated_schedule_listing():
    async def responder(method, path, params=None):
        return response({"data": [schedule()], "meta": {"total_pages": 6, "total_count": 512}})

    tools, request = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"]()
    assert result["error"]
    assert result["error_type"] == "validation_error"
    assert "512 schedules" in result["message"]
    assert "Pass team_ids or schedule_ids" in result["message"]
    assert "schedules" not in result
    assert request.await_count == 1


async def test_handoff_over_the_filtered_cap_points_at_list_shifts():
    async def responder(method, path, params=None):
        return response({"data": [schedule()], "meta": {"total_pages": 21, "total_count": 2050}})

    tools, request = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"](schedule_ids="sched-1")
    assert result["error_type"] == "validation_error"
    assert "2050 schedules, more than the 2000" in result["message"]
    assert "Use list_shifts with schedule_ids instead." in result["message"]
    assert request.await_count == 1


async def test_handoff_with_a_filter_reads_past_500_schedules():
    """ROOTLY-MCP: an organization with 512 schedules got no handoff at all."""
    now = datetime.now(UTC)
    active = shift((now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat())

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            assert params is not None
            page = params["page[number]"]
            listed = schedule() if page == 6 else {"id": f"other-{page}", "attributes": {}}
            return response({"data": [listed], "meta": {"total_pages": 6, "total_count": 512}})
        assert path == "/v1/schedules/sched-1/shifts"
        return response({"data": [active]})

    tools, _ = oncall_tools(responder)
    result = await tools["get_oncall_handoff_summary"](schedule_ids="sched-1")
    assert [entry["schedule_id"] for entry in result["schedules"]] == ["sched-1"]
    assert result["schedules"][0]["current_oncall"]["user_id"] == "1001"


async def test_availability_does_not_report_free_users_when_schedule_query_fails():
    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"errors": [{"title": "Unavailable"}]}, 500)
        return response({"data": []})

    tools, _ = oncall_tools(responder)
    result = await tools["check_responder_availability"](
        start_date="2026-10-13", end_date="2026-10-20", user_ids="1001"
    )
    assert result["error"]
    assert "not_scheduled" not in result


async def test_severity_level_can_match_multiple_configured_severities():
    async def responder(method, path, params=None):
        if path == "/v1/severities":
            return response(
                {
                    "data": [
                        {"id": f"sev-{i}", "attributes": {"severity": "critical"}} for i in range(2)
                    ]
                }
            )
        assert params is not None
        assert params["filter[severity_id][in]"] == "sev-0,sev-1"
        return response({"data": []})

    tools, _ = oncall_tools(responder)
    result = await tools["get_shift_incidents"](
        start_time="2026-10-06T00:00:00Z", end_time="2026-10-07T00:00:00Z", severity="critical"
    )
    assert result["success"]


async def test_null_severity_attributes_do_not_match_none_string():
    async def responder(method, path, params=None):
        assert path == "/v1/severities"
        return response({"data": [{"id": "sev-1", "attributes": {"name": "SEV0", "slug": None}}]})

    tools, request = oncall_tools(responder)
    result = await tools["get_shift_incidents"](
        start_time="2026-10-06T00:00:00Z", end_time="2026-10-07T00:00:00Z", severity="none"
    )
    assert result["error_type"] == "validation_error"
    assert request.await_count == 1


async def test_availability_accepts_space_separated_datetime_and_excludes_touching_shift():
    touching = shift("2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z", shift_id="ended")
    active = shift("2026-10-13T00:00:00Z", "2026-10-14T00:00:00Z")

    async def responder(method, path, params=None):
        if path == "/v1/schedules":
            return response({"data": [schedule()]})
        if path.endswith("/shifts"):
            return response({"data": [touching, active]})
        return response({"data": []})

    tools, request = oncall_tools(responder)
    result = await tools["check_responder_availability"](
        start_date="2026-10-13 00:00", end_date="2026-10-13 10:00", user_ids="1001"
    )
    assert result["scheduled"][0]["total_hours"] == 10
    assert len(result["scheduled"][0]["shifts"]) == 1
    assert sum(c.args[1] == "/v1/schedules" for c in request.await_args_list) == 1
