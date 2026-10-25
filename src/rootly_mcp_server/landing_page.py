"""The page served at GET / on hosted deployments.

A human who opens the server URL in a browser used to get a 404. This page tells
them what the server is and how to connect a client to it. MCP clients never
request it: they talk to the transport paths.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from urllib.parse import quote

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

# The Rootly glyph on the brand purple, as served at rootly.com/brand/favicon.svg.
# Inlined as a data URI so the page still makes no request of its own.
_LOGO_SVG = (
    '<svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/200'
    '0/svg"><rect width="32" height="32" fill="#8D6FDE"/><path d="M6.19141 18.8463C6.21248 18'
    ".8424 13.1815 17.5605 16.0888 21.3252C18.9213 17.6089 25.9864 18.8463 25.9864 18.8463L25"
    ".6658 20.8958C25.6658 20.8958 17.89 19.1903 17.29 24.1252H14.89C14.29 19.2026 6.51191 20"
    ".8958 6.51191 20.8958L6.19141 18.8463ZM16.09 13.6002C16.09 13.6002 17.44 15.0252 17.44 1"
    "6.5252C17.44 17.4252 16.8356 18.2502 16.09 18.2502C15.3445 18.2502 14.74 17.4252 14.74 1"
    "6.5252C14.74 15.0252 16.09 13.6002 16.09 13.6002ZM7.33955 14.8147C7.33955 14.8147 9.1003"
    " 13.9469 10.5331 14.3908C11.3928 14.6572 12.0019 15.4786 11.7813 16.1908C11.5606 16.903 "
    "10.5938 17.2362 9.73408 16.9699C8.30242 16.5264 7.34109 14.8175 7.33955 14.8147ZM21.6378"
    " 14.3908C23.0698 13.9472 24.8293 14.8138 24.8313 14.8147C24.8271 14.8221 23.8665 16.527 "
    "22.4368 16.9699C21.5771 17.2363 20.6102 16.903 20.3896 16.1908C20.1689 15.4786 20.7781 1"
    "4.6572 21.6378 14.3908ZM9.76397 10.6146L9.76877 10.6137 11.6922 10.2404 12.9567 11.0438C"
    "13.7164 11.5264 14.0887 12.4789 13.6889 13.1082C13.2891 13.7375 12.2685 13.8053 11.5089 "
    "13.3227C10.2428 12.5183 9.76397 10.6146 9.76397 10.6146ZM19.1688 11.0438C20.4334 10.2404"
    " 22.3568 10.6137 22.3616 10.6146C22.3616 10.6146 21.8828 12.5183 20.6167 13.3227C19.857 "
    "13.8053 18.8365 13.7375 18.4367 13.1082C18.0369 12.4789 18.4092 11.5264 19.1688 11.0438Z"
    "M16.09 7.0752C16.09 7.0752 17.44 8.5002 17.44 10.0002C17.44 10.9002 16.8356 11.7252 16.0"
    "9 11.7252C15.3445 11.7252 14.74 10.9002 14.74 10.0002C14.74 8.5002 16.09 7.0752 16.09 7."
    '0752Z" fill="white"/></svg>'
)
_LOGO_DATA_URI = "data:image/svg+xml," + quote(_LOGO_SVG)

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
.wordmark { display: flex; align-items: center; gap: 10px; font-weight: 650; font-size: 20px; letter-spacing: -0.01em; }
.wordmark img { width: 28px; height: 28px; border-radius: 7px; }
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
.tabs { margin: 12px 0 16px; }
.tabs > input { position: absolute; opacity: 0; pointer-events: none; }
.tab-labels { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.tab-labels label { padding: 6px 14px; border: 1px solid var(--line); border-radius: 999px; background: var(--card);
  color: var(--muted); font-size: 14px; font-weight: 600; cursor: pointer; user-select: none; }
.tab-labels label:hover { border-color: var(--brand); }
.panel { display: none; }
.panel .code { margin-bottom: 10px; }
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


def _mcp_servers_json(entry: dict) -> str:
    return json.dumps({"mcpServers": {"rootly": entry}}, indent=2)


def _clients(url: str) -> list[tuple[str, str, str]]:
    """(key, tab label, panel HTML) per client, following docs.rootly.com's setup steps."""
    token_header = {"Authorization": "Bearer YOUR_ROOTLY_API_TOKEN"}
    return [
        (
            "claude-code",
            "Claude Code",
            "<p>Run this. A browser window opens so you can sign in to Rootly.</p>"
            + _code_block("claude-code-oauth", f"claude mcp add --transport http rootly {url}")
            + "<p>Or commit a <code>.mcp.json</code> to your project root:</p>"
            + _code_block("claude-code-json", _mcp_servers_json({"type": "http", "url": url}))
            + "<details><summary>With an API token</summary>"
            + _code_block(
                "claude-code-token",
                f"claude mcp add --transport http rootly {url} \\\n"
                '  --header "Authorization: Bearer YOUR_ROOTLY_API_TOKEN"',
            )
            + "</details>",
        ),
        (
            "cursor",
            "Cursor",
            "<p>Add to <code>.cursor/mcp.json</code> in your project, or <code>~/.cursor/mcp.json</code>"
            " for every project.</p>"
            + _code_block("cursor-oauth", _mcp_servers_json({"url": url}))
            + "<details><summary>With an API token</summary>"
            + _code_block("cursor-token", _mcp_servers_json({"url": url, "headers": token_header}))
            + "</details>",
        ),
        (
            "codex",
            "Codex",
            "<p>Add to <code>~/.codex/config.toml</code>, then export your API key as"
            " <code>ROOTLY_API_TOKEN</code>.</p>"
            + _code_block(
                "codex-toml",
                f'[mcp_servers.rootly]\nurl = "{url}"\nbearer_token_env_var = "ROOTLY_API_TOKEN"',
            ),
        ),
        (
            "claude-desktop",
            "Claude Desktop",
            "<p>Add to <code>claude_desktop_config.json</code>. Claude Desktop opens a browser"
            " window so you can sign in.</p>"
            + _code_block("desktop-oauth", _mcp_servers_json({"url": url}))
            + "<details><summary>With an API token (through mcp-remote)</summary>"
            + _code_block(
                "desktop-token",
                _mcp_servers_json(
                    {
                        "command": "npx",
                        "args": [
                            "-y",
                            "mcp-remote",
                            url,
                            "--transport",
                            "http",
                            "--header",
                            "Authorization: Bearer YOUR_ROOTLY_API_TOKEN",
                        ],
                    }
                ),
            )
            + "</details>",
        ),
        (
            "gemini",
            "Gemini CLI",
            "<p>Install the extension:</p>"
            + _code_block(
                "gemini-ext",
                "gemini extensions install https://github.com/rootlyhq/rootly-mcp-server",
            )
            + "<p>Or run the server locally from <code>~/.gemini/settings.json</code> with an API key:</p>"
            + _code_block(
                "gemini-json",
                _mcp_servers_json(
                    {
                        "command": "uvx",
                        "args": ["--from", "rootly-mcp-server", "rootly-mcp-server"],
                        "env": {"ROOTLY_API_TOKEN": "YOUR_ROOTLY_API_TOKEN"},
                    }
                ),
            ),
        ),
    ]


