"""Code Mode helpers for exposing a third MCP endpoint."""

from __future__ import annotations

import builtins
import difflib
import json
import keyword
import logging
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

import fastmcp.server.middleware as fastmcp_middleware
import mcp.types as mt
from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.experimental.transforms.code_mode import (
    CodeMode,
    GetSchemas,
    GetTags,
    ListTools,
    MontySandboxProvider,
    Search,
    _unwrap_tool_result,
)
from fastmcp.server.context import Context
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from .server import create_rootly_mcp_server
from .spec_transform import to_snake_case
from .tool_arguments import suggest_parameter, valid_parameters_line

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Discovery tools (list_tools, tool_search, get_schema, tags) only read the tool
# catalog — safe, idempotent, no external side effects.
_DISCOVERY_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
# execute is a gateway that can invoke ANY Rootly tool, including writes and
# destructive actions, so it gets the most conservative annotations.
_EXECUTE_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
_EXECUTE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {}},
    "required": ["result"],
    "x-fastmcp-wrap-result": True,
}

DEFAULT_CODE_MODE_PATH = "/mcp-codemode"
# Same per-execute budget fastmcp's CodeMode applies by default.
DEFAULT_CODE_MODE_MAX_TOOL_CALLS = 50
CODE_MODE_MAX_TOOL_CALLS_ENV = "ROOTLY_CODE_MODE_MAX_TOOL_CALLS"
_TOOL_CALL_LIMIT_MARKER = "Tool call limit exceeded"
_TOOL_NAME_PREFIXES = (
    "mcp__rootly-codemode__",
    "mcp__rootly__",
    "rootly-codemode:",
    "rootly:",
    "rootly.",
    "rootly_",
)
# Any other MCP client namespace, e.g. `mcp__claude_ai_Rootly__list_teams`.
_GENERIC_MCP_PREFIX = re.compile(r"^mcp__.+?__(?=.)")
_MAX_TOOL_SUGGESTIONS = 3
_UNDEFINED_NAME = re.compile(r"name '([^']+)' is not defined")
_CALL_TOOL_FAILURE = re.compile(r"call_tool\('[^']*'\) failed: ")
_INTROSPECTION_BUILTINS = frozenset({"dir", "globals", "locals", "vars"})
# JSON / JavaScript literals scripts write in place of Python's.
_FOREIGN_LITERALS = {"true": "True", "false": "False", "null": "None", "undefined": "None"}
_DISPLAY_LINE_NUMBER = re.compile(r'^\s*File "[^"]*", line (\d+)')
# Scripts that guessed the shape of a call_tool result. Monty raises these as
# MontyRuntimeError, so they are recognized by message, not exception type.
_RESULT_SHAPE_ERROR = re.compile(
    r"^KeyError: "
    r"|'(?:str|dict|list)' object has no attribute "
    r"|(?:str|string|list) indices must be integers"
    r"|'NoneType' object is not subscriptable"
)
_RESULT_SHAPE_HINT = (
    "call_tool returns the tool's result directly, with no 'result' wrapper: a dict "
    "such as the JSON:API body {'data': [...], 'meta': {...}} for API tools, or the "
    "tool's own keys for custom tools (list_incidents returns 'incidents' and "
    "'pagination'). Return the value once to inspect its keys, and use .get() for "
    "keys that may be missing."
)
_TOOL_NAME_ALIASES = {
    "search": "tool_search",
}
# Same markers fastmcp's own Code Mode uses to recognize a validation failure
# that reached call_tool as plain text rather than a ValidationError.
_VALIDATION_ERROR_MARKERS = (
    "validation error for",
    "unexpected keyword argument",
    "missing required argument",
)


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


def code_mode_max_tool_calls_from_env(default: int = DEFAULT_CODE_MODE_MAX_TOOL_CALLS) -> int:
    """Return the per-execute tool call budget for hosted Code Mode.

    Only a positive integer is accepted; anything else logs a warning and
    keeps ``default``, so a bad value can never lift the limit.
    """
    raw = os.getenv(CODE_MODE_MAX_TOOL_CALLS_ENV)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "Invalid %s=%r (expected a positive integer); falling back to %d",
            CODE_MODE_MAX_TOOL_CALLS_ENV,
            raw,
            default,
        )
        return default
    return value


