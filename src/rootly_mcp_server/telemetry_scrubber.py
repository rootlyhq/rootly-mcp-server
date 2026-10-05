"""Scrubbing of credentials from telemetry strings.

AgentCat's redaction hook is `Callable[[str], str]`: one string at a time, no
field name, and the result replaces that string in every event field. So a rule
that over-matches destroys diagnostics -- an earlier version returned a constant
and collapsed every error message into one Sentry issue.

Nothing here keys off value length. `hunter2` is a credential and `Token
expired` is a diagnostic, and both are short.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# Values that describe a state rather than carry a credential, so that
# "authorization: failed" survives.
NON_SECRET_VALUES = frozenset(
    {
        "absent",
        "empty",
        "error",
        "expired",
        "failed",
        "invalid",
        "missing",
        "none",
        "null",
        "ok",
        "present",
        "required",
        "true",
        "false",
        "unknown",
        "unset",
        "valid",
    }
)

# Words that follow an auth scheme in prose: "Bearer token required".
NON_SECRET_SCHEME_VALUES = frozenset({"auth", "credentials", "header", "token"})

# From sentry_sdk's DEFAULT_DENYLIST, minus everything that only applies to
# browser sessions (PHPSESSID, connect.sid, csrf, ...) -- none reach this
# server -- and minus mysql_pwd, which no Rootly tool can produce.
# Compared against the key with separators removed, so prefixed and suffixed
# forms are covered too.
#
# Credentials only. Sentry's DEFAULT_PII_DENYLIST (emails, x_forwarded_for,
# ip_address, ...) is deliberately not applied: this telemetry exists to show
# which customers use the server and how, and that is worth more than the
# privacy margin of dropping a responder's address from an incident payload.
CREDENTIAL_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "privatekey",
    "credentials",
    "authorization",
    "cookie",
    "sessionid",
)

# Too short or too common to match as substrings: "auth" is inside "author",
# "token" inside "tokens". These count only as a whole key.
CREDENTIAL_KEYS_EXACT = frozenset({"auth", "token", "session"})

# `session_id` contains a credential word but is not a credential: it was
# AgentCat's correlation handle, and is now a retired argument (see below).
NON_CREDENTIAL_KEYS = frozenset({"session_id"})

# Parameters AgentCat used to inject into every tool schema and no longer does.
# `context` went with #223 (intent capture off); `session_id` went when session
# correlation moved to the SDK's hook mode (`resolve_session_id`). Clients
# holding a cached tool list still send both. Two places act on this set: the
# call middleware in `server.py` drops them before the tool validates its
# arguments, and `scrub_event_arguments` below removes them from the published
# event, because AgentCat records the raw arguments before any of our
# middleware runs.
RETIRED_INJECTED_PARAMS: frozenset[str] = frozenset({"context", "session_id"})


def is_credential_key(key: str) -> bool:
    """Whether a key name means its value should be removed."""
    normalized = key.strip().strip("\"'").lower()
    if normalized in NON_CREDENTIAL_KEYS:
        return False
    if normalized in CREDENTIAL_KEYS_EXACT:
        return True
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return any(word in compact for word in CREDENTIAL_KEY_SUBSTRINGS)


def _is_already_redacted(value: str) -> bool:
    return value.startswith("[redacted")


def _scrub_keyed_value(match: re.Match[str]) -> str:
    key, separator, value = match.group(1), match.group(2), match.group(3)
    if not is_credential_key(key):
        return match.group(0)
    if value.strip().lower() in NON_SECRET_VALUES or _is_already_redacted(value):
        return match.group(0)
    # The scheme rule ran first and already handled these; redacting again would
    # lose which scheme was used.
    if value.split(" ", 1)[0].lower() in {"bearer", "basic", "token"}:
        return match.group(0)
    return f"{key}{separator}{REDACTED}"


def _scrub_quoted_value(match: re.Match[str]) -> str:
    key, opening, value, closing = (
        match.group(1),
        match.group(2),
        match.group(3),
        match.group(4),
    )
    if not is_credential_key(key):
        return match.group(0)
    if value.strip().lower() in NON_SECRET_VALUES or _is_already_redacted(value):
        return match.group(0)
    return f"{key}{opening}{REDACTED}{closing}"


def _scrub_scheme_credential(match: re.Match[str]) -> str:
    scheme, value = match.group(1), match.group(2)
    if value.lower() in NON_SECRET_VALUES | NON_SECRET_SCHEME_VALUES:
        return match.group(0)
    return f"{scheme} {REDACTED}"


# Matched loosely and judged by `is_credential_key`, so a prefix cannot hide it:
# `\bsecret` never matches inside `aws_secret_access_key`.
#
# Possessive. The class excludes every character that can follow a key -- quote,
# space, `=`, `:` -- so giving characters back can never turn a failure into a
# match. Without `*+`, dotted text like "a.b.a.b..." is swallowed whole and then
# backtracked from thousands of start positions: 27s on a 60KB string.
_KEY = r"([A-Za-z_][A-Za-z0-9_.\[\]-]{0,63}+)"

TELEMETRY_REDACTIONS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # Pydantic validation errors repeat the rejected argument verbatim:
    # `[type=unexpected_keyword_argument, input_value='<caller text>', input_type=str]`.
    # That is caller-supplied content, not a diagnostic -- the type and field
    # already say what went wrong. Runs first so no later rule redacts only part
    # of it. Everything to the end of the line goes, keeping only a trailing
    # `, input_type=<type>]` -- Pydantic always puts that last, and anchoring to
    # the line end means caller text containing `, input_type=` cannot end the
    # value early. A truncated message loses the whole tail instead.
    (
        re.compile(r"input_value=[^\n]*?(, input_type=[\w.]+\])?$", re.MULTILINE),
        r"input_value=[redacted]\1",
    ),
    (
        re.compile(r"\b(Bearer|Basic|Token)\s+([A-Za-z0-9._~+/=-]+)", re.IGNORECASE),
        _scrub_scheme_credential,
    ),
    # Quoted values run before unquoted so a multi-word value goes whole. A
    # backslashed quote is the delimiter in stringified JSON but content in
    # plain quoting, so those are separate families -- one pattern tolerating
    # both closed early on `\"` and left the tail in plaintext.
    #
    # Stringified JSON: the delimiter carries the backslash, so a lone backslash
    # is content only when no quote follows.
    *(
        (
            re.compile(
                rf"\b{_KEY}"
                rf"(\\{quote}\s*[=:]\s*\\{quote})"
                rf"((?:[^{quote}\\]|\\(?!{quote}))*)"
                rf"(\\{quote})",
            ),
            _scrub_quoted_value,
        )
        for quote in ('"', "'")
    ),
    # Plain quoting, including the Python repr form a stringified argument dict
    # arrives in: {'password': 'hunter2'}.
    *(
        (
            re.compile(
                rf"\b{_KEY}"
                rf"({quote}?\s*[=:]\s*{quote})"
                rf"((?:\\.|[^{quote}\\])*)"
                rf"({quote})",
            ),
            _scrub_quoted_value,
        )
        for quote in ('"', "'")
    ),
    (
        re.compile(rf"\b{_KEY}(\s*[=:]\s*)([^\s,&\"']+)"),
        _scrub_keyed_value,
    ),
    # The body is base64 rather than `.*?` so an unterminated header cannot make
    # every start position scan to the end, which is quadratic once headers
    # repeat: 437ms versus 7ms on 54KB.
    (
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----"
            r"[A-Za-z0-9+/=\s]*"
            r"-----END[A-Z ]*PRIVATE KEY-----"
        ),
        "[redacted-private-key]",
    ),
    # postgres://user:pw@host -- the host stays, since it is usually the point
    # of the error message. The user part is optional: redis://:pw@host is
    # valid and was leaking.
    (
        re.compile(r"\b([a-z][a-z0-9+.-]{0,31}+://)([^\s:/@]*+):([^\s/@]+)@", re.IGNORECASE),
        r"\1\2:[redacted]@",
    ),
    # Tokens identifiable from shape alone, for values that arrive with no key.
    # Only unambiguous prefixes: an entropy check would also match incident IDs
    # and slugs.
    #
    # The JWT segments are possessive. Each class already excludes `.`, so the
    # greedy match stops at the separator anyway and giving back characters can
    # never help -- but without `+` a non-JWT run like "eyJ" + 30k letters made
    # the three segments backtrack against each other for 1.9s.
    (
        re.compile(
            r"\b(?:"
            r"eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]++"  # JWT
            r"|gh[pousr]_[A-Za-z0-9]{16,}"  # GitHub
            r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
            r"|sk-[A-Za-z0-9_-]{16,}"  # OpenAI / Anthropic
            r"|AKIA[0-9A-Z]{16}"  # AWS access key ID
            r"|AIza[A-Za-z0-9_-]{20,}"  # Google
            r")"
        ),
        REDACTED,
    ),
    # `\w` rather than [A-Za-z0-9] so accented and non-Latin local parts are
    # covered too -- resume@x.com was matched but résumé@x.com was not.
)


# Depth past which an argument subtree is replaced instead of walked. AgentCat
# truncates events to depth 5 anyway, and this runs before that, so the limit is
# generous -- it exists to bound the walk, not to shape the payload.
_MAX_ARGUMENT_DEPTH = 32


def scrub_event_arguments(event: Any) -> Any:
    """Scrub credential-named arguments from an event, keys included.

    Registered as AgentCat's `redact_event` hook, which -- unlike
    `redact_sensitive_information` -- receives the whole event rather than one
    string at a time. That is the only place the field name is visible: the SDK
    walks the argument dict and hands the string hook bare values, so `hunter2`
    arrives with nothing to say it came from a field called `password`.

    Also removes what a cached client still sends from the retired injected
    parameters, and clears `user_intent`. AgentCat sits at index 0 of the
    middleware chain and copies the raw arguments before our call middleware
    strips them, and it fills `user_intent` from a `context` argument whenever
    one is present, even with `enable_tool_call_context` off. Intent capture
    is off on purpose (#223): a narrative of the user's goal is not something
    this server collects, so neither field may reach the exporter.

    Beyond that only `parameters` is touched. `response` and `error` are free
    text with no keys to read, so they stay with the string scrubber.
    """

    def walk(node: Any, depth: int) -> Any:
        # Past the limit the subtree is replaced rather than descended into.
        # Arguments are attacker-shaped JSON, and an unbounded walk raises
        # RecursionError on deep nesting or on a cycle -- which AgentCat turns
        # into a dropped event. Replacing keeps the failure on the safe side:
        # a value is never published just because it was buried deeply.
        if depth > _MAX_ARGUMENT_DEPTH:
            return REDACTED
        if isinstance(node, dict):
            return {
                key: REDACTED if is_credential_key(str(key)) else walk(value, depth + 1)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        if isinstance(node, tuple):
            # JSON cannot produce one, but a credential inside a tuple would
            # otherwise be published: the SDK's own walker skips them too.
            return tuple(walk(item, depth + 1) for item in node)
        return node

    parameters = getattr(event, "parameters", None)
    if parameters:
        scrubbed = walk(parameters, 0)
        arguments = scrubbed.get("arguments") if isinstance(scrubbed, dict) else None
        if isinstance(arguments, dict):
            for name in RETIRED_INJECTED_PARAMS:
                arguments.pop(name, None)
        event.parameters = scrubbed
    if getattr(event, "user_intent", None) is not None:
        event.user_intent = None
    return event


class TelemetryScrubber:
    """Removes credentials from telemetry strings, preserving everything else."""

    def scrub(self, value: str) -> str:
        if not value:
            return value
        scrubbed = value
        for pattern, replacement in TELEMETRY_REDACTIONS:
            scrubbed = pattern.sub(replacement, scrubbed)
        return scrubbed


_DEFAULT_SCRUBBER = TelemetryScrubber()


def redact_agentcat_telemetry_text(value: str) -> str:
    """AgentCat's `redact_sensitive_information` hook."""
    return _DEFAULT_SCRUBBER.scrub(value)


# PostHog MCP properties that carry caller-supplied or tool-produced text.
# Error messages and tool names are included because an unknown tool name is
# echoed verbatim into both. Everything else is SDK-generated metadata (ids,
# timings, client info) and is left untouched.
_POSTHOG_MCP_SCRUBBED_PROPERTIES = (
    "$mcp_parameters",
    "$mcp_response",
    "$mcp_error_message",
    "$exception_list",
    "$mcp_tool_name",
    "$mcp_resource_name",
)


def scrub_posthog_mcp_event(event: Any) -> Any:
    """PostHog MCP analytics' `before_send` hook.

    Applies both AgentCat rules in one pass: credential-named keys are
    replaced, and every remaining string goes through the text scrubber.
    """

    def walk(node: Any, depth: int) -> Any:
        if depth > _MAX_ARGUMENT_DEPTH:
            return REDACTED
        if isinstance(node, str):
            return _DEFAULT_SCRUBBER.scrub(node)
        if isinstance(node, dict):
            return {
                key: REDACTED if is_credential_key(str(key)) else walk(value, depth + 1)
                for key, value in node.items()
            }
        if isinstance(node, list | tuple):
            return [walk(item, depth + 1) for item in node]
        return node

    properties = event.get("properties")
    if isinstance(properties, dict):
        for name in _POSTHOG_MCP_SCRUBBED_PROPERTIES:
            if name in properties:
                properties[name] = walk(properties[name], 0)
    return event
