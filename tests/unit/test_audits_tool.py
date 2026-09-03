"""Tests for the curated list_audits tool."""

from __future__ import annotations

from typing import Any

import pytest

from rootly_mcp_server.tools.audits import register_audit_tools


class FakeMCP:
    """Small tool registry used for direct custom tool testing."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


class FakeMCPError:
    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]:
        return (exception.__class__.__name__, str(exception))

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Parameter names must match the MCPErrorLike protocol, not just types.
        return {"error": True, "error_type": error_type, "message": error_message}


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _audit(**overrides: Any) -> dict[str, Any]:
    attrs = {
        "created_at": "2026-09-03T12:00:00Z",
        "event": "update",
        "item_type": "Schedule",
        "item_type_display": "Schedule",
        "item_id": "sched-1",
        "user_id": 42,
        "user_name": "Ada Lovelace",
        "user_email": "ada@example.com",
        "source": "web",
        "object_changes": {"name": ["Old name", "New name"]},
    }
    attrs.update(overrides)
    return {"id": "1", "type": "audits", "attributes": attrs}


def _build(response: FakeResponse | list[FakeResponse]):
    """Register the tool against a fake request function, capturing calls."""
    calls: list[dict[str, Any]] = []
    responses = response if isinstance(response, list) else [response]

    async def make_request(method: str, path: str, **kwargs: Any):
        calls.append({"method": method, "path": path, **kwargs})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    mcp = FakeMCP()
    register_audit_tools(mcp=mcp, make_authenticated_request=make_request, mcp_error=FakeMCPError)
    return mcp.tools["list_audits"], calls


@pytest.mark.unit
class TestListAuditsHappyPath:
    @pytest.mark.asyncio
    async def test_summarizes_records_and_reports_totals(self):
        tool, calls = _build(
            FakeResponse({"data": [_audit(), _audit()], "meta": {"total_count": 759}})
        )
        result = await tool()

        assert result["returned"] == 2
        assert result["total_matching"] == 759
        first = result["audits"][0]
        assert first["action"] == "update"
        assert first["item_type"] == "Schedule"
        assert first["actor"]["email"] == "ada@example.com"
        assert calls[0]["path"] == "/v1/audits"

    @pytest.mark.asyncio
    async def test_paper_trail_pairs_become_from_and_to(self):
        tool, _ = _build(FakeResponse({"data": [_audit()], "meta": {}}))
        result = await tool()
        assert result["audits"][0]["changed_fields"] == {
            "name": {"from": "Old name", "to": "New name"}
        }

    @pytest.mark.asyncio
    async def test_filters_are_forwarded_as_api_params(self):
        tool, calls = _build(FakeResponse({"data": [], "meta": {}}))
        await tool(
            item_type="GeniusWorkflow",
            user_id="42",
            api_key_id="key-1",
            source="API",
            since="2026-08-01",
            until="2026-09-01",
        )
        params = calls[0]["params"]
        assert params["filter[item_type]"] == "GeniusWorkflow"
        assert params["filter[user_id]"] == "42"
        assert params["filter[api_key_id]"] == "key-1"
        # source is normalised; the API expects the lowercase form
        assert params["filter[source]"] == "api"
        assert params["filter[created_at][gte]"] == "2026-08-01"
        assert params["filter[created_at][lte]"] == "2026-09-01"

    @pytest.mark.asyncio
    async def test_full_object_is_opt_in(self):
        payload = {"data": [_audit(object={"name": "New name"})], "meta": {}}
        tool, _ = _build(FakeResponse(payload))
        assert "object" not in (await tool())["audits"][0]

        tool, _ = _build(FakeResponse(payload))
        assert "object" in (await tool(include_full_object=True))["audits"][0]


@pytest.mark.unit
class TestListAuditsPermissionError:
    """A 404 here almost always means the role lacks audit access."""

    @pytest.mark.asyncio
    async def test_404_explains_the_permission_rather_than_reporting_not_found(self):
        tool, _ = _build(FakeResponse({}, status_code=404))
        result = await tool()

        assert result["error"] is True
        assert result["error_type"] == "permission_error"
        assert "Audits - read" in result["message"]
        # The caller must not be told the feature is absent.
        assert "not found" not in result["message"].lower().replace("returns 404 both when", "")


@pytest.mark.unit
class TestListAuditsEmptyResultIsExplained:
    """An empty page is ambiguous: the filter accepts anything and matches nothing."""

    @pytest.mark.asyncio
    async def test_unaudited_child_type_is_called_out(self):
        tool, _ = _build(FakeResponse({"data": [], "meta": {"total_count": 0}}))
        result = await tool(item_type="ScheduleRotation")

        assert result["returned"] == 0
        assert "not audited" in result["note"]
        assert "Schedule" in result["note"]

    @pytest.mark.asyncio
    async def test_unrecognised_item_type_is_flagged_rather_than_read_as_no_changes(self):
        tool, _ = _build(FakeResponse({"data": [], "meta": {"total_count": 0}}))
        result = await tool(item_type="scheduleRotationz")

        note = result["note"]
        # Names the value back, and warns against the wrong conclusion. The
        # list of known types can go stale, so the note must allow for a
        # newly added type rather than insisting on a misspelling.
        assert "scheduleRotationz" in note
        assert "no changes" in note

    @pytest.mark.asyncio
    async def test_known_type_with_no_rows_gets_no_misleading_note(self):
        tool, _ = _build(FakeResponse({"data": [], "meta": {"total_count": 0}}))
        result = await tool(item_type="Schedule")
        assert "note" not in result

    @pytest.mark.asyncio
    async def test_no_note_when_records_were_returned(self):
        tool, _ = _build(FakeResponse({"data": [_audit()], "meta": {}}))
        result = await tool(item_type="ScheduleRotation")
        assert "note" not in result


@pytest.mark.unit
class TestListAuditsArgumentClamping:
    @pytest.mark.asyncio
    async def test_oversized_max_results_is_clamped_and_reported(self):
        tool, calls = _build(FakeResponse({"data": [], "meta": {}}))
        result = await tool(max_results=500)

        assert "argument_adjustments" in result
        assert any("max_results=500" in n for n in result["argument_adjustments"])
        assert calls[0]["params"]["page[size]"] <= 25

    @pytest.mark.asyncio
    async def test_in_range_max_results_is_not_reported(self):
        tool, _ = _build(FakeResponse({"data": [], "meta": {}}))
        assert "argument_adjustments" not in await tool(max_results=5)

    @pytest.mark.asyncio
    async def test_returns_no_more_than_requested(self):
        tool, _ = _build(FakeResponse({"data": [_audit() for _ in range(10)], "meta": {}}))
        result = await tool(max_results=3)
        assert result["returned"] == 3


@pytest.mark.unit
class TestListAuditsPayloadIsBounded:
    @pytest.mark.asyncio
    async def test_long_values_are_trimmed(self):
        long_value = "x" * 5000
        tool, _ = _build(
            FakeResponse(
                {"data": [_audit(object_changes={"description": [None, long_value]})], "meta": {}}
            )
        )
        result = await tool()
        rendered = result["audits"][0]["changed_fields"]["description"]["to"]
        assert len(rendered) < len(long_value)
        assert "5000 chars" in rendered

    @pytest.mark.asyncio
    async def test_full_object_stays_structured_when_large(self):
        # Asked for structured object state, a caller must not receive a
        # stringified dict; fields are trimmed individually instead.
        big = {f"field_{i}": f"value_{i}" for i in range(60)}
        tool, _ = _build(FakeResponse({"data": [_audit(object=big)], "meta": {}}))
        obj = (await tool(include_full_object=True))["audits"][0]["object"]

        assert isinstance(obj, dict)
        assert obj["field_0"] == "value_0"
        assert obj["_fields_omitted"] == 20

    @pytest.mark.asyncio
    async def test_long_values_inside_the_object_are_trimmed(self):
        tool, _ = _build(
            FakeResponse({"data": [_audit(object={"description": "y" * 5000})], "meta": {}})
        )
        obj = (await tool(include_full_object=True))["audits"][0]["object"]
        assert "5000 chars" in obj["description"]

    @pytest.mark.asyncio
    async def test_many_changed_fields_are_capped_and_the_remainder_counted(self):
        changes = {f"field_{i}": [i, i + 1] for i in range(60)}
        tool, _ = _build(FakeResponse({"data": [_audit(object_changes=changes)], "meta": {}}))
        entry = (await tool())["audits"][0]

        assert len(entry["changed_fields"]) == 25
        assert entry["changed_fields_omitted"] == 35


@pytest.mark.unit
class TestListAuditsErrorHandling:
    @pytest.mark.asyncio
    async def test_upstream_failure_is_reported_not_raised(self):
        tool, _ = _build(FakeResponse({}, status_code=500))
        result = await tool()
        assert result["error"] is True
        assert "Failed to list audits" in result["message"]
