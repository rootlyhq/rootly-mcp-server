"""Integration tests for self-hosted tool allowlists over live MCP transport."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _find_free_port() -> int:
    """Reserve an unused localhost port for a test server process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(base_url: str, timeout: float = 15.0) -> None:
    """Poll the health endpoint until the subprocess server is ready."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise AssertionError(f"Timed out waiting for MCP server health at {base_url}/health")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _server_env(
    *,
    port: int,
    enabled_tools: str,
    enable_write_tools: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ROOTLY_API_TOKEN": "local_test_token_12345",
            "ROOTLY_MCP_ENABLED_TOOLS": enabled_tools,
            "ROOTLY_TRANSPORT": "streamable-http",
            "FASTMCP_PORT": str(port),
        }
    )
    if enable_write_tools:
        env["ROOTLY_MCP_ENABLE_WRITE_TOOLS"] = "true"
    else:
        env.pop("ROOTLY_MCP_ENABLE_WRITE_TOOLS", None)
    return env


def _terminate_process(process: subprocess.Popen[str]) -> str:
    """Terminate a subprocess and collect any remaining stderr for diagnostics."""
    if process.poll() is None:
        process.terminate()
        try:
            _stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=5)
        return stderr
    _stdout, stderr = process.communicate(timeout=5)
    return stderr


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled_tools", "enable_write_tools", "expected_tools"),
    [
        (
            # Snake_case canonical names map to themselves; the surfaced tool list
            # is exactly the canonicalized allowlist. See canonicalize_tool_names().
            "list_incidents,get_incident,list_teams",
            False,
            ["get_incident", "list_incidents", "list_teams"],
        ),
        (
            # Hard cutover back-compat: legacy camelCase entries are canonicalized
            # to their snake_case form, which is what tools/list advertises.
            "listIncidents,getIncident,listTeams",
            False,
            ["get_incident", "list_incidents", "list_teams"],
        ),
        (
            "createIncident,createWorkflowTask,listTeams",
            True,
            ["create_incident", "create_workflow_task", "list_teams"],
        ),
    ],
)
async def test_self_hosted_allowlists_match_live_mcp_tool_list(
    enabled_tools: str,
    enable_write_tools: bool,
    expected_tools: list[str],
) -> None:
    """Boot the server as a subprocess and verify the live tools/list payload."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rootly_mcp_server",
            "--transport",
            "streamable-http",
            "--log-level",
            "ERROR",
        ],
        cwd=_repo_root(),
        env=_server_env(
            port=port,
            enabled_tools=enabled_tools,
            enable_write_tools=enable_write_tools,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        await _wait_for_health(base_url)

        async with streamable_http_client(f"{base_url}/mcp") as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = sorted(tool.name for tool in tools.tools)

        assert tool_names == sorted(expected_tools)
    finally:
        stderr = _terminate_process(process)
        if process.returncode not in (0, -15):
            pytest.fail(f"Server exited unexpectedly with code {process.returncode}:\n{stderr}")
