"""Tests for OpenAI plugin submission requirements.

Covers the two changes made to satisfy the OpenAI submission scan for Rootly
Code Mode:

1. Explicit MCP ToolAnnotations on the Code Mode tools.
2. The unauthenticated ``/.well-known/openai-apps-challenge`` domain-verification
   endpoint.
"""

import pytest
from starlette.testclient import TestClient

from rootly_mcp_server.code_mode import build_code_mode_transform, create_rootly_codemode_server
from rootly_mcp_server.server import create_rootly_mcp_server

_DISCOVERY_TOOL_NAMES = ("list_tools", "tool_search", "get_schema", "tags")

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"
CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
CHALLENGE_ENV = "ROOTLY_OPENAI_APPS_CHALLENGE_TOKEN"


@pytest.mark.unit
class TestCodeModeAnnotations:
    """Code Mode tools must expose explicit, accurate ToolAnnotations."""

    def _tools_by_name(self):
        transform = build_code_mode_transform()
        tools = {t.name: t for t in transform._build_discovery_tools()}  # noqa: SLF001
        execute = transform._make_execute_tool()  # noqa: SLF001
        tools[execute.name] = execute
        return tools

    def test_discovery_tools_are_read_only(self):
        tools = self._tools_by_name()
        for name in ("list_tools", "tool_search", "get_schema", "tags"):
            assert name in tools, f"missing discovery tool {name}"
            ann = tools[name].annotations
            assert ann is not None, f"{name} has no annotations"
            assert ann.readOnlyHint is True
            assert ann.destructiveHint is False
            assert ann.idempotentHint is True
            assert ann.openWorldHint is False

    def test_execute_has_conservative_gateway_annotations(self):
        ann = self._tools_by_name()["execute"].annotations
        assert ann is not None
        assert ann.readOnlyHint is False
        assert ann.destructiveHint is True
        assert ann.idempotentHint is False
        assert ann.openWorldHint is True

    def test_execute_declares_generic_wrapped_output_schema(self):
        schema = self._tools_by_name()["execute"].output_schema
        assert schema == {
            "type": "object",
            "properties": {"result": {}},
            "required": ["result"],
            "x-fastmcp-wrap-result": True,
        }


@pytest.mark.unit
class TestCodeModeServerExposedAnnotations:
    """End-to-end: assert on the tools the Code Mode server actually exposes.

    This guards the whole chain the OpenAI scanner sees (transform -> registration
    -> the served tool list), not just the internal builder methods, so a future
    FastMCP change that drops the annotations is caught here.
    """

    async def test_exposed_code_mode_tools_carry_annotations(self):
        server = create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=True)
        tools = {t.name: t for t in await server.list_tools()}

        # Only the Code Mode discovery + execute tools are exposed.
        assert set(tools) == {*_DISCOVERY_TOOL_NAMES, "execute"}

        # Every exposed tool is annotated.
        for name, tool in tools.items():
            assert tool.annotations is not None, f"{name} exposed without annotations"

        # The hints match the submission requirements.
        for name in _DISCOVERY_TOOL_NAMES:
            ann = tools[name].annotations
            assert ann is not None
            assert (
                ann.readOnlyHint,
                ann.destructiveHint,
                ann.idempotentHint,
                ann.openWorldHint,
            ) == (True, False, True, False)

        execute = tools["execute"].annotations
        assert execute is not None
        assert (
            execute.readOnlyHint,
            execute.destructiveHint,
            execute.idempotentHint,
            execute.openWorldHint,
        ) == (False, True, False, True)


@pytest.mark.unit
class TestOpenAIAppsChallenge:
    """The domain-verification endpoint returns the configured token or 404."""

    def _hosted_server(self):
        return create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=True)

    def test_challenge_route_registered_in_hosted_app(self):
        server = self._hosted_server()
        paths = [getattr(r, "path", None) for r in server._get_additional_http_routes()]  # noqa: SLF001
        assert CHALLENGE_PATH in paths

    def test_returns_configured_token(self, monkeypatch):
        monkeypatch.setenv(CHALLENGE_ENV, "portal-token-xyz")
        with TestClient(self._hosted_server().http_app()) as client:
            resp = client.get(CHALLENGE_PATH)
        assert resp.status_code == 200
        assert resp.text == "portal-token-xyz"
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.headers["cache-control"] == "no-store"

    def test_token_is_stripped(self, monkeypatch):
        monkeypatch.setenv(CHALLENGE_ENV, "  padded-token  ")
        with TestClient(self._hosted_server().http_app()) as client:
            resp = client.get(CHALLENGE_PATH)
        assert resp.status_code == 200
        assert resp.text == "padded-token"

    def test_404_when_token_absent(self, monkeypatch):
        monkeypatch.delenv(CHALLENGE_ENV, raising=False)
        with TestClient(self._hosted_server().http_app()) as client:
            resp = client.get(CHALLENGE_PATH)
        assert resp.status_code == 404

    def test_404_when_token_empty(self, monkeypatch):
        monkeypatch.setenv(CHALLENGE_ENV, "   ")
        with TestClient(self._hosted_server().http_app()) as client:
            resp = client.get(CHALLENGE_PATH)
        assert resp.status_code == 404
