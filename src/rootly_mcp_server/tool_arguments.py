"""Argument repair and legible argument errors for curated (non-OpenAPI) tools.

Models call curated tools with the raw Rootly API's query shapes --
``filter[status]``, ``page[size]``, ``filter_started_at={"gte": ...}`` -- or
with near-miss names such as ``search`` for ``query``. pydantic then rejects
the whole call with a bare "Unexpected keyword argument".

:func:`normalize_tool_arguments` rewrites the unambiguous shapes, driven by the
tool's real input schema: a rewrite happens only when its target is a declared
parameter, the source key is not, and the caller did not set the target
explicitly. Whatever is still unknown afterwards is described by
:func:`unknown_arguments_message`, in the same style Code Mode uses for a
failed ``call_tool`` (see ``code_mode._legible_call_error``).
"""

from __future__ import annotations

import difflib
import json
import re
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import AliasChoices
from pydantic.fields import FieldInfo

# Parameter names models send that a Rootly tool spells differently, mapped
# to the real names to suggest, in preference order (the first one the tool
# declares wins). Observed in production Code Mode failures. An empty tuple
# means "no equivalent": it also stops the fuzzy fallback, which would
# otherwise suggest started_after for updated_after -- a different filter.
_START_SYNONYMS = ("started_after", "start_date", "from_date")
_END_SYNONYMS = ("started_before", "end_date", "to_date")
_LIMIT_SYNONYMS = ("page_size", "max_results")
PARAMETER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "start_time": _START_SYNONYMS,
    "start_date": _START_SYNONYMS,
    "start": _START_SYNONYMS,
    "from": _START_SYNONYMS,
    "since": _START_SYNONYMS,
    "end_time": _END_SYNONYMS,
    "end_date": _END_SYNONYMS,
    "end": _END_SYNONYMS,
    "to": _END_SYNONYMS,
    "until": _END_SYNONYMS,
    "max_results": ("page_size",),
    "limit": _LIMIT_SYNONYMS,
    "per_page": _LIMIT_SYNONYMS,
    "count": _LIMIT_SYNONYMS,
    "updated_after": (),
    "updated_before": (),
}

# Names that are rewritten, not just suggested, when the tool lacks the
# source name and declares the target (first declared target wins). Kept to
# the shapes production telemetry shows for list_incidents direct calls;
# ``filter_<x>`` / ``filter[<x>]`` / ``page[<x>]`` are handled generically.
# A tool that declares ``max_results`` itself, or accepts it as a pydantic
# validation alias (list_incidents), never reaches this table: accepted names
# pass through first. The entry covers tools that only take ``page_size``
# (list_shifts, list_incident_post_mortems, ...) and ``filters={...}`` dicts,
# which never see validation aliases.
# start_time/end_time map to the inclusive started_after/started_before,
# which is what an unqualified start/end bound means, or to the shift tools'
# start_date/end_date; starts_at/ends_at (the shift records' own field
# names) only reach the shift tools' range parameters. page_size reaches
# collect_incidents' batch_size, which is sent as page[size], or list_audits'
# max_results. start_date/end_date reach get_shift_incidents' start_time/end_time.
# ends_before never reaches started_before: an incident's end is not its start.
ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "search": ("query",),
    "start_time": ("started_after", "start_date"),
    "end_time": ("started_before", "end_date"),
    "start_date": ("started_after", "start_time", "from_date"),
    "end_date": ("started_before", "end_time", "to_date"),
    "starts_at": ("start_date", "from_date"),
    "ends_at": ("end_date", "to_date"),
    "starts_after": ("started_after", "from_date", "start_date"),
    "ends_before": ("to_date", "end_date"),
    "services": ("service_ids",),
    "max_results": ("page_size",),
    "page_size": ("batch_size", "max_results"),
    "page": ("page_number",),
}

