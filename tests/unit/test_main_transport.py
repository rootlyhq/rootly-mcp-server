"""Tests for CLI transport normalization and config propagation in __main__."""

import argparse
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.requests import Request

from rootly_mcp_server.__main__ import (
    _get_sorted_tool_names,
    build_mcpcat_identify_callback,
    get_server,
    main,
    maybe_enable_mcpcat_tracking,
    normalize_transport,
    resolve_requested_hosted_tool_profile,
    run_profiled_streamable_http_server,
    streamable_http_stateless_enabled,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("stdio", "stdio"),
        ("sse", "sse"),
        ("streamable-http", "streamable-http"),
        ("streamable", "streamable-http"),
        ("http", "streamable-http"),
        ("both", "both"),
        ("dual", "both"),
        ("dual-http", "both"),
        ("streamable+sse", "both"),
        ("sse+streamable", "both"),
    ],
)
def test_normalize_transport_supported_aliases(value: str, expected: str):
    assert normalize_transport(value) == expected


def test_normalize_transport_rejects_invalid_value():
    with pytest.raises(argparse.ArgumentTypeError):
        normalize_transport("invalid-transport")


def test_get_server_passes_write_tool_env_flag():
    with patch.dict(
        "os.environ",
        {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": "true"},
        clear=True,
    ):
        with patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create:
            get_server()

    assert mock_create.call_args is not None
    assert mock_create.call_args.kwargs["enable_write_tools"] is True


def test_get_server_passes_enabled_tools_env_flag():
    with patch.dict(
        "os.environ",
        {"ROOTLY_MCP_ENABLED_TOOLS": "list_incidents,getIncident"},
        clear=True,
    ):
        with patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create:
            get_server()

    assert mock_create.call_args is not None
    assert mock_create.call_args.kwargs["enabled_tools"] == {"list_incidents", "getIncident"}


def test_get_server_defaults_self_hosted_to_all_tools():
    with patch.dict("os.environ", {}, clear=True):
        with patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create:
            get_server()

    assert mock_create.call_args is not None
    assert mock_create.call_args.kwargs["hosted"] is False
    assert mock_create.call_args.kwargs["enable_write_tools"] is True


def test_get_server_keeps_hosted_default_write_surface():
    with patch.dict("os.environ", {"ROOTLY_HOSTED": "true"}, clear=True):
        with patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create:
            get_server()

    assert mock_create.call_args is not None
    assert mock_create.call_args.kwargs["hosted"] is True
    assert mock_create.call_args.kwargs["enable_write_tools"] is True
    assert mock_create.call_args.kwargs["enabled_tools"] is None


def test_get_server_applies_slim_hosted_profile_from_env():
    with patch.dict(
        "os.environ",
        {"ROOTLY_HOSTED": "true", "ROOTLY_MCP_HOSTED_TOOL_PROFILE": "slim"},
        clear=True,
    ):
        with patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create:
            get_server()

    assert mock_create.call_args is not None
    assert mock_create.call_args.kwargs["enabled_tools"] is not None


def test_resolve_requested_hosted_tool_profile_prefers_query_param():
    profile = resolve_requested_hosted_tool_profile(
        query_params={"tool_profile": "slim"},
        headers={"x-rootly-tool-profile": "full"},
    )

    assert profile == "slim"


def test_resolve_requested_hosted_tool_profile_uses_header_fallback():
    profile = resolve_requested_hosted_tool_profile(
        query_params={},
        headers={"x-rootly-tool-profile": "all"},
    )

    assert profile == "full"


def test_resolve_requested_hosted_tool_profile_falls_back_to_default_on_unknown_value():
    profile = resolve_requested_hosted_tool_profile(
        query_params={"tool_profile": "unexpected"},
        headers={},
        default="slim",
    )

    assert profile == "slim"


def test_streamable_http_defaults_hosted_mode_to_stateless_when_unset():
    with patch.dict("os.environ", {}, clear=True):
        assert streamable_http_stateless_enabled(hosted=True, fastmcp_stateless_http=False) is True
        assert (
            streamable_http_stateless_enabled(hosted=False, fastmcp_stateless_http=False) is False
        )


def test_streamable_http_respects_explicit_fastmcp_setting():
    with patch.dict("os.environ", {"FASTMCP_STATELESS_HTTP": "false"}, clear=True):
        assert streamable_http_stateless_enabled(hosted=True, fastmcp_stateless_http=False) is False

    with patch.dict("os.environ", {"FASTMCP_STATELESS_HTTP": "true"}, clear=True):
        assert streamable_http_stateless_enabled(hosted=False, fastmcp_stateless_http=True) is True


def test_maybe_enable_mcpcat_tracking_is_noop_without_project_id():
    server = object()
    logger = Mock()

    with patch("rootly_mcp_server.__main__.importlib.import_module") as mock_import:
        maybe_enable_mcpcat_tracking(server, None, logger)

    mock_import.assert_not_called()


def test_maybe_enable_mcpcat_tracking_logs_when_package_missing():
    server = object()
    logger = Mock()

    with patch(
        "rootly_mcp_server.__main__.importlib.import_module",
        side_effect=ImportError,
    ) as mock_import:
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    mock_import.assert_called_once_with("mcpcat")
    logger.warning.assert_called_once()


def test_maybe_enable_mcpcat_tracking_tracks_when_available():
    server = object()
    logger = Mock()
    mcpcat_module = SimpleNamespace(track=Mock())
    mcpcat_types_module = SimpleNamespace(
        MCPCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "mcpcat":
            return mcpcat_module
        if module_name == "mcpcat.types":
            return mcpcat_types_module
        raise ImportError(module_name)

    with patch(
        "rootly_mcp_server.__main__.importlib.import_module", side_effect=import_side_effect
    ):
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    mcpcat_module.track.assert_called_once()
    call = mcpcat_module.track.call_args
    assert call.args[:2] == (server, "proj_test_123")
    assert callable(call.args[2].identify)


def test_maybe_enable_mcpcat_tracking_logs_when_track_raises():
    server = object()
    logger = Mock()
    mcpcat_module = SimpleNamespace(track=Mock(side_effect=RuntimeError("boom")))
    mcpcat_types_module = SimpleNamespace(
        MCPCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "mcpcat":
            return mcpcat_module
        if module_name == "mcpcat.types":
            return mcpcat_types_module
        raise ImportError(module_name)

    with patch(
        "rootly_mcp_server.__main__.importlib.import_module", side_effect=import_side_effect
    ):
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    assert mcpcat_module.track.call_args.args[:2] == (server, "proj_test_123")
    logger.warning.assert_called_once_with(
        "MCPcat tracking could not be enabled; skipping",
        exc_info=True,
    )


def test_build_mcpcat_identify_callback_returns_authenticated_user_identity():
    user_identity_cls = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    callback = build_mcpcat_identify_callback(user_identity_cls)

    with patch(
        "rootly_mcp_server.__main__.get_hosted_authenticated_user",
        return_value={
            "id": "user_123",
            "email": "example.user@example.test",
            "name": "Example User",
            "full_name_with_team": "[Acme Reliability] Example User",
        },
    ):
        identity = callback({}, SimpleNamespace())

    assert identity.user_id == "user_123"
    assert identity.user_name == "[Acme Reliability] Example User"
    assert identity.user_data is None


def test_build_mcpcat_identify_callback_returns_none_without_authenticated_user():
    callback = build_mcpcat_identify_callback(SimpleNamespace)

    with patch("rootly_mcp_server.__main__.get_hosted_authenticated_user", return_value=None):
        identity = callback({}, SimpleNamespace())

    assert identity is None


@pytest.mark.asyncio
async def test_get_sorted_tool_names_returns_sorted_names():
    server = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=[
                SimpleNamespace(name="getIncident"),
                SimpleNamespace(name="createIncident"),
                SimpleNamespace(name="listTeams"),
            ]
        )
    )

    names = await _get_sorted_tool_names(server)

    assert names == ["createIncident", "getIncident", "listTeams"]


def test_main_list_tools_prints_effective_tool_names_and_exits(capsys):
    args = SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="stdio",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=False,
        enable_code_mode=False,
        enable_write_tools=False,
        enabled_tools=None,
        list_tools=True,
        code_mode_path=None,
        host=False,
    )
    fake_server = object()

    def fake_asyncio_run(coro):
        coro.close()
        return ["get_server_version", "list_incidents"]

    with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
        with patch("rootly_mcp_server.__main__.setup_logging"):
            with patch("rootly_mcp_server.__main__.check_api_token"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server", return_value=fake_server
                ):
                    with patch(
                        "rootly_mcp_server.__main__.asyncio.run",
                        side_effect=fake_asyncio_run,
                    ) as mock_run:
                        main()

    assert mock_run.call_count == 1
    assert capsys.readouterr().out.splitlines() == ["get_server_version", "list_incidents"]


