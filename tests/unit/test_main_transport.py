"""Tests for CLI transport normalization and config propagation in __main__."""

import argparse
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from rootly_mcp_server.__main__ import (
    _get_sorted_tool_names,
    build_mcpcat_identify_callback,
    get_server,
    main,
    maybe_enable_mcpcat_tracking,
    normalize_transport,
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
    fake_server = SimpleNamespace(run=Mock())

    with patch.dict("os.environ", {}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=fake_server,
                ):
                    with patch(
                        "rootly_mcp_server.__main__.get_hosted_auth_middleware", return_value=[]
                    ):
                        main()

    fake_server.run.assert_called_once()
    assert fake_server.run.call_args.kwargs["transport"] == "streamable-http"
    assert fake_server.run.call_args.kwargs["stateless_http"] is True


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
    code_mode_server = SimpleNamespace()

    with patch.dict("os.environ", {"ROOTLY_MCPCAT_PROJECT_ID": "proj_test_123"}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=main_server,
                ):
                    with patch(
                        "rootly_mcp_server.__main__.create_rootly_codemode_server",
                        return_value=code_mode_server,
                    ):
                        with patch("rootly_mcp_server.__main__.run_dual_http_server"):
                            with patch(
                                "rootly_mcp_server.__main__.maybe_enable_mcpcat_tracking"
                            ) as mock_track:
                                main()

    assert len(mock_track.call_args_list) == 2
    assert mock_track.call_args_list[0].args[:2] == (main_server, "proj_test_123")
    assert mock_track.call_args_list[1].args[:2] == (code_mode_server, "proj_test_123")