def _client_tabs(url: str) -> str:
    """CSS-only tabs: a radio per client, so the page needs no script beyond copy buttons."""
    clients = _clients(url)
    radios = "".join(
        f'<input type="radio" name="client" id="tab-{key}"{" checked" if i == 0 else ""}>'
        for i, (key, _, _) in enumerate(clients)
    )
    labels = "".join(f'<label for="tab-{key}">{label}</label>' for key, label, _ in clients)
    panels = "".join(
        f'<div class="panel" id="panel-{key}">{body}</div>' for key, _, body in clients
    )
    rules = "".join(
        f'#tab-{key}:checked ~ .tab-labels label[for="tab-{key}"]'
        "{background:var(--brand);border-color:var(--brand);color:#fff}"
        f'#tab-{key}:focus-visible ~ .tab-labels label[for="tab-{key}"]'
        "{outline:2px solid var(--brand);outline-offset:2px}"
        f"#tab-{key}:checked ~ .panels #panel-{key}{{display:block}}"
        for key, _, _ in clients
    )
    return (
        f'<div class="tabs"><style>{rules}</style>{radios}'
        f'<div class="tab-labels">{labels}</div><div class="panels">{panels}</div></div>'
    )


def render_landing_page(base_url: str, code_mode_path: str) -> str:
    """Render the page for a server reachable at ``base_url``."""
    mcp_url = f"{base_url}/mcp"
    code_mode_url = f"{base_url}{code_mode_path}"
    # Code Mode is the endpoint docs.rootly.com recommends.
    add_command = f"claude mcp add --transport http rootly {code_mode_url}"
    esc = html.escape
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rootly MCP server</title>
<meta name="description" content="Connect Claude, Cursor or any MCP client to Rootly.">
<link rel="icon" type="image/svg+xml" href="{_LOGO_DATA_URI}">
<style>{_STYLE}</style>
</head>
<body>
<div class="bar"></div>
<main>
  <div class="top">
    <div class="wordmark"><img src="{_LOGO_DATA_URI}" alt=""><div>Rootly <span>MCP</span></div></div>
    <div class="pill"><span class="dot"></span>Up and answering</div>
  </div>

  <h1>Bring Rootly into your AI assistant</h1>
  <p class="lede">This is the Rootly MCP server. Point Claude, Cursor or any Model Context
  Protocol (MCP) client at it to search incidents, check who's on-call, find similar past
  incidents and act on them without leaving your editor.</p>

  <div class="terminal" aria-label="Example command">
    <div class="terminal-head"><i></i><i></i><i></i></div>
    <div class="terminal-body">
      <span class="muted">$ </span><span class="typed" style="--chars: {len(add_command)}">{esc(add_command)}</span>
    </div>
  </div>

  <h2>Connect your client</h2>
  {_client_tabs(code_mode_url)}
  <p>Clients that support OAuth open a browser window so you can sign in to Rootly; no API
  token needed. For a token, generate one under <strong>Account › Manage API keys</strong>.
  Prefer a global key: on-call tools such as the handoff summary need organization-wide
  visibility.</p>

  <h2>Endpoints</h2>
  <div class="card">
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(code_mode_url)}</code><span class="tag">Recommended</span></div>
      <p>Code Mode. Your assistant gets five meta-tools instead of 200+ schemas, finds the tools
      it needs, and chains them in one short Python block. Fewer tokens, fewer round trips.</p>
    </div>
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(mcp_url)}</code><span class="tag">Classic</span></div>
      <p>Streamable HTTP with every tool registered directly. Add <code>?tool_profile=slim</code>
      for about 70 of them.</p>
    </div>
    <div class="endpoint">
      <div class="endpoint-head"><code>{esc(base_url)}/sse</code><span class="tag old">Deprecated</span></div>
      <p>Removed after 2026-11-20. Switch to Code Mode or Classic; usually only the URL changes.</p>
    </div>
  </div>
  <p>Connect Code Mode or Classic, not both. With both, assistants tend to call the classic
  tools directly and the full tool list loads anyway.</p>

  <h2>Pair it with the docs</h2>
  <p>The <a href="https://docs.rootly.com/integrations/mcp-server#rootly-docs-mcp-server">Rootly
  Docs MCP server</a> at <code>https://docs.rootly.com/mcp</code> answers "how do I configure
  this?" from current documentation. Connect both and your assistant can look up a setting,
  then change it.</p>

  <h2>Things to ask once you're connected</h2>
  <ul class="asks">
    <li>Who's on-call for the payments team this weekend?</li>
    <li>Find past incidents like this one and what fixed them.</li>
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