def _strip_tool_name_prefixes(tool_name: str) -> list[str]:
    """Return every form of ``tool_name`` with one known client prefix removed."""
    stripped: list[str] = []
    for prefix in _TOOL_NAME_PREFIXES:
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
            stripped.append(tool_name[len(prefix) :])
    generic = _GENERIC_MCP_PREFIX.match(tool_name)
    if generic:
        stripped.append(tool_name[generic.end() :])
    return [_TOOL_NAME_ALIASES.get(name, name) for name in dict.fromkeys(stripped)]


@dataclass(frozen=True)
class ToolNameResolution:
    """Outcome of resolving a requested name against the session's tool catalog."""

    requested: str
    tool: Tool | None = None
    ambiguous: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    def error_message(self) -> str:
        """Describe why the name did not resolve, naming candidates when known."""
        if self.ambiguous:
            options = ", ".join(repr(name) for name in self.ambiguous)
            return (
                f"Unknown tool: {self.requested} (ambiguous: it could mean {options}; "
                "call one of them by its exact name)"
            )
        message = f"Unknown tool: {self.requested}"
        if self.suggestions:
            options = ", ".join(repr(name) for name in self.suggestions)
            message += f" (did you mean {options}?)"
        return message


def resolve_tool_name(requested: str, tools: Sequence[Tool]) -> ToolNameResolution:
    """Resolve a model-written tool name to exactly one tool in ``tools``.

    ``tools`` must be the catalog the current session is allowed to call, so
    no normalization can ever reach a tool outside it. Stages run in order and
    the first stage that matches anything decides:

    1. exact name (plus the ``search`` -> ``tool_search`` alias)
    2. one known client prefix stripped (``mcp__<server>__``, ``rootly:``, ...)
    3. camelCase/PascalCase folded to snake_case -- the normalization the
       server applies to operationIds -- compared against the folded catalog

    A stage that matches more than one distinct tool resolves nothing: the
    caller gets the candidates back instead of a guess.
    """
    name = (requested or "").strip()
    stripped = _strip_tool_name_prefixes(name)
    folded = {to_snake_case(candidate) for candidate in (name, *stripped)}
    exact = {name, _TOOL_NAME_ALIASES.get(name, name)}

    stages: tuple[Callable[[Tool], bool], ...] = (
        lambda tool: tool.name in exact,
        lambda tool: tool.name in stripped,
        lambda tool: to_snake_case(tool.name) in folded,
    )
    for matches_stage in stages:
        # Keyed by name: an exact-name tie (discovery tool listed before a
        # backend tool) keeps the first entry rather than counting as two.
        matches = {tool.name: tool for tool in reversed(tools) if matches_stage(tool)}
        if len(matches) == 1:
            return ToolNameResolution(name, tool=next(iter(matches.values())))
        if len(matches) > 1:
            return ToolNameResolution(name, ambiguous=tuple(sorted(matches)))

    names = [tool.name for tool in tools]
    suggestions: list[str] = []
    for candidate in sorted(folded, key=len):
        for match in difflib.get_close_matches(candidate, names, n=_MAX_TOOL_SUGGESTIONS):
            if match not in suggestions:
                suggestions.append(match)
    return ToolNameResolution(name, suggestions=tuple(suggestions[:_MAX_TOOL_SUGGESTIONS]))


