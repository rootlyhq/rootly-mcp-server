"""Contract check: curated tools only read response fields the bundled spec declares.

The request side (``test_request_contract.py``) proves every request a curated
tool sends is one ``data/swagger.json`` declares. This is the other half: what
the tool does with the answer. Reading a key the API never returns is just as
silent as sending a filter it ignores -- ``.get()`` hands back ``None`` and the
tool quietly reports "unknown" or drops a whole section. Past examples:

* reading a ``schedule`` relationship on shifts, which shifts do not have;
* reading ``on_call_role`` from the shift rather than the included user (#260);
* reading an alert ``payload`` key that does not exist (4923cf9);
* crashing on a null relationship or a non-string severity (84aca06, 4ab3b9e).

Unit tests that hand-write a response cannot catch these: the fixture contains
exactly what the code reads, bug included. Here the fixtures come from the spec.

How it works
------------

Every scenario from ``test_request_contract.SCENARIOS`` is re-run through the
real ``AuthenticatedHTTPXClient`` on an ``httpx2.MockTransport``. Each request
is answered with a JSON:API body built from that operation's response schema:

* ``full`` -- every declared property populated with a schema-driven example,
  with the request-contract stub's semantic values (ids, time windows, names)
  overlaid where the schema declares those keys. Keys the schema does not
  declare are dropped, so a fixture never contains a key just because the code
  reads it. ``included`` resources are typed: an included ``users`` entry
  follows the ``users`` resource schema. A JSON:API sparse fieldset sent with
  the request (``fields[incidents]=a,b``) trims the attributes as the API does.
* ``nulled`` -- optional objects present but recursively sparse; every
  nullable property ``null`` and every other optional scalar or list absent.
  Reaches nested nulls such as ``{"user": {"data": null}}`` (84aca06).
* ``sparse`` -- required properties only (required nullables ``null``).

Scenario arguments that name a stub value (``INC-4460``, ``severity=critical``)
keep it in the sparse variants (``SPARSE_KEEP``), so the tool gets past lookup.

Each body is validated with ``jsonschema`` against the operation's response
schema (OpenAPI 3.0 translated to JSON Schema: ``$ref`` resolved against
``components``, ``nullable`` as a ``null`` alternative).

``response.json()`` returns the body wrapped in tracking ``dict``/``list``
subclasses. Every named read -- ``x[key]``, ``x.get(key)``, ``key in x``,
``x.setdefault``/``x.pop`` -- is checked against the keys the schema declares
at that position (narrowed to the requested sparse fieldset, if any). An
undeclared read is a violation, reported with the tool, endpoint, key path
(``data[].relationships.on_call_role``, ``included[users].attributes.name``),
the source line doing the read and the keys declared there. Free-form objects
(``additionalProperties``, or nothing declared) are not checked below that
point.

Checks:

* every variant: no undeclared reads (sparse variants reach ``x or y``
  fallbacks a full response never takes), and every body is schema-valid;
* ``full``: no tool errors (a tool that bailed out early would hide reads),
  and every requested ``fields[type]`` value is a declared attribute of that
  type;
* ``nulled`` / ``sparse``: the tool returns a structured result and no
  ``TypeError``/``AttributeError``/``KeyError``/``IndexError`` reached its error
  handler or escaped -- it tolerates nulls and missing optional fields.

Allowlists, each entry with a reason:

* ``RESPONSE_SPEC_GAPS`` / ``RESPONSE_SCHEMA_OVERRIDES`` -- the read is right
  and the spec is incomplete; cite the Rails serializer (rootlyhq/rootly).
* ``DEFENSIVE_READS`` -- probes for shapes the API does not produce, made only
  as a fallback beside the declared shape. Harmless, accepted.
* ``KNOWN_RESPONSE_VIOLATIONS`` / ``KNOWN_FIELDSET_VIOLATIONS`` /
  ``KNOWN_SPARSE_CRASHES`` -- the tool is wrong and needs a code fix. Each is
  its own strict ``xfail``, so fixing it turns the test red until the entry
  is removed.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import difflib
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_request_contract as request_contract  # noqa: E402
from test_request_contract import (  # noqa: E402
    BASE_URL,
    PACKAGE_DIR,
    SCENARIOS,
    SPEC,
    SWAGGER_PATH,
    FakeMCP,
)

from rootly_mcp_server.och_client import OnCallHealthClient  # noqa: E402
from rootly_mcp_server.tools import (  # noqa: E402
    register_alert_tools,
    register_audit_tools,
    register_incident_tools,
    register_investigation_tools,
    register_oncall_tools,
    register_resource_handlers,
)
from rootly_mcp_server.transport import AuthenticatedHTTPXClient  # noqa: E402

RAW_SPEC: dict[str, Any] = json.loads(SWAGGER_PATH.read_text())
COMPONENTS: dict[str, Any] = RAW_SPEC["components"]["schemas"]
INCLUDED_REF = "#/components/schemas/jsonapi_included_resource"

ContractKey = tuple[str, str, str]  # (METHOD, path template, key path)

# --------------------------------------------------------------------------- #
# Allowlists
# --------------------------------------------------------------------------- #
# Key paths use `.` between keys, `[]` for a list item and `[<type>]` for a
# typed JSON:API `included` entry. Matching is exact: no wildcards, so an
# entry never hides a neighbouring typo.
# Rails citations are rootlyhq/rootly master.


@dataclass(frozen=True)
class SpecGap:
    reason: str
    # When the undocumented key is an object, the keys Rails renders inside it.
    # Reads below it are then checked against these instead of going unchecked.
    declares: frozenset[str] | None = None


RESPONSE_SPEC_GAPS: dict[ContractKey, SpecGap] = {
    # The spec's incident_response declares no `relationships`, but
    # Api::V1::IncidentSerializer renders them
    # (app/serializers/api/v1/incident_serializer.rb:201-226).
    ("GET", "/v1/incidents/{id}", "data.relationships"): SpecGap(
        "IncidentSerializer has_one/has_many associations; undeclared in swagger",
        frozenset(
            {
                "sub_status",
                "incident_post_mortem",
                "sub_statuses",
                "causes",
                "subscribers",
                "roles",
                "environments",
                "incident_types",
                "services",
                "functionalities",
                "groups",
                "events",
                "action_items",
                "custom_field_selections",
                "feedbacks",
                "attachments",
                "slack_messages",
                "alerts",
            }
        ),
    ),
    # Api::V1::AuditSerializer `attributes ... :source`
    # (app/serializers/api/v1/audit_serializer.rb:4-9); missing from `audit`.
    ("GET", "/v1/audits", "data[].attributes.source"): SpecGap(
        "AuditSerializer renders :source; undeclared in swagger"
    ),
    # /v1/post_mortems is Api::V1::IncidentPostMortemController#index
    # (config/routes/api.rb:119), rendering IncidentPostMortemSerializer
    # (app/controllers/api/v1/incident_post_mortem_controller.rb:4-6), whose
    # attributes include :incident_id and :published_at
    # (app/serializers/api/v1/incident_post_mortem_serializer.rb:4-11).
    ("GET", "/v1/post_mortems", "data[].attributes.incident_id"): SpecGap(
        "IncidentPostMortemSerializer renders :incident_id; undeclared in swagger"
    ),
    ("GET", "/v1/post_mortems", "data[].attributes.published_at"): SpecGap(
        "IncidentPostMortemSerializer renders :published_at; undeclared in swagger"
    ),
    # Api::V1::UserSerializer `attributes :id, :name, ...`
    # (app/serializers/api/v1/user_serializer.rb:2-3); `user` in swagger lists
    # full_name/first_name/last_name but not name. Read as full_name's fallback.
    ("GET", "/v1/users", "data[].attributes.name"): SpecGap(
        "UserSerializer renders :name; undeclared in swagger"
    ),
    # Live-transcribed segments are stored as Recall's `bot.transcription`
    # payload (`data.transcript`) verbatim, words aside
    # (app/controllers/webhooks/incoming/recall_webhooks_controller.rb:44-67,
    # MeetingRecording#append_transcript_segment!), and that payload carries
    # `language`. Desktop imports build {speaker, words} only
    # (app/jobs/meeting_recordings/import_recall_desktop_recording_job.rb:89-97),
    # which is all meeting_recording_transcript_segment declares. Optional read.
    ("GET", "/v1/meeting_recordings/{id}", "data.attributes.transcript[].language"): SpecGap(
        "Recall live-transcription segments are stored verbatim and include language"
    ),
}

# Operations whose response schema depends on the request. Keyed by (METHOD,
# path template, include value that switches it); value is (component name,
# reason).
RESPONSE_SCHEMA_OVERRIDES: dict[ContractKey, tuple[str, str]] = {
    # MeetingRecordingsController#show switches to
    # MeetingRecordingDetailSerializer (which adds :transcript) when
    # `include` names `transcript` (app/controllers/api/v1/
    # meeting_recordings_controller.rb:22-25; app/serializers/api/v1/
    # meeting_recording_detail_serializer.rb:4). swagger.json has the matching
    # meeting_recording_detail_response component but the operation's 200
    # references meeting_recording_response only.
    ("GET", "/v1/meeting_recordings/{id}", "transcript"): (
        "meeting_recording_detail_response",
        "include=transcript renders MeetingRecordingDetailSerializer",
    ),
}

# Fallback probes for shapes the API does not produce, read only beside the
# declared shape. Harmless dead branches; accepted.
# IncidentSerializer#severity is render_nested(SeveritySerializer)
# (incident_serializer.rb:141-143, app/serializers/api/v1/nested_rendering.rb),
# i.e. the JSON:API document {"data": {"id", "type", "attributes": {...}}} the
# spec declares, and both normalizers do read data.attributes.{name,slug}.
# Before that they probe flat keys for other shapes:
# * _extract_incident_severity (tools/incidents.py) tries severity.slug/.name;
# * _normalize_incident_severity (tools/oncall.py) tries name, slug, label,
#   value, severity, title at each level, then descends into `attributes` and
#   `data` -- so severity.<probe>, severity.attributes and
#   severity.data.<probe>.
# Exactly those paths are accepted; a misspelling anywhere else under severity
# (e.g. severity.data.attributes.nmae) is still a violation.
_SEVERITY_PROBES = ("name", "slug", "label", "value", "severity", "title")
_SEVERITY = "data[].attributes.severity"
DEFENSIVE_READS: dict[ContractKey, str] = {
    ("GET", "/v1/incidents", path): (
        "multi-shape severity normalizers probe flat keys before data.attributes"
    )
    for path in (
        *(f"{_SEVERITY}.{probe}" for probe in _SEVERITY_PROBES),
        f"{_SEVERITY}.attributes",
        *(f"{_SEVERITY}.data.{probe}" for probe in _SEVERITY_PROBES),
    )
}

# Reads of keys the API does not return. Each is a strict xfail test.
KNOWN_RESPONSE_VIOLATIONS: dict[ContractKey, str] = {}

# Requested sparse-fieldset values that are not attributes of the type, keyed
# by (resource type, field). Each is a strict xfail test.
KNOWN_FIELDSET_VIOLATIONS: dict[tuple[str, str], str] = {}

# (mode, scenario) pairs whose tool crashes on a schema-valid sparse response.
# Each is a strict xfail test.
KNOWN_SPARSE_CRASHES: dict[tuple[str, str], str] = {}

# Exceptions that mean the tool mishandled a value's shape, as opposed to
# reporting a legitimate condition (no data, bad input).
CRASH_TYPES = (TypeError, AttributeError, KeyError, IndexError)

# Sparse fixtures keep these stub values even when optional, because the
# scenario's own arguments refer to them (INC-4460, severity="critical").
# (path template or "*", key)
SPARSE_KEEP = {("*", "sequential_id"), ("/v1/severities", "severity")}


def _key_matches(pattern: ContractKey, key: ContractKey) -> bool:
    if pattern[:2] != key[:2]:
        return False
    return pattern[2] == key[2]


def _matching(allowlist: Iterable[ContractKey], key: ContractKey) -> ContractKey | None:
    return next((pattern for pattern in allowlist if _key_matches(pattern, key)), None)


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #


def _resolve(node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        target: Any = RAW_SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


@dataclass
class ObjectShape:
    """The keys a schema position declares."""

    properties: dict[str, Any]
    required: set[str]
    open: bool

    @property
    def declared(self) -> frozenset[str] | None:
        """``None`` for a free-form object, whose keys are not checked."""
        return None if self.open else frozenset(self.properties)


def _shape(node: Any, depth: int = 0) -> ObjectShape:
    node = _resolve(node)
    if not isinstance(node, dict) or depth > 20:
        return ObjectShape({}, set(), True)
    properties = dict(node.get("properties") or {})
    required = set(node.get("required") or [])
    additional = node.get("additionalProperties")
    is_open = bool(additional)
    for combinator in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(combinator) or []:
            resolved = _resolve(sub)
            if resolved.get("type") not in (None, "object") or resolved.get("maxProperties") == 0:
                continue
            inner = _shape(resolved, depth + 1)
            for key, value in inner.properties.items():
                properties.setdefault(key, value)
            if combinator == "allOf":
                required |= inner.required
            is_open |= inner.open
    if not properties and node.get("maxProperties") != 0:
        # `{"type": "object"}` with nothing declared is free-form.
        is_open = True
    return ObjectShape(properties, required, is_open)


def _child_schema(node: Any, key: str) -> Any:
    shape = _shape(node)
    if key in shape.properties:
        return shape.properties[key]
    additional = _resolve(node).get("additionalProperties")
    return additional if isinstance(additional, dict) else None


def _items_schema(node: Any) -> Any:
    node = _resolve(node)
    if not isinstance(node, dict):
        return None
    if "items" in node:
        return node["items"]
    for combinator in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(combinator) or []:
            items = _items_schema(sub)
            if items is not None:
                return items
    return None


def _is_included_item(node: Any) -> bool:
    return isinstance(node, dict) and node.get("$ref") == INCLUDED_REF


def _build_type_index() -> dict[str, Any]:
    """JSON:API resource type -> schema of one resource object of that type.

    Taken from the ``*_response`` (else ``*_list``) component whose ``data``
    declares that ``type`` enum, so an ``included`` entry is checked against
    the same schema as a primary resource of its type.
    """
    index: dict[str, tuple[int, Any]] = {}
    for name, schema in COMPONENTS.items():
        data = (schema.get("properties") or {}).get("data")
        if not data:
            continue
        resource = _resolve(data)
        if resource.get("type") == "array":
            resource = _resolve(resource.get("items") or {})
        type_schema = _resolve((resource.get("properties") or {}).get("type") or {})
        rank = 0 if name.endswith("_response") else 1 if name.endswith("_list") else 2
        for type_name in type_schema.get("enum") or []:
            if type_name not in index or rank < index[type_name][0]:
                index[type_name] = (rank, resource)
    return {type_name: resource for type_name, (_, resource) in index.items()}


TYPE_INDEX = _build_type_index()


def _included_schema(item: Any, fallback: Any) -> Any:
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return TYPE_INDEX.get(item["type"], fallback)
    return fallback


def _response_schema(method: str, template: str, includes: Iterable[str] = ()) -> Any:
    for include in includes:
        override = RESPONSE_SCHEMA_OVERRIDES.get((method, template, include))
        if override:
            return {"$ref": f"#/components/schemas/{override[0]}"}
    operation = RAW_SPEC["paths"][template].get(method.lower()) or {}
    for status, response in sorted((operation.get("responses") or {}).items()):
        if not str(status).startswith("2"):
            continue
        content = _resolve(response).get("content") or {}
        for media in content.values():
            if "schema" in media:
                return media["schema"]
    return None


@dataclass(frozen=True)
class RequestShape:
    """What about a request changes the response: includes and sparse fieldsets."""

    includes: tuple[str, ...]
    fieldsets: dict[str, frozenset[str]]

    @classmethod
    def of(cls, request: httpx2.Request) -> RequestShape:
        includes: list[str] = []
        fieldsets: dict[str, frozenset[str]] = {}
        for name, value in request.url.params.multi_items():
            if name == "include":
                includes += [v.strip() for v in value.split(",") if v.strip()]
            elif name.startswith("fields[") and name.endswith("]"):
                fieldsets[name[7:-1]] = frozenset(v.strip() for v in value.split(",") if v.strip())
        return cls(tuple(includes), fieldsets)


# --------------------------------------------------------------------------- #
# OpenAPI 3.0 -> JSON Schema, for validating fixtures
# --------------------------------------------------------------------------- #


def _to_json_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {
        key: _to_json_schema(value)
        for key, value in node.items()
        if key not in {"nullable", "example", "examples", "discriminator", "xml"}
    }
    if node.get("nullable") is True:
        return {"anyOf": [converted, {"type": "null"}]}
    return converted


JSON_SCHEMA_COMPONENTS = _to_json_schema(COMPONENTS)


def _validation_errors(body: Any, schema: Any) -> list[str]:
    root = {"components": {"schemas": JSON_SCHEMA_COMPONENTS}, **_to_json_schema(schema)}
    validator = jsonschema.Draft202012Validator(root)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message[:200]}"
        for error in validator.iter_errors(body)
    ]


# --------------------------------------------------------------------------- #
# Schema-driven fixtures
# --------------------------------------------------------------------------- #

FULL = "full"
# Optional objects kept (recursively sparse), nullables null, other optionals
# absent: reaches `{"user": {"data": null}}`-style nested nulls.
NULLED = "nulled"
# Required properties only; required nullables null.
SPARSE = "sparse"
SPARSE_MODES = (NULLED, SPARSE)
# Populating these would send every paginating tool round a stub that always
# answers with the same page.
_PAGINATION_KEYS = {"next_page", "next_cursor", "next", "prev", "prev_page"}


def _is_object_schema(node: Any) -> bool:
    node = _resolve(node)
    return (
        isinstance(node, dict)
        and node.get("nullable") is not True
        and (node.get("type") == "object" or bool(node.get("properties") or node.get("allOf")))
    )


def _object_branches(node: Any) -> list[Any]:
    """The object alternatives of a ``oneOf``/``anyOf`` object with no properties of its own.

    Such a node (e.g. a rotation's ``schedule_rotationable_attributes``, one
    closed shape per rotation type) is valid only as one branch, so a fixture
    built from the merged properties of every branch matches none of them.
    """
    if not isinstance(node, dict) or node.get("properties"):
        return []
    for combinator in ("oneOf", "anyOf"):
        branches = [_resolve(sub) for sub in node.get(combinator) or []]
        if branches and all(_is_object_schema(b) for b in branches):
            return branches
    return []


def _select_branch(branches: list[Any], base: Any) -> Any:
    """The first branch declaring every key of ``base`` with a matching value."""
    if isinstance(base, dict):
        for branch in branches:
            properties = _shape(branch).properties
            if all(k in properties and _matches_type(v, properties[k]) for k, v in base.items()):
                return branch
    return branches[0]


def _example(node: Any, mode: str, key: str = "", depth: int = 0) -> Any:
    """A schema-valid example: every property (full), or required ones (sparse modes)."""
    node = _resolve(node)
    if not isinstance(node, dict) or depth > 12:
        return None
    branches = _object_branches(node)
    if branches:
        return _example(branches[0], mode, key, depth + 1)
    if node.get("nullable") is True and (mode in SPARSE_MODES or key in _PAGINATION_KEYS):
        return None
    if node.get("enum"):
        return next((value for value in node["enum"] if value is not None), None)
    if node.get("maxProperties") == 0:
        return {}
    schema_type = node.get("type")
    if schema_type == "array" or (schema_type is None and "items" in node):
        if mode in SPARSE_MODES:
            return []
        item = _example(node.get("items") or {}, mode, key, depth + 1)
        return [] if item is None else [item]
    shape = _shape(node)
    if schema_type == "object" or node.get("properties") or node.get("allOf"):
        result: dict[str, Any] = {}
        for name, sub in shape.properties.items():
            if mode in SPARSE_MODES and name not in shape.required:
                if _resolve(sub).get("nullable") is True:
                    result[name] = None
                elif mode == NULLED and _is_object_schema(sub):
                    result[name] = _example(sub, mode, name, depth + 1)
                continue
            result[name] = _example(sub, mode, name, depth + 1)
        return result
    if schema_type == "string":
        fmt = node.get("format")
        if fmt == "date" or key == "date":
            return datetime.now(UTC).date().isoformat()
        if fmt == "date-time" or key.endswith("_at"):
            return request_contract._iso(datetime.now(UTC))
        return f"example-{key}" if key else "example"
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return False
    for combinator in ("anyOf", "oneOf"):
        for sub in node.get(combinator) or []:
            value = _example(sub, mode, key, depth + 1)
            if value is not None:
                return value
    return None


def _matches_type(value: Any, node: Any) -> bool:
    node = _resolve(node)
    if not isinstance(node, dict):
        return True
    if value is None:
        return node.get("nullable") is True
    if node.get("enum") and value not in node["enum"]:
        return False
    checks: dict[str, Callable[[Any], bool]] = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    check = checks.get(node.get("type") or "")
    return check(value) if check else True


@dataclass(frozen=True)
class Where:
    method: str
    template: str
    path: str = ""

    def child(self, segment: str) -> Where:
        joined = f"{self.path}{segment}" if segment.startswith("[") else f"{self.path}.{segment}"
        return Where(self.method, self.template, joined.lstrip("."))

    def gap(self) -> SpecGap | None:
        return RESPONSE_SPEC_GAPS.get((self.method, self.template, self.path))


def _sparse_keeps(where: Where, key: str) -> bool:
    return ("*", key) in SPARSE_KEEP or (where.template, key) in SPARSE_KEEP


def _conform(base: Any, node: Any, mode: str, where: Where) -> Any:
    """Shape a hand-written stub value to the schema at ``node``.

    Declared keys keep the stub's value (so ids and time windows still line
    up); undeclared keys are dropped unless a SpecGap documents them; missing
    ones come from ``_example``. In the sparse modes only required keys (and
    optional objects, for NULLED) survive, and every nullable is null --
    required or not, whatever the stub says -- except SPARSE_KEEP values.
    Routing still works because the stub values the tools match on are
    non-nullable in the spec and kept: resource ``id``/``type``, shift
    ``starts_at``/``ends_at``/``schedule_id``, relationship ``data.id`` when
    the relationship data is present.
    """
    resolved = _resolve(node)
    if not isinstance(resolved, dict):
        return base
    branches = _object_branches(resolved)
    if branches:
        return _conform(base, _select_branch(branches, base), mode, where)
    key = where.path.rsplit(".", 1)[-1]
    if isinstance(base, list) and _items_schema(resolved) is not None:
        items = _items_schema(resolved)
        if _is_included_item(items):
            return [
                _conform(
                    item, _included_schema(item, items), mode, where.child(f"[{item['type']}]")
                )
                for item in base
            ]
        return [_conform(item, items, mode, where.child("[]")) for item in base]
    if isinstance(base, dict):
        shape = _shape(resolved)
        if shape.open:
            return base
        conformed = _example(resolved, mode, key)
        if not isinstance(conformed, dict):
            conformed = {}
        for name, value in base.items():
            child = where.child(name)
            if name not in shape.properties:
                gap = child.gap()
                if mode == FULL and gap and gap.declares and isinstance(value, dict):
                    conformed[name] = {k: v for k, v in value.items() if k in gap.declares}
                continue
            keep = _sparse_keeps(where, name)
            if mode in SPARSE_MODES and not keep:
                if _resolve(shape.properties[name]).get("nullable") is True:
                    # Nullable stays null even when the stub has a value
                    # (e.g. a shift's required, nullable rotation_id).
                    conformed[name] = None
                    continue
                if name not in shape.required and not (
                    mode == NULLED and _is_object_schema(shape.properties[name])
                ):
                    continue
            conformed[name] = _conform(value, shape.properties[name], mode, child)
        return conformed
    if base is None or not _matches_type(base, resolved):
        if base is None and mode in SPARSE_MODES and resolved.get("nullable") is True:
            return None
        return _example(resolved, FULL if base is not None else mode, key)
    return base


def _ensure_gap_objects(body: Any, where: Where) -> None:
    """Serve every SpecGap object (``declares``) as at least ``{}`` in full mode.

    The fixture drops undeclared keys, so without this a tool reading
    ``relationships`` gets a plain default and its reads inside go unseen.
    """
    for (method, template, path), gap in RESPONSE_SPEC_GAPS.items():
        if (method, template) != (where.method, where.template) or gap.declares is None:
            continue
        *parents, last = path.replace("[]", ".[]").split(".")
        targets = [body]
        for segment in parents:
            nxt: list[Any] = []
            for target in targets:
                if segment == "[]" and isinstance(target, list):
                    nxt.extend(target)
                elif isinstance(target, dict) and isinstance(target.get(segment), dict | list):
                    nxt.append(target[segment])
            targets = nxt
        for target in targets:
            if isinstance(target, dict):
                target.setdefault(last, {})


def _apply_fieldsets(body: Any, fieldsets: dict[str, frozenset[str]]) -> None:
    """Trim resource attributes to the requested sparse fieldsets, as the API does."""
    if not fieldsets or not isinstance(body, dict):
        return
    resources: list[Any] = []
    for section in ("data", "included"):
        value = body.get(section)
        resources.extend(value if isinstance(value, list) else [value])
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        wanted = fieldsets.get(resource.get("type") or "")
        attributes = resource.get("attributes")
        if wanted is not None and isinstance(attributes, dict):
            resource["attributes"] = {k: v for k, v in attributes.items() if k in wanted}


# --------------------------------------------------------------------------- #
# Read tracking
# --------------------------------------------------------------------------- #


@dataclass
class ReadViolation:
    scenario: str
    tool: str
    method: str
    template: str
    parent: str
    key: str
    declared: list[str]
    read_at: str
    fieldset: str | None = None  # set when the schema declares it but fields[...] omits it

    @property
    def key_path(self) -> str:
        return f"{self.parent}.{self.key}" if self.parent else self.key

    @property
    def contract_key(self) -> ContractKey:
        return (self.method, self.template, self.key_path)

    def describe(self) -> str:
        where = self.parent or "<root>"
        if self.fieldset:
            why = f"the schema declares it, but the request's {self.fieldset} omits it"
        elif len(self.declared) <= 25:
            why = f"declared at {where}: {self.declared}"
        else:
            closest = difflib.get_close_matches(self.key, self.declared, n=8, cutoff=0.0)
            why = f"closest of {len(self.declared)} declared at {where}: {closest}"
        return (
            f"[{self.scenario}] {self.tool} read {self.key_path!r} from "
            f"{self.method} {self.template} at {self.read_at}; {why}"
        )


@dataclass
class TrackContext:
    scenario: str
    tool: str
    method: str
    template: str
    violations: list[ReadViolation]
    fieldsets: dict[str, frozenset[str]] = field(default_factory=dict)


def _read_site() -> str:
    """The innermost rootly_mcp_server frame performing the read."""
    frame: Any = sys._getframe(3)
    while frame is not None:
        filename = Path(frame.f_code.co_filename)
        if PACKAGE_DIR in filename.parents:
            return f"{filename.relative_to(PACKAGE_DIR)}:{frame.f_lineno}"
        frame = frame.f_back
    return "<unknown>"


class TrackedDict(dict):
    """A JSON object that records reads of keys its schema does not declare."""

    _ctx: TrackContext | None = None
    _path: str = ""
    _declared: frozenset[str] | None = None
    _fieldset: tuple[str, frozenset[str]] | None = None

    def _check(self, key: Any) -> None:
        ctx = self._ctx
        if ctx is None or self._declared is None or not isinstance(key, str):
            return
        fieldset: str | None = None
        if key in self._declared:
            if self._fieldset is None or key in self._fieldset[1]:
                return
            fieldset = f"fields[{self._fieldset[0]}]"
        ctx.violations.append(
            ReadViolation(
                ctx.scenario,
                ctx.tool,
                ctx.method,
                ctx.template,
                self._path,
                key,
                sorted(self._declared),
                _read_site(),
                fieldset,
            )
        )

    def __getitem__(self, key: Any) -> Any:
        self._check(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._check(key)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        self._check(key)
        return super().__contains__(key)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._check(key)
        return super().setdefault(key, default)

    def pop(self, key: Any, *default: Any) -> Any:
        self._check(key)
        return super().pop(key, *default)


class TrackedList(list):
    """A JSON array whose items are tracked; it has no keys of its own to check."""


def _track(value: Any, node: Any, where: Where, ctx: TrackContext) -> Any:
    if isinstance(value, list):
        items = _items_schema(node) if node is not None else None
        tracked_list = TrackedList()
        for item in value:
            if _is_included_item(items) and isinstance(item, dict) and "type" in item:
                tracked_list.append(
                    _track(
                        item, _included_schema(item, items), where.child(f"[{item['type']}]"), ctx
                    )
                )
            else:
                tracked_list.append(_track(item, items, where.child("[]"), ctx))
        return tracked_list
    if not isinstance(value, dict):
        return value
    tracked = TrackedDict()
    gap = where.gap()
    declared = (
        gap.declares
        if gap is not None and gap.declares is not None
        else _shape(node).declared
        if node is not None
        else None
    )
    for key, child in value.items():
        child_schema = _child_schema(node, key) if node is not None else None
        dict.__setitem__(tracked, key, _track(child, child_schema, where.child(key), ctx))
    resource_type = value.get("type")
    if isinstance(resource_type, str) and resource_type in ctx.fieldsets:
        attributes = dict.get(tracked, "attributes")
        if isinstance(attributes, TrackedDict):
            attributes._fieldset = (resource_type, ctx.fieldsets[resource_type])
    tracked._ctx = ctx
    tracked._path = where.path
    tracked._declared = declared
    return tracked


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

_mode: contextvars.ContextVar[str] = contextvars.ContextVar("response_mode")


@dataclass
class ServedResponse:
    method: str
    template: str
    shape: RequestShape
    schema_errors: list[str]


@dataclass
class ResponseRun:
    scenario: str
    tool: str
    mode: str
    served: list[ServedResponse] = field(default_factory=list)
    violations: list[ReadViolation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    crashes: list[str] = field(default_factory=list)
    result: Any = None


class CapturingMCPError:
    """Records each shape-mishandling exception a tool catches, and each tool error."""

    def __init__(self, run: ResponseRun) -> None:
        self._run = run

    def categorize_error(self, exception: Exception) -> tuple[str, str]:
        if isinstance(exception, CRASH_TYPES):
            self._run.crashes.append(f"{type(exception).__name__}: {exception}")
        return (type(exception).__name__, str(exception))

    def tool_error(
        self,
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._run.errors.append(f"{error_type}: {error_message}")
        return {"error": True, "error_type": error_type, "message": error_message}


_validation_cache: dict[str, list[str]] = {}


def _fixture_body(
    request: httpx2.Request, template: str, shape: RequestShape, mode: str
) -> tuple[Any, list[str]]:
    base = request_contract._stub_response(request, template).json()
    schema = _response_schema(request.method, template, shape.includes)
    if schema is None:
        return base, [f"no 2xx JSON response schema for {request.method} {template}"]
    where = Where(request.method, template)
    body = _conform(base, schema, mode, where)
    # Validate before adding unspecced gap objects and trimming to the sparse
    # fieldset: both are deliberate departures from the declared schema.
    cache_key = json.dumps([json.dumps(schema, sort_keys=True), body], sort_keys=True, default=str)
    if cache_key not in _validation_cache:
        _validation_cache[cache_key] = _validation_errors(body, schema)
    body = copy.deepcopy(body)
    if mode == FULL:
        _ensure_gap_objects(body, where)
    _apply_fieldsets(body, shape.fieldsets)
    return body, _validation_cache[cache_key]


def _build_tools(run: ResponseRun) -> dict[str, Any]:
    client = AuthenticatedHTTPXClient(base_url=BASE_URL)

    def handler(request: httpx2.Request) -> httpx2.Response:
        template = SPEC.template_for(request.url.path) or request.url.path
        shape = RequestShape.of(request)
        body, errors = _fixture_body(request, template, shape, _mode.get())
        run.served.append(ServedResponse(request.method, template, shape, errors))
        return httpx2.Response(200, json=body, headers={"content-type": "application/vnd.api+json"})

    client.client._transport = httpx2.MockTransport(handler)

    async def make_authenticated_request(method: str, url: str, **kwargs: Any) -> Any:
        response = await client.request(method, url, **kwargs)
        request = response.request
        template = SPEC.template_for(request.url.path) or request.url.path
        shape = RequestShape.of(request)
        schema = _response_schema(request.method, template, shape.includes)
        ctx = TrackContext(
            run.scenario, run.tool, request.method, template, run.violations, shape.fieldsets
        )
        raw_json = response.json

        def tracked_json(**json_kwargs: Any) -> Any:
            return _track(raw_json(**json_kwargs), schema, Where(request.method, template), ctx)

        response.json = tracked_json
        return response

    mcp = FakeMCP()
    mcp_error = CapturingMCPError(run)
    register_incident_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        strip_heavy_nested_data=lambda data: data,
        mcp_error=mcp_error,
        generate_recommendation=lambda _data: "",
        enable_write_tools=True,
    )
    register_oncall_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        mcp_error=mcp_error,
        enable_oncall_health=True,
    )
    register_resource_handlers(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        strip_heavy_nested_data=lambda data: data,
        mcp_error=mcp_error,
    )
    register_alert_tools(
        mcp=mcp, make_authenticated_request=make_authenticated_request, mcp_error=mcp_error
    )
    register_audit_tools(
        mcp=mcp, make_authenticated_request=make_authenticated_request, mcp_error=mcp_error
    )
    # A poll interval longer than the wait budget makes progress return after one fetch.
    register_investigation_tools(
        mcp=mcp,
        make_authenticated_request=make_authenticated_request,
        mcp_error=mcp_error,
        enable_write_tools=True,
        poll_interval=60,
    )
    return mcp.tools


async def _run_all(mode: str) -> dict[str, ResponseRun]:
    runs: dict[str, ResponseRun] = {}
    for scenario_id, tool_name, kwargs, *rest in SCENARIOS:
        run = ResponseRun(scenario_id, tool_name, mode)
        # Fresh registration per scenario, as on the request side: the on-call
        # lookup cache would otherwise skip later scenarios' fetches.
        tools = _build_tools(run)
        mode_token = _mode.set(mode)
        scenario_token = request_contract._current_scenario.set(scenario_id)
        options_token = request_contract._stub_options.set(rest[0] if rest else {})
        try:
            run.result = await tools[tool_name](**kwargs)
        except Exception as exc:  # an escaping exception is the finding
            run.crashes.append(f"raised {type(exc).__name__}: {exc}")
        finally:
            request_contract._stub_options.reset(options_token)
            request_contract._current_scenario.reset(scenario_token)
            _mode.reset(mode_token)
        runs[scenario_id] = run
    return runs


def _run_mode(mode: str) -> dict[str, ResponseRun]:
    mp = pytest.MonkeyPatch()
    mp.setenv("ROOTLY_API_TOKEN", "test-token")
    mp.setenv("ONCALLHEALTH_API_KEY", "test-och-key")

    async def _latest_analysis(_self: OnCallHealthClient) -> dict[str, Any]:
        members = [
            {"user_name": "Ada", "rootly_user_id": 1, "och_score": 80},
            {"user_name": "Linus", "rootly_user_id": 3, "och_score": 5},
        ]
        return {"id": 7, "analysis_data": {"team_analysis": {"members": members}}}

    mp.setattr(OnCallHealthClient, "get_latest_analysis", _latest_analysis)
    try:
        return asyncio.run(_run_all(mode))
    finally:
        mp.undo()


Runs = dict[str, ResponseRun]
MODES = (FULL, NULLED, SPARSE)


@pytest.fixture(scope="module")
def runs() -> dict[str, Runs]:
    return {mode: _run_mode(mode) for mode in MODES}


@pytest.fixture(scope="module")
def full_runs(runs: dict[str, Runs]) -> Runs:
    return runs[FULL]


@pytest.fixture(scope="module")
def reads(runs: dict[str, Runs]) -> dict[str, list[ReadViolation]]:
    """Undeclared reads per scenario, from every variant.

    Sparse responses reach fallback branches (``x or y``) a fully populated one
    never takes, so reads are checked on all of them.
    """
    return {sid: [v for mode in MODES for v in runs[mode][sid].violations] for sid in runs[FULL]}


def _accepted(key: ContractKey) -> bool:
    return key in RESPONSE_SPEC_GAPS or _matching(DEFENSIVE_READS, key) is not None


def _unexpected(violations: Iterable[ReadViolation]) -> list[ReadViolation]:
    return [
        v
        for v in violations
        if not _accepted(v.contract_key)
        and _matching(KNOWN_RESPONSE_VIOLATIONS, v.contract_key) is None
    ]


def _describe_unique(violations: Iterable[ReadViolation]) -> list[str]:
    return list(dict.fromkeys(v.describe() for v in violations))


def _requested_fieldsets(runs: dict[str, ResponseRun]) -> set[tuple[str, str, str]]:
    """(scenario, resource type, field) for every sparse-fieldset value sent."""
    return {
        (run.scenario, resource_type, name)
        for run in runs.values()
        for served in run.served
        for resource_type, names in served.shape.fieldsets.items()
        for name in names
    }


def _undeclared_fieldset_values(runs: dict[str, ResponseRun]) -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for scenario, resource_type, name in _requested_fieldsets(runs):
        resource = TYPE_INDEX.get(resource_type)
        declared = _shape(_child_schema(resource, "attributes")).declared if resource else None
        if declared is not None and name not in declared and name != "id":
            found.setdefault((resource_type, name), set()).add(scenario)
    return found


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

SCENARIO_IDS = [s[0] for s in SCENARIOS]


@pytest.mark.unit
@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_responses_read_only_declared_keys(
    full_runs: dict[str, ResponseRun], reads: dict[str, list[ReadViolation]], scenario_id: str
) -> None:
    run = full_runs[scenario_id]
    assert run.served, f"[{scenario_id}] made no requests; tool errors: {run.errors or 'none'}"
    unique = _describe_unique(_unexpected(reads[scenario_id]))
    assert not unique, (
        "Curated tool read response keys src/rootly_mcp_server/data/swagger.json does "
        "not declare (the API never returns them, so the read is always empty):\n  "
        + "\n  ".join(unique)
        + "\nRead the right key, or -- only if the API really returns it -- add a "
        "documented entry to RESPONSE_SPEC_GAPS citing the Rails serializer."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        pytest.param(key, id="|".join(key), marks=pytest.mark.xfail(reason=reason, strict=True))
        for key, reason in KNOWN_RESPONSE_VIOLATIONS.items()
    ],
)
def test_known_response_violation_is_fixed(
    reads: dict[str, list[ReadViolation]], key: ContractKey
) -> None:
    offenders = [
        v.describe()
        for violations in reads.values()
        for v in violations
        if _key_matches(key, v.contract_key)
    ]
    assert not offenders, "\n".join(dict.fromkeys(offenders))


@pytest.mark.unit
def test_every_response_allowlist_entry_is_exercised(
    full_runs: dict[str, ResponseRun], reads: dict[str, list[ReadViolation]]
) -> None:
    """An entry no read needs is a stale claim about upstream (or a fixed bug)."""
    read = {v.contract_key for violations in reads.values() for v in violations}
    entries = [*RESPONSE_SPEC_GAPS, *DEFENSIVE_READS]
    stale = sorted("|".join(e) for e in entries if not any(_key_matches(e, k) for k in read))
    served = {
        (s.method, s.template, include)
        for run in full_runs.values()
        for s in run.served
        for include in s.shape.includes
    }
    stale += sorted("|".join(e) for e in RESPONSE_SCHEMA_OVERRIDES if e not in served)
    assert not stale, f"Response allowlist entries no scenario exercises: {stale}"


@pytest.mark.unit
def test_full_responses_run_without_tool_errors(full_runs: dict[str, ResponseRun]) -> None:
    """A tool that errors on a fully populated response stopped early and hides reads."""
    failing = {
        sid: run.errors + run.crashes for sid, run in full_runs.items() if run.errors or run.crashes
    }
    assert not failing, f"Scenarios failed on full schema-valid responses: {failing}"


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_fixtures_are_schema_valid(runs: dict[str, Runs], mode: str) -> None:
    """The stub only serves bodies the spec says the API can return."""
    invalid = {
        f"{served.method} {served.template}": served.schema_errors[:5]
        for run in runs[mode].values()
        for served in run.served
        if served.schema_errors
    }
    assert not invalid, f"{mode} fixtures invalid against swagger: {invalid}"


@pytest.mark.unit
def test_sparse_fieldsets_name_declared_attributes(full_runs: dict[str, ResponseRun]) -> None:
    """`fields[type]=x` for an attribute the type lacks asks for nothing."""
    found = {
        key: scenarios
        for key, scenarios in _undeclared_fieldset_values(full_runs).items()
        if key not in KNOWN_FIELDSET_VIOLATIONS
    }
    assert not found, (
        "Sparse fieldsets name attributes the resource schema does not declare: "
        + "; ".join(
            f"fields[{t}] {name!r} (scenarios {sorted(s)})"
            for (t, name), s in sorted(found.items())
        )
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        pytest.param(key, id="|".join(key), marks=pytest.mark.xfail(reason=reason, strict=True))
        for key, reason in KNOWN_FIELDSET_VIOLATIONS.items()
    ],
)
def test_known_fieldset_violation_is_fixed(
    full_runs: dict[str, ResponseRun], key: tuple[str, str]
) -> None:
    assert key not in _undeclared_fieldset_values(full_runs)


def _sparse_params() -> list[Any]:
    return [
        pytest.param(
            mode,
            sid,
            id=f"{mode}-{sid}",
            marks=pytest.mark.xfail(reason=KNOWN_SPARSE_CRASHES[(mode, sid)], strict=True)
            if (mode, sid) in KNOWN_SPARSE_CRASHES
            else (),
        )
        for mode in SPARSE_MODES
        for sid in SCENARIO_IDS
    ]


@pytest.mark.unit
@pytest.mark.parametrize(("mode", "scenario_id"), _sparse_params())
def test_tools_tolerate_sparse_responses(
    runs: dict[str, Runs], mode: str, scenario_id: str
) -> None:
    run = runs[mode][scenario_id]
    assert run.served, f"[{mode}:{scenario_id}] made no requests"
    endpoints = sorted({f"{s.method} {s.template}" for s in run.served})
    what = (
        "optional objects present with null/absent contents"
        if mode == NULLED
        else "only required fields"
    )
    assert not run.crashes, (
        f"[{mode}:{scenario_id}] {run.tool} crashed on schema-valid responses with "
        f"{what}: {run.crashes}. Endpoints served: {endpoints}"
    )
    assert isinstance(run.result, dict | list | str), (
        f"[{mode}:{scenario_id}] {run.tool} returned {type(run.result).__name__}, not a "
        f"structured result. Endpoints served: {endpoints}"
    )


# --------------------------------------------------------------------------- #
# Self-checks: the tracker would have caught the past bugs
# --------------------------------------------------------------------------- #


def _tracked_for(
    method: str, template: str, body: Any, fieldsets: dict[str, frozenset[str]] | None = None
) -> tuple[Any, list[ReadViolation]]:
    violations: list[ReadViolation] = []
    ctx = TrackContext("self-check", "self-check", method, template, violations, fieldsets or {})
    return _track(
        body, _response_schema(method, template), Where(method, template), ctx
    ), violations


@pytest.mark.unit
def test_tracker_flags_shift_relationships_the_api_does_not_return() -> None:
    """#260: shifts have no `schedule` or `on_call_role` relationship."""
    body = {"data": [{"id": "1", "type": "shifts", "attributes": {}, "relationships": {}}]}
    tracked, violations = _tracked_for("GET", "/v1/shifts", body)
    relationships = tracked["data"][0]["relationships"]
    relationships.get("user")
    relationships.get("schedule")
    relationships.get("on_call_role")
    assert [v.key_path for v in violations] == [
        "data[].relationships.schedule",
        "data[].relationships.on_call_role",
    ]
    assert "user" in violations[0].declared
    message = violations[0].describe()
    assert "GET /v1/shifts" in message and "declared at data[].relationships" in message


