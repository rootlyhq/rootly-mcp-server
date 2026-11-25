"""Tests for the telemetry scrubber.

The scrubber sits on PostHog MCP analytics' `before_send` hook. It has to remove credentials
without removing diagnostics -- collapsing everything to a placeholder is the
failure this module was written to fix, so the "must survive" cases below carry
as much weight as the "must be removed" ones.
"""

import pytest

from rootly_mcp_server.telemetry_scrubber import (
    is_credential_key,
    redact_telemetry_text,
    scrub_posthog_mcp_event,
)


class TestIsCredentialKey:
    """Key classification, which is where the prefix/suffix bugs lived."""

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "auth",
            "token",
            # Prefixed and suffixed forms. A `\bsecret` regex misses every one
            # of these, because the character before is a word character.
            "aws_secret_access_key",
            "AWS_SECRET_ACCESS_KEY",
            "x_api_key",
            "set_cookie",
            "my_password_field",
            "db_conn_password",
            "filter[api_key_id]",
            # Token compounds: "token" is exact-match only, so these need
            # their own entries.
            "api_token",
            "ROOTLY_API_TOKEN",
            "apiToken",
            "auth_token",
            "oauth_token",
            "bearer_token",
            "session_token",
            "private_token",
        ],
    )
    def test_credential_keys(self, key):
        assert is_credential_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            # Contains "auth" but names a person.
            "author",
            "authored_at",
            # Contains "token" but counts things.
            "tokens",
            "prompt_tokens",
            "max_tokens",
            "token_count",
            # The retired SDK's correlation key. Losing it is what made the
            # incident behind this module hard to trace.
            "session_id",
            "incident_id",
            "title",
            "status",
            "url",
        ],
    )
    def test_non_credential_keys(self, key):
        assert is_credential_key(key) is False