def test_main_hosted_streamable_http_passes_stateless_default():
    args = SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="streamable-http",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=True,
        enable_code_mode=False,
        enable_write_tools=True,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    main_server = SimpleNamespace()
    slim_server = SimpleNamespace()

    with patch.dict("os.environ", {}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    side_effect=[main_server, slim_server],
                ):
                    with patch(
                        "rootly_mcp_server.__main__.get_hosted_auth_middleware", return_value=[]
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.run_profiled_streamable_http_server"
                        ) as mock_run:
                            main()

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["server"] is main_server
    assert mock_run.call_args.kwargs["profiled_servers"] == {
        "full": main_server,
        "slim": slim_server,
    }
    assert mock_run.call_args.kwargs["default_tool_profile"] == "full"


def test_main_hosted_streamable_http_uses_slim_as_default_when_requested_by_env():
    args = SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="streamable-http",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=True,
        enable_code_mode=False,
        enable_write_tools=True,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    slim_server = SimpleNamespace()
    full_server = SimpleNamespace()

    with patch.dict("os.environ", {"ROOTLY_MCP_HOSTED_TOOL_PROFILE": "slim"}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    side_effect=[slim_server, full_server],
                ):
                    with patch(
                        "rootly_mcp_server.__main__.get_hosted_auth_middleware", return_value=[]
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.run_profiled_streamable_http_server"
                        ) as mock_run:
                            main()

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["server"] is slim_server
    assert mock_run.call_args.kwargs["profiled_servers"] == {
        "slim": slim_server,
        "full": full_server,
    }
    assert mock_run.call_args.kwargs["default_tool_profile"] == "slim"