def _legible_call_error(tool_name: str, tool: Tool, error: Exception) -> str:
    """Rewrite a failed call's error for the model that wrote the script.

    Ported from fastmcp's Code Mode: validation failures list each problem as
    ``<param>: <message>`` and append the tool's parameter list, always in the
    name the caller used. On top of that, every unexpected argument gets a
    "did you mean" pointing at the real parameter. Pydantic's own messages
    already enumerate the allowed values of a literal (e.g. ``sort``), so
    those are kept verbatim.
    """
    raw_parameters = getattr(tool, "parameters", None)
    parameters = raw_parameters if isinstance(raw_parameters, dict) else {}
    properties = parameters.get("properties")
    names = list(properties) if isinstance(properties, dict) else []

    validation = isinstance(error, ValidationError)
    cause = error.__cause__
    if validation and isinstance(cause, PydanticValidationError):
        problems: list[str] = []
        for item in cause.errors(include_url=False):
            loc = ".".join(str(part) for part in item["loc"]) or "arguments"
            problem = f"{loc}: {item['msg']}"
            if item["type"] == "unexpected_keyword_argument":
                suggestion = suggest_parameter(loc, names)
                if suggestion is not None:
                    problem += f" (did you mean {suggestion!r}?)"
            problems.append(problem)
        text = "; ".join(problems)
    else:
        text = str(error)

    message = f"call_tool({tool_name!r}) failed: {text}"
    # The curated-tool argument check (server.ToolArgumentAliasMiddleware) already
    # appends the parameter list; never list it twice.
    if "\nValid parameters for " in text:
        return message
    if names and (validation or any(m in text.lower() for m in _VALIDATION_ERROR_MARKERS)):
        message += f"\n{valid_parameters_line(tool_name, parameters)}"
    return message


# Names a bare tool callable must never shadow inside the sandbox.
_RESERVED_SANDBOX_NAMES = frozenset(dir(builtins)) | {"call_tool"}


def _is_injectable_tool_name(name: str) -> bool:
    return (
        name.isidentifier() and not keyword.iskeyword(name) and name not in _RESERVED_SANDBOX_NAMES
    )


def _example_arguments(tool: Tool) -> str:
    """Render a call_tool params example from the tool's own input schema.

    Required parameters become `<name>` placeholders so following the hint
    can't fail on a missing argument; `page_size` is shown only when the tool
    takes it.
    """
    parameters = getattr(tool, "parameters", None)
    schema = parameters if isinstance(parameters, dict) else {}
    properties = schema.get("properties") or {}
    required = [name for name in schema.get("required") or [] if name in properties]
    example: dict[str, Any] = {name: f"<{name}>" for name in required}
    if not example and "page_size" in properties:
        example["page_size"] = 10
    return repr(example)


def _undefined_name_message(missing: str, tools: Sequence[Tool]) -> str | None:
    """Teach the call convention when a script used a tool name as a bare name."""
    resolution = resolve_tool_name(missing, tools)
    if resolution.tool is not None:
        canonical = resolution.tool.name
        return (
            f"NameError: name '{missing}' is not defined. It refers to the Rootly tool "
            f"'{canonical}'; call it as "
            f"`await call_tool('{canonical}', {_example_arguments(resolution.tool)})`"
            " with the parameters from its schema (get_schema / tool_search)."
        )
    candidates = resolution.ambiguous or resolution.suggestions
    if not candidates:
        return None
    options = ", ".join(repr(name) for name in candidates)
    lead = (
        f"It could mean the Rootly tools {options}"
        if resolution.ambiguous
        else f"If you meant a Rootly tool, the closest are {options}"
    )
    return (
        f"NameError: name '{missing}' is not defined. {lead}. Pick the right one "
        "(tool_search / get_schema) and call it by its exact name with "
        "`await call_tool('<exact_tool_name>', {...})`."
    )


