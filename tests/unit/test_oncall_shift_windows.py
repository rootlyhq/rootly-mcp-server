"""Tests for shift date-range splitting and upstream-failure reporting.

Two customer-reported defects drive these:

- A 60-day shift query reported "0 shifts" while 30- and 45-day queries returned
  data. Any non-200 from the first page was returned as an empty list, so an
  upstream failure was indistinguishable from an empty result.
- Schedules visible in the Rootly UI into 2027 appeared unreachable. The
  upstream caps a shift query's `from`/`to` span, so a longer range was rejected
  outright instead of being split into windows the API accepts.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_oncall_handoff import FakeMCP, FakeMCPError  # noqa: E402

from rootly_mcp_server.exceptions import RootlyValidationError  # noqa: E402
from rootly_mcp_server.tools.oncall import (  # noqa: E402
    MAX_SHIFT_SPAN_DAYS,
    _parse_iso_datetime,
    _shift_window_limit_days,
    _split_date_windows,
    register_oncall_tools,
)
from rootly_mcp_server.transport import (  # noqa: E402
    LIST_SHIFTS_LIMIT_DAYS,
    SCHEDULE_SHIFTS_LIMIT_DAYS,
)


def _ok(payload: dict[str, Any]) -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload
    return response


def _status(code: int) -> Mock:
    response = Mock()
    response.status_code = code
    response.json.return_value = {}
    return response


def _build_tools(responder) -> dict[str, Any]:
    mcp = FakeMCP()
    register_oncall_tools(
        mcp=mcp,
        make_authenticated_request=AsyncMock(side_effect=responder),
        mcp_error=FakeMCPError(),
    )
    return mcp.tools


def _shift(shift_id: str = "S1", *, starts: str = "2026-08-02T00:00:00Z") -> dict[str, Any]:
    return {
        "id": shift_id,
        "attributes": {
            "starts_at": starts,
            "ends_at": "2026-08-03T00:00:00Z",
            "schedule_id": "s1",
        },
        "relationships": {"user": {"data": {"id": "u1"}}},
    }


def _full_page() -> list[dict[str, Any]]:
    """A page at the 100-item size, which is what makes the fetcher fan out."""
    return [_shift(f"S{i}") for i in range(100)]


_USER = {"id": "u1", "type": "users", "attributes": {"full_name": "A", "email": "a@x"}}


def _responder(on_shifts=None, *, users_status: int = 200):
    """A request stub plus the list of shift params it saw.

    ``on_shifts`` receives the request params and returns the response for a
    shift path; omit it for an empty successful page. Lookup paths answer
    successfully so a test only has to describe the part it cares about.
    """
    recorded: list[dict[str, Any]] = []

    async def responder(method, path, params=None, **kwargs):
        request_params = dict(params or {})
        if path.endswith("/shifts"):
            recorded.append(request_params)
            if on_shifts is not None:
                return on_shifts(request_params)
            return _ok({"data": [], "meta": {"total_pages": 1}})
        if path == "/v1/users":
            if users_status != 200:
                return _status(users_status)
            return _ok({"data": [_USER], "meta": {"total_pages": 1}})
        return _ok({"data": [], "meta": {"total_pages": 1}})

    return responder, recorded


class TestWindowLimits:
    """The split must use the same caps the transport validates against."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/v1/shifts", LIST_SHIFTS_LIMIT_DAYS),
            ("/v1/shifts/", LIST_SHIFTS_LIMIT_DAYS),
            ("/v1/schedules/abc/shifts", SCHEDULE_SHIFTS_LIMIT_DAYS),
        ],
    )
    def test_shift_paths_report_their_cap(self, path, expected):
        assert _shift_window_limit_days(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/incidents",
            # Lookalikes that are not shift-listing endpoints. The transport
            # guard excludes these too; the split must agree with it.
            "/v1/override_shifts/1",
            "/v1/schedules/x/override_shifts",
        ],
    )
    def test_non_shift_paths_are_not_split(self, path):
        assert _shift_window_limit_days(path) is None