# Members a JSON:API resource object may carry besides its attributes.
_JSONAPI_ENVELOPE_KEYS = frozenset({"type", "id", "attributes", "relationships"})
# Keys that carry a dict of filters (``filters={"status": ...}``).
_FILTER_CONTAINERS = ("filters", "filter")
# Range operators of the Rootly API's ``filter[<field>_at][gte]`` shape.
# Only the inclusive ones are rewritten: <field>_after / <field>_before are
# inclusive (>= / <=), so mapping a strict gt / lt onto them would silently
# widen the range by its boundary. Strict ones are rejected with a hint.
_LOWER_BOUND_OPS = frozenset({"gte"})
_UPPER_BOUND_OPS = frozenset({"lte"})
_STRICT_OPS = {"gt": ("lower", "after", "next"), "lt": ("upper", "before", "previous")}
_RANGE_OPS = _LOWER_BOUND_OPS | _UPPER_BOUND_OPS | frozenset(_STRICT_OPS)
_BRACKET_KEY = re.compile(r"^(filter|page)\[([^\[\]]+)\](?:\[([^\[\]]+)\])?$")
_OP_SUFFIX = re.compile(r"^(.+)_(gte|gt|lte|lt)$")

# Explanations for arguments that look like a filter the tool does not have.
# Keyed by the bare field name (after stripping filter_/filter[...] and ops).
_NO_EQUIVALENT_HINTS: dict[str, str] = {
    "updated_at": "there is no updated-time filter",
    "updated_after": "there is no updated-time filter",
    "updated_before": "there is no updated-time filter",
    "created_at": "there is no created-time filter",
    "created_after": "there is no created-time filter",
    "created_before": "there is no created-time filter",
    "include": "related records cannot be side-loaded; the response shape is fixed",
}

# Tools whose rejected arguments belong to a sibling tool, named in the error.
_SIBLING_TOOL_HINTS: dict[str, str] = {
    "search_incidents": (
        " search_incidents only runs free-text lookups; use list_incidents (one page) "
        "or collect_incidents (many pages) to filter by teams, status, severity or "
        "started_after/started_before."
    ),
}

# Fuzzy did-you-mean threshold. difflib's default (0.6) pairs short unrelated
# names such as types/teams; real typos score well above 0.7.
_SUGGESTION_CUTOFF = 0.7


@dataclass
class NormalizedArguments:
    """Result of :func:`normalize_tool_arguments`."""

    arguments: dict[str, Any]
    # Human-readable rewrites, e.g. ``"filter[status]->status"``.
    rewrites: list[str] = field(default_factory=list)
    # Keys still not declared by the tool, with a hint for each (may be "").
    unknown: dict[str, str] = field(default_factory=dict)


def validation_aliases(fn: Callable[..., Any] | None) -> dict[str, str]:
    """Map every pydantic validation alias of ``fn``'s parameters to the parameter.

    Curated tools accept extra names through ``Annotated[..., Field(
    validation_alias=AliasChoices(...))]`` (e.g. ``list_shifts(start_date=)``
    for ``from_date``). FastMCP validates with them but the advertised JSON
    schema omits them, so they are read from the function's own annotations.
    """
    if fn is None:
        return {}
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:
        return {}
    aliases: dict[str, str] = {}
    for parameter, hint in hints.items():
        for metadata in getattr(hint, "__metadata__", ()):
            if not isinstance(metadata, FieldInfo):
                continue
            for alias in (metadata.alias, metadata.validation_alias):
                choices: list[Any] = (
                    list(alias.choices) if isinstance(alias, AliasChoices) else [alias]
                )
                for choice in choices:
                    if isinstance(choice, str) and choice != parameter:
                        aliases.setdefault(choice, parameter)
    return aliases


def _strict_bound_hint(op: str, lower: str | None, upper: str | None) -> str:
    side, word, instant = _STRICT_OPS[op]
    target = lower if side == "lower" else upper
    if target is None:
        return ""
    symbol = ">=" if side == "lower" else "<="
    return (
        f"{target} is inclusive ({symbol}); there is no strict-{word} filter -- pass the "
        f"{instant} instant to {target}, or use {op}e"
    )