def test_main_hosted_streamable_http_with_explicit_enabled_tools_skips_profiled_servers():
    args = SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="streamable-http",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=True,
        enable_code_mode=False,
        enable_write_tools=True,
        enabled_tools="listTeams",
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    main_server = SimpleNamespace(run=Mock())

    with patch.dict("os.environ", {}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=main_server,
                ) as mock_create:
                    with patch(
                        "rootly_mcp_server.__main__.get_hosted_auth_middleware", return_value=[]
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.run_profiled_streamable_http_server"
                        ) as mock_profiled_run:
                            main()

    mock_create.assert_called_once()
    mock_profiled_run.assert_not_called()
    main_server.run.assert_called_once()
    assert main_server.run.call_args.kwargs["transport"] == "streamable-http"
    assert main_server.run.call_args.kwargs["stateless_http"] is True
    assert main_server.run.call_args.kwargs["middleware"] == []


def test_main_tracks_main_and_code_mode_servers_when_mcpcat_project_id_set():
    args = SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="both",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=True,
        enable_code_mode=True,
        enable_write_tools=True,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    main_server = SimpleNamespace()
    slim_server = SimpleNamespace()
    code_mode_server = SimpleNamespace()
    slim_code_mode_server = SimpleNamespace()

    with patch.dict("os.environ", {"ROOTLY_MCPCAT_PROJECT_ID": "proj_test_123"}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    side_effect=[main_server, slim_server],
                ):
                    with patch(
                        "rootly_mcp_server.__main__.create_rootly_codemode_server",
                        side_effect=[code_mode_server, slim_code_mode_server],
                    ):
                        with patch("rootly_mcp_server.__main__.run_dual_http_server"):
                            with patch(
                                "rootly_mcp_server.__main__.maybe_enable_mcpcat_tracking"
                            ) as mock_track:
                                main()

    assert len(mock_track.call_args_list) == 4
    assert mock_track.call_args_list[0].args[:2] == (main_server, "proj_test_123")
    assert mock_track.call_args_list[1].args[:2] == (slim_server, "proj_test_123")
    assert mock_track.call_args_list[2].args[:2] == (code_mode_server, "proj_test_123")
    assert mock_track.call_args_list[3].args[:2] == (slim_code_mode_server, "proj_test_123")