@pytest.mark.unit
def test_tracker_flags_undeclared_alert_attributes() -> None:
    """4923cf9: alerts carry `data`, not `payload`."""
    tracked, violations = _tracked_for(
        "GET", "/v1/alerts/{id}", {"data": {"id": "a", "type": "alerts", "attributes": {}}}
    )
    attributes = tracked["data"]["attributes"]
    attributes.get("data")
    assert "payload" not in attributes
    assert [v.key_path for v in violations] == ["data.attributes.payload"]


@pytest.mark.unit
def test_tracker_checks_included_resources_by_type() -> None:
    body = {
        "data": [],
        "included": [{"id": "1", "type": "users", "attributes": {}, "relationships": {}}],
    }
    tracked, violations = _tracked_for("GET", "/v1/shifts", body)
    user = tracked["included"][0]
    user["relationships"].get("on_call_role")
    user["attributes"].get("full_name")
    user["attributes"].get("nickname_that_does_not_exist")
    assert [v.key_path for v in violations] == [
        "included[users].attributes.nickname_that_does_not_exist"
    ]


@pytest.mark.unit
def test_tracker_flags_attributes_outside_the_requested_fieldset() -> None:
    body = {"data": [{"id": "1", "type": "incidents", "attributes": {"title": "t"}}]}
    tracked, violations = _tracked_for(
        "GET", "/v1/incidents", body, {"incidents": frozenset({"title"})}
    )
    attributes = tracked["data"][0]["attributes"]
    attributes.get("title")
    attributes.get("summary")
    assert [(v.key_path, v.fieldset) for v in violations] == [
        ("data[].attributes.summary", "fields[incidents]")
    ]


