"""
Unit tests for alert response stripping.

Tests cover:
- strip_heavy_alert_data() with list responses, single-resource responses,
  empty/malformed data
- AuthenticatedHTTPXClient._is_alert_endpoint() URL matching
- AuthenticatedHTTPXClient._maybe_strip_alert_response() integration
"""

import json

import httpx

from rootly_mcp_server.server import (
    ALERT_ESSENTIAL_ATTRIBUTES,
    AuthenticatedHTTPXClient,
    strip_heavy_alert_data,
)


def _make_alert(alert_id="abc-123", extra_attrs=None):
    """Helper to create a realistic alert dict with heavy fields."""
    attrs = {
        # Essential fields
        "short_id": "XYZ",
        "source": "pagerduty",
        "status": "triggered",
        "summary": "CPU usage above 90%",
        "description": "Host db-01 CPU spiked",
        "noise": "not_noise",
        "alert_urgency_id": "urg-1",
        "url": "https://rootly.com/alerts/abc",
        "external_url": "https://pagerduty.com/incidents/123",
        "created_at": "2026-02-18T10:00:00Z",
        "updated_at": "2026-02-18T10:05:00Z",
        "started_at": "2026-02-18T10:00:00Z",
        "ended_at": None,
        # Heavy fields that should be stripped
        "labels": [{"key": "env", "value": "prod"}],
        "services": [{"id": "svc-1", "name": "API", "nested": {"deep": "data"}}],
        "service_ids": ["svc-1"],
        "groups": [{"id": "grp-1", "name": "Backend"}],
        "group_ids": ["grp-1"],
        "environments": [{"id": "env-1", "name": "Production"}],
        "environment_ids": ["env-1"],
        "responders": [{"user_id": "u1", "name": "Alice"}],
        "incidents": [{"id": "inc-1"}],
        "data": {"raw_payload": "very large blob" * 100},
        "deduplication_key": "dedup-abc",
        "external_id": "ext-123",
        "group_leader_alert_id": None,
        "is_group_leader_alert": False,
        "notification_target_type": "schedule",
        "notification_target_id": "sched-1",
        "alert_urgency": {"id": "urg-1", "name": "high"},
        "notified_users": [{"id": "u1"}, {"id": "u2"}],
        "alerting_targets": [{"type": "schedule", "id": "s1"}],
        "alert_field_values": [{"field": "region", "value": "us-east-1"}],
    }
    if extra_attrs:
        attrs.update(extra_attrs)
    return {
        "id": alert_id,
        "type": "alerts",
        "attributes": attrs,
        "relationships": {
            "events": {
                "data": [
                    {"id": "ev-1", "type": "events"},
                    {"id": "ev-2", "type": "events"},
                ]
            }
        },
    }


class TestStripHeavyAlertData:
    """Tests for the strip_heavy_alert_data function."""

    def test_strips_heavy_fields_from_list_response(self):
        data = {"data": [_make_alert("a1"), _make_alert("a2")]}
        result = strip_heavy_alert_data(data)

        for alert in result["data"]:
            attr_keys = set(alert["attributes"].keys())
            assert attr_keys <= ALERT_ESSENTIAL_ATTRIBUTES
            # Verify essential fields are kept
            assert alert["attributes"]["summary"] == "CPU usage above 90%"
            assert alert["attributes"]["status"] == "triggered"
            assert alert["attributes"]["source"] == "pagerduty"
            # Verify heavy fields are gone
            assert "services" not in alert["attributes"]
            assert "data" not in alert["attributes"]
            assert "labels" not in alert["attributes"]
            assert "notified_users" not in alert["attributes"]

    def test_preserves_full_attributes_on_single_resource_response(self):
        """A single-resource (detail) response keeps all attributes, including
        the raw payload/custom fields, since it's a point lookup."""
        data = {"data": _make_alert("a1")}
        result = strip_heavy_alert_data(data)

        attrs = result["data"]["attributes"]
        # Essential fields are still present
        assert attrs["summary"] == "CPU usage above 90%"
        # Heavy / payload-bearing fields are preserved on detail lookups
        assert "services" in attrs
        assert "data" in attrs
        assert "alert_field_values" in attrs
        assert "labels" in attrs

    def test_single_resource_still_collapses_relationships_and_drops_included(self):
        """Even on detail lookups, relationships collapse and sideloads drop
        to keep the response bounded."""
        data = {
            "data": _make_alert("a1"),
            "included": [{"id": "svc-1", "type": "services"}],
        }
        result = strip_heavy_alert_data(data)

        assert result["data"]["relationships"]["events"] == {"count": 2}
        assert "included" not in result

    def test_collapses_relationships_to_counts(self):
        data = {"data": [_make_alert()]}
        result = strip_heavy_alert_data(data)

        events = result["data"][0]["relationships"]["events"]
        assert events == {"count": 2}

    def test_removes_included_sideloads(self):
        data = {
            "data": [_make_alert()],
            "included": [
                {"id": "svc-1", "type": "services", "attributes": {"name": "API"}},
                {"id": "u1", "type": "users", "attributes": {"name": "Alice", "email": "a@b.com"}},
            ],
        }
        result = strip_heavy_alert_data(data)
        assert "included" not in result

    def test_preserves_id_and_type(self):
        data = {"data": [_make_alert("my-id")]}
        result = strip_heavy_alert_data(data)

        assert result["data"][0]["id"] == "my-id"
        assert result["data"][0]["type"] == "alerts"

    def test_preserves_meta_and_links(self):
        data = {
            "data": [_make_alert()],
            "meta": {"total_count": 42, "current_page": 1},
            "links": {"self": "/v1/alerts?page[number]=1"},
        }
        result = strip_heavy_alert_data(data)
        assert result["meta"] == {"total_count": 42, "current_page": 1}
        assert result["links"] == {"self": "/v1/alerts?page[number]=1"}

    def test_handles_empty_data_list(self):
        data = {"data": []}
        result = strip_heavy_alert_data(data)
        assert result == {"data": []}

    def test_handles_no_data_key(self):
        data = {"error": "something went wrong"}
        result = strip_heavy_alert_data(data)
        assert result == {"error": "something went wrong"}

    def test_handles_alert_with_no_attributes(self):
        data = {"data": [{"id": "a1", "type": "alerts"}]}
        result = strip_heavy_alert_data(data)
        assert result["data"][0]["id"] == "a1"

    def test_handles_alert_with_no_relationships(self):
        alert = _make_alert()
        del alert["relationships"]
        data = {"data": [alert]}
        result = strip_heavy_alert_data(data)
        assert "relationships" not in result["data"][0]

    def test_relationship_without_data_list_left_alone(self):
        """Relationships with non-list data (e.g. single resource) are not collapsed."""
        alert = _make_alert()
        alert["relationships"]["urgency"] = {"data": {"id": "urg-1", "type": "alert_urgencies"}}
        data = {"data": [alert]}
        result = strip_heavy_alert_data(data)
        # Single-resource relationship should be left alone
        assert result["data"][0]["relationships"]["urgency"] == {
            "data": {"id": "urg-1", "type": "alert_urgencies"}
        }


