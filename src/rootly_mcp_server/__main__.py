#!/usr/bin/env python3
"""
Rootly MCP Server - Main entry point

This module provides the main entry point for the Rootly MCP Server.
"""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal, cast

from .code_mode import (
    code_mode_enabled_from_env,
    code_mode_path_from_env,
    create_rootly_codemode_server,
    normalize_code_mode_path,
)
from .exceptions import RootlyConfigurationError, RootlyMCPError
from .security import validate_api_token
from .server import create_rootly_mcp_server, get_hosted_auth_middleware
from .server_defaults import enabled_tools_from_env, write_tools_enabled_from_env

TransportName = Literal["stdio", "sse", "streamable-http", "both"]
TRANSPORT_ALIASES: dict[str, TransportName] = {
    "stdio": "stdio",
    "sse": "sse",
    "streamable-http": "streamable-http",
    "streamable": "streamable-http",
    "http": "streamable-http",
    "both": "both",
    "dual": "both",
    "dual-http": "both",
    "streamable+sse": "both",
    "sse+streamable": "both",
}


def normalize_transport(value: str) -> TransportName:
    """Normalize transport names and validate supported values."""
    normalized = value.strip().lower().replace("_", "-")
    mapped = TRANSPORT_ALIASES.get(normalized)
    if mapped is None:
        supported = ", ".join(sorted({"stdio", "sse", "streamable-http", "both"}))
        raise argparse.ArgumentTypeError(
            f"Unsupported transport '{value}'. Supported values: {supported}, http, dual"
        )
    return mapped


def normalize_transport_or_default(value: str, default: TransportName = "stdio") -> TransportName:
    """Normalize transport value, falling back to default when invalid."""
    try:
        return normalize_transport(value)
    except argparse.ArgumentTypeError:
        logging.getLogger(__name__).warning(
            f"Invalid ROOTLY_TRANSPORT value '{value}', defaulting to '{default}'"
        )
        return default


def streamable_http_stateless_enabled(*, hosted: bool, fastmcp_stateless_http: bool) -> bool:
    """Choose streamable HTTP session mode with a safe hosted default.

    Hosted streamable HTTP traffic is high-churn and most clients do not send
    DELETE to close MCP sessions. On current MCP SDK versions that leaks
    stateful session transports until process restart. We therefore default
    hosted deployments to stateless mode unless the operator explicitly sets
    ``FASTMCP_STATELESS_HTTP``.
    """
    if "FASTMCP_STATELESS_HTTP" in os.environ:
        return fastmcp_stateless_http
    return hosted


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Start the Rootly MCP server for API integration.")
    parser.add_argument(
        "--swagger-path",
        type=str,
        help="Path to the Swagger JSON file. If not provided, will look for swagger.json in the current directory and parent directories.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set the logging level. Default: INFO",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="Rootly",
        help="Name of the MCP server. Default: Rootly",
    )
    parser.add_argument(
        "--transport",
        type=normalize_transport,
        default="stdio",
        help="Transport protocol to use: stdio, sse, streamable-http/http, or both/dual. Default: stdio",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (equivalent to --log-level DEBUG)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        help="Base URL for the Rootly API. Default: https://api.rootly.com",
    )
    parser.add_argument(
        "--allowed-paths",
        type=str,
        help="Comma-separated list of allowed API paths to include",
    )
    parser.add_argument(
        "--hosted",
        action="store_true",
        help="Enable hosted mode for remote MCP server",
    )
    parser.add_argument(
        "--enable-code-mode",
        action="store_true",
        help="Expose a separate hosted Code Mode endpoint (HTTP only)",
    )
    parser.add_argument(
        "--no-enable-write-tools",
        dest="enable_write_tools",
        action="store_false",
        default=True,
        help="Disable write tools to expose read-only operations",
    )
    parser.add_argument(
        "--enabled-tools",
        type=str,
        help="Comma-separated allowlist of exact MCP tool names to expose",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the exact MCP tool names exposed by the current configuration, then exit",
    )
    parser.add_argument(
        "--code-mode-path",
        type=str,
        help="Hosted path for the Code Mode endpoint. Default: /mcp-codemode",
    )
    # Backward compatibility: support deprecated --host argument
    parser.add_argument(
        "--host",
        action="store_true",
        help="(Deprecated) Use --hosted instead. Enable hosted mode for remote MCP server",
    )
    return parser.parse_args()


