# Minimal container for the agent-tools-mcp MCP server (stdio transport).
# Used by Glama / Smithery scanners and anyone wanting a sandboxed run.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="agent-tools-mcp"
LABEL org.opencontainers.image.description="Discover and call x402 paid services from any MCP-compatible agent."
LABEL org.opencontainers.image.source="https://github.com/JoursBleu/agent-tools-mcp"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

# Copy package source and install (uses pyproject.toml).
COPY pyproject.toml README.md ./
COPY agent_tools_mcp ./agent_tools_mcp
RUN pip install --no-cache-dir .

# stdio MCP server — no ports to expose. Clients launch this binary directly.
ENTRYPOINT ["agent-tools-mcp"]
