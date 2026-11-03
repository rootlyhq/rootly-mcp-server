"""AI automations and skills (``/v1/ai/``) on the default self-hosted surface."""

from __future__ import annotations

import pytest

from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.spec_transform import _filter_openapi_spec

pytestmark = pytest.mark.unit

AI_READ_TOOLS = {
    "list_ai_automations",
    "get_ai_automation",
    "list_ai_automation_runs",
    "get_ai_automation_run",
    "list_ai_automation_slack_channels",
    "list_ai_automation_slack_users",
    "list_ai_automation_slack_user_groups",
    "get_ai_automation_trigger_options",
    "list_ai_skills",
    "get_ai_skill",
    "list_ai_skill_versions",
    "get_ai_skill_version",
}
AI_WRITE_TOOLS = {
    "create_ai_automation",
    "update_ai_automation",
    "run_ai_automation",
    "create_ai_skill",
    "update_ai_skill",
}


async def _tool_names(**kwargs) -> set[str]:
    server = create_rootly_mcp_server(hosted=False, **kwargs)
    return {tool.name for tool in await server.list_tools()}


async def test_ai_read_and_write_tools_are_exposed(mock_environment_token):
    names = await _tool_names()

    assert AI_READ_TOOLS | AI_WRITE_TOOLS <= names


async def test_ai_writes_stay_off_when_write_tools_are_disabled(mock_environment_token):
    names = await _tool_names(enable_write_tools=False)

    assert AI_READ_TOOLS <= names
    assert not AI_WRITE_TOOLS & names


async def test_ai_deletes_secret_rotation_and_duplicate_put_are_not_exposed(
    mock_environment_token,
):
    names = await _tool_names()

    assert (
        not {
            "delete_ai_automation",
            "delete_ai_skill",
            "rotate_ai_automation_webhook_secret",
            "put_ai_automation",
            "put_ai_skill",
        }
        & names
    )


def _op(operation_id: str) -> dict:
    return {"operationId": operation_id, "responses": {}}


def test_put_dropped_only_when_patch_is_the_named_update():
    spec = {
        "paths": {
            "/v1/ai/skills/{id}": {"patch": _op("UpdateAISkill"), "put": _op("PutAISkill")},
            "/v1/alert_routes/{id}": {
                "put": _op("updateAlertRoute"),
                "patch": _op("patchAlertRoute"),
            },
        }
    }

    filtered = _filter_openapi_spec(spec, allowed_paths=list(spec["paths"]))

    assert set(filtered["paths"]["/v1/ai/skills/{id}"]) == {"patch"}
    assert set(filtered["paths"]["/v1/alert_routes/{id}"]) == {"put", "patch"}
