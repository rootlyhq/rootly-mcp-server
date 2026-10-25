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
from dataclasses import dataclass
from typing import Any
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
a.install { display: inline-block; padding: 8px 16px; border-radius: 8px; background: var(--text); color: var(--bg);
  font-weight: 600; text-decoration: none; }
a.install:hover { opacity: .85; }
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


def _token_details(block_id: str, intro: str, text: str) -> str:
    """An API-token alternative, collapsed: OAuth is the first choice everywhere."""
    return (
        f"<details><summary>With an API token instead</summary><p>{intro}</p>"
        + _code_block(block_id, text)
        + "</details>"
    )


def _cursor_install_link(url: str) -> str:
    """Cursor's MCP install deeplink: name plus the base64 of the mcp.json entry."""
    config = base64.b64encode(json.dumps({"url": url}).encode()).decode()
    return (
        "cursor://anysphere.cursor-deeplink/mcp/install"
        f"?name=rootly&config={quote(config, safe='')}"
    )


def _clients(url: str) -> list[tuple[str, str, str]]:
    """(key, tab label, panel HTML) per client: OAuth first, an API token as the fallback.

    Commands follow each client's own docs: Claude Code, Codex and Gemini CLI
    register a remote server from the CLI and sign in over OAuth; Cursor takes
    an install link; Claude Desktop adds remote servers as custom connectors.
    """
    token_header = {"Authorization": "Bearer YOUR_ROOTLY_API_TOKEN"}
    return [
        (
            "claude-code",
            "Claude Code",
            "<p>Run this. A browser window opens so you can sign in to Rootly.</p>"
            + _code_block("claude-code-oauth", f"claude mcp add --transport http rootly {url}")
            + "<p>Or share it with your team by committing a <code>.mcp.json</code> to the"
            " project root:</p>"
            + _code_block("claude-code-json", _mcp_servers_json({"type": "http", "url": url}))
            + _token_details(
                "claude-code-token",
                "Pass the key as a header:",
                f"claude mcp add --transport http rootly {url} \\\n"
                '  --header "Authorization: Bearer YOUR_ROOTLY_API_TOKEN"',
            ),
        ),
        (
            "cursor",
            "Cursor",
            f'<p><a class="install" href="{html.escape(_cursor_install_link(url))}">Add to Cursor</a></p>'
            "<p>Cursor asks you to confirm, then opens a browser window so you can sign in."
            " Or add it to <code>.cursor/mcp.json</code> in your project (or"
            " <code>~/.cursor/mcp.json</code> for every project):</p>"
            + _code_block("cursor-oauth", _mcp_servers_json({"url": url}))
            + _token_details(
                "cursor-token",
                "Add the key as a header:",
                _mcp_servers_json({"url": url, "headers": token_header}),
            ),
        ),
        (
            "codex",
            "Codex",
            "<p>Register the server, then sign in. <code>login</code> opens a browser window.</p>"
            + _code_block(
                "codex-oauth",
                f"codex mcp add rootly --url {url}\ncodex mcp login rootly",
            )
            + _token_details(
                "codex-token",
                "Add this to <code>~/.codex/config.toml</code> and export the key as"
                " <code>ROOTLY_API_TOKEN</code>:",
                f'[mcp_servers.rootly]\nurl = "{url}"\nbearer_token_env_var = "ROOTLY_API_TOKEN"',
            ),
        ),
        (
            "claude-desktop",
            "Claude Desktop",
            "<p>Open <strong>Customize › Connectors</strong>, click <strong>+ Add</strong>, then"
            " <strong>Add custom connector</strong>. Name it Rootly, paste this URL and choose"
            " <strong>Sign in now</strong>. On Team and Enterprise plans an owner adds it under"
            " Organization settings first.</p>"
            + _code_block("desktop-url", url)
            + _token_details(
                "desktop-token",
                "Run it through <code>mcp-remote</code> from <code>claude_desktop_config.json</code>:",
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
            ),
        ),
        (
            "gemini",
            "Gemini CLI",
            "<p>Register the server:</p>"
            + _code_block("gemini-oauth", f"gemini mcp add --transport http rootly {url}")
            + "<p>Then, inside Gemini CLI, sign in. A browser window opens.</p>"
            + _code_block("gemini-auth", "/mcp auth rootly")
            + _token_details(
                "gemini-token",
                "Install the Rootly extension, which runs the server locally and asks for your key:",
                "gemini extensions install https://github.com/rootlyhq/rootly-mcp-server",
            ),
        ),
    ]


def _client_tabs(clients: list[tuple[str, str, str]]) -> tuple[str, str]:
    """CSS-only tabs: a radio per client, so the page needs no script beyond copy buttons.

    Returns the markup and the per-tab CSS rules, which belong in <head>.
    """
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
    markup = (
        f'<div class="tabs">{radios}'
        f'<div class="tab-labels">{labels}</div><div class="panels">{panels}</div></div>'
    )
    return markup, rules