class TestSplitDateWindows:
    def test_range_within_the_cap_is_not_split(self):
        assert _split_date_windows("2026-08-11", "2026-10-01", 62) == []

    @pytest.mark.parametrize(
        ("start", "end", "limit", "expected_windows"),
        [
            ("2026-11-01", "2027-03-01", 62, 2),
            ("2026-01-01", "2026-12-31", 62, 6),
            ("2026-11-01", "2027-03-01", 31, 4),
        ],
    )
    def test_long_ranges_split_into_windows(self, start, end, limit, expected_windows):
        windows = _split_date_windows(start, end, limit)
        assert len(windows) == expected_windows

    def test_windows_are_contiguous_and_cover_the_whole_range(self):
        windows = _split_date_windows("2026-11-01", "2027-03-01", 62)
        assert windows[0][0] == _parse_iso_datetime("2026-11-01")
        assert windows[-1][1] == _parse_iso_datetime("2027-03-01")
        for earlier, later in zip(windows, windows[1:], strict=False):
            # No gap (a missing day loses shifts) and no overlap beyond the
            # boundary itself (which dedup handles).
            assert earlier[1] == later[0]

    def test_no_window_exceeds_the_cap(self):
        for limit in (LIST_SHIFTS_LIMIT_DAYS, SCHEDULE_SHIFTS_LIMIT_DAYS):
            windows = _split_date_windows("2026-01-01", "2026-12-31", limit)
            for start, end in windows:
                assert (end - start).total_seconds() / 86400 <= limit

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("2026-03-01", "2026-03-01"),  # zero span
            ("2027-01-01", "2026-01-01"),  # inverted
            ("garbage", "2026-01-01"),  # unparseable start
            ("2026-01-01", "garbage"),  # unparseable end
        ],
    )
    def test_degenerate_ranges_are_left_to_upstream(self, start, end):
        # Returning [] means "don't split" -- the upstream keeps ownership of
        # rejecting nonsense with its own error.
        assert _split_date_windows(start, end, 62) == []

    def test_span_beyond_the_ceiling_is_refused(self):
        with pytest.raises(RootlyValidationError) as excinfo:
            _split_date_windows("2026-01-01", "2028-01-01", 62)
        assert str(MAX_SHIFT_SPAN_DAYS) in str(excinfo.value)


class TestListShiftsSplitsLongRanges:
    @staticmethod
    async def _empty(method, path, params=None, **kwargs):
        return _ok({"data": [], "meta": {"total_pages": 1}})

    @pytest.mark.asyncio
    async def test_long_range_issues_one_request_per_window(self):
        seen: list[dict[str, Any]] = []

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                seen.append(dict(params or {}))
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-11-01T00:00:00Z", to_date="2027-03-01T00:00:00Z"
        )

        assert not result.get("error"), result
        assert len(seen) == 2
        assert result["meta"]["upstream_windows"] == 2
        # Consecutive windows: the second starts where the first ended.
        assert seen[0]["to"] == seen[1]["from"]

    @pytest.mark.asyncio
    async def test_short_range_is_a_single_request_with_no_window_metadata(self):
        seen: list[dict[str, Any]] = []

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                seen.append(dict(params or {}))
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert len(seen) == 1
        assert "upstream_windows" not in result["meta"]

    @pytest.mark.asyncio
    async def test_a_shift_spanning_a_window_boundary_is_returned_once(self):
        # The upstream returns any shift overlapping the window, so one that
        # straddles a boundary comes back from both sides.
        shift = {
            "id": "SHIFT-BOUNDARY",
            "attributes": {
                "starts_at": "2027-01-01T00:00:00Z",
                "ends_at": "2027-01-03T00:00:00Z",
                "schedule_id": "s1",
            },
            "relationships": {"user": {"data": {"id": "u1"}}},
        }

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": [shift], "meta": {"total_pages": 1}})
            if path == "/v1/users":
                return _ok(
                    {
                        "data": [
                            {
                                "id": "u1",
                                "type": "users",
                                "attributes": {"full_name": "A", "email": "a@x"},
                            }
                        ],
                        "meta": {"total_pages": 1},
                    }
                )
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-11-01T00:00:00Z", to_date="2027-03-01T00:00:00Z"
        )

        assert result["meta"]["upstream_windows"] == 2
        assert result["total_shifts"] == 1
        assert [s["shift_id"] for s in result["shifts"]] == ["SHIFT-BOUNDARY"]

    @pytest.mark.asyncio
    async def test_span_beyond_the_ceiling_is_reported_as_an_error(self):
        tools = _build_tools(self._empty)
        result = await tools["list_shifts"](
            from_date="2026-01-01T00:00:00Z", to_date="2028-01-01T00:00:00Z"
        )
        assert result["error"] is True