class TestIsAlertEndpoint:
    """Tests for AuthenticatedHTTPXClient._is_alert_endpoint."""

    def test_matches_alerts_list(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alerts") is True

    def test_matches_single_alert(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alerts/abc-123") is True

    def test_matches_incident_alerts(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/incidents/inc-1/alerts") is True

    def test_excludes_alert_urgencies(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alert_urgencies") is False

    def test_excludes_alert_events(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alerts/abc/alert_events") is False

    def test_excludes_alert_sources(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alert_sources") is False

    def test_excludes_alert_routing(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/alert_routing") is False

    def test_excludes_unrelated_endpoints(self):
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/incidents") is False
        assert AuthenticatedHTTPXClient._is_alert_endpoint("/v1/users") is False


class TestMaybeStripAlertResponse:
    """Tests for AuthenticatedHTTPXClient._maybe_strip_alert_response."""

    def _make_response(self, data, status_code=200):
        """Create an httpx.Response with given JSON data."""
        return httpx.Response(
            status_code=status_code,
            json=data,
        )

    def test_strips_get_alert_response(self):
        alert_data = {"data": [_make_alert()]}
        response = self._make_response(alert_data)
        result = AuthenticatedHTTPXClient._maybe_strip_alert_response("GET", "/v1/alerts", response)
        parsed = result.json()
        assert "services" not in parsed["data"][0]["attributes"]

    def test_skips_non_get_methods(self):
        alert_data = {"data": _make_alert()}
        response = self._make_response(alert_data)
        result = AuthenticatedHTTPXClient._maybe_strip_alert_response(
            "POST", "/v1/alerts", response
        )
        parsed = result.json()
        # POST response should not be stripped
        assert "services" in parsed["data"]["attributes"]

    def test_skips_error_responses(self):
        error_data = {"errors": [{"detail": "Not found"}]}
        response = self._make_response(error_data, status_code=404)
        result = AuthenticatedHTTPXClient._maybe_strip_alert_response(
            "GET", "/v1/alerts/bad-id", response
        )
        parsed = result.json()
        assert parsed == error_data

    def test_skips_non_alert_endpoints(self):
        data = {"data": [{"id": "1", "attributes": {"title": "incident"}}]}
        response = self._make_response(data)
        result = AuthenticatedHTTPXClient._maybe_strip_alert_response(
            "GET", "/v1/incidents", response
        )
        parsed = result.json()
        assert "title" in parsed["data"][0]["attributes"]

    def test_handles_malformed_json_gracefully(self):
        response = httpx.Response(status_code=200, content=b"not json")
        result = AuthenticatedHTTPXClient._maybe_strip_alert_response("GET", "/v1/alerts", response)
        # Should return the original response without raising
        assert result.content == b"not json"


class TestPayloadSizeReduction:
    """Verify that stripping actually reduces payload size significantly."""

    def test_significant_size_reduction(self):
        """A realistic alert response should be much smaller after stripping."""
        alerts = [_make_alert(f"alert-{i}") for i in range(10)]
        data = {
            "data": alerts,
            "included": [
                {"id": f"svc-{i}", "type": "services", "attributes": {"name": f"Service {i}"}}
                for i in range(20)
            ],
        }
        original_size = len(json.dumps(data))
        stripped = strip_heavy_alert_data(data)
        stripped_size = len(json.dumps(stripped))

        # Should be at least 50% smaller
        assert stripped_size < original_size * 0.5, (
            f"Expected >50% reduction, got {original_size} -> {stripped_size} "
            f"({stripped_size / original_size * 100:.0f}%)"
        )
