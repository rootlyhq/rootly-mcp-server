"""429 handling in AuthenticatedHTTPXClient: one in-place retry on a short Retry-After."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx2
import pytest

from rootly_mcp_server import transport


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        request=httpx2.Request("GET", "https://api.rootly.com/v1/alerts/a1/events"),
        content=b'{"data": []}' if status == 200 else b'{"errors": [{"title": "Too many"}]}',
    )


def _client() -> transport.AuthenticatedHTTPXClient:
    with patch.object(transport.AuthenticatedHTTPXClient, "_get_api_token", return_value="t"):
        return transport.AuthenticatedHTTPXClient(hosted=False, transport="stdio")


class TestRateLimitRetryDelay:
    def test_non_429_is_not_retried(self):
        assert transport._rate_limit_retry_delay(_response(500)) is None

    def test_missing_header_uses_default(self):
        assert (
            transport._rate_limit_retry_delay(_response(429))
            == transport._RATE_LIMIT_DEFAULT_RETRY_WAIT_SECONDS
        )

    def test_delta_seconds(self):
        assert transport._rate_limit_retry_delay(_response(429, {"Retry-After": "2"})) == 2.0

    def test_negative_delta_clamps_to_zero(self):
        assert transport._rate_limit_retry_delay(_response(429, {"Retry-After": "-3"})) == 0.0

    def test_long_wait_is_not_retried(self):
        assert transport._rate_limit_retry_delay(_response(429, {"Retry-After": "60"})) is None

    def test_http_date(self):
        retry_at = datetime.now(UTC) + timedelta(seconds=3)
        delay = transport._rate_limit_retry_delay(
            _response(429, {"Retry-After": format_datetime(retry_at, usegmt=True)})
        )
        assert delay is not None
        assert 0.0 <= delay <= 3.0

    def test_http_date_in_past_clamps_to_zero(self):
        retry_at = datetime.now(UTC) - timedelta(seconds=30)
        delay = transport._rate_limit_retry_delay(
            _response(429, {"Retry-After": format_datetime(retry_at, usegmt=True)})
        )
        assert delay == 0.0

    @pytest.mark.parametrize("value", ["soon", "nan"])
    def test_garbage_uses_default(self, value):
        assert (
            transport._rate_limit_retry_delay(_response(429, {"Retry-After": value}))
            == transport._RATE_LIMIT_DEFAULT_RETRY_WAIT_SECONDS
        )


class TestClientRetriesOn429:
    @pytest.mark.asyncio
    async def test_request_retries_once_then_succeeds(self):
        client = _client()
        client.client.request = AsyncMock(
            side_effect=[_response(429, {"Retry-After": "1"}), _response(200)]
        )
        with patch.object(transport.asyncio, "sleep", new=AsyncMock()) as sleep:
            returned = await client.request("GET", "/v1/alerts/a1/events")
        assert returned.status_code == 200
        assert client.client.request.await_count == 2
        sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_request_retries_only_once(self):
        client = _client()
        client.client.request = AsyncMock(
            side_effect=[_response(429), _response(429), _response(200)]
        )
        with patch.object(transport.asyncio, "sleep", new=AsyncMock()):
            returned = await client.request("GET", "/v1/alerts/a1/events")
        assert returned.status_code == 429
        assert client.client.request.await_count == 2

    @pytest.mark.asyncio
    async def test_request_does_not_retry_long_wait(self):
        client = _client()
        client.client.request = AsyncMock(return_value=_response(429, {"Retry-After": "120"}))
        with patch.object(transport.asyncio, "sleep", new=AsyncMock()) as sleep:
            returned = await client.request("GET", "/v1/alerts/a1/events")
        assert returned.status_code == 429
        assert client.client.request.await_count == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_retries_once_then_succeeds(self):
        client = _client()
        client.client.send = AsyncMock(side_effect=[_response(429), _response(200)])
        request = httpx2.Request("GET", "https://api.rootly.com/v1/alerts/a1/events")
        with patch.object(transport.asyncio, "sleep", new=AsyncMock()):
            returned = await client.send(request)
        assert returned.status_code == 200
        assert client.client.send.await_count == 2
        # The retry resends the same request.
        first, second = client.client.send.await_args_list
        assert first.args[0] is second.args[0]

    @pytest.mark.asyncio
    async def test_non_429_error_is_not_retried(self):
        client = _client()
        client.client.request = AsyncMock(return_value=_response(503))
        with patch.object(transport.asyncio, "sleep", new=AsyncMock()) as sleep:
            returned = await client.request("GET", "/v1/alerts/a1/events")
        assert returned.status_code == 503
        assert client.client.request.await_count == 1
        sleep.assert_not_awaited()


class TestUnsortableFieldHint:
    """A sort on a field the search index lacks gets a structured hint (ROOTLY-MCP-65)."""

    @staticmethod
    def _bad_sort(url: str) -> httpx2.Response:
        return httpx2.Response(
            400,
            request=httpx2.Request("GET", url),
            json={"errors": [{"title": "Invalid search query or sort parameter", "status": "400"}]},
        )

    @pytest.mark.asyncio
    async def test_request_path_names_the_rejected_sort(self):
        client = _client()
        client.client.request = AsyncMock(
            return_value=self._bad_sort("https://api.rootly.com/v1/action_items?sort=-updated_at")
        )
        returned = await client.request("GET", "/v1/action_items", params={"sort": "-updated_at"})
        hint = returned.json()["_sort_hint"]
        assert hint["rejected_sort"] == "-updated_at"
        assert "Retry without `sort`" in hint["how"]

    @pytest.mark.asyncio
    async def test_send_path_names_the_rejected_sort(self):
        client = _client()
        client.client.send = AsyncMock(
            return_value=self._bad_sort("https://api.rootly.com/v1/action_items?sort=-updated_at")
        )
        request = httpx2.Request("GET", "https://api.rootly.com/v1/action_items?sort=-updated_at")
        returned = await client.send(request)
        assert returned.json()["_sort_hint"]["rejected_sort"] == "-updated_at"

    def test_no_sort_parameter_no_hint(self):
        response = self._bad_sort("https://api.rootly.com/v1/action_items?filter[search]=x")
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_unsortable_field(response)
        assert "_sort_hint" not in annotated.json()

    def test_other_400_no_hint(self):
        response = httpx2.Response(
            400,
            request=httpx2.Request("GET", "https://api.rootly.com/v1/action_items?sort=x"),
            json={"errors": [{"title": "Something else", "status": "400"}]},
        )
        annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_unsortable_field(response)
        assert "_sort_hint" not in annotated.json()