@pytest.mark.asyncio
async def test_run_profiled_streamable_http_server_routes_requests_by_profile():
    captured: dict[str, Any] = {}
    fake_apps: dict[str, Any] = {}

    class FakeSessionManager:
        def __init__(self, app, event_store, retry_interval, json_response, stateless):
            self.app = app
            self.event_store = event_store
            self.retry_interval = retry_interval
            self.json_response = json_response
            self.stateless = stateless

        class _RunContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def run(self):
            return self._RunContext()

    class FakeASGIApp:
        def __init__(self, session_manager):
            self.session_manager = session_manager
            self.calls: list[str] = []
            fake_apps[session_manager.app] = self

        async def __call__(self, scope, receive, send):
            self.calls.append(scope["query_string"].decode())

    def fake_create_base_app(*, routes, middleware, debug, lifespan):
        captured["routes"] = routes
        captured["middleware"] = middleware
        captured["debug"] = debug
        captured["lifespan"] = lifespan
        return SimpleNamespace(state=SimpleNamespace())

    class FakeConfig:
        def __init__(self, app, **kwargs):
            captured["app"] = app
            captured["config_kwargs"] = kwargs

    class FakeServerRunner:
        def __init__(self, config):
            self.config = config

        def run(self):
            captured["server_run_called"] = True

    fake_fastmcp = cast(Any, ModuleType("fastmcp"))
    fake_fastmcp.settings = SimpleNamespace(
        streamable_http_path="/mcp",
        stateless_http=False,
        json_response=False,
        debug=False,
        host="127.0.0.1",
        port=8000,
        log_level="INFO",
    )
    fake_fastmcp_http = cast(Any, ModuleType("fastmcp.server.http"))
    fake_fastmcp_http.StreamableHTTPASGIApp = FakeASGIApp
    fake_fastmcp_http.create_base_app = fake_create_base_app
    fake_streamable_manager = cast(Any, ModuleType("mcp.server.streamable_http_manager"))
    fake_streamable_manager.StreamableHTTPSessionManager = FakeSessionManager
    fake_uvicorn = cast(Any, ModuleType("uvicorn"))
    fake_uvicorn.Config = FakeConfig
    fake_uvicorn.Server = FakeServerRunner

    full_server = SimpleNamespace(_mcp_server="full-server", _get_additional_http_routes=lambda: [])
    slim_server = SimpleNamespace(_mcp_server="slim-server", _get_additional_http_routes=lambda: [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    with patch.dict(
        "sys.modules",
        {
            "fastmcp": fake_fastmcp,
            "fastmcp.server.http": fake_fastmcp_http,
            "mcp.server.streamable_http_manager": fake_streamable_manager,
            "uvicorn": fake_uvicorn,
        },
        clear=False,
    ):
        run_profiled_streamable_http_server(
            server=full_server,
            log_level="ERROR",
            middleware=[],
            profiled_servers={"full": full_server, "slim": slim_server},
            default_tool_profile="full",
        )

    assert captured["server_run_called"] is True
    route = cast(Any, captured["routes"][0])

    full_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "headers": [],
        },
        receive=receive,
        send=send,
    )
    slim_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"tool_profile=slim",
            "headers": [],
        },
        receive=receive,
        send=send,
    )
    header_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "headers": [(b"x-rootly-tool-profile", b"slim")],
        },
        receive=receive,
        send=send,
    )
    unknown_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"tool_profile=unexpected",
            "headers": [],
        },
        receive=receive,
        send=send,
    )

    await route.endpoint(full_request)
    await route.endpoint(slim_request)
    await route.endpoint(header_request)
    await route.endpoint(unknown_request)

    assert fake_apps["full-server"].calls == ["", "tool_profile=unexpected"]
    assert fake_apps["slim-server"].calls == ["tool_profile=slim", ""]
