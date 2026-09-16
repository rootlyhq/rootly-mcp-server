"""Tests that the on-call aggregating tools query the shifts they claim to.

Four tools answer a question about a period and a set of people or schedules:
``get_oncall_schedule_summary``, ``check_responder_availability``,
``create_override_recommendation`` and ``check_oncall_health_risk``. Each of
them returned an answer scoped differently from the one asked for:

- ``check_oncall_health_risk`` sent ``filter[starts_at_lte]``/``filter[ends_at_gte]``
  to ``/v1/shifts``, which takes ``from``/``to`` and no ``filter[...]`` at all.
  An unsupported query parameter is ignored rather than rejected, so no date
  bound was applied and a shift from any period could be reported as scheduled
  for the week asked about.
- ``get_oncall_schedule_summary`` applied ``schedule_ids``/``team_ids``
  client-side through a set that was empty both when no filter was given and
  when the filter matched nothing, so a filter matching nothing returned the
  whole workspace under the caller's filter.
- All four pulled the workspace and discarded most of it, spending a bounded
  page budget on rows that could never appear in the answer, and reported
  nothing when that budget cut the fetch short.

These drive the registered tools rather than re-deriving their logic, so they
fail if the query or the scoping regresses.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_oncall_handoff import FakeMCP, FakeMCPError  # noqa: E402

import rootly_mcp_server.tools.oncall as oncall_module  # noqa: E402
from rootly_mcp_server.tools.oncall import register_oncall_tools  # noqa: E402

# A range in the past, so the future-horizon note never fires and `meta` stays
# empty unless the thing under test put something there.
START = "2026-02-09"
END = "2026-02-15"


def _ok(payload: dict[str, Any]) -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload
    return response


def _shift(
    shift_id: str,
    *,
    user_id: str,
    schedule_id: str,
    starts: str = "2026-02-09T08:00:00Z",
    ends: str = "2026-02-09T16:00:00Z",
) -> dict[str, Any]:
    return {
        "id": shift_id,
        "type": "shifts",
        "attributes": {
            "schedule_id": schedule_id,
            "starts_at": starts,
            "ends_at": ends,
            "is_override": False,
        },
        "relationships": {"user": {"data": {"id": user_id, "type": "users"}}},
    }


SHIFTS = [
    _shift("sh-1", user_id="2381", schedule_id="sched-a"),
    _shift("sh-2", user_id="94178", schedule_id="sched-a"),
    _shift("sh-3", user_id="27965", schedule_id="sched-b"),
]

SCHEDULES = [
    {"id": "sched-a", "attributes": {"name": "Infra Primary", "owner_group_ids": ["team-1"]}},
    {"id": "sched-b", "attributes": {"name": "Cloud Ops", "owner_group_ids": ["team-2"]}},
]

USERS = [
    {"id": "2381", "type": "users", "attributes": {"full_name": "Quentin Rousseau"}},
    {"id": "94178", "type": "users", "attributes": {"full_name": "Gideon Lapshun"}},
    {"id": "27965", "type": "users", "attributes": {"full_name": "Alexandra Chapin"}},
]

TEAMS = [
    {"id": "team-1", "attributes": {"name": "Infrastructure"}},
    {"id": "team-2", "attributes": {"name": "Cloud Ops"}},
]


class Upstream:
    """Fake Rootly API that records every ``/v1/shifts`` query it is asked."""

    def __init__(
        self,
        *,
        shifts: list[dict[str, Any]] | None = None,
        total_pages: int = 1,
        rotation_users: list[str] | None = None,
        schedules_status: int = 200,
        schedules_total_pages: int = 1,
    ) -> None:
        self.shifts = SHIFTS if shifts is None else shifts
        self.total_pages = total_pages
        self.rotation_users = rotation_users or []
        # The schedules listing is bounded and best-effort, so it can come back
        # short either by failing or by running past its page budget.
        self.schedules_status = schedules_status
        self.schedules_total_pages = schedules_total_pages
        self.shift_queries: list[dict[str, Any]] = []

    async def handle(self, method: str, url: str, params: dict | None = None, **_: Any) -> Mock:
        if url.endswith("/v1/shifts"):
            self.shift_queries.append(dict(params or {}))
            return _ok(
                {
                    "data": self.shifts,
                    "included": USERS,
                    "meta": {"total_pages": self.total_pages},
                }
            )
        if "/schedule_rotation_users" in url:
            return _ok(
                {
                    "data": [
                        {"relationships": {"user": {"data": {"id": uid}}}}
                        for uid in self.rotation_users
                    ],
                    "meta": {"total_pages": 1},
                }
            )
        if "/v1/schedules/" in url:
            return _ok(
                {
                    "data": {
                        "id": "sched-a",
                        "relationships": {
                            "schedule_rotations": {"data": [{"id": "rot-1"}]},
                        },
                    }
                }
            )
        if url.endswith("/v1/schedules"):
            if self.schedules_status != 200:
                failed = Mock()
                failed.status_code = self.schedules_status
                failed.json.return_value = {}
                return failed
            if self.schedules_total_pages > 1:
                # A full page, so the fetcher reads meta.total_pages instead of
                # treating a short first page as the end of the listing.
                padding = [
                    {"id": f"pad-{i}", "attributes": {"name": f"Pad {i}", "owner_group_ids": []}}
                    for i in range(100 - len(SCHEDULES))
                ]
                return _ok(
                    {
                        "data": [*SCHEDULES, *padding],
                        "meta": {"total_pages": self.schedules_total_pages},
                    }
                )
            return _ok({"data": SCHEDULES, "meta": {"total_pages": 1}})
        if url.endswith("/v1/users"):
            return _ok({"data": USERS, "meta": {"total_pages": 1}})
        if url.endswith("/v1/teams"):
            return _ok({"data": TEAMS, "meta": {"total_pages": 1}})
        return _ok({"data": [], "meta": {"total_pages": 1}})

    @property
    def last_shift_query(self) -> dict[str, Any]:
        assert self.shift_queries, "no /v1/shifts request was made"
        return self.shift_queries[-1]


def _tools(upstream: Upstream) -> dict[str, Any]:
    mcp = FakeMCP()
    register_oncall_tools(
        mcp=mcp,
        # The bound method, not the instance: AsyncMock only awaits a
        # side_effect it recognises as a coroutine function, and an object with
        # an async `__call__` does not qualify.
        make_authenticated_request=AsyncMock(side_effect=upstream.handle),
        mcp_error=FakeMCPError(),
    )
    return mcp.tools


@pytest.mark.unit
@pytest.mark.asyncio
class TestScheduleSummaryScoping:
    """``get_oncall_schedule_summary`` must honour the filter it was given."""

    async def test_a_schedule_filter_matching_nothing_returns_nothing(self):
        # The regression: the filter was applied through a set that was empty
        # both for "no filter" and for "matched nothing", so an unmatched filter
        # was read as "no filter" and the whole workspace came back under it.
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="does-not-exist"
        )

        assert result["schedule_coverage"] == []
        assert result["responder_load"] == []
        assert result["total_schedules"] == 0

    async def test_a_team_filter_matching_nothing_says_so(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, team_ids="no-such-team"
        )

        assert result["schedule_coverage"] == []
        # An empty result that looks identical to "nobody was on call" has to
        # name the filter as the reason.
        assert "No schedule matched the filter" in result["note"]
        assert "team_ids" in result["note"]

    async def test_an_unresolvable_team_is_not_reported_as_no_match(self):
        # The schedules listing is how team_ids becomes schedule ids, and it is
        # bounded and best-effort. When it fails, a team with schedules looks
        # exactly like a team without any, so "no schedule matched" would be a
        # confident claim drawn from data known to be incomplete.
        upstream = Upstream(schedules_status=500)
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, team_ids="team-1"
        )

        assert result["error"] is True
        assert "came back incomplete" in result["message"]
        assert not upstream.shift_queries

    async def test_a_truncated_schedule_listing_is_also_unresolvable(self):
        upstream = Upstream(schedules_total_pages=40)
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, team_ids="no-such-team"
        )

        assert result["error"] is True
        assert "came back incomplete" in result["message"]

    async def test_a_complete_listing_still_reports_a_genuine_no_match(self):
        # The hedge above must not swallow the real answer: with the whole
        # listing in hand, an unmatched team is a fact about the workspace.
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, team_ids="no-such-team"
        )

        assert result.get("error") is None
        assert "No schedule matched the filter" in result["note"]

    async def test_schedule_ids_do_not_depend_on_the_listing(self):
        # They are passed upstream untouched, so an unusable listing costs
        # display names but not the filter itself.
        upstream = Upstream(schedules_status=500)
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="sched-a"
        )

        assert result.get("error") is None
        assert upstream.last_shift_query["schedule_ids[]"] == ["sched-a"]

    async def test_both_filters_together_must_agree(self):
        # schedule_ids and team_ids intersect rather than union, so a schedule
        # outside the named team matches nothing, and the note names both.
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="sched-a", team_ids="team-2"
        )

        assert result["schedule_coverage"] == []
        assert "schedule_ids and team_ids" in result["note"]

    async def test_both_filters_agreeing_selects_the_overlap(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="sched-a", team_ids="team-1"
        )

        assert upstream.last_shift_query["schedule_ids[]"] == ["sched-a"]
        assert [c["schedule_name"] for c in result["schedule_coverage"]] == ["Infra Primary"]

    async def test_a_schedule_filter_is_pushed_upstream(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="sched-a"
        )

        assert upstream.last_shift_query["schedule_ids[]"] == ["sched-a"]
        assert [c["schedule_name"] for c in result["schedule_coverage"]] == ["Infra Primary"]

    async def test_a_team_filter_resolves_to_that_teams_schedules(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, team_ids="team-2"
        )

        assert upstream.last_shift_query["schedule_ids[]"] == ["sched-b"]
        assert [c["schedule_name"] for c in result["schedule_coverage"]] == ["Cloud Ops"]

    async def test_an_unfiltered_query_asks_for_every_schedule(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END
        )

        # No selection means no `schedule_ids[]`, rather than an empty list that
        # upstream would read as "match nothing".
        assert "schedule_ids[]" not in upstream.last_shift_query
        assert {c["schedule_name"] for c in result["schedule_coverage"]} == {
            "Infra Primary",
            "Cloud Ops",
        }

    async def test_an_unknown_schedule_id_is_still_asked_of_upstream(self):
        # The schedules lookup pages out well before a large workspace is
        # exhausted, so a real id missing from it must not be treated as a bad
        # filter. Upstream decides, and returns nothing if it really is unknown.
        upstream = Upstream()
        await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END, schedule_ids="does-not-exist"
        )

        assert upstream.last_shift_query["schedule_ids[]"] == ["does-not-exist"]

    async def test_hours_are_aggregated_per_user_per_schedule(self):
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END
        )

        by_name = {c["schedule_name"]: c for c in result["schedule_coverage"]}
        infra = {r["user_name"]: r for r in by_name["Infra Primary"]["responders"]}
        assert infra["Quentin Rousseau"]["total_hours"] == 8.0
        assert infra["Gideon Lapshun"]["total_hours"] == 8.0

    async def test_an_unusable_date_is_refused(self):
        # Upstream ignores an unusable bound rather than rejecting it, so
        # continuing would summarise a different period than the one asked for.
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date="last tuesday", end_date=END
        )

        assert result["error_type"] == "validation_error"
        assert not upstream.shift_queries


@pytest.mark.unit
@pytest.mark.asyncio
class TestResponderAvailabilityScoping:
    """``check_responder_availability`` only ever reports on the users asked for."""

    async def test_the_user_filter_is_pushed_upstream(self):
        upstream = Upstream()
        await _tools(upstream)["check_responder_availability"](
            start_date=START, end_date=END, user_ids="2381,94178"
        )

        assert upstream.last_shift_query["user_ids[]"] == ["2381", "94178"]

    async def test_users_are_split_into_scheduled_and_not(self):
        upstream = Upstream()
        result = await _tools(upstream)["check_responder_availability"](
            start_date=START, end_date=END, user_ids="2381,99999"
        )

        assert [u["user_id"] for u in result["scheduled"]] == [2381]
        assert [u["user_id"] for u in result["not_scheduled"]] == [99999]
        assert result["scheduled"][0]["total_hours"] == 8.0

    async def test_an_unusable_date_is_refused(self):
        upstream = Upstream()
        result = await _tools(upstream)["check_responder_availability"](
            start_date=START, end_date="", user_ids="2381"
        )

        assert result["error_type"] == "validation_error"
        assert not upstream.shift_queries


@pytest.mark.unit
@pytest.mark.asyncio
class TestOverrideRecommendationScoping:
    """``create_override_recommendation`` scores only the rotation's own users."""

    async def test_rotation_users_are_pushed_upstream(self):
        upstream = Upstream(rotation_users=["94178", "27965"])
        await _tools(upstream)["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        assert upstream.last_shift_query["user_ids[]"] == ["27965", "94178"]

    async def test_no_rotation_users_means_no_shift_query_at_all(self):
        # An empty `user_ids[]` would be dropped and fetch the workspace to
        # answer a question that already has no answer.
        upstream = Upstream(rotation_users=[])
        result = await _tools(upstream)["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        assert not upstream.shift_queries
        assert "No rotation users found" in result["warning"]

    async def test_an_unusable_date_is_refused(self):
        upstream = Upstream(rotation_users=["94178"])
        result = await _tools(upstream)["create_override_recommendation"](
            schedule_id="sched-a",
            original_user_id=2381,
            start_date=START,
            end_date="2026-13-45",
        )

        assert result["error_type"] == "validation_error"
        assert not upstream.shift_queries


@pytest.mark.unit
@pytest.mark.asyncio
class TestOverrideRecommendationOutput:
    """The ranking and payload the recommendation hands back."""

    @staticmethod
    def _upstream() -> Upstream:
        # 27965 carries twice the load of 94178, so the ranking is unambiguous.
        return Upstream(
            shifts=[
                _shift("busy-1", user_id="27965", schedule_id="sched-a"),
                _shift(
                    "busy-2",
                    user_id="27965",
                    schedule_id="sched-a",
                    starts="2026-02-10T08:00:00Z",
                    ends="2026-02-10T16:00:00Z",
                ),
                _shift("light-1", user_id="94178", schedule_id="sched-a"),
            ],
            rotation_users=["94178", "27965"],
        )

    async def test_the_lightest_loaded_candidate_is_recommended_first(self):
        result = await _tools(self._upstream())["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        ranked = [
            (r["user_id"], r["current_hours_in_period"]) for r in result["recommended_replacements"]
        ]
        assert ranked == [(94178, 8.0), (27965, 16.0)]

    async def test_the_payload_targets_the_top_recommendation(self):
        result = await _tools(self._upstream())["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        assert result["override_payload"] == {
            "schedule_id": "sched-a",
            "user_id": 94178,
            "starts_at": f"{START}T00:00:00Z",
            "ends_at": f"{END}T23:59:59Z",
        }

    async def test_the_replaced_user_is_never_recommended(self):
        upstream = self._upstream()
        upstream.rotation_users = ["94178", "2381"]
        result = await _tools(upstream)["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        assert 2381 not in [r["user_id"] for r in result["recommended_replacements"]]

    async def test_excluded_users_are_dropped(self):
        result = await _tools(self._upstream())["create_override_recommendation"](
            schedule_id="sched-a",
            original_user_id=2381,
            start_date=START,
            end_date=END,
            exclude_user_ids="94178",
        )

        assert [r["user_id"] for r in result["recommended_replacements"]] == [27965]

    async def test_excluding_everyone_says_so_rather_than_returning_empty(self):
        result = await _tools(self._upstream())["create_override_recommendation"](
            schedule_id="sched-a",
            original_user_id=2381,
            start_date=START,
            end_date=END,
            exclude_user_ids="94178,27965",
        )

        assert result["recommended_replacements"] == []
        assert result["override_payload"] is None
        assert "All rotation users are either excluded" in result["warning"]


class _FakeOCH:
    """Stands in for the On-Call Health client, which is a separate service."""

    def __init__(self, safe: list[dict[str, Any]] | None = None) -> None:
        self._safe = safe or []

    async def get_latest_analysis(self) -> dict[str, Any]:
        return {"id": 7}

    def extract_at_risk_users(self, _analysis, threshold):  # noqa: ANN001
        at_risk = [
            {
                "rootly_user_id": 2381,
                "user_name": "Quentin Rousseau",
                "och_score": 91.0,
                "risk_level": "high",
                "health_risk_score": 91.0,
            }
        ]
        return at_risk, self._safe


@pytest.mark.unit
@pytest.mark.asyncio
class TestHealthRiskDateScoping:
    """``check_oncall_health_risk`` must bound its shift query by the period."""

    @staticmethod
    async def _run(upstream: Upstream, och: _FakeOCH | None = None, **kwargs):
        # Awaited inside the patch, not returned as a coroutine for the caller
        # to await after the context has already exited.
        with (
            patch.dict("os.environ", {"ONCALLHEALTH_API_KEY": "test-key"}),
            patch.object(oncall_module, "OnCallHealthClient", lambda: och or _FakeOCH()),
        ):
            return await _tools(upstream)["check_oncall_health_risk"](
                start_date=START, end_date=END, **kwargs
            )

    async def test_the_period_is_sent_as_from_and_to(self):
        upstream = Upstream()
        await self._run(upstream)

        query = upstream.last_shift_query
        assert query["from"] == f"{START}T00:00:00Z"
        assert query["to"] == f"{END}T23:59:59Z"

    async def test_no_filter_parameters_are_sent(self):
        # `/v1/shifts` has no `filter[...]` parameters and ignores unsupported
        # ones, so sending them applied no date bound at all.
        upstream = Upstream()
        await self._run(upstream)

        unsupported = [key for key in upstream.last_shift_query if key.startswith("filter[")]
        assert unsupported == []

    async def test_only_the_reported_on_users_are_fetched(self):
        upstream = Upstream()
        safe = [
            {
                "rootly_user_id": 94178,
                "user_name": "Gideon Lapshun",
                "och_score": 12.0,
                "risk_level": "low",
            }
        ]
        await self._run(upstream, _FakeOCH(safe=safe))

        assert upstream.last_shift_query["user_ids[]"] == ["2381", "94178"]

    async def test_declining_replacements_does_not_fetch_candidates(self):
        # The candidate slice drives both the query and the scoring, so turning
        # replacements off must drop those users from the query too.
        upstream = Upstream()
        safe = [
            {
                "rootly_user_id": 94178,
                "user_name": "Gideon Lapshun",
                "och_score": 12.0,
                "risk_level": "low",
            }
        ]
        result = await self._run(upstream, _FakeOCH(safe=safe), include_replacements=False)

        assert upstream.last_shift_query["user_ids[]"] == ["2381"]
        assert result["recommended_replacements"] == []

    async def test_a_shift_names_its_schedule(self):
        # The schedule came from a `schedule` relationship that `/v1/shifts`
        # does not have, so every shift was reported against "Unknown".
        upstream = Upstream()
        result = await self._run(upstream)

        assert result["at_risk_scheduled"][0]["shifts"][0]["schedule_name"] == "Infra Primary"

    async def test_an_unusable_date_is_refused(self):
        upstream = Upstream()
        with (
            patch.dict("os.environ", {"ONCALLHEALTH_API_KEY": "test-key"}),
            patch.object(oncall_module, "OnCallHealthClient", _FakeOCH),
        ):
            result = await _tools(upstream)["check_oncall_health_risk"](
                start_date="not-a-date", end_date=END
            )

        assert result["error_type"] == "validation_error"
        assert not upstream.shift_queries


@pytest.mark.unit
@pytest.mark.asyncio
class TestTruncationIsReported:
    """A fetch cut short by the page cap has to say so.

    These tools return one number per person or per schedule. A partial fetch
    reads as a quiet week rather than as a missing page, so unlike the listing
    tools there is nothing for the caller to page through and notice.
    """

    @staticmethod
    def _truncating() -> Upstream:
        # A full page, so the fetcher fans out rather than treating a short
        # first page as the end, and more pages upstream than its cap of 10.
        full_page = [_shift(f"sh-{i}", user_id="2381", schedule_id="sched-a") for i in range(100)]
        return Upstream(shifts=full_page, total_pages=40)

    async def test_schedule_summary_reports_truncation(self):
        upstream = self._truncating()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END
        )

        assert result["meta"]["truncated"] is True
        assert "of 40 pages" in result["meta"]["truncation_note"]

    async def test_responder_availability_reports_truncation(self):
        upstream = self._truncating()
        result = await _tools(upstream)["check_responder_availability"](
            start_date=START, end_date=END, user_ids="2381"
        )

        assert result["meta"]["truncated"] is True

    async def test_override_recommendation_reports_truncation(self):
        upstream = self._truncating()
        upstream.rotation_users = ["94178"]
        result = await _tools(upstream)["create_override_recommendation"](
            schedule_id="sched-a", original_user_id=2381, start_date=START, end_date=END
        )

        assert result["meta"]["truncated"] is True

    async def test_health_risk_reports_truncation(self):
        upstream = self._truncating()
        with (
            patch.dict("os.environ", {"ONCALLHEALTH_API_KEY": "test-key"}),
            patch.object(oncall_module, "OnCallHealthClient", _FakeOCH),
        ):
            result = await _tools(upstream)["check_oncall_health_risk"](
                start_date=START, end_date=END
            )

        assert result["meta"]["truncated"] is True

    async def test_a_complete_fetch_carries_no_meta(self):
        # Present only when it applies, so any `meta` here is a real signal.
        upstream = Upstream()
        result = await _tools(upstream)["get_oncall_schedule_summary"](
            start_date=START, end_date=END
        )

        assert "meta" not in result
