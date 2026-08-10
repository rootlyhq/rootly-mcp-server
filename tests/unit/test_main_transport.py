"""Tests for CLI transport normalization and config propagation in __main__."""

import argparse
import dataclasses
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.requests import Request

from rootly_mcp_server.__main__ import (
    _get_sorted_tool_names,
    agentcat_options_supports,
    build_mcpcat_identify_callback,
    get_server,
    main,
    maybe_enable_mcpcat_tracking,
    normalize_transport,
    resolve_requested_hosted_tool_profile,
    run_profiled_streamable_http_server,
    streamable_http_stateless_enabled,
)
from rootly_mcp_server.telemetry_scrubber import redact_agentcat_telemetry_text


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


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        # Regression for #149: env var must take effect when the flag is absent.
        (None, "false", False),
        (None, "true", True),
        (None, None, True),  # nothing set -> full access preserved
        (False, None, False),  # --no-enable-write-tools honored
        (False, "true", False),  # explicit flag wins over env
    ],
)
def test_main_write_tools_respects_flag_then_env(flag, env, expected):
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
        enable_write_tools=flag,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    server = SimpleNamespace(run=Mock())
    environ = {"ROOTLY_API_TOKEN": "x" * 40}  # stdio path requires a valid-length token
    if env is not None:
        environ["ROOTLY_MCP_ENABLE_WRITE_TOOLS"] = env

    with patch.dict("os.environ", environ, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=server,
                ) as mock_create:
                    main()

    assert mock_create.call_args.kwargs["enable_write_tools"] is expected


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        (None, "false", False),  # #149 2nd bug: env=false must apply on hosted too
        (False, None, False),  # #149 2nd bug: --no-enable-write-tools honored on hosted
        (None, None, True),  # hosted default preserved: full access
    ],
)
def test_main_write_tools_hosted_path_respects_flag_then_env(flag, env, expected):
    # The resolution is transport-independent; this guards the hosted streamable
    # path specifically (where --no-enable-write-tools + no env was previously
    # ignored via `False or write_tools_enabled_from_env(default=hosted_mode)`).
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
        enable_write_tools=flag,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )
    environ = {} if env is None else {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": env}

    with patch.dict("os.environ", environ, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=args):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=SimpleNamespace(),
                ) as mock_create:
                    with patch(
                        "rootly_mcp_server.__main__.get_hosted_auth_middleware",
                        return_value=[],
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.run_profiled_streamable_http_server"
                        ):
                            main()

    # every profile server is built with the resolved value
    assert mock_create.call_args.kwargs["enable_write_tools"] is expected


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


def _run_main_capture_write_flag(extra_argv, env):
    """Invoke main() through the --list-tools early-return and return the
    enable_write_tools kwarg passed to create_rootly_mcp_server."""
    env = {"ROOTLY_API_TOKEN": "test-token-0123456789abcdef", **env}
    argv = ["rootly-mcp-server", "--list-tools", *extra_argv]
    with (
        patch.dict("os.environ", env, clear=True),
        # Mock setup_logging so invoking main() doesn't mutate global logging
        # config and leak into other tests.
        patch("rootly_mcp_server.__main__.setup_logging"),
        patch("rootly_mcp_server.__main__.create_rootly_mcp_server") as mock_create,
        patch(
            "rootly_mcp_server.__main__._get_sorted_tool_names",
            new=AsyncMock(return_value=[]),
        ),
        patch("sys.argv", argv),
    ):
        main()
    assert mock_create.call_args is not None
    return mock_create.call_args.kwargs["enable_write_tools"]