class TestUpstreamFailureIsNotReportedAsEmpty:
    """The 0-shifts defect: a failed fetch must not look like an empty result."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 403, 500, 502])
    async def test_shift_fetch_failure_returns_an_error(self, status):
        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _status(status)
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert result["error"] is True
        assert "total_shifts" not in result
        assert str(status) in str(result.get("message", ""))

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_result_is_still_reported_as_zero(self):
        # The fix must not turn "no shifts scheduled" into an error.
        async def responder(method, path, params=None, **kwargs):
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert not result.get("error")
        assert result["total_shifts"] == 0

    @pytest.mark.asyncio
    async def test_enrichment_lookup_failure_still_returns_shifts(self):
        # Users/schedules are enrichment, not the answer. Losing them costs a
        # display name; it must not fail the query.
        shift = {
            "id": "S1",
            "attributes": {
                "starts_at": "2026-08-02T00:00:00Z",
                "ends_at": "2026-08-03T00:00:00Z",
                "schedule_id": "s1",
            },
            "relationships": {"user": {"data": {"id": "u1"}}},
        }

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": [shift], "meta": {"total_pages": 1}})
            return _status(500)

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert not result.get("error"), result
        assert result["total_shifts"] == 1


class TestTruncationIsReported:
    @pytest.mark.asyncio
    async def test_hitting_the_page_ceiling_sets_a_flag(self):
        # A full page plus a total_pages beyond max_pages means the result is
        # partial; without a flag the caller cannot tell.
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": page, "meta": {"total_pages": 50}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert result["meta"]["truncated"] is True
        note = result["meta"]["truncation_note"]
        # The note has to say how much is missing, not just that some is.
        assert "10 of 50 pages" in note

    @pytest.mark.asyncio
    async def test_truncation_across_windows_is_summed_not_overwritten(self):
        # Windows run concurrently and each reports its own truncation. If the
        # report were assigned rather than accumulated, one window's numbers
        # would stand in for the whole query and understate what is missing.
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                # Two windows with different amounts of unfetched data.
                total = 50 if (params or {}).get("from", "").startswith("2026-11") else 12
                return _ok({"data": page, "meta": {"total_pages": total}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-11-01T00:00:00Z", to_date="2027-03-01T00:00:00Z"
        )

        note = result["meta"]["truncation_note"]
        # 10 + 10 fetched of 50 + 12 available -- both windows counted.
        assert "20 of 62 pages" in note

    @pytest.mark.asyncio
    async def test_a_fully_fetched_window_still_counts_toward_the_totals(self):
        # The note describes query-wide totals, so a window that completed has
        # to contribute its pages too -- counting only truncated windows
        # understates how much was fetched and how much exists.
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                # One window completes in 3 pages; the other is cut at 10 of 50.
                total = 3 if (params or {}).get("from", "").startswith("2026-11") else 50
                return _ok({"data": page, "meta": {"total_pages": total}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-11-01T00:00:00Z", to_date="2027-03-01T00:00:00Z"
        )

        # 3 + 10 fetched of 3 + 50 available.
        assert "13 of 53 pages" in result["meta"]["truncation_note"]

    @pytest.mark.asyncio
    async def test_windows_that_all_complete_report_no_truncation(self):
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": page, "meta": {"total_pages": 3}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-11-01T00:00:00Z", to_date="2027-03-01T00:00:00Z"
        )

        assert "truncated" not in result["meta"]
        assert "truncation_note" not in result["meta"]


class TestConcurrencyIsBounded:
    @pytest.mark.asyncio
    async def test_a_split_range_does_not_exceed_the_semaphore_limit(self):
        # Every page request, including each window's first, goes through the
        # semaphore. Leaving the first page unbounded would let the number of
        # windows decide the real concurrency.
        in_flight = 0
        peak = 0
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1
            if path == "/v1/shifts":
                return _ok({"data": page, "meta": {"total_pages": 10}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-01-01T00:00:00Z", to_date="2026-12-31T00:00:00Z"
        )

        assert not result.get("error"), result
        # Six windows of ten pages each, bounded by the tool's Semaphore(10).
        assert peak <= 10

    @pytest.mark.asyncio
    async def test_a_failure_on_a_later_page_is_not_reported_as_complete(self):
        # The first page succeeding does not make the answer complete. A page
        # failing partway through returns a short list that reads as the whole
        # result -- the same defect as the first page, just further in.
        page = _full_page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                if (params or {}).get("page[number]") == 3:
                    return _status(500)
                return _ok({"data": page, "meta": {"total_pages": 5}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert result["error"] is True
        assert "total_shifts" not in result

    @pytest.mark.asyncio
    async def test_a_later_page_failure_still_does_not_break_enrichment(self):
        # Enrichment stays best-effort: a failed users page costs a display
        # name, so it must not fail a query that has its shifts.
        shift = {
            "id": "S1",
            "attributes": {
                "starts_at": "2026-08-02T00:00:00Z",
                "ends_at": "2026-08-03T00:00:00Z",
                "schedule_id": "s1",
            },
            "relationships": {"user": {"data": {"id": "u1"}}},
        }
        users_page = [
            {"id": f"u{i}", "type": "users", "attributes": {"full_name": "A", "email": "a@x"}}
            for i in range(100)
        ]

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": [shift], "meta": {"total_pages": 1}})
            if path == "/v1/users":
                if (params or {}).get("page[number]") == 2:
                    return _status(500)
                return _ok({"data": users_page, "meta": {"total_pages": 3}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert not result.get("error"), result
        assert result["total_shifts"] == 1


class TestHandoffSummarySchedulePages:
    """The handoff summary's schedules are its answer, not enrichment."""

    @pytest.mark.asyncio
    async def test_a_failed_schedule_page_is_not_silently_omitted(self):
        schedules = [
            {"id": f"s{i}", "attributes": {"name": f"S{i}", "owner_group_ids": []}}
            for i in range(100)
        ]

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/schedules":
                if (params or {}).get("page[number]") == 2:
                    return _status(500)
                return _ok({"data": schedules, "meta": {"total_pages": 3}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["get_oncall_handoff_summary"]()

        # Dropping the page would omit whole schedules from the handoff while
        # the result still read as the full picture.
        assert result["error"] is True
        assert "schedule pages" in str(result.get("message", ""))

    @pytest.mark.asyncio
    async def test_all_schedule_pages_succeeding_is_not_an_error(self):
        schedules = [
            {"id": f"s{i}", "attributes": {"name": f"S{i}", "owner_group_ids": []}}
            for i in range(100)
        ]

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/schedules":
                return _ok({"data": schedules, "meta": {"total_pages": 2}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["get_oncall_handoff_summary"]()

        assert not result.get("error"), result


class TestPaginationMetadataTypes:
    """Reporting must accept whatever pagination accepts, or a truncated
    result comes back with nothing saying so."""

    @staticmethod
    def _page() -> list[dict[str, Any]]:
        return _full_page()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("total_pages", [50, "50"])
    async def test_truncation_is_reported_for_int_and_numeric_string(self, total_pages):
        page = self._page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": page, "meta": {"total_pages": total_pages}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert result["meta"]["truncated"] is True
        assert "10 of 50 pages" in result["meta"]["truncation_note"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("total_pages", [3, "3"])
    async def test_a_complete_fetch_reports_no_truncation_either_way(self, total_pages):
        page = self._page()

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                return _ok({"data": page, "meta": {"total_pages": total_pages}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert "truncated" not in result["meta"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("meta", [{}, {"total_pages": "abc"}])
    async def test_an_unreported_page_count_with_full_pages_is_flagged(self, meta):
        # Without a page count the ceiling is a guess. Every page coming back
        # full is the only evidence more exist, and ten pages would otherwise
        # read as the whole answer.
        responder, _ = _responder(lambda _p: _ok({"data": _full_page(), "meta": meta}))
        tools = _build_tools(responder)

        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert result["meta"]["truncated"] is True
        assert "an unreported number of" in result["meta"]["truncation_note"]

    @pytest.mark.asyncio
    async def test_an_unreported_page_count_with_a_short_page_is_not_flagged(self):
        # A short page means the data ended; claiming truncation would be a
        # false alarm on every small query.
        responder, _ = _responder(
            lambda _p: _ok({"data": [_shift(f"S{i}") for i in range(40)], "meta": {}})
        )
        tools = _build_tools(responder)

        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert "truncated" not in result["meta"]


class TestCuratedScheduleShifts:
    """The generated getScheduleShifts passed long ranges straight through and
    failed with `Datetime range exceeds 1 month`; this is the top shift error
    in production. The curated tool splits instead."""

    @pytest.mark.asyncio
    async def test_a_sixty_day_range_is_split_against_the_one_month_cap(self):
        responder, seen = _responder(
            lambda _p: _ok({"data": [_shift()], "meta": {"total_pages": 1}})
        )
        tools = _build_tools(responder)
        # The exact range seen failing upstream.
        result = await tools["get_schedule_shifts"](
            id="f1dd57bb-2ba2-49cd-be00-64961b796b9e",
            from_date="2026-07-30",
            to_date="2026-09-28",
        )

        assert not result.get("error"), result
        assert len(seen) == 2
        assert result["upstream_windows"] == 2
        # Consecutive, covering the requested range end to end.
        assert seen[0]["from"].startswith("2026-07-30")
        assert seen[0]["to"] == seen[1]["from"]
        assert seen[1]["to"].startswith("2026-09-28")

    @pytest.mark.asyncio
    async def test_each_window_stays_within_the_schedule_cap(self):
        responder, seen = _responder(
            lambda _p: _ok({"data": [_shift()], "meta": {"total_pages": 1}})
        )
        tools = _build_tools(responder)
        await tools["get_schedule_shifts"](id="s1", from_date="2026-01-01", to_date="2026-06-30")

        assert seen, "expected the range to be split into windows"
        for window in seen:
            start = _parse_iso_datetime(window["from"])
            end = _parse_iso_datetime(window["to"])
            # Unparseable bounds would mean the split emitted something the
            # upstream could not read.
            assert start is not None and end is not None
            assert (end - start).total_seconds() / 86400 <= SCHEDULE_SHIFTS_LIMIT_DAYS

    @pytest.mark.asyncio
    async def test_a_short_range_is_a_single_request(self):
        responder, seen = _responder(
            lambda _p: _ok({"data": [_shift()], "meta": {"total_pages": 1}})
        )
        tools = _build_tools(responder)
        result = await tools["get_schedule_shifts"](
            id="s1", from_date="2026-08-01", to_date="2026-08-20"
        )

        assert len(seen) == 1
        assert "upstream_windows" not in result

    @pytest.mark.asyncio
    async def test_an_upstream_failure_is_an_error_not_zero_shifts(self):
        async def responder(method, path, params=None, **kwargs):
            if path.endswith("/shifts"):
                return _status(500)
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["get_schedule_shifts"](
            id="s1", from_date="2026-08-01", to_date="2026-08-20"
        )

        assert result["error"] is True
        assert "total_shifts" not in result

    @pytest.mark.asyncio
    async def test_no_date_range_still_works(self):
        # Both bounds are optional on the generated tool; omitting them must
        # not attempt a split.
        async def responder(method, path, params=None, **kwargs):
            return _ok({"data": [{"id": "S1", "attributes": {}}], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["get_schedule_shifts"](id="s1")

        assert not result.get("error"), result
        assert result["total_shifts"] == 1


class TestTransientFailureRetry:
    """A split year is up to sixty requests, and any one failing fails the
    query. One retry absorbs a blip without letting partial data through."""

    @staticmethod
    def _shift() -> dict[str, Any]:
        return {
            "id": "S1",
            "attributes": {
                "starts_at": "2026-08-02T00:00:00Z",
                "ends_at": "2026-08-03T00:00:00Z",
                "schedule_id": "s1",
            },
            "relationships": {"user": {"data": {"id": "u1"}}},
        }

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_and_the_query_succeeds(self):
        attempts = {"n": 0}

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return _status(500)
                return _ok({"data": [self._shift()], "meta": {"total_pages": 1}})
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert attempts["n"] == 2
        assert not result.get("error"), result
        assert result["total_shifts"] == 1

    @pytest.mark.asyncio
    async def test_a_persistent_failure_is_retried_once_then_fails(self):
        attempts = {"n": 0}

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                attempts["n"] += 1
                return _status(500)
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert attempts["n"] == 2
        assert result["error"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 403, 404, 422])
    async def test_a_client_error_is_not_retried(self, status):
        # The upstream said no; it will say no again. Retrying only doubles
        # the load on a request that cannot succeed.
        attempts = {"n": 0}

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                attempts["n"] += 1
                return _status(status)
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert attempts["n"] == 1
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_a_missing_response_is_treated_as_transient(self):
        attempts = {"n": 0}

        async def responder(method, path, params=None, **kwargs):
            if path == "/v1/shifts":
                attempts["n"] += 1
                return None
            return _ok({"data": [], "meta": {"total_pages": 1}})

        tools = _build_tools(responder)
        result = await tools["list_shifts"](
            from_date="2026-08-01T00:00:00Z", to_date="2026-08-20T00:00:00Z"
        )

        assert attempts["n"] == 2
        assert result["error"] is True