def _properties(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    properties = (schema or {}).get("properties")
    return properties if isinstance(properties, dict) else {}


def suggest_parameter(name: str, properties: Sequence[str]) -> str | None:
    """Return the real parameter a mistyped ``name`` most likely meant."""
    synonyms = PARAMETER_SYNONYMS.get(name)
    if synonyms is not None:
        for candidate in synonyms:
            if candidate in properties:
                return candidate
        if not synonyms:
            return None
    matches = difflib.get_close_matches(name, list(properties), n=1, cutoff=_SUGGESTION_CUTOFF)
    return matches[0] if matches else None


def valid_parameters_line(tool_name: str, schema: Mapping[str, Any] | None) -> str:
    """``Valid parameters for <tool> (* = required): a*, b`` or "" without properties."""
    names = list(_properties(schema))
    if not names:
        return ""
    required = set((schema or {}).get("required") or [])
    listed = ", ".join(name + ("*" if name in required else "") for name in names)
    return f"Valid parameters for {tool_name} (* = required): {listed}"


def _parse_key(key: str) -> tuple[str, str | None]:
    """Split an API-shaped key into (field, range operator).

    ``filter[started_at][gte]`` -> ("started_at", "gte"); ``page[size]`` ->
    ("page_size", None); ``filter_started_at_gte`` -> ("started_at", "gte");
    ``filter_status`` -> ("status", None); anything else -> (key, op-suffix).
    """
    bracket = _BRACKET_KEY.match(key)
    if bracket:
        kind, inner, op = bracket.groups()
        if kind == "page":
            return (f"page_{inner}", None) if op is None else (key, None)
        return inner, op
    base = key[len("filter_") :] if key.startswith("filter_") and len(key) > 7 else key
    suffix = _OP_SUFFIX.match(base)
    if suffix:
        return suffix.group(1), suffix.group(2)
    return base, None


def _range_targets(base: str, properties: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(lower, upper) parameters for a ``<field>_at`` range, when the tool has them."""
    if not base.endswith("_at"):
        return None, None
    stem = base[: -len("_at")]
    lower, upper = f"{stem}_after", f"{stem}_before"
    return (lower if lower in properties else None, upper if upper in properties else None)


def _names_own_resource(key: str, tool_name: str | None, properties: Mapping[str, Any]) -> bool:
    """True for ``schedule_id`` on ``get_schedule_shifts``: the tool's ``id`` is that resource."""
    if not tool_name or "id" not in properties or not key.endswith("_id"):
        return False
    return key[: -len("_id")] in tool_name.split("_")


def _own_resource_id(tool_name: str | None, properties: Mapping[str, Any]) -> str | None:
    """``incident_id`` for a bare ``id`` on ``update_incident``, when exactly one matches."""
    if not tool_name:
        return None
    candidates = [f"{token}_id" for token in tool_name.split("_") if f"{token}_id" in properties]
    return candidates[0] if len(candidates) == 1 else None


def _resolve(
    key: str, value: Any, properties: Mapping[str, Any], tool_name: str | None = None
) -> tuple[list[tuple[str, Any]] | None, str]:
    """Map one argument to [(declared parameter, value), ...], or None plus a hint."""
    base, op = _parse_key(key)
    lower, upper = _range_targets(base, properties)

    if op is not None:
        if op not in _RANGE_OPS:
            return None, ""
        if op in _STRICT_OPS:
            return None, _strict_bound_hint(op, lower, upper) or _NO_EQUIVALENT_HINTS.get(base, "")
        target = lower if op in _LOWER_BOUND_OPS else upper
        if target is None:
            return None, _NO_EQUIVALENT_HINTS.get(base, "")
        return [(target, value)], ""

    if isinstance(value, dict) and value and set(value) <= _RANGE_OPS:
        if lower is None and upper is None:
            return None, _NO_EQUIVALENT_HINTS.get(base, "")
        pairs: list[tuple[str, Any]] = []
        for range_op, bound in value.items():
            if range_op in _STRICT_OPS:
                return None, _strict_bound_hint(range_op, lower, upper) or (
                    f"{range_op!r} has no matching parameter"
                )
            target = lower if range_op in _LOWER_BOUND_OPS else upper
            if target is None:
                return None, f"{range_op!r} has no matching parameter"
            pairs.append((target, bound))
        return pairs, ""

    if base in properties:
        return [(base, value)], ""
    for candidate in ARGUMENT_ALIASES.get(base, ()):
        if candidate in properties:
            return [(candidate, value)], ""
    if _names_own_resource(base, tool_name, properties):
        return [("id", value)], ""
    if base == "id" and (own_id := _own_resource_id(tool_name, properties)):
        return [(own_id, value)], ""

    # A tool's own range parameters outrank the generic "no such filter" hints:
    # list_incident_post_mortems does have created_after/created_before.
    if lower or upper:
        options = " and/or ".join(repr(name) for name in (lower, upper) if name)
        return None, f"use {options} with an ISO 8601 timestamp"
    if base in _NO_EQUIVALENT_HINTS:
        return None, _NO_EQUIVALENT_HINTS[base]
    suggestion = suggest_parameter(base, list(properties))
    if suggestion is None and base != key:
        suggestion = suggest_parameter(key, list(properties))
    return None, f"did you mean {suggestion!r}?" if suggestion else ""


def _is_comma_separated_string(spec: Any) -> bool:
    if not isinstance(spec, dict) or spec.get("type") != "string":
        return False
    return "comma-separated" in str(spec.get("description", "")).lower()


def normalize_tool_arguments(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    accepted_aliases: Mapping[str, str] | None = None,
    tool_name: str | None = None,
) -> NormalizedArguments:
    """Rewrite unambiguous argument aliases against ``schema`` (a JSON schema).

    ``accepted_aliases`` maps names the tool already accepts (pydantic
    validation aliases, see :func:`validation_aliases`) to their parameter;
    those are passed through untouched, never rewritten or rejected.
    ``tool_name`` lets ``<resource>_id`` reach ``id`` when the name is that
    resource's (``schedule_id`` on ``get_schedule_shifts``).

    Never overwrites a parameter the caller set: an alias whose target is
    already present is dropped when it carries the same value and otherwise
    reported as unknown, with the conflict named.
    """
    properties = _properties(schema)
    accepted = dict(accepted_aliases or {})
    result = NormalizedArguments(arguments={})
    # Values the caller set per parameter, whichever accepted name they used.
    provided: dict[str, Any] = {}
    # Two accepted names for one parameter (max_results and page_size on
    # list_incidents) make pydantic reject the call as an unexpected keyword.
    # Keep the parameter's own name, else the first alias; drop a duplicate
    # with the same value and report one that disagrees.
    known = sorted(
        (k for k in arguments if k in properties or k in accepted),
        key=lambda k: k not in properties,
    )
    for key in known:
        parameter = accepted.get(key, key)
        if parameter not in provided:
            result.arguments[key] = arguments[key]
            provided[parameter] = arguments[key]
        elif provided[parameter] == arguments[key]:
            result.rewrites.append(f"{key}->{parameter}:duplicate")
        else:
            kept = next(k for k in result.arguments if accepted.get(k, k) == parameter)
            result.unknown[key] = f"conflicts with {kept!r}, which is also set"

    def assign(source: str, pairs: list[tuple[str, Any]]) -> bool:
        conflicts = [
            target for target, value in pairs if target in provided and provided[target] != value
        ]
        if conflicts:
            names = ", ".join(repr(name) for name in conflicts)
            result.unknown[source] = f"conflicts with {names}, which is also set"
            return False
        for target, value in pairs:
            if target not in provided:
                result.arguments[target] = value
                provided[target] = value
            result.rewrites.append(f"{source}->{target}")
        return True

    for key, value in arguments.items():
        if key in properties or key in accepted:
            continue
        if key in _FILTER_CONTAINERS and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                pairs, hint = _resolve(str(sub_key), sub_value, properties, tool_name)
                label = f"{key}.{sub_key}"
                if pairs is None:
                    result.unknown[label] = hint
                else:
                    assign(label, pairs)
            continue
        pairs, hint = _resolve(key, value, properties, tool_name)
        if pairs is None:
            result.unknown[key] = hint
        else:
            assign(key, pairs)

    for key, value in list(result.arguments.items()):
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str | int) and not isinstance(item, bool) for item in value)
            and _is_comma_separated_string(properties.get(key))
        ):
            result.arguments[key] = ",".join(str(item) for item in value)
            result.rewrites.append(f"{key}:list->csv")
    return result