def test_main_env_false_enables_read_only_without_flag():
    # Regression: ROOTLY_MCP_ENABLE_WRITE_TOOLS=false must restrict to read-only
    # even when --no-enable-write-tools is absent. Previously the env var was
    # ignored because `args.enable_write_tools or <env>` short-circuited on the
    # flag's True default.
    assert _run_main_capture_write_flag([], {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": "false"}) is False


def test_main_flag_forces_read_only_over_env_true():
    # An explicit --no-enable-write-tools wins over ROOTLY_MCP_ENABLE_WRITE_TOOLS=true.
    assert (
        _run_main_capture_write_flag(
            ["--no-enable-write-tools"], {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": "true"}
        )
        is False
    )


def test_main_defaults_to_write_enabled():
    # Full access by default when neither the flag nor the env var is set.
    assert _run_main_capture_write_flag([], {}) is True


def test_main_env_true_enables_write_tools():
    assert _run_main_capture_write_flag([], {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": "true"}) is True


def test_main_flag_forces_read_only_without_env():
    # An explicit --no-enable-write-tools wins even when the env var is unset,
    # independent of any env-derived default.
    assert _run_main_capture_write_flag(["--no-enable-write-tools"], {}) is False


def test_main_hosted_env_false_enables_read_only():
    # In hosted mode the env var is still honored: read-only when it is false.
    # Locks in the env fallback default (write-enabled) not being tied to
    # hosted_mode.
    assert (
        _run_main_capture_write_flag(["--hosted"], {"ROOTLY_MCP_ENABLE_WRITE_TOOLS": "false"})
        is False
    )


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

    mock_import.assert_called_once_with("agentcat")
    logger.warning.assert_called_once_with(
        "AgentCat or Sentry telemetry is configured but agentcat is not installed; "
        "skipping telemetry"
    )


@pytest.mark.parametrize(
    ("project_id", "sentry_dsn", "expect_exporters"),
    [
        ("proj_test_123", "", False),
        ("proj_test_123", "https://abc123@o1.ingest.sentry.io/1", True),
        (None, "https://abc123@o1.ingest.sentry.io/1", True),
        # An invalid DSN disables the Sentry exporter but leaves telemetry on
        # via project_id -- a typo must not silently stop credential scrubbing.
        ("proj_test_123", "not-a-dsn", False),
    ],
)
def test_scrubber_is_registered_whenever_telemetry_is_enabled(
    project_id, sentry_dsn, expect_exporters, monkeypatch
):
    # AgentCat redacts nothing on its own: event_queue only redacts when a hook
    # is set. So the hook has to be attached for every configuration that sends
    # telemetry, not just the ones that also export to Sentry.
    monkeypatch.setenv("SENTRY_DSN", sentry_dsn)
    captured = {}
    agentcat_module = SimpleNamespace(track=Mock())
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=Mock(side_effect=lambda **kwargs: captured.update(kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def fake_import(name):
        return agentcat_module if name == "agentcat" else agentcat_types_module

    with patch("rootly_mcp_server.__main__.importlib.import_module", fake_import):
        maybe_enable_mcpcat_tracking(object(), project_id, Mock())

    assert captured.get("redact_sensitive_information") is redact_agentcat_telemetry_text
    assert ("exporters" in captured) is expect_exporters


@pytest.mark.parametrize("supported", [True, False])
def test_redact_event_is_offered_only_when_the_sdk_accepts_it(supported, monkeypatch):
    # The event hook landed after 2.0.1. Passing an unknown option raises
    # TypeError, which maybe_enable_mcpcat_tracking catches by disabling
    # telemetry outright -- so an older SDK must simply not be offered it.
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    fields: dict[str, Any] = {
        "identify": None,
        "redact_sensitive_information": None,
        "exporters": None,
    }
    if supported:
        fields["redact_event"] = None
    options_cls = dataclasses.make_dataclass(
        "AgentCatOptions", [(name, Any, None) for name in fields]
    )

    captured: dict[str, Any] = {}

    def make_options(**kwargs):
        captured.update(kwargs)
        return options_cls(**kwargs)

    # A subclass keeps dataclasses.fields() working while letting the call
    # record its kwargs, which a Mock wrapper would hide.
    factory = type("Factory", (options_cls,), {"__new__": lambda cls, **kw: make_options(**kw)})

    agentcat_module = SimpleNamespace(track=Mock())
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=factory,
        UserIdentity=Mock(side_effect=lambda **kw: SimpleNamespace(**kw)),
    )

    def fake_import(name):
        return agentcat_module if name == "agentcat" else agentcat_types_module

    logger = Mock()
    with patch("rootly_mcp_server.__main__.importlib.import_module", fake_import):
        maybe_enable_mcpcat_tracking(object(), "proj_test_123", logger)

    # Telemetry stays enabled either way -- that is the point of the detection.
    assert agentcat_module.track.called
    assert captured["redact_sensitive_information"] is redact_agentcat_telemetry_text
    assert ("redact_event" in captured) is supported

    # An SDK too old for the hook must say so. Silently skipping the scrubber is
    # how the SENTRY_DSN gap survived unnoticed.
    warned = any("redact_event" in str(call.args[0]) for call in logger.warning.call_args_list)
    assert warned is (not supported)


def test_agentcat_options_supports_detects_the_field():
    options_cls = dataclasses.make_dataclass("Opts", [("redact_event", Any, None)])
    assert agentcat_options_supports(options_cls, "redact_event") is True
    assert agentcat_options_supports(options_cls, "nope") is False


def test_agentcat_options_supports_is_false_for_a_non_dataclass():
    assert agentcat_options_supports(object, "redact_event") is False


def test_maybe_enable_mcpcat_tracking_tracks_when_available():
    server = object()
    logger = Mock()
    agentcat_module = SimpleNamespace(track=Mock())
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "agentcat":
            return agentcat_module
        if module_name == "agentcat.types":
            return agentcat_types_module
        raise ImportError(module_name)

    with patch(
        "rootly_mcp_server.__main__.importlib.import_module", side_effect=import_side_effect
    ):
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    agentcat_module.track.assert_called_once()
    call = agentcat_module.track.call_args
    assert call.args[:2] == (server, "proj_test_123")
    assert callable(call.args[2].identify)


def test_maybe_enable_mcpcat_tracking_configures_sentry_exporter():
    server = object()
    logger = Mock()
    agentcat_module = SimpleNamespace(track=Mock())
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "agentcat":
            return agentcat_module
        if module_name == "agentcat.types":
            return agentcat_types_module
        raise ImportError(module_name)

    with (
        patch.dict(
            "os.environ",
            {
                "SENTRY_DSN": "https://abcdef@example.ingest.sentry.io/123",
                "ENVIRONMENT": "staging",
                "SENTRY_RELEASE": "rootly-mcp-server@test",
            },
            clear=True,
        ),
        patch(
            "rootly_mcp_server.__main__.importlib.import_module",
            side_effect=import_side_effect,
        ),
    ):
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    options = agentcat_module.track.call_args.args[2]
    # The hook scrubs the credential while leaving the surrounding text
    # readable — a blanket placeholder would destroy error grouping and the
    # client/server identifiers the telemetry backend needs.
    redact = options.redact_sensitive_information
    assert redact("Bearer production-secret") == "Bearer [redacted]"
    assert redact("HTTP error 404: Not Found") == "HTTP error 404: Not Found"
    identity = options.identify({}, SimpleNamespace())
    assert identity is None
    assert options.exporters == {
        "sentry": {
            "type": "sentry",
            "dsn": "https://abcdef@example.ingest.sentry.io/123",
            "environment": "staging",
            "release": "rootly-mcp-server@test",
            "enable_tracing": True,
        }
    }


def test_maybe_enable_mcpcat_tracking_supports_sentry_without_agentcat_project():
    server = object()
    logger = Mock()
    agentcat_module = SimpleNamespace(track=Mock())
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "agentcat":
            return agentcat_module
        if module_name == "agentcat.types":
            return agentcat_types_module
        raise ImportError(module_name)

    with (
        patch.dict(
            "os.environ",
            {
                "SENTRY_DSN": "https://abcdef@example.ingest.sentry.io/123",
                "ENVIRONMENT": "production-us-east-1",
            },
            clear=True,
        ),
        patch(
            "rootly_mcp_server.__main__.importlib.import_module",
            side_effect=import_side_effect,
        ),
    ):
        maybe_enable_mcpcat_tracking(server, None, logger)

    assert agentcat_module.track.call_args.args[:2] == (server, None)
    options = agentcat_module.track.call_args.args[2]
    assert options.exporters["sentry"]["environment"] == "production-us-east-1"
    assert options.exporters["sentry"]["release"].startswith("rootly-mcp-server@")


def test_maybe_enable_mcpcat_tracking_rejects_invalid_sentry_dsn_without_logging_it():
    server = object()
    logger = Mock()
    invalid_dsn = "not-a-valid-dsn-containing-a-secret"

    with (
        patch.dict("os.environ", {"SENTRY_DSN": invalid_dsn}, clear=True),
        patch("rootly_mcp_server.__main__.importlib.import_module") as mock_import,
    ):
        maybe_enable_mcpcat_tracking(server, None, logger)

    mock_import.assert_not_called()
    logger.warning.assert_called_once_with(
        "Sentry telemetry is disabled because SENTRY_DSN is invalid"
    )
    assert invalid_dsn not in repr(logger.mock_calls)


def test_maybe_enable_mcpcat_tracking_logs_when_track_raises():
    server = object()
    logger = Mock()
    agentcat_module = SimpleNamespace(track=Mock(side_effect=RuntimeError("boom")))
    agentcat_types_module = SimpleNamespace(
        AgentCatOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        UserIdentity=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    def import_side_effect(module_name: str):
        if module_name == "agentcat":
            return agentcat_module
        if module_name == "agentcat.types":
            return agentcat_types_module
        raise ImportError(module_name)

    with patch(
        "rootly_mcp_server.__main__.importlib.import_module", side_effect=import_side_effect
    ):
        maybe_enable_mcpcat_tracking(server, "proj_test_123", logger)

    assert agentcat_module.track.call_args.args[:2] == (server, "proj_test_123")
    # assert_any_call, not assert_called_once_with: this stub's options class has
    # no redact_event field, so the unsupported-SDK warning fires too. This test
    # is about the track() failure path.
    logger.warning.assert_any_call(
        "AgentCat tracking could not be enabled; skipping (%s)",
        "RuntimeError",
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


def test_build_mcpcat_identify_callback_omits_user_name_for_sentry():
    user_identity_cls = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    callback = build_mcpcat_identify_callback(user_identity_cls, include_user_name=False)

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
    assert identity.user_name is None
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
