"""GET / on hosted deployments serves a setup page instead of a 404."""

from __future__ import annotations

import base64
import hashlib
import re
from unittest.mock import patch

import httpx2 as httpx
import pytest
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from rootly_mcp_server.__main__ import build_dual_http_app
from rootly_mcp_server.landing_page import CONTENT_SECURITY_POLICY, render_landing_page
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


def test_hosted_root_serves_the_setup_page(hosted_server):
    response = _get_root(hosted_server)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Rootly MCP server</title>" in response.text
    # Code Mode is the recommended endpoint, as on docs.rootly.com.
    assert "claude mcp add --transport http rootly http://testserver/mcp-codemode" in response.text
    assert "<code>http://testserver/mcp</code>" in response.text
    assert "http://testserver/sse" in response.text
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


def test_configured_public_url_is_used_and_cacheable(hosted_server):
    with patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"):
        response = _get_root(hosted_server)

    assert "https://mcp.example.com/mcp" in response.text
    assert "testserver" not in response.text
    assert response.headers["cache-control"] == "public, max-age=300"


def test_custom_code_mode_path_is_shown(hosted_server, monkeypatch):
    monkeypatch.setenv("ROOTLY_CODE_MODE_PATH", "codemode/")

    assert "http://testserver/codemode" in _get_root(hosted_server).text


def test_render_escapes_every_interpolated_url():
    page = render_landing_page('https://x"<b>', "/mcp-codemode")

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