def _syntax_error_message(exc: Exception, message: str) -> str:
    """Point a Monty syntax error at its line; the bare message names no location.

    ``MontySyntaxError.display()`` renders a CPython-style traceback whose last
    frame carries the line number, the offending source line and a caret.
    """
    try:
        display_lines = str(exc.display()).splitlines()  # type: ignore[attr-defined]
    except Exception:
        display_lines = []
    line_number = None
    source: list[str] = []
    for line in display_lines:
        found = _DISPLAY_LINE_NUMBER.match(line)
        if found:
            line_number, source = found.group(1), []
        elif line_number is not None and not line.startswith(("SyntaxError", "Traceback")):
            source.append(line)
    if "line continuation character" in message:
        # A script sent with escaped newlines arrives as one line full of "\n".
        hint = (
            "The code contains a literal backslash-n (\\n) where a line break belongs; "
            "send real newlines in `code`."
        )
    else:
        hint = (
            "Code Mode runs a Python subset: write Python, not JavaScript or JSON, "
            "with one statement per line."
        )
    location = f" on line {line_number}" if line_number else ""
    parts = [f"SyntaxError{location}: {message}", *source, hint]
    return "\n".join(parts)


def _format_execute_exception(exc: Exception, tools: Sequence[Tool] = ()) -> str | None:
    """Return a friendlier execute error message for known sandbox failures.

    ``tools`` is the session's callable catalog; it lets an undefined-name
    error point at the tool the script most likely meant.
    """
    message = str(exc).strip()
    if not message:
        return None

    # A failed call_tool already names the tool, the problem and the valid
    # parameters (see _legible_call_error); nothing below would add to it.
    if _CALL_TOOL_FAILURE.search(message):
        return None

    if type(exc).__name__ == "MontySyntaxError":
        return _syntax_error_message(exc, message)

    undefined = _UNDEFINED_NAME.search(message)
    if undefined and (isinstance(exc, NameError) or "NameError" in message):
        missing = undefined.group(1)
        if missing in _FOREIGN_LITERALS:
            return (
                f"{message}. Code Mode runs Python: write {_FOREIGN_LITERALS[missing]}, "
                f"not the JSON/JavaScript literal {missing}."
            )
        if missing in _INTROSPECTION_BUILTINS:
            return (
                f"{message}. Introspection builtins (dir, globals, locals, vars) are not "
                "available in the Code Mode sandbox. Use tool_search / get_schema to "
                "discover tools and their parameters, and `return` a value to inspect it."
            )
        if missing == "datetime":
            return (
                f"{message}. Add `import datetime` first; the current UTC time is "
                "datetime.datetime.now(datetime.timezone.utc)."
            )
        return _undefined_name_message(missing, tools)

    if "object is not an iterator" in message:
        return (
            f"{message}. next() needs an iterator: use `next(iter(items), None)`, or "
            "index the list directly (`items[0] if items else None`)."
        )

    if "not supported between instances of" in message:
        return (
            f"{message}. Sort or compare by a plain value, e.g. "
            "`sorted(items, key=lambda item: item.get('id') or '')`, and give "
            "missing values a default so None never meets a str or int."
        )

    if _RESULT_SHAPE_ERROR.search(message):
        return f"{message}. {_RESULT_SHAPE_HINT}"

    if _TOOL_CALL_LIMIT_MARKER in message:
        return (
            f"{message} Do the work in fewer calls: use list tools with filters and a "
            "larger page_size instead of one call per item, or split the workflow across "
            "several execute calls and return intermediate results."
        )

    if isinstance(exc, NotFoundError) or "Unknown tool:" in message:
        return (
            f"{message}. Use tool_search to discover available tools and call them "
            "without client prefixes like 'mcp__rootly-codemode__' or 'rootly:'."
        )

    if isinstance(exc, ModuleNotFoundError) or "No module named" in message:
        return (
            f"{message}. Code Mode runs in a sandbox with part of the standard library "
            "(json, datetime, re, asyncio, ...) and no third-party packages, network or "
            "filesystem. Use `await call_tool(name, params)` for Rootly operations."
        )

    if message.startswith("Expected ") or "got Subscript(" in message:
        return (
            f"{message}. Code Mode supports a restricted Python subset. Keep the block "
            "simple: assign intermediate values to variables, call `await call_tool(...)`, "
            "and `return` the final value."
        )

    return None


