# The image installs exactly what uv.lock resolves, so production runs the same
# dependency versions CI tests. Never `pip install` / `uv pip install` the
# project here: that re-resolves at build time and silently ships newer
# versions than the lock.

# uv is pinned (Dependabot's docker ecosystem bumps this tag). The binary is
# copied in rather than pip-installed so it never lands in the runtime image.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# ---------------------------------------------------------------------------
# Builder: resolve nothing, install the lock into /opt/venv.
#
# Both stages must use the same tag, and it must be a version the CI test
# matrix covers (test_dependency_pins.py enforces this). Every locked package
# needs a wheel for it: there is no compiler here, so a package without one
# fails the build instead of being compiled from source.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Compile bytecode at build time (faster cold start, and appuser cannot write
# __pycache__ into a root-owned venv anyway). Copy instead of hardlinking out
# of the cache mount. Use the base image's interpreter, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock
# change. --locked installs only what uv.lock says and fails the build if the
# lock no longer matches pyproject.toml, instead of re-resolving.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, as a regular (non-editable) wheel install so the
# venv does not depend on /app. The wheel bundles data/swagger.json via
# [tool.hatch.build] artifacts in pyproject.toml.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# AgentCat (formerly MCPcat) provides hosted telemetry and is deliberately kept
# out of the project's dependency graph. The [community] extra provides FastMCP
# support, which this server relies on. v2 supports the MCP 2026-07-28 spec and
# tracks sessions via a session_id in the tool list rather than stateful
# connections — a better fit for our stateless hosted transport.
#
# It is installed with every locked version as a constraint, so it can only add
# its own packages: it can never move a locked dependency (pydantic, mcp,
# fastmcp, ...). If a future AgentCat needs a version the lock does not allow,
# this step fails the build rather than shipping an untested combination.
# `uv pip check` then confirms the final environment is consistent. This step
# must stay after the last `uv sync`, which removes anything not in the lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --quiet --locked --no-dev --no-emit-project --no-hashes -o /tmp/constraints.txt \
    && uv pip install --python /opt/venv/bin/python \
        --constraint /tmp/constraints.txt \
        "agentcat[community]==2.1.0" \
    && uv pip check --python /opt/venv/bin/python

# ---------------------------------------------------------------------------
# Runtime: same base image, no compiler, no uv, no build cache.
# ---------------------------------------------------------------------------
FROM python:3.14-slim

# curl stays for container health checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The venv's interpreter symlinks to /usr/local/bin/python3.14, which is the
# same path in both stages because they share a base image.
COPY --from=builder /opt/venv /opt/venv

# Create non-root user. The venv stays root-owned (read-only to the server);
# the working directory is the user's, as before.
RUN useradd -m -u 1000 appuser \
    && mkdir /app && chown appuser:appuser /app

WORKDIR /app

# Expose the port the app runs on
EXPOSE 8000

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV FASTMCP_HOST=0.0.0.0
ENV FASTMCP_PORT=8000
ENV FASTMCP_STATELESS_HTTP=true
ENV ROOTLY_TRANSPORT=both
ENV ROOTLY_LOG_LEVEL=INFO

# Switch to non-root user
USER appuser

# Run the application. `exec` replaces the shell so the server is PID 1 and
# receives SIGTERM directly; without it the shell swallows the signal, ECS
# SIGKILLs the task after stopTimeout, and the graceful shutdown never runs.
CMD ["sh", "-c", "exec rootly-mcp-server --transport \"$ROOTLY_TRANSPORT\" --log-level \"$ROOTLY_LOG_LEVEL\" --hosted"]