def setup_logging(log_level, debug=False):
    """Set up logging configuration."""
    if debug or os.getenv("DEBUG", "").lower() in ("true", "1", "yes"):
        log_level = "DEBUG"

    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Configure root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],  # Log to stderr for stdio transport
    )

    # Set specific logger levels
    logging.getLogger("rootly_mcp_server").setLevel(numeric_level)
    logging.getLogger("mcp").setLevel(numeric_level)

    # Log the configuration
    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured with level: {log_level}")
    logger.debug(f"Python version: {sys.version}")
    logger.debug(f"Current directory: {Path.cwd()}")
    # SECURITY: Never log actual token values or prefixes
    logger.debug(
        f"Environment variables configured: {', '.join([k for k in os.environ.keys() if k.startswith('ROOTLY_') or k in ['DEBUG']])}"
    )


def check_api_token():
    """Check if the Rootly API token is set and valid."""
    logger = logging.getLogger(__name__)

    try:
        api_token = os.environ.get("ROOTLY_API_TOKEN")
        validate_api_token(api_token)
        # SECURITY: Never log token values or prefixes
        logger.info("ROOTLY_API_TOKEN is configured and valid")
    except RootlyConfigurationError as e:
        logger.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        print("Please set it with: export ROOTLY_API_TOKEN='your-api-token-here'", file=sys.stderr)
        sys.exit(1)


# Create the server instance for FastMCP CLI (follows quickstart pattern)
def get_server():
    """Get a configured Rootly MCP server instance."""
    # Get configuration from environment variables
    swagger_path = os.getenv("ROOTLY_SWAGGER_PATH")
    server_name = os.getenv("ROOTLY_SERVER_NAME", "Rootly")
    hosted = os.getenv("ROOTLY_HOSTED", "false").lower() in ("true", "1", "yes")
    base_url = os.getenv("ROOTLY_BASE_URL")
    transport = normalize_transport_or_default(os.getenv("ROOTLY_TRANSPORT", "stdio"))
    enable_write_tools = write_tools_enabled_from_env(default=True)
    enabled_tools = enabled_tools_from_env()

    # Parse allowed paths from environment variable
    allowed_paths = None
    allowed_paths_env = os.getenv("ROOTLY_ALLOWED_PATHS")
    if allowed_paths_env:
        allowed_paths = [path.strip() for path in allowed_paths_env.split(",")]

    # Create and return the server
    return create_rootly_mcp_server(
        swagger_path=swagger_path,
        name=server_name,
        allowed_paths=allowed_paths,
        hosted=hosted,
        base_url=base_url,
        transport=transport,
        enable_write_tools=enable_write_tools,
        enabled_tools=enabled_tools,
    )


async def _get_sorted_tool_names(server) -> list[str]:
    """Return the effective MCP tool names for the provided server."""
    tools = await server.list_tools()
    return sorted(tool.name for tool in tools)


# Create the server instance for FastMCP CLI (follows quickstart pattern).
# Avoid eager construction when executing `python -m rootly_mcp_server`, because
# CLI flags like `--hosted` and `--transport` are parsed later in `main()`.
mcp = get_server() if __name__ != "__main__" else None