class RootlyCodeMode(CodeMode):
    """Rootly-specific Code Mode transform with friendlier execute ergonomics."""

    def _build_discovery_tools(self) -> list[Tool]:
        # Attach read-only annotations to the actual Tool objects FastMCP exposes
        # (list_tools, tool_search, get_schema, tags). The base result is cached,
        # so we annotate the concrete objects the runtime returns.
        tools = super()._build_discovery_tools()
        for tool in tools:
            tool.annotations = _DISCOVERY_TOOL_ANNOTATIONS
        return tools

    def _make_execute_tool(self) -> Tool:
        transform = self
        # fastmcp's CodeMode stores this from its `max_tool_calls` constructor
        # argument (default 50; None disables the limit).
        max_tool_calls = self.max_tool_calls

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
            # Discovery tools first so they win an exact-name tie, then the
            # session-filtered backend catalog (allowlists, hosted profiles,
            # visibility). Every name resolution runs against this list only.
            tools: list[Tool] = [
                *transform._build_discovery_tools(),
                *await transform.get_tool_catalog(ctx),
            ]
            # Per execute(): every call_tool() invocation counts, including the
            # bare tool functions (they route through call_tool), discovery
            # tools and unknown names -- the same semantics as fastmcp's own
            # execute, which counts before resolving the name.
            call_count = 0

            async def call_tool(tool_name: str, params: dict[str, Any] | None = None) -> Any:
                nonlocal call_count
                if max_tool_calls is not None:
                    call_count += 1
                    if call_count > max_tool_calls:
                        raise ToolError(
                            f"{_TOOL_CALL_LIMIT_MARKER}: at most {max_tool_calls} "
                            "call_tool() invocations are allowed per execute()."
                        )

                resolution = resolve_tool_name(tool_name, tools)
                if resolution.tool is None:
                    raise NotFoundError(resolution.error_message())

                try:
                    result = await ctx.fastmcp.call_tool(resolution.tool.name, params or {})
                except (ToolError, ValidationError) as exc:
                    raise ToolError(_legible_call_error(tool_name, resolution.tool, exc)) from exc
                return _unwrap_tool_result(result)

            def bare_tool_function(tool: Tool) -> Callable[..., Any]:
                name = tool.name

                async def tool_function(*args: Any, **kwargs: Any) -> Any:
                    if len(args) > 1 or (args and not isinstance(args[0], dict)):
                        example = _example_arguments(tool)
                        raise TypeError(
                            f"{name}() takes one params dict or keyword arguments, e.g. "
                            f"`await {name}({example})` or "
                            f"`await call_tool('{name}', {example})`."
                        )
                    return await call_tool(name, {**(args[0] if args else {}), **kwargs})

                return tool_function

            # Scripts often call tools as plain functions (`await list_shifts()`).
            # Monty consults these only for names the script leaves undefined,
            # so builtins and the script's own variables always win, and each
            # one routes through call_tool, so it reaches nothing call_tool
            # could not.
            external_functions: dict[str, Callable[..., Any]] = {
                tool.name: bare_tool_function(tool)
                for tool in tools
                if _is_injectable_tool_name(tool.name)
            }
            external_functions["call_tool"] = call_tool

            try:
                return await transform.sandbox_provider.run(
                    code,
                    external_functions=external_functions,
                )
            except Exception as exc:
                friendly_message = _format_execute_exception(exc, tools)
                if friendly_message is None:
                    raise
                raise ValueError(friendly_message) from exc

        return Tool.from_function(
            fn=execute,
            name=self.execute_tool_name,
            description=self._build_execute_description(),
            annotations=_EXECUTE_TOOL_ANNOTATIONS,
            output_schema=_EXECUTE_OUTPUT_SCHEMA,
        )


