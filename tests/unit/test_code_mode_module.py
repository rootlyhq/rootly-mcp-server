"""Tests for Rootly Code Mode helpers."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.tools.function_tool import FunctionTool

from rootly_mcp_server.code_mode import (
    DEFAULT_CODE_MODE_PATH,
    CompatibleMontySandboxProvider,
    _format_execute_exception,
    _normalize_execute_tool_name,
    build_code_mode_transform,
    code_mode_enabled_from_env,
    code_mode_path_from_env,
    create_rootly_codemode_server,
)


def test_code_mode_enabled_from_env_defaults_true():
    with patch.dict("os.environ", {}, clear=True):
        assert code_mode_enabled_from_env() is True


def test_code_mode_enabled_from_env_accepts_truthy_values():
    with patch.dict("os.environ", {"ROOTLY_CODE_MODE_ENABLED": "true"}, clear=True):
        assert code_mode_enabled_from_env() is True


def test_code_mode_enabled_from_env_accepts_false_override():
    with patch.dict("os.environ", {"ROOTLY_CODE_MODE_ENABLED": "false"}, clear=True):
        assert code_mode_enabled_from_env() is False


def test_code_mode_path_from_env_uses_default_and_normalizes():
    with patch.dict("os.environ", {}, clear=True):
        assert code_mode_path_from_env() == DEFAULT_CODE_MODE_PATH

    with patch.dict("os.environ", {"ROOTLY_CODE_MODE_PATH": "custom-codemode/"}, clear=True):
        assert code_mode_path_from_env() == "/custom-codemode"


def test_build_code_mode_transform_uses_expected_discovery_tools():
    transform = build_code_mode_transform()

    assert isinstance(transform, CodeMode)
    assert isinstance(transform.sandbox_provider, CompatibleMontySandboxProvider)
    discovery_names = [tool.name for tool in transform._build_discovery_tools()]  # noqa: SLF001
    assert discovery_names == ["list_tools", "tool_search", "get_schema", "tags"]


def test_build_code_mode_transform_includes_pagination_guidance():
    transform = build_code_mode_transform()
    assert transform.execute_description is not None

    assert "tool_search only to discover tools" in transform.execute_description
    assert "page_size, page_number, and max_results" in transform.execute_description
    assert "per_page" in transform.execute_description
    assert "mcp__rootly-codemode__tool_search" in transform.execute_description
    assert "rootly:get_current_user" in transform.execute_description
    assert "Avoid imports such as json or asyncio" in transform.execute_description
    assert "await call_tool('search_incidents'" in transform.execute_description


def test_create_rootly_codemode_server_adds_code_mode_transform():
    mock_transform_server = type(
        "TransformServer",
        (),
        {
            "add_transform": lambda self, transform: setattr(self, "_transform", transform),
        },
    )()

    with patch(
        "rootly_mcp_server.code_mode.create_rootly_mcp_server", return_value=mock_transform_server
    ) as mock_create:
        server = create_rootly_codemode_server(
            swagger_path="swagger.json",
            name="Rootly Code Mode",
            allowed_paths=["/incidents"],
            hosted=True,
            base_url="https://api.rootly.com",
            enable_write_tools=True,
            enabled_tools={"list_incidents", "getIncident"},
        )

    assert server is mock_transform_server
    assert isinstance(server._transform, CodeMode)  # type: ignore[attr-defined]  # noqa: SLF001
    mock_create.assert_called_once_with(
        swagger_path="swagger.json",
        name="Rootly Code Mode",
        allowed_paths=["/incidents"],
        hosted=True,
        base_url="https://api.rootly.com",
        transport="streamable-http",
        enable_write_tools=True,
        enabled_tools={"list_incidents", "getIncident"},
    )


@pytest.mark.asyncio
async def test_compatible_monty_provider_falls_back_for_legacy_constructor():
    class LegacyMonty:
        def __init__(self, code, *, inputs=None):
            self.code = code
            self.inputs = inputs

    captured: dict[str, Any] = {}

    async def fake_run_monty_async(monty_runner, **kwargs):
        captured["monty_runner"] = monty_runner
        captured["kwargs"] = kwargs
        return "ok"

    fake_module = SimpleNamespace(Monty=LegacyMonty, run_monty_async=fake_run_monty_async)

    provider = CompatibleMontySandboxProvider()

    async def fake_call_tool():
        return "done"

    with patch("rootly_mcp_server.code_mode.importlib.import_module", return_value=fake_module):
        result = await provider.run(
            "return await call_tool()",
            inputs={"incident_id": "123"},
            external_functions={"call_tool": fake_call_tool},
        )

    assert result == "ok"
    monty_runner = captured["monty_runner"]
    assert isinstance(monty_runner, LegacyMonty)
    assert monty_runner.inputs == ["incident_id"]
    kwargs = captured["kwargs"]
    assert kwargs["inputs"] == {"incident_id": "123"}
    assert list(kwargs["external_functions"]) == ["call_tool"]


@pytest.mark.asyncio
async def test_compatible_monty_provider_uses_modern_constructor_when_supported():
    class ModernMonty:
        def __init__(self, code, *, inputs=None, external_functions=None):
            self.code = code
            self.inputs = inputs
            self.external_functions = external_functions

    captured: dict[str, Any] = {}

    async def fake_run_monty_async(monty_runner, **kwargs):
        captured["monty_runner"] = monty_runner
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    fake_module = SimpleNamespace(Monty=ModernMonty, run_monty_async=fake_run_monty_async)
    provider = CompatibleMontySandboxProvider()

    def fake_call_tool():
        return "done"

    with patch("rootly_mcp_server.code_mode.importlib.import_module", return_value=fake_module):
        result = await provider.run(
            "return call_tool()",
            external_functions={"call_tool": fake_call_tool},
        )

    assert result == {"status": "ok"}
    monty_runner = captured["monty_runner"]
    assert isinstance(monty_runner, ModernMonty)
    assert monty_runner.external_functions == ["call_tool"]


def test_normalize_execute_tool_name_handles_prefixes_and_aliases():
    assert _normalize_execute_tool_name("mcp__rootly-codemode__tool_search") == "tool_search"
    assert _normalize_execute_tool_name("rootly:getCurrentUser") == "getCurrentUser"
    assert _normalize_execute_tool_name("search") == "tool_search"


def test_format_execute_exception_returns_friendlier_messages():
    unknown_tool = _format_execute_exception(
        Exception("Unknown tool: mcp__rootly-codemode__tool_search")
    )
    assert unknown_tool is not None
    assert "Use tool_search to discover available tools" in unknown_tool

    missing_import = _format_execute_exception(
        Exception("ModuleNotFoundError: No module named 'json'")
    )
    assert missing_import is not None
    assert "restricted sandbox" in missing_import

    asyncio_sleep = _format_execute_exception(
        Exception("AttributeError: module 'asyncio' has no attribute 'sleep'")
    )
    assert asyncio_sleep is not None
    assert "does not provide `asyncio.sleep()`" in asyncio_sleep

    parser_error = _format_execute_exception(Exception("Expected name, got Subscript(...)"))
    assert parser_error is not None
    assert "restricted Python subset" in parser_error


@pytest.mark.asyncio
async def test_execute_normalizes_namespaced_discovery_tool_calls():
    class FakeSandboxProvider:
        async def run(self, code, *, inputs=None, external_functions=None):
            assert external_functions is not None
            return await external_functions["call_tool"](
                "mcp__rootly-codemode__tool_search",
                {"query": "alerts"},
            )

    transform = build_code_mode_transform()
    transform.sandbox_provider = FakeSandboxProvider()
    execute_tool = cast(FunctionTool, transform._get_execute_tool())  # noqa: SLF001

    fake_fastmcp = SimpleNamespace(
        list_tools=AsyncMock(return_value=[]),
        call_tool=AsyncMock(
            return_value=SimpleNamespace(structured_content={"ok": True}, content=[])
        ),
    )
    ctx = SimpleNamespace(fastmcp=fake_fastmcp)

    result = await execute_tool.fn("return 1", ctx=ctx)

    assert result == {"ok": True}
    fake_fastmcp.call_tool.assert_awaited_once_with("tool_search", {"query": "alerts"})


@pytest.mark.asyncio
async def test_execute_normalizes_namespaced_backend_tool_calls():
    class FakeSandboxProvider:
        async def run(self, code, *, inputs=None, external_functions=None):
            assert external_functions is not None
            return await external_functions["call_tool"]("rootly:getCurrentUser", {})

    transform = build_code_mode_transform()
    transform.sandbox_provider = FakeSandboxProvider()
    execute_tool = cast(FunctionTool, transform._get_execute_tool())  # noqa: SLF001

    backend_tools = [SimpleNamespace(name="getCurrentUser", version=None)]
    fake_fastmcp = SimpleNamespace(
        list_tools=AsyncMock(return_value=backend_tools),
        call_tool=AsyncMock(
            return_value=SimpleNamespace(structured_content={"id": "u_1"}, content=[])
        ),
    )
    ctx = SimpleNamespace(fastmcp=fake_fastmcp)

    result = await execute_tool.fn("return 1", ctx=ctx)

    assert result == {"id": "u_1"}
    fake_fastmcp.call_tool.assert_awaited_once_with("getCurrentUser", {})


@pytest.mark.asyncio
async def test_execute_returns_friendlier_unknown_tool_errors():
    class FakeSandboxProvider:
        async def run(self, code, *, inputs=None, external_functions=None):
            assert external_functions is not None
            return await external_functions["call_tool"](
                "mcp__rootly-codemode__missing_tool",
                {},
            )

    transform = build_code_mode_transform()
    transform.sandbox_provider = FakeSandboxProvider()
    execute_tool = cast(FunctionTool, transform._get_execute_tool())  # noqa: SLF001

    fake_fastmcp = SimpleNamespace(
        list_tools=AsyncMock(return_value=[]),
        call_tool=AsyncMock(),
    )
    ctx = SimpleNamespace(fastmcp=fake_fastmcp)

    with pytest.raises(ValueError, match="Use tool_search to discover available tools"):
        await execute_tool.fn("return 1", ctx=ctx)

    fake_fastmcp.call_tool.assert_not_awaited()