class TestScrubbing:
    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
            ("my_password_field=x1", "x1"),
            ("db_conn_password: p@ss", "p@ss"),
            ("api_token=abc123def", "abc123def"),
            ("auth_token: xyz789", "xyz789"),
            ('{"api_token": "hunter2"}', "hunter2"),
            ("ROOTLY_API_TOKEN=rootly_9f8e7d", "rootly_9f8e7d"),
        ],
    )
    def test_prefixed_credential_keys_are_scrubbed(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    @pytest.mark.parametrize(
        "value",
        [
            "author='spencer'",
            "authored_at=2026-08-06",
            "tokens=512",
            "prompt tokens: 512",
            "session_id: 9f2c-abc",
            "incident_id=44",
            "status: resolved",
            "title='Checkout down'",
            "url=https://rootly.com/x",
            "Error calling tool 'get_alert': HTTP error 404: Not Found",
        ],
    )
    def test_diagnostics_survive_the_loose_key_match(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            # An escaped quote inside the value must not end it. Treating it as
            # the delimiter truncated the match and left the tail in plaintext.
            (r'{"password": "he said \"hi\" then hunter2"}', "hunter2"),
            (r'{"api_key": "abc\"def_SECRETTAIL"}', "SECRETTAIL"),
            (r"{'password': 'it\'s hunter2'}", "hunter2"),
            (r'{"password": "a\"b\"c\"TAIL"}', "TAIL"),
            # JSON stringified into a log line, where the delimiter itself
            # carries the backslash.
            (r"\"password\": \"hunter2\"", "hunter2"),
        ],
    )
    def test_escaped_quotes_do_not_truncate_the_value(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    @pytest.mark.parametrize(
        ("value", "kept"),
        [
            (
                '{"password": "p", "incident_id": "44", "title": "Checkout down"}',
                ['"incident_id": "44"', '"title": "Checkout down"'],
            ),
            ("{'password': 'p', 'incident_id': '44'}", ["'incident_id': '44'"]),
            ('{"api_key": "k", "status": "resolved"}', ['"status": "resolved"']),
        ],
    )
    def test_scrubbing_stops_at_the_end_of_the_value(self, value, kept):
        # The escape-aware value group must not run past its closing quote and
        # swallow the rest of the payload.
        result = redact_telemetry_text(value)
        for fragment in kept:
            assert fragment in result


class TestAgainstDetectSecrets:
    """Validate output against an independent, maintained detector corpus.

    detect-secrets is a scanner rather than a redactor -- it reports findings
    without offsets, needs a file for its filters, and is ~26x slower per
    string -- so it is not a runtime dependency. As a test oracle it costs
    nothing and it is not graded by the same regexes it is checking. It found
    the `aws_secret_access_key` gap that the hand-written cases missed.
    """

    CREDENTIALS = [
        "password=hunter2",
        "password='hunter2'",
        "{'password': 'hunter2'}",
        "postgres://svc:hunter2@db/x",
        "api_key=TEtoWdXkvJrdmLLWHKZHiw1o7I6",
        "auth=s3cr3t",
        "session=abc123",
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
        "{'arguments': {'incident_id': '44', 'password': 'hunter2'}}",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh",
    ]

    @pytest.mark.parametrize("value", CREDENTIALS)
    def test_no_secret_survives_scrubbing(self, value, tmp_path):
        scan = pytest.importorskip("detect_secrets.core.scan")
        from detect_secrets.settings import default_settings

        scrubbed = redact_telemetry_text(value)
        target = tmp_path / "scrubbed.txt"
        target.write_text(scrubbed + "\n")

        with default_settings():
            findings = list(scan.scan_file(str(target)))

        assert not findings, (
            f"detect-secrets still finds {[f.secret_value for f in findings]} in {scrubbed!r}"
        )


class TestTelemetryRedactionRules:
    """Content-aware scrubbing for telemetry strings.

    The text scrubber sees one string at a time - there is no field name - so it
    must scrub by content. A previous version returned a constant for any
    input, and error messages, client/server names and the tool context all
    collapsed to one placeholder: every error grouped into a single issue.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "Rootly",
            "Odin",
            "3.4.5",
            "ToolError",
            "Error calling tool 'list_incident_events': HTTP error 404: Not Found",
            "1 validation error for call[search_incidents]",
            "Reviewing incident timeline events to detect whether a responder acknowledged",
            "https://api.rootly.com/v1/incidents/44/events?page%5Bsize%5D=20",
        ],
    )
    def test_diagnostics_pass_through_unchanged(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Bearer eyJhbGciOiJIUzI1NiJ9.abc12345", "Bearer [redacted]"),
            (
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc12345",
                "Authorization: Bearer [redacted]",
            ),
            ("api_key=TEtoWdXkvJrdmLLWHKZ", "api_key=[redacted]"),
            ("client_secret=bhAY5rkA85ZJezcMr8", "client_secret=[redacted]"),
            ('{"password": "hunter2supersecret"}', '{"password": "[redacted]"}'),
        ],
    )
    def test_credentials_and_emails_are_scrubbed(self, value, expected):
        assert redact_telemetry_text(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            # Auth *states* are diagnostics, not credentials. An earlier
            # minimum length of 4 redacted these and lost useful signal.
            "authorization: failed",
            "authorization: missing",
            "password: unset",
            "secret: none",
        ],
    )
    def test_short_diagnostic_values_are_not_mistaken_for_secrets(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            # Short credentials: a length threshold would let these through.
            ("password=hunter2", "hunter2"),
            ("password: hunter2", "hunter2"),
            ("secret=abc", "abc"),
            # Multi-word credentials: matching to the first space would leak
            # everything after it.
            (
                '{"password": "correct horse battery staple"}',
                "correct horse battery staple",
            ),
            ('{"client_secret": "a b c d"}', "a b c d"),
        ],
    )
    def test_short_and_multiword_credentials_are_fully_removed(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    @pytest.mark.parametrize(
        "token",
        [
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.dBjftJeZ4CVPmB92K27uhbU",
            "ghp_16CharsMinimumAAAA",
            "xoxb-1234567890-abcdef",
            "sk-proj_ABCDEFGHIJKLMNOP1234",
            "AKIAIOSFODNN7EXAMPLE",
            "AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe",
        ],
    )
    def test_bare_tokens_are_removed_without_key_context(self, token):
        # Tool arguments arrive as standalone strings, so a key-based rule
        # cannot see them; these are matched on their own shape.
        assert token not in redact_telemetry_text(token)

    @pytest.mark.parametrize(
        "value",
        [
            "44",
            "INC-1234",
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "list_incidents",
            "payments-api",
            "2026-08-06T12:31:00Z",
            "Sev1: checkout down",
            "https://rootly.com/incidents/44",
        ],
    )
    def test_ordinary_identifiers_are_not_mistaken_for_tokens(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            # Short scheme credentials: a length threshold would let these
            # through, but so would matching every word after the scheme.
            ("Bearer abc", "abc"),
            ("Authorization: Basic YTpi", "YTpi"),
            ("Token xyz", "xyz"),
            # Key names from Sentry's default scrubbing denylist.
            ("auth=s3cr3t", "s3cr3t"),
            ("credentials: mypw", "mypw"),
            ("private_key=abc", "abc"),
            ("token=abc123", "abc123"),
            ("Cookie: session=xyz", "xyz"),
            # Credentials embedded in a connection URL.
            ("postgres://svc:hunter2@db.internal:5432/rootly", "hunter2"),
            # PEM blocks.
            (
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----",
                "MIIEowIBAAKC",
            ),
        ],
    )
    def test_industry_standard_credential_forms_are_removed(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    @pytest.mark.parametrize(
        "value",
        [
            # Status words after a credential key or an auth scheme. These read
            # as diagnostics, not secrets, and losing them is what made the
            # original incident hard to debug.
            "auth: failed",
            "token: expired",
            "credentials: missing",
            "Token expired",
            "Bearer token required",
            "Basic auth failed",
            "HTTP error 401: Unauthorized",
            # A URL with no userinfo must keep its host.
            "postgres://db.internal:5432/rootly",
            "prompt tokens: 512",
        ],
    )
    def test_diagnostics_survive_the_broader_rules(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            # Names from sentry_sdk's DEFAULT_DENYLIST that a narrower key list
            # missed, including two word-boundary gaps (set_cookie, x_api_key).
            ("session=abc123", "abc123"),
            ("sessionid=abc123", "abc123"),
            ("set_cookie=sid=xyz", "xyz"),
            ("x_api_key=k123", "k123"),
            ("proxy-authorization: Zm9v", "Zm9v"),
        ],
    )
    def test_sentry_denylist_names_are_covered(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    @pytest.mark.parametrize(
        "value",
        [
            # The retired SDK's correlation key, deliberately not scrubbed -- losing it
            # is what made the original incident hard to trace.
            "session_id: 9f2c-abc",
            "session_id=9f2c-abc",
            "session_id 9f2c for tool list_incidents",
            # A bare address in prose is a diagnostic, not a record about a
            # person; only the PII *key names* are scrubbed.
            "connection from 1.2.3.4 refused",
        ],
    )
    def test_correlation_keys_and_prose_addresses_survive(self, value):
        assert redact_telemetry_text(value) == value

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            # Python renders dicts with single quotes, so this is the shape a
            # stringified argument dict actually arrives in.
            ("password='hunter2'", "hunter2"),
            ("{'password': 'hunter2'}", "hunter2"),
            ("{'client_secret': 'a b c d'}", "a b c d"),
            ("token='t1'", "t1"),
            # The value may contain the other kind of quote.
            ('password="it\'s"', "it's"),
            ("password='say \"hi\"'", 'say "hi"'),
        ],
    )
    def test_single_quoted_credentials_are_removed(self, value, secret):
        assert secret not in redact_telemetry_text(value)

    def test_stringified_argument_dict_is_scrubbed_but_stays_readable(self):
        arguments = {
            "arguments": {
                "incident_id": "44",
                "password": "hunter2",
                "api_key": "sk_live_x",
            }
        }
        result = redact_telemetry_text(str(arguments))
        assert "hunter2" not in result
        assert "sk_live_x" not in result
        # The non-secret argument is what makes the event worth keeping.
        assert "'incident_id': '44'" in result

    def test_empty_input_is_returned_as_is(self):
        assert redact_telemetry_text("") == ""

    def test_secret_value_never_survives_scrubbing(self):
        """Whatever the surrounding shape, the secret itself must not remain."""
        secret = "eyJhbGciOiJIUzI1NiJ9supersecrettoken"
        for template in (
            "Bearer {s}",
            "Authorization: Bearer {s}",
            "?access_token={s}",
            '{{"refresh_token": "{s}"}}',
        ):
            assert secret not in redact_telemetry_text(template.format(s=secret))


class TestClientIdentificationSurvives:
    """User-agent strings must not be scrubbed.

    The user-agent header identifies which client made a call, so redacting it
    would cost exactly the attribution this telemetry exists to provide.
    """

    @pytest.mark.parametrize(
        "key",
        ["user-agent", "user_agent", "User-Agent", "useragent", "x-client-name"],
    )
    def test_header_names_are_not_credentials(self, key):
        assert is_credential_key(key) is False

    @pytest.mark.parametrize(
        "value",
        [
            "User-Agent: Odin/1.2.0",
            "user-agent: claude-code/2.1.4 (darwin; arm64)",
            "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "user_agent=cursor-mcp/0.9",
            "{'headers': {'user-agent': 'Odin/1.2.0', 'accept': 'application/json'}}",
            "User-Agent: python-httpx/0.27.0",
            "user-agent: node-fetch/1.0 (+https://github.com/bitinn/node-fetch)",
        ],
    )
    def test_user_agents_pass_through_untouched(self, value):
        assert redact_telemetry_text(value) == value

    def test_event_hook_keeps_user_agent_but_removes_credentials(self):
        headers = _scrubbed_arguments(
            {"headers": {"user-agent": "Odin/1.2.0", "authorization": "Bearer x"}}
        )["headers"]
        assert headers["user-agent"] == "Odin/1.2.0"
        assert headers["authorization"] == "[redacted]"


class TestEventHookHostileInput:
    """Arguments are attacker-shaped JSON, so a walk that blows the stack would
    lose the event -- or worse, publish it unscrubbed."""

    def test_deeply_nested_arguments_do_not_raise(self):
        node = root = {}
        for _ in range(5000):
            node["n"] = {}
            node = node["n"]
        node["password"] = "hunter2"
        # No RecursionError; the event survives to be published.
        _scrubbed_arguments(root)

    def test_circular_arguments_terminate(self):
        cycle: dict = {}
        cycle["self"] = cycle
        _scrubbed_arguments(cycle)

    def test_a_credential_below_the_depth_limit_is_not_published(self):
        # The subtree is replaced rather than descended into, so burying a
        # credential deeply cannot smuggle it out.
        node = root = {}
        for _ in range(40):
            node["n"] = {}
            node = node["n"]
        node["password"] = "DEEPSECRET"
        assert "DEEPSECRET" not in str(_scrubbed_arguments(root))

    def test_ordinary_nesting_is_still_walked(self):
        inner = _scrubbed_arguments({"a": {"b": {"c": {"password": "p", "id": "44"}}}})
        assert inner["a"]["b"]["c"] == {"password": "[redacted]", "id": "44"}

    def test_dicts_inside_tuples_are_walked(self):
        args = _scrubbed_arguments({"k": ({"password": "p"},), "id": "44"})
        assert args["k"] == [{"password": "[redacted]"}]
        assert args["id"] == "44"

    def test_non_string_keys_do_not_raise(self):
        assert _scrubbed_arguments({1: "a", None: "b", ("t",): "c"}) == {
            1: "a",
            None: "b",
            ("t",): "c",
        }


def _scrubbed_arguments(arguments):
    event = {"event": "$mcp_tool_call", "properties": {"$mcp_parameters": {"arguments": arguments}}}
    return scrub_posthog_mcp_event(event)["properties"]["$mcp_parameters"]["arguments"]


class TestScrubPostHogMcpEvent:
    """PostHog's `before_send` hook receives the built capture payload."""

    def test_credential_keys_and_values_are_removed(self):
        event = {
            "event": "$mcp_tool_call",
            "properties": {
                "$mcp_parameters": {"arguments": {"password": "hunter2", "incident_id": "44"}},
                "$mcp_response": {"content": [{"text": "Bearer abcdefghijklmnopqrstuvwxyz123456"}]},
                "$mcp_error_message": "failed with api_key=" + "sk_" + "live_abcdefghijklmnop",
                "$exception_list": [{"value": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"}],
            },
        }
        properties = scrub_posthog_mcp_event(event)["properties"]
        assert properties["$mcp_parameters"]["arguments"] == {
            "password": "[redacted]",
            "incident_id": "44",
        }
        assert "abcdefghijklmnopqrstuvwxyz123456" not in repr(properties)
        assert "abcdefghijklmnop" not in properties["$mcp_error_message"]
        assert "ghp_" not in repr(properties["$exception_list"])

    def test_metadata_is_left_untouched(self):
        properties = {
            "$session_id": "ses_123",
            "$mcp_tool_name": "list_incidents",
            "$mcp_duration_ms": 12.5,
            "$mcp_is_error": False,
        }
        event = {"event": "$mcp_tool_call", "properties": dict(properties)}
        assert scrub_posthog_mcp_event(event)["properties"] == properties

    @pytest.mark.parametrize(
        "parameters",
        [
            # posthog.mcp <= 7.60: the arguments dict itself.
            {"arguments": {"page_size": 5, "context": "Acme", "session_id": "s"}},
            # Later versions: a pre-middleware copy of the whole request.
            {"request": {"params": {"arguments": {"page_size": 5, "context": "Acme"}}}},
        ],
        ids=["arguments", "request-params-arguments"],
    )
    def test_retired_injected_arguments_are_dropped(self, parameters):
        event = {"event": "$mcp_tool_call", "properties": {"$mcp_parameters": parameters}}
        properties = scrub_posthog_mcp_event(event)["properties"]
        assert "Acme" not in repr(properties)
        assert "session_id" not in repr(properties)
        assert "page_size" in repr(properties)

    def test_event_without_properties_is_returned_unchanged(self):
        event = {"event": "$mcp_initialize"}
        assert scrub_posthog_mcp_event(event) == {"event": "$mcp_initialize"}


class TestPydanticInputValue:
    """Pydantic errors echo the rejected argument, which is caller content."""

    MESSAGE = (
        "1 validation error for call[list_incidents]\n"
        "context\n"
        "  Unexpected keyword argument [type=unexpected_keyword_argument, "
        "input_value='Checking incidents for Acme, then paging', input_type=str]\n"
        "    For further information visit https://errors.pydantic.dev/2.13/v/unexpected_keyword_argument"
    )

    def test_input_value_is_removed_and_diagnostics_kept(self):
        scrubbed = redact_telemetry_text(self.MESSAGE)

        assert "Acme" not in scrubbed
        assert "input_value=[redacted], input_type=str]" in scrubbed
        assert "call[list_incidents]" in scrubbed
        assert "type=unexpected_keyword_argument" in scrubbed

    def test_truncated_message_is_removed_to_end_of_line(self):
        truncated = self.MESSAGE.split(", input_type=")[0]

        scrubbed = redact_telemetry_text(truncated)

        assert "Acme" not in scrubbed
        assert scrubbed.endswith("input_value=[redacted]")

    @pytest.mark.parametrize(
        "caller_text",
        [
            "Acme, input_type=str] secret tail",
            "Acme, input_type=str]",
            "Acme\\n, input_type=str] more",
        ],
        ids=["delimiter-mid-value", "delimiter-at-end", "escaped-newline"],
    )
    def test_delimiter_inside_caller_text_does_not_end_the_value(self, caller_text):
        message = self.MESSAGE.replace("Checking incidents for Acme, then paging", caller_text)

        scrubbed = redact_telemetry_text(message)

        assert "Acme" not in scrubbed
        assert "tail" not in scrubbed
        assert "more" not in scrubbed
        assert "input_value=[redacted], input_type=str]" in scrubbed

    def test_posthog_error_message_is_scrubbed(self):
        event = {"properties": {"$mcp_error_message": self.MESSAGE}}

        scrubbed = scrub_posthog_mcp_event(event)["properties"]["$mcp_error_message"]

        assert "Acme" not in scrubbed

    def test_long_input_does_not_backtrack(self):
        import time

        hostile = "input_value=" + ", input_type=str" * 20_000
        start = time.perf_counter()
        redact_telemetry_text(hostile * 3)
        assert time.perf_counter() - start < 1.0
