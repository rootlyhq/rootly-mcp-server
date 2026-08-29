"""Guard the tool names that shipped guidance tells the model to call.

The ``rootly://workflow-guide`` resource and the example Claude Code skill both
name tools verbatim. Tool names are derived from OpenAPI operationIds, which are
snake_cased at load time (see ``spec_transform.snakecase_operation_ids``); the
historical camelCase names stay callable through the alias middleware but are
hidden from ``tools/list``. Guidance that names a hidden alias therefore tells
the model to call a tool it cannot see. These tests fail whenever the guidance
drifts from the advertised surface.
"""

import json
import re
from pathlib import Path

import pytest

from rootly_mcp_server.server import create_rootly_mcp_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "examples" / "skills" / "rootly-incident-responder.md"

# ``tool_name(`` — a call-shaped reference such as ``list_incidents(status=...)``.
_CALL_REFERENCE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(")
# ```tool_name``` — an inline-code identifier with no dots, spaces, or operators.
_BACKTICK_IDENTIFIER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


@pytest.fixture
async def advertised_tool_names(mock_environment_token) -> set[str]:
    server = create_rootly_mcp_server(hosted=False)
    tools = await server.list_tools()
    return {tool.name for tool in tools}


@pytest.fixture
async def workflow_guide_text(mock_environment_token) -> str:
    server = create_rootly_mcp_server(hosted=False)
    result = await server.read_resource("rootly://workflow-guide")
    payload = json.loads(result.contents[0].content)
    return str(payload["text"])


@pytest.mark.unit
class TestDocumentedToolNames:
    async def test_workflow_guide_names_only_advertised_tools(
        self, workflow_guide_text, advertised_tool_names
    ):
        referenced = set(_CALL_REFERENCE.findall(workflow_guide_text))
        assert referenced, "workflow guide no longer contains any tool calls"

        unknown = sorted(referenced - advertised_tool_names)
        assert unknown == [], f"rootly://workflow-guide names tools not in tools/list: {unknown}"

    async def test_incident_responder_skill_names_only_advertised_tools(
        self, advertised_tool_names
    ):
        referenced = set(_BACKTICK_IDENTIFIER.findall(SKILL_PATH.read_text(encoding="utf-8")))
        assert referenced, "example skill no longer references any tools"

        unknown = sorted(referenced - advertised_tool_names)
        assert unknown == [], f"{SKILL_PATH.name} names tools not in tools/list: {unknown}"