def unknown_arguments_message(
    tool_name: str, schema: Mapping[str, Any] | None, unknown: Mapping[str, str]
) -> str:
    """``<tool> does not accept 'x' (hint), 'y'.`` plus the valid parameter list."""
    described = ", ".join(
        repr(name) + (f" ({hint})" if hint else "") for name, hint in unknown.items()
    )
    message = f"{tool_name} does not accept {described}."
    properties = _properties(schema)
    sort = properties.get("sort")
    if (
        any("updated" in name for name in unknown)
        and isinstance(sort, dict)
        and "-updated_at" in (sort.get("enum") or [])
    ):
        message += " To find recently updated records, pass sort='-updated_at'."
    if (
        any("created" in name for name in unknown)
        and isinstance(sort, dict)
        and "-created_at" in (sort.get("enum") or [])
    ):
        message += " To find recently created records, pass sort='-created_at'."
    message += _SIBLING_TOOL_HINTS.get(tool_name, "")
    line = valid_parameters_line(tool_name, schema)
    return f"{message}\n{line}" if line else message


def _jsonapi_data_schema(schema: Mapping[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    """(single ``data.type`` enum value, ``data.attributes`` properties) of a write body."""
    data = _properties(schema).get("data")
    if not isinstance(data, dict):
        return None, {}
    data_properties = _properties(data)
    type_enum = (data_properties.get("type") or {}).get("enum") or []
    resource_type = type_enum[0] if len(type_enum) == 1 else None
    return resource_type, _properties(data_properties.get("attributes"))


class MalformedJsonApiBody(ValueError):
    """A write tool's ``data`` arrived as a string that does not parse as JSON."""


def repair_jsonapi_body(
    arguments: Mapping[str, Any], schema: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Fix unambiguous JSON:API envelope mistakes in an OpenAPI write tool's ``data``.

    Models send ``data`` as a JSON string, wrap it twice (``{"data": {"data":
    ...}}``), or put attributes straight on ``data`` -- which Rootly rejects
    with "must be nested under data.attributes". Attributes are moved only
    when every misplaced key is one the schema declares under
    ``data.attributes``; anything else is left for the API to reject.

    A ``data`` string that is not valid JSON raises ``MalformedJsonApiBody``:
    Rootly's 400 for it ("data: Expected hash") echoes the whole string back
    without saying where it broke.
    """
    resource_type, attribute_properties = _jsonapi_data_schema(schema)
    if not attribute_properties:
        return dict(arguments), []
    result = dict(arguments)
    rewrites: list[str] = []
    data = result.get("data")

    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            example = json.dumps({"type": resource_type or "<type>", "attributes": {}})
            raise MalformedJsonApiBody(
                f"`data` must be a JSON object, but it arrived as a string that is not "
                f"valid JSON ({exc.msg} at character {exc.pos} of {len(data)}). Pass "
                f"`data` as an object rather than a string, e.g. {example}."
            ) from None
        if not isinstance(parsed, dict):
            return result, rewrites
        data = parsed
        rewrites.append("data:json->object")
    if isinstance(data, dict) and set(data) == {"data"} and isinstance(data["data"], dict):
        data = data["data"]
        rewrites.append("data.data->data")
    if not isinstance(data, dict):
        return result, rewrites

    misplaced = [key for key in data if key not in _JSONAPI_ENVELOPE_KEYS]
    attributes = data.get("attributes")
    if (
        misplaced
        and all(key in attribute_properties for key in misplaced)
        and (attributes is None or isinstance(attributes, dict))
    ):
        merged = dict(attributes or {})
        if any(key in merged and merged[key] != data[key] for key in misplaced):
            return {**result, "data": data}, rewrites
        merged.update({key: data[key] for key in misplaced})
        data = {key: value for key, value in data.items() if key not in misplaced}
        data["attributes"] = merged
        rewrites.extend(f"data.{key}->data.attributes.{key}" for key in misplaced)
    if "attributes" in data and "type" not in data and resource_type is not None:
        data = {"type": resource_type, **data}
        rewrites.append("data.type")
    result["data"] = data
    return result, rewrites