@pytest.mark.unit
def test_spec_gap_declares_narrow_reads_inside_undocumented_objects() -> None:
    body = {"data": {"id": "i", "type": "incidents", "attributes": {}, "relationships": {}}}
    tracked, violations = _tracked_for("GET", "/v1/incidents/{id}", body)
    relationships = tracked["data"]["relationships"]
    relationships.get("action_items")
    relationships.get("action_items_count")
    assert [v.key_path for v in violations] == [
        "data.relationships",
        "data.relationships.action_items_count",
    ]


@pytest.mark.unit
def test_misspelled_severity_key_is_not_a_defensive_read() -> None:
    """Only the listed fallback probes are accepted; a typo under the declared shape is not."""
    severity = {"data": {"id": "s", "type": "severities", "attributes": {"name": "SEV1"}}}
    body = {"data": [{"id": "1", "type": "incidents", "attributes": {"severity": severity}}]}
    tracked, violations = _tracked_for("GET", "/v1/incidents", body)
    attributes = tracked["data"][0]["attributes"]["severity"]["data"]["attributes"]
    attributes.get("name")
    attributes.get("nmae")
    tracked["data"][0]["attributes"]["severity"].get("slug")  # a listed probe
    assert [v.key_path for v in violations] == [
        "data[].attributes.severity.data.attributes.nmae",
        "data[].attributes.severity.slug",
    ]
    assert [v.key_path for v in _unexpected(violations)] == [
        "data[].attributes.severity.data.attributes.nmae"
    ]