# Every tool parameter named here must exist in that tool's schema;
# tests/unit/test_code_mode_module.py checks it against the built server.
EXECUTE_DESCRIPTION = (
    "Write a short async Python block that chains `await call_tool(name, params)` calls to "
    "complete a Rootly workflow, and `return` the final value. Use tool_search or get_schema "
    "to discover tools, then pass only the parameter names that tool's schema lists. "
    "call_tool returns the tool's result directly, with no 'result' wrapper: a dict such as "
    "the JSON:API body {'data': [...], 'meta': {...}} for API tools, or the tool's own keys "
    "for custom tools (list_incidents returns 'incidents' and 'pagination'); return it once "
    "to inspect its keys. Prefer Rootly's higher-level custom tools when they fit the task, "
    "then fall back to lower-level API tools. Use list_incidents for structured incident "
    "queries with filters like teams, team_ids, service_ids, severity, status, started_after, "
    "and started_before (ISO 8601 timestamps), paged with page_size and page_number; use "
    "collect_incidents with the same filters and max_results to gather incidents across "
    "pages; use search_incidents for free-text lookups with query, page_size, and "
    "max_results. Do not use client prefixes like mcp__rootly-codemode__tool_search or "
    "rootly:get_current_user inside execute; call tool_search or the raw Rootly tool name "
    "directly. Return native Python values (no json.dumps); for "
    "relative dates, `import datetime` and start from "
    "datetime.datetime.now(datetime.timezone.utc). "
    "Example: await call_tool('list_incidents', {'status': 'resolved', "
    "'started_after': '2026-01-01T00:00:00Z', 'page_size': 10})."
)

_EXECUTE_TOOL_KEYS = ("tool", "tool_name", "name")
_EXECUTE_ARGUMENT_KEYS = ("arguments", "params", "args", "parameters")
_EXECUTE_EXAMPLE = "{\"code\": \"return await call_tool('list_incidents', {'page_size': 5})\"}"


def _is_literal_value(value: Any) -> bool:
    """True when ``repr(value)`` is a Python literal that evaluates back to it.

    Holds for decoded JSON: None, bool, int, finite float, str, and lists and
    str-keyed dicts of those.
    """
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_literal_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_literal_value(v) for k, v in value.items())
    return False


def _first_present(arguments: dict[str, Any], keys: Sequence[str]) -> str | None:
    return next((key for key in keys if key in arguments), None)


def execute_code_for_call_shape(arguments: dict[str, Any]) -> str:
    """Translate a call_tool-shaped execute request into the equivalent code.

    Models call ``execute`` the way they call ``call_tool`` --
    ``{"tool": "list_incidents", "arguments": {...}}`` -- with no ``code``.
    Rather than fail, run ``return await call_tool(<tool>, <arguments>)``:
    the call takes the same path as any script (name resolution, tool call
    limit, session catalog), so it can reach nothing a script could not.

    Raises ToolError, with an example of the right shape, when the request
    cannot be translated.
    """
    tool_key = _first_present(arguments, _EXECUTE_TOOL_KEYS)
    tool = arguments.get(tool_key) if tool_key else None
    if not isinstance(tool, str) or not tool.strip():
        keys = ", ".join(sorted(arguments)) or "none"
        raise ToolError(
            "execute takes one argument, `code`: an async Python block that calls "
            f"Rootly tools with `await call_tool(name, params)`, e.g. {_EXECUTE_EXAMPLE}. "
            f"Got arguments: {keys}."
        )

    rest = {key: value for key, value in arguments.items() if key != tool_key}
    params_key = _first_present(rest, _EXECUTE_ARGUMENT_KEYS)
    if params_key is None:
        # {"tool": "list_incidents", "page_size": 5}: the rest are the params.
        params: Any = rest
    else:
        params = rest.pop(params_key)
        if params is None:
            params = {}
        elif isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except ValueError:
                params = None  # rejected below with the shape to use

    example = f"return await call_tool({tool!r}, {{...}})"
    if params_key is not None and rest:
        raise ToolError(
            f"execute got both {params_key!r} and other arguments "
            f"({', '.join(sorted(rest))}); pass a single `code` argument instead, "
            f"e.g. {{'code': {example!r}}}."
        )
    if not isinstance(params, dict) or not _is_literal_value(params):
        raise ToolError(
            f"execute could not read the arguments for {tool!r}: pass them as a JSON "
            f"object, or pass a single `code` argument, e.g. {{'code': {example!r}}}."
        )
    return f"return await call_tool({tool!r}, {params!r})"


