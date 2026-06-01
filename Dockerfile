# Use Python 3.13 slim image as base
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Copy the entire project first for installation
COPY . .

# Install the package and its dependencies
RUN uv pip install --system --no-cache-dir -e .

# Install MCPcat for hosted telemetry without changing the repo dependency graph.
# MCPcat's current package metadata pins an older Pydantic range, but the SDK
# imports successfully with our runtime pin, so we restore the server's pinned
# version after installation.
RUN uv pip install --system --no-cache-dir mcpcat==0.1.14 \
    && uv pip install --system --no-cache-dir pydantic==2.13.4

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Expose the port the app runs on
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV FASTMCP_HOST=0.0.0.0
ENV FASTMCP_PORT=8000
ENV FASTMCP_STATELESS_HTTP=true
ENV ROOTLY_TRANSPORT=both
ENV ROOTLY_LOG_LEVEL=INFO

# Switch to non-root user
USER appuser

# Run the application
CMD ["sh", "-c", "rootly-mcp-server --transport \"$ROOTLY_TRANSPORT\" --log-level \"$ROOTLY_LOG_LEVEL\" --hosted"]
