"""GET / on hosted deployments serves a setup page instead of a 404."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import fastmcp
import httpx2 as httpx
import pytest
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from rootly_mcp_server.__main__ import build_dual_http_app
from rootly_mcp_server.landing_page import (
    CONTENT_SECURITY_POLICY,
    HOSTED_ENDPOINTS,
    McpEndpoints,
    render_landing_page,
)
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.transport import AuthCaptureMiddleware

pytestmark = pytest.mark.unit

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"


@pytest.fixture
def hosted_server(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=True)


def _get_root(server, **kwargs):
    with TestClient(server.http_app()) as client:
        return client.get("/", **kwargs)


def _get_dual_root(server, code_mode_path: str | None = "/mcp-codemode"):
    """GET / on the hosted dual-transport app, with Code Mode mounted at ``code_mode_path``."""
    code_mode = create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=True, name="cm")
    app = build_dual_http_app(
        server,
        code_mode_server=code_mode if code_mode_path else None,
        code_mode_path=code_mode_path,
    )
    with TestClient(app) as client:
        return client.get("/")


def test_hosted_dual_app_lists_every_mounted_endpoint(hosted_server):
    response = _get_dual_root(hosted_server)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Rootly MCP server</title>" in response.text
    # Code Mode is the recommended endpoint, as on docs.rootly.com.
    assert "claude mcp add --transport http rootly http://testserver/mcp-codemode" in response.text
    assert "<code>http://testserver/mcp</code>" in response.text
    assert "http://testserver/sse" in response.text
    assert "not both" in response.text
    assert "front door" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'href="https://docs.rootly.com/integrations/mcp-server"' in response.text


def test_local_server_has_no_landing_page(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    server = create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=False)

    assert _get_root(server).status_code == 404


def test_csp_allows_exactly_the_inline_script(hosted_server):
    response = _get_root(hosted_server)

    scripts = re.findall(r"<script>(.*?)</script>", response.text, re.DOTALL)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    csp = response.headers["content-security-policy"]
    assert csp == CONTENT_SECURITY_POLICY
    assert f"script-src 'sha256-{digest}'" in csp
    assert "default-src 'none'" in csp


def test_host_header_is_escaped(hosted_server):
    # Without ROOTLY_MCP_SERVER_URL the URLs come from the Host header.
    response = _get_root(hosted_server, headers={"host": 'evil"><script>alert(1)</script>'})

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_non_web_forwarded_scheme_is_replaced(hosted_server):
    response = _get_root(hosted_server, headers={"x-forwarded-proto": "javascript"})

    assert "javascript:" not in response.text
    assert "https://testserver/mcp" in response.text


def test_streamable_only_app_points_setup_at_mcp(hosted_server):
    """Greptile P1: no Code Mode mounted, so nothing may point at it."""
    page = _get_root(hosted_server).text

    assert "claude mcp add --transport http rootly http://testserver/mcp" in page
    assert "mcp-codemode" not in page
    assert "/sse" not in page
    assert "not both" not in page


def test_dual_app_without_code_mode_hides_it(hosted_server):
    page = _get_dual_root(hosted_server, code_mode_path=None).text

    assert "mcp-codemode" not in page
    assert "claude mcp add --transport http rootly http://testserver/mcp</span>" in page
    assert '<code>http://testserver/mcp</code><span class="tag">Recommended</span>' in page


def test_configured_public_url_is_used_and_cacheable(hosted_server):
    with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
        response = _get_root(hosted_server)

    assert "https://mcp.example.com/mcp" in response.text
    assert "testserver" not in response.text
    assert response.headers["cache-control"] == "public, max-age=300"


def test_mounted_paths_are_used(hosted_server, monkeypatch):
    """--code-mode-path and fastmcp's streamable path are what the page shows."""
    monkeypatch.setattr(fastmcp.settings, "streamable_http_path", "/custom-mcp")
    page = _get_dual_root(hosted_server, code_mode_path="/custom-code").text

    assert "claude mcp add --transport http rootly http://testserver/custom-code" in page
    assert "<code>http://testserver/custom-mcp</code>" in page
    assert "testserver/mcp-codemode" not in page


