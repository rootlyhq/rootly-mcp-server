"""Contract check: every request a curated tool sends is one the bundled spec declares.

The Rootly API ignores query parameters it does not know rather than rejecting
them, so a misspelled or invented filter is invisible: the request succeeds and
the filter simply never applies. #220 was that bug -- ``/v1/shifts`` was sent
``filter[starts_at_lte]``/``filter[ends_at_gte]`` when it only accepts
``from``/``to``, so the date bound was silently dropped. Unit tests that mock
``make_authenticated_request`` assert whatever the code already sends, so they
cannot catch it.

This module drives every hand-written tool and resource (``tools/*.py``) with
representative arguments -- date ranges, filters, includes, team names,
sequential incident references -- through the real ``AuthenticatedHTTPXClient``
on an ``httpx2.MockTransport``. Each request that reaches the wire is matched to
its operation in ``data/swagger.json`` and checked for:

* a documented path and method;
* query parameter names declared on that operation;
* ``include`` values within the declared enum, when there is one;
* JSON:API ``data.attributes`` keys declared on the request body schema.

A coverage test then confirms every ``make_authenticated_request(...)`` call
site in ``tools/*.py`` was reached by at least one scenario, so a new tool or a
new request inside an existing one cannot skip the contract. When it fails, add
a scenario to ``SCENARIOS`` (or a branch-reaching variant of an existing one).

Two escape hatches, both deliberately small:

* ``SPEC_GAPS`` -- the request is right and the spec is incomplete. Accepted.
* ``KNOWN_VIOLATIONS`` -- the request is wrong (or unverified) and needs a code
  fix. Excluded from the per-scenario check so CI stays green, but each one is
  its own strict ``xfail`` test, so it stays visible and turns red the moment
  it is fixed (prompting removal of the entry).
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import difflib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from rootly_mcp_server.och_client import OnCallHealthClient
from rootly_mcp_server.tools import (
    register_alert_tools,
    register_audit_tools,
    register_incident_tools,
    register_oncall_tools,
    register_resource_handlers,
)
from rootly_mcp_server.transport import AuthenticatedHTTPXClient

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "src" / "rootly_mcp_server"
TOOLS_DIR = PACKAGE_DIR / "tools"
SWAGGER_PATH = PACKAGE_DIR / "data" / "swagger.json"
BASE_URL = "https://api.rootly.com"

# --------------------------------------------------------------------------- #
# Allowlists
# --------------------------------------------------------------------------- #

# Requests that are correct but that the bundled spec under-documents. Keys are
# (METHOD, path template, kind, name); ``kind`` is "param", "include" or
# "attribute", and a path template of "*" applies to every operation. Every
# entry needs a reason, and a link where one exists.
SPEC_GAPS: dict[tuple[str, str, str, str], str] = {
    # JSON:API sparse fieldsets (`fields[<type>]=a,b`) are part of the JSON:API
    # spec the Rootly API implements, and the curated incident tools rely on
    # them to keep payloads small. swagger.json declares no `fields[...]`
    # parameter on any operation, so this is a spec-wide omission rather than
    # a per-endpoint one. Matched by prefix in `_is_spec_gap`.
    ("GET", "*", "param", "fields[*]"): (
        "JSON:API sparse fieldsets; undeclared spec-wide "
        "(https://jsonapi.org/format/#fetching-sparse-fieldsets)"
    ),
    # /v1/shifts declares `include` as a single-value enum of
    # shift_override|user|assignee|shift_shadow. Upstream accepts a
    # comma-separated list (ShiftsController#included_relationships splits on
    # ",", rootlyhq/rootly app/controllers/api/v1/shifts_controller.rb:100-102)
    # -- handled generically here: every include value is split on "," and
    # checked item by item.
    #
    # It also honours nested include paths: the action renders with
    # `include: params[:include]` (shifts_controller.rb:56, :59), which
    # ActiveModelSerializers resolves as a JSON:API include directive, and
    # preloads user details when that directive names `user`
    # (shifts_controller.rb:108-114 -> app/services/shifts/
    # preload_rendered_associations_service.rb:12-20, :80, which preloads
    # current_context_membership: [:role, :on_call_role]). Shifts carry no role
    # of their own (app/serializers/api/v1/shift_serializer.rb:7-10:
    # shift_override, user, assignee); `on_call_role` is a user relationship
    # (app/serializers/api/v1/user_serializer.rb:53-57, :63), so
    # get_oncall_shift_metrics asks for it as `user.on_call_role` and reads it
    # from the included user. Bare `on_call_role` and `schedule_rotation` are
    # deliberately absent: neither is a shift relationship, so upstream ignores
    # them (a shift's rotation is its `rotation_id` attribute,
    # shift_serializer.rb:4).
    ("GET", "/v1/shifts", "include", "user.on_call_role"): (
        "Nested include honoured upstream (ShiftsController renders include: "
        "params[:include]; on_call_role is a UserSerializer relationship) but the "
        "spec enum lists top-level shift relationships only"
    ),
    # /v1/schedules/{id}/shifts declares only from/to, but the action renders
    # with `include: params[:include]` (rootlyhq/rootly
    # app/controllers/api/v1/schedules_controller.rb:101, Api::V1::ShiftSerializer)
    # and preloads user details when the include names `user` (:117-120). The
    # serializer's relationships are shift_override, user and assignee
    # (app/serializers/api/v1/shift_serializer.rb:7-10; assignee only with
    # schedule nesting enabled). `on_call_role` is not one of them -- it is a
    # user relationship (app/serializers/api/v1/user_serializer.rb:63) -- so
    # _fetch_schedule_shift_window asks for it as `user.on_call_role`. The
    # whole parameter is missing from the spec, so there is no enum to check.
    ("GET", "/v1/schedules/{id}/shifts", "param", "include"): (
        "Honoured upstream (SchedulesController#shifts renders include: params[:include]) "
        "but undeclared in swagger"
    ),
}

# Requests that are wrong, or that nothing confirms upstream honours. Keys are
# (METHOD, path template, kind, name). Each becomes a strict xfail test.
KNOWN_VIOLATIONS: dict[tuple[str, str, str, str], str] = {}


# --------------------------------------------------------------------------- #
# Spec index
# --------------------------------------------------------------------------- #


@dataclass
class Operation:
    method: str
    template: str
    query_params: set[str]
    include_enum: set[str] | None
    body_attributes: set[str] | None


class SpecIndex:
    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._routes: list[tuple[int, re.Pattern[str], str]] = []
        for template in spec["paths"]:
            pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$"
            literal_segments = sum(
                1 for seg in template.strip("/").split("/") if not seg.startswith("{")
            )
            self._routes.append((literal_segments, re.compile(pattern), template))
        # A literal segment beats a placeholder: /v1/incidents/{id} must not
        # swallow a hypothetical /v1/incidents/search.
        self._routes.sort(key=lambda route: -route[0])

    def _resolve(self, node: Any) -> Any:
        while isinstance(node, dict) and "$ref" in node:
            target: Any = self._spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            node = target
        return node

    def template_for(self, path: str) -> str | None:
        for _, pattern, template in self._routes:
            if pattern.match(path):
                return template
        return None

    def methods_for(self, template: str) -> list[str]:
        return sorted(m.upper() for m in self._spec["paths"][template] if m != "parameters")

    def operation(self, method: str, template: str) -> Operation | None:
        path_item = self._spec["paths"][template]
        op = path_item.get(method.lower())
        if op is None:
            return None
        params = [self._resolve(p) for p in path_item.get("parameters", [])]
        params += [self._resolve(p) for p in op.get("parameters", [])]
        query = {p["name"] for p in params if p.get("in") == "query"}
        include_enum: set[str] | None = None
        for p in params:
            if p.get("in") == "query" and p["name"] == "include":
                schema = self._resolve(p.get("schema") or {})
                enum = schema.get("enum") or self._resolve(schema.get("items") or {}).get("enum")
                if enum:
                    include_enum = set(enum)
        body_attributes: set[str] | None = None
        content = (self._resolve(op.get("requestBody") or {}).get("content")) or {}
        for media in content.values():
            schema = self._resolve(media.get("schema") or {})
            data = self._resolve((schema.get("properties") or {}).get("data") or {})
            attrs = self._resolve((data.get("properties") or {}).get("attributes") or {})
            if "properties" in attrs:
                body_attributes = set(attrs["properties"])
        return Operation(method.upper(), template, query, include_enum, body_attributes)


SPEC = SpecIndex(json.loads(SWAGGER_PATH.read_text()))


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #


@dataclass
class RecordedRequest:
    scenario: str
    call_site: tuple[str, int]
    method: str
    path: str
    query: list[tuple[str, str]]
    body: Any

    @property
    def query_names(self) -> list[str]:
        return list(dict.fromkeys(name for name, _ in self.query))


@dataclass
class Violation:
    scenario: str
    method: str
    path: str
    template: str | None
    kind: str
    name: str
    call_site: tuple[str, int]
    declared: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.method, self.template or self.path, self.kind, self.name)

    def describe(self) -> str:
        file, line = self.call_site
        where = f"tools/{file}:{line}"
        target = f"{self.method} {self.path}" + (
            f" (spec: {self.template})" if self.template and self.template != self.path else ""
        )
        if self.kind == "path":
            return f"[{self.scenario}] {target} from {where}: path is not in swagger.json"
        if self.kind == "method":
            return (
                f"[{self.scenario}] {target} from {where}: method not declared; "
                f"spec allows {self.declared}"
            )
        what = {
            "param": "query parameter",
            "include": "include value",
            "attribute": "body attribute",
        }[self.kind]
        if len(self.declared) <= 20:
            declared = f"Declared: {self.declared}"
        else:
            closest = difflib.get_close_matches(self.name, self.declared, n=8, cutoff=0.0)
            declared = f"Closest of {len(self.declared)} declared: {closest}"
        return (
            f"[{self.scenario}] {target} from {where}: {what} {self.name!r} is not "
            f"declared. {declared}"
        )


_current_scenario: contextvars.ContextVar[str] = contextvars.ContextVar("scenario")
_current_call_site: contextvars.ContextVar[tuple[str, int]] = contextvars.ContextVar("call_site")
# Per-scenario stub knobs, for branches only a particular response shape reaches.
_stub_options: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("stub_options")


def _tool_call_site() -> tuple[str, int]:
    """(file name, line) of the tools/*.py frame that called make_authenticated_request."""
    frame = sys._getframe(2)
    while frame is not None:
        filename = Path(frame.f_code.co_filename)
        if filename.parent == TOOLS_DIR:
            return (filename.name, frame.f_lineno)
        frame = frame.f_back  # type: ignore[assignment]
    return ("<unknown>", 0)


# --------------------------------------------------------------------------- #
# Stub Rootly API
# --------------------------------------------------------------------------- #

INCIDENT_UUID = "11111111-2222-4333-8444-555555555555"
SEQUENTIAL_ID = 4460


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _incident(incident_id: str = INCIDENT_UUID, sequential_id: int = SEQUENTIAL_ID) -> dict:
    now = datetime.now(UTC)
    return {
        "id": incident_id,
        "type": "incidents",
        "attributes": {
            "sequential_id": sequential_id,
            "title": "Database connection timeout",
            "summary": "Database connection pool exhausted; restarted the pool",
            "status": "resolved",
            "severity": {"data": {"attributes": {"name": "SEV1", "slug": "sev1"}}},
            "created_at": _iso(now - timedelta(hours=3)),
            "updated_at": _iso(now - timedelta(hours=1)),
            "started_at": _iso(now - timedelta(hours=3)),
            "resolved_at": _iso(now - timedelta(hours=1)),
            "url": f"https://rootly.com/account/incidents/{sequential_id}",
            "retrospective_progress_status": "active",
            "mitigation_message": "Restarted the database connection pool",
            "resolution_message": "Raised the pool size",
        },
        "relationships": {
            "incident_post_mortem": {"data": {"id": "pm-1", "type": "incident_post_mortems"}},
        },
    }


def _shift(shift_id: str, user_id: str, starts: datetime, ends: datetime) -> dict:
    return {
        "id": shift_id,
        "type": "shifts",
        "attributes": {
            "schedule_id": "sched-1",
            "rotation_id": "rot-1",
            "starts_at": _iso(starts),
            "ends_at": _iso(ends),
            "is_override": False,
            "is_shadow": False,
        },
        "relationships": {
            "user": {"data": {"id": user_id, "type": "users"}},
            "on_call_role": {"data": {"id": "role-1", "type": "on_call_roles"}},
            "schedule_rotation": {"data": {"id": "rot-1", "type": "schedule_rotations"}},
        },
    }


def _user(user_id: str, name: str) -> dict:
    return {
        "id": user_id,
        "type": "users",
        "attributes": {
            "name": name,
            "full_name": name,
            "email": f"{user_id}@example.com",
            "time_zone": "UTC",
        },
    }


def _list(data: list[dict], **extra: Any) -> dict:
    return {
        "data": data,
        "meta": {"current_page": 1, "next_page": None, "total_pages": 1, "total_count": len(data)},
        **extra,
    }


def _stub_response(request: httpx2.Request, template: str | None) -> httpx2.Response:
    """Minimal valid JSON:API payloads, enough to drive every tool down its main path."""
    method = request.method
    path = request.url.path
    params = request.url.params
    now = datetime.now(UTC)
    body: dict[str, Any]

    if method in {"POST", "PUT", "PATCH"}:
        return httpx2.Response(200, json={"data": _incident()})

    if template == "/v1/incidents":
        sequential = params.get("filter[sequential_id]")
        if sequential:
            body = _list([_incident(sequential_id=int(sequential))])
        else:
            body = _list([_incident(), _incident("99999999-2222-4333-8444-555555555555", 4459)])
    elif template == "/v1/incidents/{id}":
        included = []
        if "roles" in (params.get("include") or ""):
            included = [
                {
                    "id": "assignment-1",
                    "type": "incident_role_assignments",
                    "attributes": {
                        "incident_role": {
                            "data": {"id": "irole-1", "attributes": {"name": "Commander"}}
                        },
                        "user": {"data": {"id": "user-1", "attributes": {"full_name": "Ada"}}},
                    },
                }
            ]
        body = {"data": _incident(path.rsplit("/", 1)[-1]), "included": included}
    elif template == "/v1/post_mortems":
        body = _list(
            [
                {
                    "id": "pm-1",
                    "type": "post_mortems",
                    "attributes": {
                        "title": "Retro",
                        "content": "## Summary\nok",
                        "status": "draft",
                    },
                }
            ]
        )
    elif template == "/v1/post_mortems/{id}":
        body = {"data": {"id": "pm-1", "type": "post_mortems", "attributes": {"content": "ok"}}}
    elif template == "/v1/incidents/{incident_id}/meeting_recordings":
        body = _list(
            [{"id": "rec-1", "type": "meeting_recordings", "attributes": {"word_count": 3}}]
        )
    elif template == "/v1/meeting_recordings/{id}":
        body = {
            "data": {
                "id": "rec-1",
                "type": "meeting_recordings",
                "attributes": {"transcript": [{"speaker": "Ada", "text": "hello there"}]},
            }
        }
    elif template == "/v1/teams":
        wanted = params.get("filter[slug]") or params.get("filter[name]") or "platform"
        attributes = {"name": wanted, "slug": wanted}
        if _stub_options.get({}).get("team_named_with_spaces"):
            # Only the separator-insensitive search accepts this name for the
            # requested "storage-access-platform".
            attributes = {"name": "Storage Access Platform", "slug": "sap"}
        body = _list([{"id": "team-1", "type": "groups", "attributes": attributes}])
    elif template == "/v1/teams/{id}":
        body = {"data": {"id": "team-1", "type": "groups", "attributes": {"name": "Platform"}}}
    elif template == "/v1/severities":
        body = _list(
            [
                {
                    "id": "sev-1",
                    "type": "severities",
                    "attributes": {"name": "SEV1", "slug": "sev1", "severity": "critical"},
                }
            ]
        )
    elif template == "/v1/severities/{id}":
        body = {"data": {"id": path.rsplit("/", 1)[-1], "type": "severities", "attributes": {}}}
    elif template == "/v1/schedules":
        body = _list(
            [
                {
                    "id": f"sched-{params.get('page[number]') or 1}",
                    "type": "schedules",
                    "attributes": {"name": "Primary", "owner_group_ids": ["team-1"]},
                }
            ]
        )
        body["meta"]["total_pages"] = _stub_options.get({}).get("schedule_pages", 1)
    elif template == "/v1/schedules/{schedule_id}/schedule_rotations":
        body = _list(
            [
                {
                    "id": "rot-1",
                    "type": "schedule_rotations",
                    "attributes": {
                        "schedule_id": "sched-1",
                        "name": "Weekly",
                        "position": 1,
                        "schedule_rotationable_type": "ScheduleWeeklyRotation",
                        "schedule_rotationable_attributes": {
                            "handoff_time": "09:00",
                            "handoff_day": "M",
                        },
                    },
                }
            ]
        )
    elif template == "/v1/schedule_rotations/{schedule_rotation_id}/schedule_rotation_users":
        body = _list(
            [
                {
                    "id": "ru-1",
                    "type": "schedule_rotation_users",
                    "attributes": {"user_id": 2, "position": 1},
                },
                {
                    "id": "ru-2",
                    "type": "schedule_rotation_users",
                    "attributes": {"user_id": 3, "position": 2},
                },
            ]
        )
    elif template in {"/v1/shifts", "/v1/schedules/{id}/shifts"}:
        start = _parse(params.get("from")) or now - timedelta(hours=1)
        end = _parse(params.get("to")) or now + timedelta(hours=1)
        if start <= now < end:
            first_start, first_end = now - timedelta(hours=1), now + timedelta(hours=1)
        else:
            first_start, first_end = start, min(end, start + timedelta(hours=8))
        body = {
            "data": [
                _shift("shift-1", "1", first_start, first_end),
                _shift("shift-2", "2", first_end, first_end + timedelta(hours=8)),
            ],
            "included": [_user("1", "Ada"), _user("2", "Grace")],
            "meta": {"total_pages": 1},
        }
    elif template == "/v1/users":
        body = _list([_user("1", "Ada"), _user("2", "Grace"), _user("3", "Linus")])
    elif template == "/v1/alerts/{id}":
        body = {
            "data": {
                "id": "alert-1",
                "type": "alerts",
                "attributes": {"short_id": "PhIQtP", "summary": "Disk full"},
            }
        }
    elif template == "/v1/audits":
        body = _list(
            [
                {
                    "id": "audit-1",
                    "type": "audits",
                    "attributes": {"item_type": "Schedule", "event": "update"},
                }
            ]
        )
    else:
        body = _list([])

    return httpx2.Response(200, json=body, headers={"content-type": "application/vnd.api+json"})


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator

    def resource(self, uri: str, **_: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class RecordingMCPError:
    """Records tool errors so a scenario that silently bailed out is visible."""

    def __init__(self, errors: list[str]) -> None:
        self._errors = errors

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        return (type(exception).__name__, str(exception))

    def tool_error(
        self,
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._errors.append(f"{error_type}: {error_message}")
        return {"error": True, "error_type": error_type, "message": error_message}


def _build_tools(records: list[RecordedRequest], errors: list[str]) -> dict[str, Any]:
    client = AuthenticatedHTTPXClient(base_url=BASE_URL)

    def handler(request: httpx2.Request) -> httpx2.Response:
        template = SPEC.template_for(request.url.path)
        body = json.loads(request.content) if request.content else None
        records.append(
            RecordedRequest(
                scenario=_current_scenario.get(),
                call_site=_current_call_site.get(("<unknown>", 0)),
                method=request.method,
                path=request.url.path,
                query=list(request.url.params.multi_items()),
                body=body,
            )
        )
        return _stub_response(request, template)

    client.client._transport = httpx2.MockTransport(handler)

    async def make_authenticated_request(method: str, url: str, **kwargs: Any) -> Any:
        _current_call_site.set(_tool_call_site())
        return await client.request(method, url, **kwargs)

    mcp = FakeMCP()
    mcp_error = RecordingMCPError(errors)
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
    return mcp.tools


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

_NOW = datetime.now(UTC).replace(microsecond=0)
_WEEK_AGO = _iso(_NOW - timedelta(days=7))
_TOMORROW = _iso(_NOW + timedelta(days=1))
# Longer than the 60-day /v1/shifts cap, so the window splitter runs too.
_QUARTER_AGO = _iso(_NOW - timedelta(days=90))

# (scenario id, tool or resource function name, kwargs[, stub options])
SCENARIOS: list[tuple[str, str, dict[str, Any]] | tuple[str, str, dict[str, Any], dict]] = [
    # incidents.py
    (
        "list_incidents/all_filters",
        "list_incidents",
        {
            "query": "database",
            "teams": "platform",
            "team_ids": "team-2",
            "service_ids": "svc-1,svc-2",
            "severity": "sev1",
            "status": "resolved",
            "started_after": _WEEK_AGO,
            "started_before": _TOMORROW,
            "custom_field_selected_option_ids": "opt-1",
            "sort": "-updated_at",
            "page_size": 50,
            "page_number": 2,
        },
    ),
    (
        "collect_incidents/all_filters",
        "collect_incidents",
        {
            "query": "database",
            "teams": "platform",
            "service_ids": "svc-1",
            "severity": "sev1",
            "status": "resolved",
            "started_after": _WEEK_AGO,
            "started_before": _TOMORROW,
            "custom_field_selected_option_ids": "opt-1",
            "max_results": 10,
            "batch_size": 10,
        },
    ),
    (
        "list_incidents/team_found_by_search",
        "list_incidents",
        {"teams": "storage-access-platform"},
        {"team_named_with_spaces": True},
    ),
    ("search_incidents/single_page", "search_incidents", {"query": "db", "page_number": 1}),
    (
        "search_incidents/all_pages",
        "search_incidents",
        {"query": "db", "page_number": 0, "page_size": 2},
    ),
    ("get_incident/sequential_ref", "get_incident", {"incident_id": f"INC-{SEQUENTIAL_ID}"}),
    (
        "get_incident_retrospective",
        "get_incident_retrospective",
        {"incident_id": f"#{SEQUENTIAL_ID}"},
    ),
    (
        "list_incident_post_mortems/all_filters",
        "list_incident_post_mortems",
        {
            "page_size": 5,
            "page_number": 2,
            "status": "published",
            "severity": "sev1",
            "team_ids": "team-1,team-2",
            "service_ids": "svc-1",
            "created_after": _WEEK_AGO,
            "created_before": _TOMORROW,
            "sort": "-created_at",
        },
    ),
    ("list_incident_roles", "list_incident_roles", {"incident_id": str(SEQUENTIAL_ID)}),
    (
        "get_incident_meeting_transcripts",
        "get_incident_meeting_transcripts",
        {"incident_id": INCIDENT_UUID, "max_recordings": 2},
    ),
    (
        "create_incident/all_fields",
        "create_incident",
        {
            "title": "DB down",
            "summary": "Pool exhausted",
            "severity_id": "sev-1",
            "service_ids": "svc-1",
            "team_ids": "team-1",
            "environment_ids": "env-1",
            "incident_type_ids": "type-1",
        },
    ),
    (
        "update_incident/all_fields",
        "update_incident",
        {
            "incident_id": f"INC-{SEQUENTIAL_ID}",
            "summary": "Pool exhausted",
            "title": "DB down",
            "status": "mitigated",
            "severity_id": "sev-1",
            "resolution_message": "Raised pool size",
            "mitigation_message": "Restarted pool",
            "detected_at": _WEEK_AGO,
            "started_at": _WEEK_AGO,
            "acknowledged_at": _WEEK_AGO,
            "mitigated_at": _WEEK_AGO,
            "resolved_at": _WEEK_AGO,
            "incident_type_ids": "type-1",
            "service_ids": "svc-1",
            "team_ids": "team-1",
            "environment_ids": "env-1",
            "functionality_ids": "fn-1",
        },
    ),
    (
        "find_related_incidents/by_id",
        "find_related_incidents",
        {"incident_id": INCIDENT_UUID, "status_filter": "resolved"},
    ),
    (
        "suggest_solutions/by_id",
        "suggest_solutions",
        {"incident_id": INCIDENT_UUID, "status_filter": "resolved"},
    ),
    # oncall.py
    (
        "get_oncall_shift_metrics/filters",
        "get_oncall_shift_metrics",
        {
            "start_date": _QUARTER_AGO,
            "end_date": _TOMORROW,
            "user_ids": "1,2",
            "schedule_ids": "sched-1",
            "team_ids": "team-1",
            "group_by": "team",
        },
    ),
    (
        "get_oncall_handoff_summary/with_incidents",
        "get_oncall_handoff_summary",
        {"team_ids": "team-1", "timezone": "UTC", "include_incidents": True},
    ),
    (
        "get_oncall_handoff_summary/multi_page_schedules",
        "get_oncall_handoff_summary",
        {"schedule_ids": "sched-1,sched-2"},
        {"schedule_pages": 2},
    ),
    (
        "get_shift_incidents/all_filters",
        "get_shift_incidents",
        {
            "start_time": _WEEK_AGO,
            "end_time": _TOMORROW,
            "schedule_ids": "sched-1",
            "severity": "critical",
            "status": "resolved",
            "tags": "db,network",
        },
    ),
    (
        "get_schedule_shifts/long_range",
        "get_schedule_shifts",
        {"id": "sched-1", "start_date": _QUARTER_AGO, "end_date": _TOMORROW},
    ),
    (
        "list_shifts/filters",
        "list_shifts",
        {
            "from_date": _QUARTER_AGO,
            "to_date": _TOMORROW,
            "user_ids": "1",
            "schedule_ids": "sched-1",
            "page_size": 10,
        },
    ),
    (
        "get_oncall_schedule_summary/filters",
        "get_oncall_schedule_summary",
        {
            "start_date": _WEEK_AGO,
            "end_date": _TOMORROW,
            "schedule_ids": "sched-1",
            "team_ids": "team-1",
            "include_user_ids": True,
        },
    ),
    (
        "check_responder_availability",
        "check_responder_availability",
        {"start_date": _WEEK_AGO, "end_date": _TOMORROW, "user_ids": "1,2"},
    ),
    (
        "create_override_recommendation",
        "create_override_recommendation",
        {
            "schedule_id": "sched-1",
            "original_user_id": 1,
            "start_date": _WEEK_AGO,
            "end_date": _TOMORROW,
            "exclude_user_ids": "3",
        },
    ),
    (
        "check_oncall_health_risk",
        "check_oncall_health_risk",
        {"start_date": _WEEK_AGO[:10], "end_date": _TOMORROW[:10]},
    ),
    # alerts.py / audits.py
    ("get_alert_by_short_id", "get_alert_by_short_id", {"short_id": "PhIQtP"}),
    (
        "list_audits/all_filters",
        "list_audits",
        {
            "item_type": "Schedule",
            "user_id": "1",
            "api_key_id": "key-1",
            "source": "web",
            "since": _WEEK_AGO,
            "until": _TOMORROW,
            "max_results": 10,
        },
    ),
    # resources.py
    (
        "resource:incident",
        "get_incident_resource",
        {"incident_id": f"INC-{SEQUENTIAL_ID}"},
    ),
    ("resource:team", "get_team_resource", {"team_id": "team-1"}),
    ("resource:incidents", "list_incidents_resource", {}),
    ("resource:oncall_status", "get_oncall_status_resource", {}),
]

# Curated functions that never call the API, so need no scenario.
NO_REQUEST_FUNCTIONS = {"get_workflow_guide_resource"}


@dataclass
class ScenarioRun:
    records: list[RecordedRequest]
    errors: list[str]
    tool_names: set[str]


async def _run_all_scenarios() -> dict[str, ScenarioRun]:
    runs: dict[str, ScenarioRun] = {}
    for scenario_id, tool_name, kwargs, *rest in SCENARIOS:
        records: list[RecordedRequest] = []
        errors: list[str] = []
        # Fresh registration per scenario: the on-call lookup-map cache would
        # otherwise let later scenarios skip the users/schedules/teams fetches.
        tools = _build_tools(records, errors)
        token = _current_scenario.set(scenario_id)
        options_token = _stub_options.set(rest[0] if rest else {})
        try:
            await tools[tool_name](**kwargs)
        finally:
            _stub_options.reset(options_token)
            _current_scenario.reset(token)
        runs[scenario_id] = ScenarioRun(records, errors, set(tools))
    return runs


@pytest.fixture(scope="module")
def scenario_runs() -> dict[str, ScenarioRun]:
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
        return asyncio.run(_run_all_scenarios())
    finally:
        mp.undo()


# --------------------------------------------------------------------------- #
# Contract evaluation
# --------------------------------------------------------------------------- #


def _is_spec_gap(key: tuple[str, str, str, str]) -> bool:
    method, template, kind, name = key
    if key in SPEC_GAPS:
        return True
    if kind == "param" and name.startswith("fields["):
        return (method, "*", "param", "fields[*]") in SPEC_GAPS
    return False


def _violations_for(record: RecordedRequest) -> list[Violation]:
    def violation(kind: str, name: str, template: str | None, declared: list[str]) -> Violation:
        return Violation(
            record.scenario,
            record.method,
            record.path,
            template,
            kind,
            name,
            record.call_site,
            declared,
        )

    template = SPEC.template_for(record.path)
    if template is None:
        return [violation("path", record.path, None, [])]
    op = SPEC.operation(record.method, template)
    if op is None:
        return [violation("method", record.method, template, SPEC.methods_for(template))]

    found: list[Violation] = []
    declared_params = sorted(op.query_params)
    for name in record.query_names:
        if name not in op.query_params:
            found.append(violation("param", name, template, declared_params))

    if "include" in op.query_params and op.include_enum is not None:
        for name, value in record.query:
            if name != "include":
                continue
            for item in (v.strip() for v in value.split(",")):
                if item and item not in op.include_enum:
                    found.append(violation("include", item, template, sorted(op.include_enum)))

    attributes = (
        ((record.body or {}).get("data") or {}).get("attributes")
        if isinstance(record.body, dict)
        else None
    )
    if isinstance(attributes, dict) and op.body_attributes is not None:
        for name in attributes:
            if name not in op.body_attributes:
                found.append(violation("attribute", name, template, sorted(op.body_attributes)))
    return found


def _all_violations(runs: dict[str, ScenarioRun]) -> list[Violation]:
    return [
        v
        for run in runs.values()
        for record in run.records
        for v in _violations_for(record)
        if not _is_spec_gap(v.key)
    ]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("scenario_id", [s[0] for s in SCENARIOS])
def test_requests_match_swagger(scenario_runs: dict[str, ScenarioRun], scenario_id: str) -> None:
    run = scenario_runs[scenario_id]
    assert run.records, (
        f"[{scenario_id}] made no requests; tool errors: {run.errors or 'none'}. "
        "Fix the scenario arguments or stub responses."
    )
    violations = [
        v
        for record in run.records
        for v in _violations_for(record)
        if not _is_spec_gap(v.key) and v.key not in KNOWN_VIOLATIONS
    ]
    unique = list({v.describe(): v for v in violations})
    assert not unique, (
        "Requests not declared by src/rootly_mcp_server/data/swagger.json (upstream "
        "ignores unknown query params, so these silently do nothing):\n  "
        + "\n  ".join(unique)
        + "\nFix the request, or -- only if upstream really accepts it -- add a "
        "documented entry to SPEC_GAPS."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        pytest.param(key, id="|".join(key), marks=pytest.mark.xfail(reason=reason, strict=True))
        for key, reason in KNOWN_VIOLATIONS.items()
    ],
)
def test_known_violation_is_fixed(
    scenario_runs: dict[str, ScenarioRun], key: tuple[str, str, str, str]
) -> None:
    offenders = [v.describe() for v in _all_violations(scenario_runs) if v.key == key]
    assert not offenders, "\n".join(dict.fromkeys(offenders))


@pytest.mark.unit
def test_every_spec_gap_is_exercised(scenario_runs: dict[str, ScenarioRun]) -> None:
    """A SPEC_GAPS entry no request needs is a stale claim about upstream.

    The allowlist documents what the API honours beyond the spec, so an entry
    left behind after the request stopped sending it (or one added for a
    request no scenario makes) asserts upstream behaviour nothing relies on.
    """
    raw = {v.key for run in scenario_runs.values() for r in run.records for v in _violations_for(r)}

    def _covers(gap: tuple[str, str, str, str], key: tuple[str, str, str, str]) -> bool:
        if gap[1] == "*":
            return key[0] == gap[0] and key[2] == "param" and key[3].startswith("fields[")
        return key == gap

    stale = sorted("|".join(gap) for gap in SPEC_GAPS if not any(_covers(gap, k) for k in raw))
    assert not stale, f"SPEC_GAPS entries no scenario request needs: {stale}"


@pytest.mark.unit
def test_scenarios_run_without_tool_errors(scenario_runs: dict[str, ScenarioRun]) -> None:
    """A scenario that returns a tool error stopped early and may hide requests."""
    failing = {sid: run.errors for sid, run in scenario_runs.items() if run.errors}
    assert not failing, f"Scenarios returned tool errors: {failing}"


@pytest.mark.unit
def test_every_curated_function_has_a_scenario(scenario_runs: dict[str, ScenarioRun]) -> None:
    registered = next(iter(scenario_runs.values())).tool_names
    covered = {scenario[1] for scenario in SCENARIOS}
    missing = sorted(registered - covered - NO_REQUEST_FUNCTIONS)
    assert not missing, f"Curated tools/resources with no contract scenario: {missing}"


def _make_authenticated_request_call_sites() -> set[tuple[str, int, int]]:
    sites: set[tuple[str, int, int]] = set()
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "make_authenticated_request"
            ):
                sites.add((path.name, node.lineno, node.end_lineno or node.lineno))
    return sites


@pytest.mark.unit
def test_every_request_call_site_is_exercised(scenario_runs: dict[str, ScenarioRun]) -> None:
    sites = _make_authenticated_request_call_sites()
    assert sites, "AST scan found no make_authenticated_request call sites"
    hit = {record.call_site for run in scenario_runs.values() for record in run.records}
    assert ("<unknown>", 0) not in hit, "A request was recorded without a tools/*.py call site"
    unexercised = sorted(
        f"tools/{file}:{start}"
        for file, start, end in sites
        if not any(f == file and start <= line <= end for f, line in hit)
    )
    assert not unexercised, (
        "make_authenticated_request call sites no contract scenario reached:\n  "
        + "\n  ".join(unexercised)
        + "\nAdd a scenario (or a branch-reaching variant) to SCENARIOS."
    )


@pytest.mark.unit
def test_spec_index_normalizes_concrete_paths() -> None:
    assert SPEC.template_for(f"/v1/incidents/{INCIDENT_UUID}") == "/v1/incidents/{id}"
    assert SPEC.template_for("/v1/schedules/abc/shifts") == "/v1/schedules/{id}/shifts"
    assert SPEC.template_for("/v1/shifts") == "/v1/shifts"
    assert SPEC.template_for("/v1/definitely-not-a-route") is None


@pytest.mark.unit
def test_contract_flags_the_issue_220_regression() -> None:
    """The check would have caught #220: undeclared /v1/shifts filters."""
    record = RecordedRequest(
        scenario="regression-220",
        call_site=("oncall.py", 1),
        method="GET",
        path="/v1/shifts",
        query=[("filter[starts_at_lte]", "x"), ("filter[ends_at_gte]", "y"), ("from", "z")],
        body=None,
    )
    names = {v.name for v in _violations_for(record)}
    assert names == {"filter[starts_at_lte]", "filter[ends_at_gte]"}
