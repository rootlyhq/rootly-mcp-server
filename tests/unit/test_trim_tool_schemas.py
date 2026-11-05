"""spec_transform.trim_tool_schemas: drop ignored fields, shorten the time zone list."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.spec_transform import trim_tool_schemas

TIME_ZONES = ["Etc/UTC", "America/New_York", *(f"Zone/{i}" for i in range(150))]


def _spec() -> dict:
    return {
        "paths": {
            "/v1/things": {
                "get": {
                    "parameters": [
                        {
                            "name": "tz",
                            "in": "query",
                            "schema": {"type": "string", "enum": TIME_ZONES},
                        }
                    ]
                }
            }
        },
        "components": {
            "schemas": {
                "update_thing": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Accepted for JSON:API client compatibility, but ignored.",
                        },
                        "slug": {"type": "string", "description": "Deprecated. Derived from name."},
                        "owner_id": {"type": "string", "description": "[DEPRECATED] Use owner."},
                        "name": {
                            "type": "string",
                            "description": "Name. Deprecated values are rejected.",
                        },
                        "time_zone": {"type": "string", "description": "Zone.", "enum": TIME_ZONES},
                        "color": {"type": "string", "enum": ["red", "blue"]},
                    },
                    "required": ["slug", "name"],
                }
            }
        },
    }


def test_ignored_and_deprecated_properties_are_dropped():
    spec = _spec()

    counts = trim_tool_schemas(spec)

    schema = spec["components"]["schemas"]["update_thing"]
    assert set(schema["properties"]) == {"name", "time_zone", "color"}
    assert schema["required"] == ["name"]
    assert counts["ignored_properties"] == 3


def test_time_zone_enum_becomes_a_string_with_an_example():
    spec = _spec()

    counts = trim_tool_schemas(spec)

    time_zone = spec["components"]["schemas"]["update_thing"]["properties"]["time_zone"]
    assert "enum" not in time_zone
    assert time_zone["description"].startswith("Zone. IANA time zone name")
    parameter = spec["paths"]["/v1/things"]["get"]["parameters"][0]["schema"]
    assert "enum" not in parameter
    assert counts["time_zone_enums"] == 2
    # Short enums are real choices and stay.
    assert spec["components"]["schemas"]["update_thing"]["properties"]["color"]["enum"] == [
        "red",
        "blue",
    ]


async def test_bundled_tools_carry_no_time_zone_list_or_ignored_fields(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    async with Client(server) as client:
        listed = await client.list_tools_mcp()
    # As sent on the wire (camelCase keys), whatever the SDK's attribute names.
    schemas = {tool.name: tool.model_dump(by_alias=True)["inputSchema"] for tool in listed.tools}

    blob = json.dumps(list(schemas.values()))
    assert "Pacific/Kiritimati" not in blob
    assert "Accepted for JSON:API client compatibility" not in blob
    schedule = schemas["create_schedule_rotation"]
    assert "IANA time zone name" in json.dumps(schedule)


async def test_update_still_sends_a_data_id_the_caller_passes(monkeypatch):
    # data.id left the schema; a client that sends it must still get it through.
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    sent: list = []

    from rootly_mcp_server.transport import AuthenticatedHTTPXClient

    async def fake_send(self, request, **kwargs):
        import httpx2 as httpx

        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"data": {}}, request=request)

    monkeypatch.setattr(AuthenticatedHTTPXClient, "send", fake_send, raising=False)
    async with Client(create_rootly_mcp_server(hosted=False, enable_write_tools=True)) as client:
        await client.call_tool(
            "update_team",
            {"id": "t1", "data": {"id": "t1", "type": "groups", "attributes": {"name": "SRE"}}},
            raise_on_error=False,
        )

    assert sent and sent[0]["data"]["id"] == "t1"
    assert sent[0]["data"]["attributes"]["name"] == "SRE"


def _capture_sends(monkeypatch) -> list:
    from rootly_mcp_server.transport import AuthenticatedHTTPXClient

    sent: list = []

    async def fake_send(self, request, **kwargs):
        import httpx2 as httpx

        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(201, json={"data": {}}, request=request)

    monkeypatch.setattr(AuthenticatedHTTPXClient, "send", fake_send, raising=False)
    return sent


@pytest.mark.parametrize(
    "arguments",
    [
        # Flat arguments, no data at all.
        {"name": "SRE", "slug": "sre"},
        # Attributes put straight on data.
        {"data": {"type": "groups", "name": "SRE", "slug": "sre"}},
    ],
)
async def test_a_trimmed_field_does_not_stop_body_repair(monkeypatch, arguments):
    # slug left the advertised schema; a caller still sending it must get the
    # same repair as before (fields gathered into data.attributes), not a refusal.
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    sent = _capture_sends(monkeypatch)
    server = create_rootly_mcp_server(hosted=False, enable_write_tools=True)
    async with Client(server) as client:
        result = await client.call_tool("create_team", arguments, raise_on_error=False)

    assert not result.is_error, result.content
    assert sent[0]["data"]["attributes"] == {"name": "SRE", "slug": "sre"}
