"""Code Mode helpers for exposing a third MCP endpoint."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import NotFoundError
from fastmcp.experimental.transforms.code_mode import (
    CodeMode,
    GetSchemas,
    GetTags,
    ListTools,
    MontySandboxProvider,
    Search,
    _ensure_async,
    _unwrap_tool_result,
)
from fastmcp.server.context import Context
from fastmcp.tools import Tool
from pydantic import Field

from .server import create_rootly_mcp_server

if TYPE_CHECKING:
    from fastmcp import FastMCP


DEFAULT_CODE_MODE_PATH = "/mcp-codemode"
_TOOL_NAME_PREFIXES = (
    "mcp__rootly-codemode__",
    "mcp__rootly__",
    "rootly-codemode:",
    "rootly:",
)
_TOOL_NAME_ALIASES = {
    "search": "tool_search",
}


def _normalize_http_path(path: str) -> str:
    """Normalize hosted HTTP path values for reliable comparisons."""
    if not path:
        return "/"
    normalized = path if path.startswith("/") else f"/{path}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def normalize_code_mode_path(path: str) -> str:
    """Normalize a hosted Code Mode path value."""
    return _normalize_http_path(path)


def code_mode_enabled_from_env(default: bool = True) -> bool:
    """Return whether hosted Code Mode exposure is enabled.

    Code Mode defaults on for hosted dual-transport deployments unless explicitly disabled.
    """
    raw = os.getenv("ROOTLY_CODE_MODE_ENABLED")
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def code_mode_path_from_env() -> str:
    """Return the configured hosted Code Mode path."""
    return normalize_code_mode_path(os.getenv("ROOTLY_CODE_MODE_PATH", DEFAULT_CODE_MODE_PATH))


def _normalize_execute_tool_name(tool_name: str) -> str:
    """Normalize common client-specific Code Mode tool name variants."""
    normalized = (tool_name or "").strip()
    for prefix in _TOOL_NAME_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return _TOOL_NAME_ALIASES.get(normalized, normalized)


def _format_execute_exception(exc: Exception) -> str | None:
    """Return a friendlier execute error message for known sandbox failures."""
    message = str(exc).strip()
    if not message:
        return None

    if isinstance(exc, NotFoundError) or "Unknown tool:" in message:
        return (
            f"{message}. Use tool_search to discover available tools and call them "
            "without client prefixes like 'mcp__rootly-codemode__' or 'rootly:'."
        )

    if isinstance(exc, ModuleNotFoundError) or "No module named" in message:
        return (
            f"{message}. Code Mode runs in a restricted sandbox, so imports like "
            "`json` or `asyncio` are not available. Return native Python dict/list/str "
            "values and use `await call_tool(name, params)` for Rootly operations."
        )

    if (
        isinstance(exc, AttributeError) and "asyncio" in message and "sleep" in message
    ) or "asyncio' has no attribute 'sleep'" in message:
        return (
            f"{message}. Code Mode does not provide `asyncio.sleep()`. Avoid manual "
            "sleep/retry loops inside execute; call Rootly tools directly and return "
            "the final result."
        )

    if message.startswith("Expected ") or "got Subscript(" in message:
        return (
            f"{message}. Code Mode supports a restricted Python subset. Keep the block "
            "simple: assign intermediate values to variables, call `await call_tool(...)`, "
            "and `return` the final value."
        )

    if isinstance(exc, TypeError) and "NoneType" in message and "subscriptable" in message:
        return (
            f"{message}. Code Mode tried to read a field from a missing result. Check "
            "that each `call_tool(...)` response contains the keys you expect before "
            "indexing into it."
        )

    return None


class CompatibleMontySandboxProvider(MontySandboxProvider):
    """Monty sandbox provider that tolerates older constructor signatures.

    Some deployed environments can end up with a Monty runtime that supports
    ``run_monty_async(..., external_functions=...)`` but still rejects the
    newer ``Monty(..., external_functions=[...])`` constructor argument. This
    provider falls back to the older constructor form so Code Mode execution
    continues to work during mixed-version rollouts.
    """

    @staticmethod
    def _build_monty_runner(
        pydantic_monty: Any,
        code: str,
        *,
        input_names: list[str],
        external_function_names: list[str],
    ) -> Any:
        try:
            return pydantic_monty.Monty(
                code,
                inputs=input_names,
                external_functions=external_function_names,
            )
        except TypeError as exc:
            if "external_functions" not in str(exc):
                raise
            return pydantic_monty.Monty(code, inputs=input_names)

    async def run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_functions: dict[str, Callable[..., Any]] | None = None,
    ) -> Any:
        try:
            pydantic_monty = importlib.import_module("pydantic_monty")
        except ModuleNotFoundError as exc:
            raise ImportError(
                "CodeMode requires pydantic-monty for the Monty sandbox provider. "
                "Install it with `fastmcp[code-mode]` or pass a custom SandboxProvider."
            ) from exc

        inputs = inputs or {}
        async_functions = {
            key: _ensure_async(value) for key, value in (external_functions or {}).items()
        }

        monty = self._build_monty_runner(
            pydantic_monty,
            code,
            input_names=list(inputs.keys()),
            external_function_names=list(async_functions.keys()),
        )
        run_kwargs: dict[str, Any] = {"external_functions": async_functions}
        if inputs:
            run_kwargs["inputs"] = inputs
        if self.limits is not None:
            run_kwargs["limits"] = self.limits
        return await pydantic_monty.run_monty_async(monty, **run_kwargs)


class RootlyCodeMode(CodeMode):
    """Rootly-specific Code Mode transform with friendlier execute ergonomics."""

    def _make_execute_tool(self) -> Tool:
        transform = self

        async def execute(
            code: Annotated[
                str,
                Field(
                    description=(
                        "Python async code to execute tool calls via call_tool(name, arguments)"
                    )
                ),
            ],
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            """Execute tool calls using Python code."""
            cached_tools: list[Tool] | None = None

            async def _get_cached_tools() -> list[Tool]:
                nonlocal cached_tools
                if cached_tools is None:
                    cached_tools = list(await transform.get_tool_catalog(ctx))
                return cached_tools

            async def call_tool(tool_name: str, params: dict[str, Any]) -> Any:
                resolved_name = _normalize_execute_tool_name(tool_name)
                backend_tools = await _get_cached_tools()
                tool = transform._find_tool(
                    resolved_name, transform._build_discovery_tools()
                ) or transform._find_tool(resolved_name, backend_tools)
                if tool is None:
                    raise NotFoundError(f"Unknown tool: {tool_name}")

                result = await ctx.fastmcp.call_tool(tool.name, params)
                return _unwrap_tool_result(result)

            try:
                return await transform.sandbox_provider.run(
                    code,
                    external_functions={"call_tool": call_tool},
                )
            except Exception as exc:
                friendly_message = _format_execute_exception(exc)
                if friendly_message is None:
                    raise
                raise ValueError(friendly_message) from exc

        return Tool.from_function(
            fn=execute,
            name=self.execute_tool_name,
            description=self._build_execute_description(),
        )


def build_code_mode_transform() -> CodeMode:
    """Build the shared Code Mode transform used by hosted deployments."""
    return RootlyCodeMode(
        sandbox_provider=CompatibleMontySandboxProvider(),
        discovery_tools=[
            ListTools(default_detail="brief"),
            Search(name="tool_search", default_detail="detailed", default_limit=12),
            GetSchemas(default_detail="detailed"),
            GetTags(default_detail="brief"),
        ],
        execute_description=(
            "Write a short async Python block and chain await call_tool(name, params) calls "
            "to complete a Rootly workflow. Use tool_search only to discover tools and their "
            "schemas, then call the actual Rootly tool with the exact parameter names shown in "
            "the schema. For paginated data tools, pass pagination arguments like page_size, "
            "page_number, and max_results exactly as documented by that tool instead of inventing "
            "alternatives like per_page. Prefer Rootly's higher-level custom tools when they fit "
            "the task, then fall back to lower-level API tools as needed. Use list_incidents for "
            "structured incident queries with filters like teams, team_ids, start_time, end_time, "
            "severity, or status; use search_incidents for lightweight free-text lookups. Do not use client "
            "prefixes like mcp__rootly-codemode__tool_search or rootly:get_current_user inside "
            "execute; call tool_search or the raw Rootly tool name directly. Avoid imports "
            "such as json or asyncio inside the sandbox and return native Python values instead. Example: "
            "await call_tool('search_incidents', {'query': '', 'page_size': 1, 'page_number': 1, "
            "'max_results': 1}). Use return to emit the final result."
        ),
    )


def create_rootly_codemode_server(
    swagger_path: str | None = None,
    name: str = "Rootly Code Mode",
    allowed_paths: list[str] | None = None,
    hosted: bool = False,
    base_url: str | None = None,
    enable_write_tools: bool | None = None,
    enabled_tools: set[str] | None = None,
) -> FastMCP:
    """Create a Rootly MCP server instance wrapped with Code Mode."""
    mcp: FastMCP = create_rootly_mcp_server(
        swagger_path=swagger_path,
        name=name,
        allowed_paths=allowed_paths,
        hosted=hosted,
        base_url=base_url,
        transport="streamable-http",
        enable_write_tools=enable_write_tools,
        enabled_tools=enabled_tools,
    )
    mcp.add_transform(build_code_mode_transform())
    return mcp
