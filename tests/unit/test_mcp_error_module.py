"""Focused tests for mcp_error module."""

from rootly_mcp_server.mcp_error import MCPError


class TestMCPErrorModule:
    """Direct unit tests for MCPError helper behavior."""

    def test_protocol_error_includes_data(self):
        result = MCPError.protocol_error(-32000, "Protocol failed", {"request_id": "abc"})
        assert result["jsonrpc"] == "2.0"
        assert result["error"]["code"] == -32000
        assert result["error"]["message"] == "Protocol failed"
        assert result["error"]["data"] == {"request_id": "abc"}

    def test_tool_error_without_details(self):
        result = MCPError.tool_error("Something failed")
        assert result == {
            "error": True,
            "error_type": "execution_error",
            "message": "Something failed",
        }

    def test_tool_error_with_details(self):
        result = MCPError.tool_error("Bad input", "validation_error", {"field": "start_date"})
        assert result["error"] is True
        assert result["error_type"] == "validation_error"
        assert result["message"] == "Bad input"
        assert result["details"] == {"field": "start_date"}

    def test_categorize_authentication_error(self):
        error_type, message = MCPError.categorize_error(Exception("401 unauthorized"))
        assert error_type == "authentication_error"
        assert "Authentication failed" in message

    def test_categorize_network_error(self):
        error_type, message = MCPError.categorize_error(TimeoutError("timed out"))
        assert error_type == "network_error"
        assert "Network error" in message

    def test_categorize_client_error(self):
        error_type, message = MCPError.categorize_error(Exception("404 not found"))
        assert error_type == "client_error"
        assert "Client error" in message

    def test_categorize_server_error(self):
        error_type, message = MCPError.categorize_error(Exception("500 internal server error"))
        assert error_type == "server_error"
        assert "Server error" in message

    def test_categorize_validation_error(self):
        class FieldValidationError(Exception):
            pass

        error_type, message = MCPError.categorize_error(FieldValidationError("bad field"))
        assert error_type == "validation_error"
        assert "Input validation error" in message

    def test_categorize_execution_error_fallback(self):
        error_type, message = MCPError.categorize_error(Exception("boom"))
        assert error_type == "execution_error"
        assert "Tool execution error" in message


class TestHttpStatusCategorization:
    """Real HTTP failures were all landing in execution_error.

    httpx renders a 400 as "Client error '400 Bad Request' for url ...", so a
    scan of the first ten characters found no status and every HTTP failure was
    categorized generically. Anything keyed off client_error -- the deep-page
    guidance among it -- never fired.
    """

    @staticmethod
    def _http_error(status: int) -> Exception:
        import httpx

        request = httpx.Request("GET", "https://api.rootly.com/v1/incidents")
        return httpx.HTTPStatusError(
            f"Client error '{status}' for url 'x'",
            request=request,
            response=httpx.Response(status, request=request),
        )

    def test_a_response_status_wins_over_the_message(self):
        error_type, _ = MCPError.categorize_error(self._http_error(400))
        assert error_type == "client_error"

    def test_server_statuses_are_separated(self):
        assert MCPError.categorize_error(self._http_error(503))[0] == "server_error"

    def test_a_status_in_the_text_still_works(self):
        # No response attached: the status is read out of the message instead.
        assert MCPError.categorize_error(ValueError("HTTP error 404: gone"))[0] == "client_error"
        assert (
            MCPError.categorize_error(Exception("500 internal server error"))[0] == "server_error"
        )

    def test_earlier_branches_still_take_precedence(self):
        # Auth and network are decided before the status, and a 401 reads as
        # authentication rather than a generic client error.
        assert MCPError.categorize_error(Exception("401 unauthorized"))[0] == "authentication_error"
        assert MCPError.categorize_error(TimeoutError("timed out"))[0] == "network_error"

    def test_an_unrelated_number_is_not_read_as_a_status(self):
        assert MCPError.categorize_error(Exception("boom"))[0] == "execution_error"