@dataclass(frozen=True)
class McpEndpoints:
    """The MCP endpoints a running app actually serves, as paths (None when absent)."""

    code_mode: str | None = None
    streamable: str | None = None
    sse: str | None = None

    @classmethod
    def from_app_state(cls, state: Any) -> McpEndpoints:
        """Read what the serving app mounted.

        The hosted app builders in ``__main__`` record ``mcp_endpoints``. Any other
        app (fastmcp's own) records a single ``path`` and its ``transport_type``.
        """
        recorded = getattr(state, "mcp_endpoints", None)
        if isinstance(recorded, McpEndpoints):
            return recorded
        path = getattr(state, "path", None)
        if not isinstance(path, str) or not path:
            return cls()
        if getattr(state, "transport_type", "") == "sse":
            return cls(sse=path)
        return cls(streamable=path)


# The hosted production layout, for rendering the page outside a running app.
HOSTED_ENDPOINTS = McpEndpoints(code_mode="/mcp-codemode", streamable="/mcp", sse="/sse")


def _endpoint(url: str, tag: str, description: str, *, old: bool = False) -> str:
    tag_class = "tag old" if old else "tag"
    return (
        '<div class="endpoint"><div class="endpoint-head">'
        f'<code>{html.escape(url)}</code><span class="{tag_class}">{tag}</span></div>'
        f"<p>{description}</p></div>"
    )


def render_landing_page(base_url: str, endpoints: McpEndpoints) -> str:
    """Render the page for a server at ``base_url`` serving ``endpoints``.

    Only endpoints the server mounts are shown, and setup points at the best one
    it has: Code Mode (what docs.rootly.com recommends), else streamable HTTP.
    """
    code_mode_url = f"{base_url}{endpoints.code_mode}" if endpoints.code_mode else None
    streamable_url = f"{base_url}{endpoints.streamable}" if endpoints.streamable else None
    setup_url = code_mode_url or streamable_url
    esc = html.escape

    rows = []
    if code_mode_url:
        rows.append(
            _endpoint(
                code_mode_url,
                "Recommended",
                "Code Mode. Your assistant gets five meta-tools instead of 200+ schemas, finds the"
                " tools it needs, and chains them in one short Python block. Fewer tokens, fewer"
                " round trips.",
            )
        )
    if streamable_url:
        rows.append(
            _endpoint(
                streamable_url,
                "Classic" if code_mode_url else "Recommended",
                "Streamable HTTP with every tool registered directly. Add"
                " <code>?tool_profile=slim</code> for about 70 of them.",
            )
        )
    if endpoints.sse:
        rows.append(
            _endpoint(
                f"{base_url}{endpoints.sse}",
                "Deprecated",
                "Removed after 2026-11-20."
                + (
                    " Switch to the endpoint above; usually only the URL changes."
                    if setup_url
                    else ""
                ),
                old=True,
            )
        )
    both_note = (
        "<p>Connect Code Mode or Classic, not both. With both, assistants tend to call the"
        " classic tools directly and the full tool list loads anyway.</p>"
        if code_mode_url and streamable_url
        else ""
    )

    if setup_url:
        add_command = f"claude mcp add --transport http rootly {setup_url}"
        tabs, tab_rules = _client_tabs(_clients(setup_url))
        terminal = (
            '<div class="terminal" aria-hidden="true"><div class="terminal-head"><i></i><i></i><i></i></div>'
            '<div class="terminal-body"><span class="muted">$ </span>'
            f'<span class="typed" style="--chars: {len(add_command)}">{esc(add_command)}</span></div></div>'
        )
        connect = (
            f"<h2>Connect your client</h2>{tabs}"
            "<p>Each option signs you in with OAuth, so there's no key to store or rotate. If you"
            " use an API token instead, generate it under <strong>Account › Manage API keys</strong>"
            " and prefer a global key: on-call tools such as the handoff summary need"
            " organization-wide visibility.</p>"
        )
    else:
        tab_rules = terminal = ""
        connect = (
            "<h2>Connect your client</h2><p>This server only serves the deprecated SSE"
            " transport. Point your client at the SSE endpoint below, and plan to move to"
            " streamable HTTP before 2026-11-20.</p>"
        )
    endpoints_section = (
        f'<h2>Endpoints</h2><div class="card">{"".join(rows)}</div>{both_note}' if rows else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rootly MCP server</title>
<meta name="description" content="Connect Claude, Cursor or any MCP client to Rootly.">
<link rel="icon" type="image/svg+xml" href="{_LOGO_DATA_URI}">
<style>{_STYLE}{tab_rules}</style>
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

  {terminal}

  {connect}

  {endpoints_section}

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
    """GET / handler: the setup page for the endpoints this app serves, on this host."""
    base_url = resolve_mcp_server_url(request).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        # The scheme can come from X-Forwarded-Proto; show only web URLs.
        base_url = "https://" + base_url.split("://", 1)[-1]
    page = render_landing_page(base_url, McpEndpoints.from_app_state(request.app.state))
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
