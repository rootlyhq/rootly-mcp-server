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

# A tool-shaped token anywhere in the text: every tool this server advertises
# starts with one of these verbs, and no tool *parameter* does (``incident_id``,
# ``filter_by_region``, ``start_time``), so this catches references whether they
# are backticked, written as a call (``list_incidents(status=...)``), or plain
# prose (``confirmed via list_environments``). The verb may be followed by a
# ``_snake_case`` tail or a ``CamelCase`` one, so a stale camelCase alias is
# extracted — and then flagged — rather than silently skipped.
_TOOL_REFERENCE = re.compile(
    r"\b((?:attach|check|collect|create|delete|find|get|list|patch|search|suggest|update)"
    r"(?:_[a-z0-9_]+|[A-Z][A-Za-z0-9]*))\b"
)


def tool_references(text: str) -> set[str]:
    """Every tool name ``text`` mentions, wherever and however it is written."""
    return set(_TOOL_REFERENCE.findall(text))


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
class TestToolReferenceExtraction:
    def test_finds_prose_backticked_and_call_shaped_references(self):
        sample = (
            "Environment: Production (confirmed via list_environments)\n"
            "Use `list_teams` to get ownership context\n"
            "Pass filter_by_region=True to get_oncall_handoff_summary(timezone=...)\n"
        )

        assert tool_references(sample) == {
            "list_environments",
            "list_teams",
            "get_oncall_handoff_summary",
        }

    def test_still_extracts_a_camelcase_regression(self):
        """A stale camelCase alias must be extracted so it can be flagged, not skipped."""
        sample = "Current responder verified via getCurrentUser; then `listTeams`. Getting started."

        assert tool_references(sample) == {"getCurrentUser", "listTeams"}


@pytest.mark.unit
class TestDocumentedToolNames:
    async def test_workflow_guide_names_only_advertised_tools(
        self, workflow_guide_text, advertised_tool_names
    ):
        referenced = tool_references(workflow_guide_text)
        assert referenced, "workflow guide no longer contains any tool references"

        unknown = sorted(referenced - advertised_tool_names)
        assert unknown == [], f"rootly://workflow-guide names tools not in tools/list: {unknown}"

    async def test_incident_responder_skill_names_only_advertised_tools(
        self, advertised_tool_names
    ):
        referenced = tool_references(SKILL_PATH.read_text(encoding="utf-8"))
        assert referenced, "example skill no longer references any tools"

        unknown = sorted(referenced - advertised_tool_names)
        assert unknown == [], f"{SKILL_PATH.name} names tools not in tools/list: {unknown}"