def run_dual_http_server(
    server,
    log_level: str,
    middleware: list | None = None,
    code_mode_server=None,
    code_mode_path: str | None = None,
) -> None:
    """Run SSE and streamable-http together on one ASGI server."""
    import fastmcp
    import uvicorn
    from fastmcp.server.http import StreamableHTTPASGIApp, create_base_app
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    logger = logging.getLogger(__name__)

    sse_path = fastmcp.settings.sse_path
    streamable_path = fastmcp.settings.streamable_http_path
    message_path = fastmcp.settings.message_path
    stateless_http = streamable_http_stateless_enabled(
        hosted=True, fastmcp_stateless_http=fastmcp.settings.stateless_http
    )
    logger.info(
        "Streamable HTTP configured in %s mode", "stateless" if stateless_http else "stateful"
    )

    sse_transport = SseServerTransport(message_path)

    async def handle_sse(scope, receive, send) -> Response:
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await server._mcp_server.run(  # noqa: SLF001
                streams[0],
                streams[1],
                server._mcp_server.create_initialization_options(),  # noqa: SLF001
            )
        return Response()

    async def sse_endpoint(request: Request) -> Response:
        return await handle_sse(request.scope, request.receive, request._send)  # noqa: SLF001

    session_manager = StreamableHTTPSessionManager(
        app=server._mcp_server,  # noqa: SLF001
        event_store=None,
        retry_interval=None,
        json_response=fastmcp.settings.json_response,
        stateless=stateless_http,
    )
    streamable_http_app = StreamableHTTPASGIApp(session_manager)
    # Always allow POST for streamable HTTP - stateless mode only affects session persistence
    streamable_methods = ["POST", "DELETE"]

    routes = [
        Route(sse_path, endpoint=sse_endpoint, methods=["GET"]),
        Mount(message_path, app=sse_transport.handle_post_message),
        Route(streamable_path, endpoint=streamable_http_app, methods=streamable_methods),
    ]

    code_mode_session_manager = None
    if code_mode_server is not None and code_mode_path:
        code_mode_session_manager = StreamableHTTPSessionManager(
            app=code_mode_server._mcp_server,  # noqa: SLF001
            event_store=None,
            retry_interval=None,
            json_response=fastmcp.settings.json_response,
            stateless=stateless_http,
        )
        code_mode_http_app = StreamableHTTPASGIApp(code_mode_session_manager)
        routes.append(
            Route(code_mode_path, endpoint=code_mode_http_app, methods=streamable_methods)
        )

    routes.extend(server._get_additional_http_routes())  # noqa: SLF001

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(server._lifespan_manager())  # noqa: SLF001
            await stack.enter_async_context(session_manager.run())
            if code_mode_server is not None:
                await stack.enter_async_context(code_mode_server._lifespan_manager())  # noqa: SLF001
            if code_mode_session_manager is not None:
                await stack.enter_async_context(code_mode_session_manager.run())
            yield

    app_middleware = cast(list[Middleware], middleware or [])
    app = create_base_app(
        routes=routes,
        middleware=app_middleware,
        debug=fastmcp.settings.debug,
        lifespan=lifespan,
    )
    app.state.fastmcp_server = server
    app.state.path = ",".join(
        [path for path in (sse_path, streamable_path, code_mode_path) if path]
    )
    app.state.transport_type = "both"

    host = fastmcp.settings.host
    port = fastmcp.settings.port
    default_log_level_to_use = (log_level or fastmcp.settings.log_level).lower()

    if code_mode_path:
        logger.info(
            "Starting MCP server %r with dual transport on http://%s:%s%s, http://%s:%s%s, and Code Mode on http://%s:%s%s",
            server.name,
            host,
            port,
            sse_path,
            host,
            port,
            streamable_path,
            host,
            port,
            code_mode_path,
        )
    else:
        logger.info(
            "Starting MCP server %r with dual transport on http://%s:%s%s and http://%s:%s%s",
            server.name,
            host,
            port,
            sse_path,
            host,
            port,
            streamable_path,
        )

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=30,
        lifespan="on",
        ws="websockets-sansio",
        log_level=default_log_level_to_use,
    )
    uvicorn.Server(config).run()