def test_sse_only_server_has_no_setup_tabs():
    page = render_landing_page("https://x.example", McpEndpoints(sse="/sse"))

    assert "only serves the deprecated SSE transport" in page
    assert "claude mcp add" not in page
    assert "https://x.example/sse" in page


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"mcp_endpoints": HOSTED_ENDPOINTS}, HOSTED_ENDPOINTS),
        ({"path": "/mcp", "transport_type": "streamable-http"}, McpEndpoints(streamable="/mcp")),
        ({"path": "/sse", "transport_type": "sse"}, McpEndpoints(sse="/sse")),
        ({}, McpEndpoints()),
    ],
)
def test_endpoints_come_from_app_state(state, expected):
    class State:
        pass

    app_state = State()
    for key, value in state.items():
        setattr(app_state, key, value)

    assert McpEndpoints.from_app_state(app_state) == expected


def test_render_escapes_every_interpolated_url():
    page = render_landing_page('https://x"<b>', HOSTED_ENDPOINTS)

    assert '"<b>' not in page


async def test_page_needs_no_token_while_mcp_still_does(hosted_server):
    async def reject_all(self, auth_header):
        return None

    with patch.object(AuthCaptureMiddleware, "_validate_token_upstream", reject_all):
        app = build_dual_http_app(hosted_server, middleware=[Middleware(AuthCaptureMiddleware)])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/")
            mcp = await client.post("/mcp", json={})

    assert page.status_code == 200
    assert "Bring Rootly into your AI assistant" in page.text
    assert mcp.status_code == 401


def test_every_documented_client_has_a_tab():
    page = render_landing_page("https://mcp.example.com", HOSTED_ENDPOINTS)

    for label in ("Claude Code", "Cursor", "Codex", "Claude Desktop", "Gemini CLI"):
        assert f">{label}</label>" in page
    assert "Windsurf" not in page
    assert "Slack" not in page
    # Each copy button points at exactly one code block.
    ids = re.findall(r'<pre id="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    assert sorted(re.findall(r'data-copy="([^"]+)"', page)) == sorted(ids)
    assert "url = &quot;https://mcp.example.com/mcp-codemode&quot;" in page


def test_oauth_comes_first_and_tokens_are_collapsed():
    page = render_landing_page("https://mcp.example.com", HOSTED_ENDPOINTS)
    url = "https://mcp.example.com/mcp-codemode"

    assert f"codex mcp add rootly --url {url}\ncodex mcp login rootly" in page
    assert f"gemini mcp add --transport http rootly {url}" in page
    assert "/mcp auth rootly" in page
    assert "Add custom connector" in page
    # Every token variant sits after its OAuth steps, inside a collapsed section.
    panels = page.split('<div class="panel" id="panel-')[1:]
    assert len(panels) == 5
    for panel in panels:
        oauth_steps, token_section = panel.split("<details><summary>With an API token instead", 1)
        assert "<pre" in oauth_steps
        assert "Bearer" not in oauth_steps and "ROOTLY_API_TOKEN" not in oauth_steps
        assert "<pre" in token_section


def test_cursor_install_link_carries_the_code_mode_url():
    page = render_landing_page("https://mcp.example.com", HOSTED_ENDPOINTS)

    link = re.search(r'href="(cursor://anysphere\.cursor-deeplink/mcp/install\?[^"]+)"', page)
    assert link
    query = parse_qs(urlparse(html.unescape(link.group(1))).query)
    assert query["name"] == ["rootly"]
    assert json.loads(base64.b64decode(query["config"][0])) == {
        "url": "https://mcp.example.com/mcp-codemode"
    }