@pytest.mark.unit
def test_nulled_fixture_nulls_required_nullable_fields_the_stub_populates() -> None:
    """A stub value never survives into a nullable field in the sparse modes."""
    schema = _response_schema("GET", "/v1/shifts")
    stub = {
        "data": [
            {
                "id": "shift-1",
                "type": "shifts",
                "attributes": {
                    "schedule_id": "sched-1",
                    "rotation_id": "rot-1",  # required and nullable
                    "user_id": 7,  # optional and nullable
                    "starts_at": "2026-01-01T00:00:00Z",
                    "ends_at": "2026-01-01T08:00:00Z",
                    "is_override": False,
                    "is_shadow": False,
                },
                "relationships": {"user": {"data": {"id": "7", "type": "users"}}},
            }
        ]
    }
    for mode in SPARSE_MODES:
        body = _conform(stub, schema, mode, Where("GET", "/v1/shifts"))
        shift = body["data"][0]
        assert shift["id"] == "shift-1" and shift["type"] == "shifts"
        assert shift["attributes"]["rotation_id"] is None, mode
        assert shift["attributes"]["user_id"] is None, mode
        assert shift["attributes"]["schedule_id"] == "sched-1", mode
        assert shift["attributes"]["starts_at"] == "2026-01-01T00:00:00Z", mode
        assert _validation_errors(body, schema) == [], mode
    nulled = _conform(stub, schema, NULLED, Where("GET", "/v1/shifts"))
    assert nulled["data"][0]["relationships"]["user"]["data"] is None


@pytest.mark.unit
def test_sparse_fixture_nulls_optional_fields() -> None:
    schema = _response_schema("GET", "/v1/shifts")
    body = _conform(
        {"data": [{"id": "1", "type": "shifts"}]}, schema, SPARSE, Where("GET", "/v1/shifts")
    )
    shift = body["data"][0]
    assert shift["attributes"]["rotation_id"] is None  # required but nullable
    assert shift["attributes"]["user_id"] is None  # optional and nullable
    assert "relationships" not in shift and "included" not in body
    assert _validation_errors(body, schema) == []
