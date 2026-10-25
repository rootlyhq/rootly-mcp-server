"""The page served at GET / on hosted deployments.

A human who opens the server URL in a browser used to get a 404. This page tells
them what the server is and how to connect a client to it. MCP clients never
request it: they talk to the transport paths.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse

from .utils import is_mcp_server_url_static, resolve_mcp_server_url

# Copy buttons. Kept as one inline script so the page needs no other request;
# the CSP allows exactly this script by hash.
_COPY_SCRIPT = """
document.querySelectorAll("[data-copy]").forEach(function (button) {
  button.addEventListener("click", function () {
    var text = document.getElementById(button.dataset.copy).textContent;
    navigator.clipboard.writeText(text).then(function () {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy"; }, 1500);
    });
  });
});
"""
_COPY_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_COPY_SCRIPT.encode()).digest()).decode()

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    f"script-src 'sha256-{_COPY_SCRIPT_HASH}'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_STYLE = """
:root {
  --bg: #F7F5FF; --card: #FFFFFF; --text: #100F12; --muted: #65646E;
  --line: #EBE5FF; --brand: #8D6FDE; --brand-strong: #6D55BD;
  --code-bg: #100F12; --code-text: #F7F5FF; --ok: #2E9E5B;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #100F12; --card: #1B1920; --text: #F7F5FF; --muted: #AAA9AE;
    --line: #2C2935; --brand: #A28AE5; --brand-strong: #D9CFFA;
    --code-bg: #0A090C; --code-text: #EBE5FF;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 17px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 760px; margin: 0 auto; padding: 48px 20px 64px; }
.bar { height: 6px; background: linear-gradient(90deg, #6D55BD 0%, #D9CFFA 62%, #FFC387 100%); }
.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.wordmark { font-weight: 650; font-size: 20px; letter-spacing: -0.01em; }
.wordmark span { color: var(--brand); }
.pill {
  display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px;
  border: 1px solid var(--line); border-radius: 999px; background: var(--card);
  color: var(--muted); font-size: 14px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); animation: pulse 2s infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(46,158,91,.5); } 70% { box-shadow: 0 0 0 8px rgba(46,158,91,0); } 100% { box-shadow: 0 0 0 0 rgba(46,158,91,0); } }
h1 { font-size: 44px; line-height: 1.1; letter-spacing: -0.02em; font-weight: 600; margin: 40px 0 16px; }
h2 { font-size: 22px; font-weight: 600; margin: 40px 0 12px; }
p { margin: 0 0 12px; }
.lede { font-size: 19px; color: var(--muted); }
a { color: var(--brand-strong); }
@media (prefers-color-scheme: dark) { a { color: var(--brand); } }
.terminal { margin-top: 28px; border-radius: 12px; background: var(--code-bg); color: var(--code-text); overflow: hidden; }
.terminal-head { display: flex; gap: 6px; padding: 12px 14px; }
.terminal-head i { width: 11px; height: 11px; border-radius: 50%; background: #44434E; }
.terminal-head i:first-child { background: #F0883E; }
.terminal-head i:nth-child(2) { background: #FFC387; }
.terminal-head i:nth-child(3) { background: #8D6FDE; }
.terminal-body { overflow: hidden; white-space: nowrap; padding: 4px 18px 18px; font: 14px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
.typed { display: inline-block; overflow: hidden; white-space: nowrap; vertical-align: bottom; border-right: 2px solid var(--code-text);
  animation: typing 2.6s steps(var(--chars)) .4s both, caret .8s step-end infinite; max-width: 100%; }
@keyframes typing { from { width: 0; } to { width: calc(var(--chars) * 1ch); } }
@keyframes caret { 50% { border-color: transparent; } }
@media (prefers-reduced-motion: reduce) { .typed, .dot { animation: none; width: auto; } }
.muted { color: #AAA9AE; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin: 12px 0; }
.code { position: relative; }
pre { margin: 0; padding: 14px 72px 14px 16px; border-radius: 10px; background: var(--code-bg); color: var(--code-text); border: 1px solid var(--line);
  font: 13.5px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-x: auto; }
button.copy { position: absolute; top: 8px; right: 8px; border: 0; border-radius: 6px; padding: 4px 10px; cursor: pointer;
  background: #2C2935; color: #EBE5FF; font: 600 12px system-ui, sans-serif; }
button.copy:hover { background: #4A3E8A; }
.endpoint { padding: 12px 2px; border-top: 1px solid var(--line); }
.endpoint:first-child { border-top: 0; padding-top: 2px; }
.endpoint:last-child { padding-bottom: 2px; }
.endpoint-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 4px; }
.endpoint-head code { overflow-wrap: anywhere; }
.endpoint p { margin: 0; color: var(--muted); }
.endpoint code, p code, li code { font: 14px ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--line); padding: 1px 6px; border-radius: 5px; }
.tag { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: #EBE5FF; color: #4A3E8A; white-space: nowrap; }
.tag.old { background: #FFE9D6; color: #8A4A12; }
ul.asks { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }
ul.asks li { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
ul.asks li::before { content: "\\201C"; color: var(--brand); font-weight: 700; margin-right: 2px; }
ul.asks li::after { content: "\\201D"; color: var(--brand); font-weight: 700; margin-left: 2px; }
details summary { cursor: pointer; color: var(--muted); margin: 8px 0; }
footer { margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line); color: var(--muted); font-size: 15px; }
footer nav { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 10px; }
@media (max-width: 560px) { h1 { font-size: 34px; } main { padding-top: 32px; } }
"""


def _code_block(block_id: str, text: str) -> str:
    return (
        '<div class="code">'
        f'<button class="copy" type="button" data-copy="{block_id}">Copy</button>'
        f'<pre id="{block_id}">{html.escape(text)}</pre>'
        "</div>"
    )


def render_landing_page(base_url: str, code_mode_path: str) -> str:
    """Render the page for a server reachable at ``base_url``."""
    mcp_url = f"{base_url}/mcp"
    code_mode_url = f"{base_url}{code_mode_path}"
    add_command = f"claude mcp add --transport http rootly {mcp_url}"
    oauth_config = (
        f'{{\n  "mcpServers": {{\n    "rootly": {{\n      "url": "{mcp_url}"\n    }}\n  }}\n}}'
    )
    token_config = (
        '{\n  "mcpServers": {\n    "rootly": {\n'
        f'      "url": "{mcp_url}",\n'
        '      "headers": {\n        "Authorization": "Bearer YOUR_ROOTLY_API_TOKEN"\n      }\n'
        "    }\n  }\n}"
    )
    esc = html.escape
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rootly MCP server</title>
<meta name="description" content="Connect Claude, Cursor or any MCP client to Rootly.">
<style>{_STYLE}</style>
</head>
<body>
<div class="bar"></div>
<main>
  <div class="top">
    <div class="wordmark">Rootly <span>MCP</span></div>
    <div class="pill"><span class="dot"></span>Up and answering</div>
  </div>

  <h1>Bring Rootly into your AI assistant</h1>
  <p class="lede">This is the Rootly MCP server. Point Claude, Cursor or any Model Context
  Protocol (MCP) client at it to search incidents, check who's on-call and work alerts
  without leaving your editor.</p>

  <div class="terminal" aria-label="Example command">
    <div class="terminal-head"><i></i><i></i><i></i></div>
    <div class="terminal-body">
      <span class="muted">$ </span><span class="typed" style="--chars: {len(add_command)}">{esc(add_command)}</span>
    </div>
  </div>

  <h2>Connect in one line</h2>
  <p>In Claude Code, run this. A browser window opens so you can sign in to Rootly.</p>
  {_code_block("cmd-claude", add_command)}

  <h2>Or add it to your client config</h2>
  <p>Cursor, Windsurf, Claude Desktop and most other clients take this shape. Your client
  handles the OAuth sign-in, so you don't need an API token.</p>
  {_code_block("cfg-oauth", oauth_config)}
  <details>
    <summary>Prefer an API token?</summary>
    <p>Send a Rootly API key as a Bearer header. A global key sees the whole organization,
    a team key sees that team's entities, and a personal key sees what you can.
    <a href="https://docs.rootly.com/api-reference/overview#how-to-generate-an-api-key%3F">Generate an API key</a>.</p>
    {_code_block("cfg-token", token_config)}
  </details>

  <h2>Endpoints</h2>
  <div class="card">
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(mcp_url)}</code><span class="tag">Use this</span></div>
      <p>Streamable HTTP. Works with every current MCP client.</p>
    </div>
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(code_mode_url)}</code><span class="tag">Code Mode</span></div>
      <p>Your agent writes short Python that chains tool calls, so one request does the work of many.</p>
    </div>
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(base_url)}/sse</code><span class="tag old">Deprecated</span></div>
      <p>Removed after 2026-11-20. Switch to <code>/mcp</code>.</p>
    </div>
  </div>
  <p>Want a smaller tool list? Add <code>?tool_profile=slim</code> to the URL for about 70 tools.</p>

  <h2>Things to ask once you're connected</h2>
  <ul class="asks">
    <li>Who's on-call for the payments team this weekend?</li>
    <li>Summarize last month's SEV1 incidents and what resolved them.</li>
    <li>Which alerts fired most this week?</li>
    <li>Pull the retrospective for INC-1234 and list its open action items.</li>
  </ul>

  <footer>
    <nav>
      <a href="https://docs.rootly.com/integrations/mcp-server">Read the MCP server docs</a>
      <a href="https://github.com/rootlyhq/rootly-mcp-server#readme">View the source on GitHub</a>
      <a href="/health">Check server health</a>
      <a href="https://rootly.com">rootly.com</a>
    </nav>
    This page used to be a 404. Now it's the front door.
  </footer>
</main>
<script>{_COPY_SCRIPT}</script>
</body>
</html>
"""


async def landing_page(request: Request) -> HTMLResponse:
    """GET / handler: the setup page, with URLs for the host that served it."""
    code_mode_path = "/" + os.getenv("ROOTLY_CODE_MODE_PATH", "/mcp-codemode").strip("/")
    base_url = resolve_mcp_server_url(request).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        # The scheme can come from X-Forwarded-Proto; show only web URLs.
        base_url = "https://" + base_url.split("://", 1)[-1]
    page = render_landing_page(base_url, code_mode_path)
    return HTMLResponse(
        page,
        headers={
            # The URLs come from the request's Host header unless the public
            # URL is configured, so only a configured page may be cached.
            "Cache-Control": "public, max-age=300" if is_mcp_server_url_static() else "no-store",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