class ExecuteCallShapeMiddleware(fastmcp_middleware.Middleware):
    """Accepts ``execute(tool=..., arguments=...)`` by rewriting it to ``code``.

    Runs before argument validation, so the advertised execute schema stays a
    single required ``code`` string. A request that already has ``code`` is
    never touched. Registered only on the Code Mode server.
    """

    def __init__(self, execute_tool_name: str = "execute") -> None:
        self._execute_tool_name = execute_tool_name

    async def on_call_tool(
        self,
        context: fastmcp_middleware.MiddlewareContext[mt.CallToolRequestParams],
        call_next: fastmcp_middleware.CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        arguments = context.message.arguments
        if context.message.name == self._execute_tool_name and "code" not in (arguments or {}):
            code = execute_code_for_call_shape(dict(arguments or {}))
            logger.info(
                "Rewrote a call_tool-shaped execute request (keys: %s) into code",
                ", ".join(sorted(arguments or {})),
            )
            context.message.arguments = {"code": code}
        return await call_next(context)


_GET_SCHEMA_TOOL_NAME = "get_schema"
_GET_SCHEMA_NAME_KEYS = ("tool_name", "name", "tool")


class GetSchemaArgumentShapeMiddleware(fastmcp_middleware.Middleware):
    """Accepts ``get_schema(tool_name="x")`` as ``get_schema(tools=["x"])``.

    Models ask for one schema by name, the way they call ``execute``'s
    ``tool`` (see ExecuteCallShapeMiddleware), and get_schema's only required
    argument is the ``tools`` list. A bare string ``tools`` is wrapped too. A
    request that already has a ``tools`` list is never touched. Registered
    only on the Code Mode server.
    """

    def __init__(self, tool_name: str = _GET_SCHEMA_TOOL_NAME) -> None:
        self._tool_name = tool_name

    async def on_call_tool(
        self,
        context: fastmcp_middleware.MiddlewareContext[mt.CallToolRequestParams],
        call_next: fastmcp_middleware.CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        arguments = context.message.arguments
        if context.message.name == self._tool_name and arguments:
            tools = arguments.get("tools")
            if isinstance(tools, str) and tools.strip():
                arguments["tools"] = [tools]
                logger.info("Wrapped a string get_schema `tools` argument in a list")
            elif "tools" not in arguments:
                key = _first_present(arguments, _GET_SCHEMA_NAME_KEYS)
                value = arguments.get(key) if key else None
                names = [value] if isinstance(value, str) else value
                if (
                    key is not None
                    and isinstance(names, list)
                    and names
                    and all(isinstance(name, str) and name.strip() for name in names)
                ):
                    arguments.pop(key)
                    arguments["tools"] = names
                    logger.info("Rewrote get_schema(%s=...) into tools=[...]", key)
        return await call_next(context)


def build_code_mode_transform(max_tool_calls: int | None = None) -> CodeMode:
    """Build the shared Code Mode transform used by hosted deployments.

    ``max_tool_calls`` caps call_tool() invocations per execute(); when omitted
    it comes from ``ROOTLY_CODE_MODE_MAX_TOOL_CALLS`` (default 50).
    """
    return RootlyCodeMode(
        sandbox_provider=MontySandboxProvider(),
        max_tool_calls=(
            max_tool_calls if max_tool_calls is not None else code_mode_max_tool_calls_from_env()
        ),
        discovery_tools=[
            ListTools(default_detail="brief"),
            Search(name="tool_search", default_detail="detailed", default_limit=12),
            GetSchemas(default_detail="detailed"),
            GetTags(default_detail="brief"),
        ],
        execute_description=EXECUTE_DESCRIPTION,
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
    transform = build_code_mode_transform()
    mcp.add_transform(transform)
    mcp.add_middleware(ExecuteCallShapeMiddleware(transform.execute_tool_name))
    mcp.add_middleware(GetSchemaArgumentShapeMiddleware())
    return mcp