def main():
    """Main entry point for the Rootly MCP Server."""
    args = parse_args()
    setup_logging(args.log_level, args.debug)

    logger = logging.getLogger(__name__)
    logger.info("Starting Rootly MCP Server")

    # Handle backward compatibility for --host argument
    hosted_mode = args.hosted
    if args.host:
        logger.warning("--host argument is deprecated, use --hosted instead")
        hosted_mode = True

    # Only check API token if not in hosted mode
    if not hosted_mode:
        check_api_token()

    try:
        # Parse allowed paths from command line argument
        allowed_paths = None
        if args.allowed_paths:
            allowed_paths = [path.strip() for path in args.allowed_paths.split(",")]
        enabled_tools = (
            {tool.strip() for tool in args.enabled_tools.split(",") if tool.strip()}
            if args.enabled_tools
            else enabled_tools_from_env()
        )

        logger.info(f"Initializing server with name: {args.name}")
        # argparse already normalizes/validates --transport via type=normalize_transport
        normalized_transport = args.transport
        code_mode_enabled = args.enable_code_mode or code_mode_enabled_from_env(default=True)
        enable_write_tools = args.enable_write_tools or write_tools_enabled_from_env(
            default=hosted_mode
        )
        code_mode_path = (
            normalize_code_mode_path(args.code_mode_path)
            if args.code_mode_path
            else code_mode_path_from_env()
        )
        server = create_rootly_mcp_server(
            swagger_path=args.swagger_path,
            name=args.name,
            allowed_paths=allowed_paths,
            hosted=hosted_mode,
            base_url=args.base_url,
            transport=normalized_transport,
            enable_write_tools=enable_write_tools,
            enabled_tools=enabled_tools,
        )

        if args.list_tools:
            for tool_name in asyncio.run(_get_sorted_tool_names(server)):
                print(tool_name)
            return

        code_mode_server = None
        if code_mode_enabled:
            if not hosted_mode:
                logger.warning("Code Mode endpoint requested without hosted mode; ignoring")
            elif normalized_transport != "both":
                logger.warning(
                    "Code Mode endpoint currently requires transport='both'; ignoring because transport=%s",
                    normalized_transport,
                )
            else:
                code_mode_server = create_rootly_codemode_server(
                    swagger_path=args.swagger_path,
                    name=f"{args.name} Code Mode",
                    allowed_paths=allowed_paths,
                    hosted=hosted_mode,
                    base_url=args.base_url,
                    enable_write_tools=enable_write_tools,
                    enabled_tools=enabled_tools,
                )
                logger.info("Code Mode enabled at path: %s", code_mode_path)

        logger.info(f"Running server with transport: {normalized_transport}...")
        direct_streamable_stateless_http = streamable_http_stateless_enabled(
            hosted=hosted_mode,
            fastmcp_stateless_http=os.getenv("FASTMCP_STATELESS_HTTP", "").lower()
            in ("true", "1", "yes"),
        )
        if normalized_transport == "both":
            run_dual_http_server(
                server=server,
                log_level=args.log_level,
                middleware=get_hosted_auth_middleware(),
                code_mode_server=code_mode_server,
                code_mode_path=code_mode_path if code_mode_server is not None else None,
            )
        elif normalized_transport == "stdio":
            server.run(transport=normalized_transport)
        else:
            run_kwargs = {
                "transport": normalized_transport,
                "middleware": get_hosted_auth_middleware(),
                # Override FastMCP's default of 0s to allow active SSE connections
                # to finish gracefully during deployments (avoids 502s).
                "uvicorn_config": {"timeout_graceful_shutdown": 30},
            }
            if normalized_transport == "streamable-http":
                run_kwargs["stateless_http"] = direct_streamable_stateless_http
            server.run(**run_kwargs)

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RootlyConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        print(f"Configuration Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RootlyMCPError as e:
        logger.error(f"Rootly MCP error: {e}", exc_info=True)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        print(f"Unexpected Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
