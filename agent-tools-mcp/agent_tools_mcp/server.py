"""Stdio MCP server exposing the agent-tools.cloud agent resource directory.

agent-tools.cloud indexes three kinds of agent-callable resources:

- **x402 paid services** — HTTP APIs an agent can pay-per-call (USDC on Base etc.)
- **MCP servers** — tool/context servers an agent can connect to
- **A2A agents** — peer agents an agent can delegate tasks to

Tools:
- search(intent, top_k, max_price_usd, category)  - x402 paid-service discovery
- get(slug)                                        - full x402 service call template
- list_categories()                                - browse x402 categories
- search_mcp_servers(intent, top_k, chain)         - MCP server discovery
- get_mcp_server(slug)                             - full MCP server record
- search_agents(intent, top_k, x402_only)          - A2A agent discovery
- get_agent(slug)                                  - full A2A agent card
- search_all(intent, protocol, top_k)              - unified search across all three
- stats()                                          - directory-wide stats (all protocols)

This is a discovery layer, not a facilitator: the agent keeps full custody of
payment. Tools never auto-pay; they only find resources and return call templates.

Reads `AGENT_TOOLS_API_BASE` env var (default https://agent-tools.cloud).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agent_tools_mcp")

DEFAULT_API_BASE = "https://agent-tools.cloud"
DEFAULT_TIMEOUT = 15.0


def _api_base() -> str:
    return os.environ.get("AGENT_TOOLS_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _user_agent() -> str:
    from . import __version__
    return f"agent-tools-mcp/{__version__} (+https://github.com/AgentTools-Cloud/AgentToolsCollection)"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
    )


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with _client() as cx:
        r = await cx.get(f"{_api_base()}{path}", params=params)
        r.raise_for_status()
        return r.json()


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="agent-tools",
        instructions=(
            "Discover agent-callable resources from the agent-tools.cloud directory: "
            "x402 paid services, MCP servers and A2A agents. "
            "Use `search` for x402 paid APIs, `search_mcp_servers` for MCP tool servers, "
            "`search_agents` for A2A peer agents, or `search_all` to look across all three. "
            "Then call the matching `get*` tool for the full call template. "
            "This is a discovery layer only — it never pays on your behalf."
        ),
    )

    # ------------------------------------------------------------------ x402
    @mcp.tool()
    async def search(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Find **x402 paid services** matching a natural-language intent.

        x402 services are HTTP APIs the agent pays for per call (USDC on Base etc.).

        Args:
            intent: What the agent wants to do, in plain English or Chinese
                (e.g. "fetch user tweets", "check on-chain whale activity").
            top_k: Max number of services to return (default 5, max 25).
            max_price_usd: Hard upper bound on per-call price in USD. Services
                whose cheapest call exceeds this are filtered out.
            category: Optional category filter (see `list_categories`).
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if category:
            params["category"] = category
        data = await _get("/api/v1/search", params)

        items = data.get("items") or data.get("services") or []
        if max_price_usd is not None:
            kept = []
            for s in items:
                price = s.get("price_min")
                if price is None:
                    price = s.get("price_max")
                if price is None or float(price) <= max_price_usd:
                    kept.append(s)
            items = kept
        return {
            "intent": intent,
            "count": len(items[:top_k]),
            "items": items[:top_k],
        }

    @mcp.tool()
    async def get(slug: str) -> dict[str, Any]:
        """Get full details (URL, price, schema, call template) of an x402 service by slug."""
        return await _get(f"/api/v1/services/{slug}")

    @mcp.tool()
    async def list_categories() -> dict[str, Any]:
        """List all available x402 service categories in the directory."""
        return await _get("/api/v1/categories")

    # ------------------------------------------------------------------- MCP
    @mcp.tool()
    async def search_mcp_servers(
        intent: str,
        top_k: int = 5,
        chain: str | None = None,
    ) -> dict[str, Any]:
        """Find **MCP servers** (tool/context servers) matching an intent.

        Use this when the agent wants extra tools or context via the Model
        Context Protocol, rather than a one-shot paid HTTP call.

        Args:
            intent: What capability the agent is looking for, plain language.
            top_k: Max number of servers to return (default 5, max 25).
            chain: Optional chain filter for x402-capable MCP servers
                (e.g. "base", "solana").
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if chain:
            params["chain"] = chain
        data = await _get("/api/v1/mcp/search", params)
        servers = data.get("servers") or []
        return {
            "intent": intent,
            "count": len(servers[:top_k]),
            "total_matched": data.get("total_matched"),
            "servers": servers[:top_k],
        }

    @mcp.tool()
    async def get_mcp_server(slug: str) -> dict[str, Any]:
        """Get full details (endpoint URL, transport, capabilities) of an MCP server by slug."""
        return await _get(f"/api/v1/mcp/servers/{slug}")

    # ------------------------------------------------------------------- A2A
    @mcp.tool()
    async def search_agents(
        intent: str,
        top_k: int = 5,
        x402_only: bool = False,
    ) -> dict[str, Any]:
        """Find **A2A agents** (peer agents) the agent can delegate a task to.

        A2A agents publish an agent-card and an A2A JSON-RPC endpoint; use this
        when the task is better handed off to another agent than called directly.

        Args:
            intent: The task or capability to delegate, plain language.
            top_k: Max number of agents to return (default 5, max 25).
            x402_only: If true, only return agents that accept x402 payment.
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if x402_only:
            params["x402_only"] = "true"
        data = await _get("/api/v1/a2a/search", params)
        agents = data.get("agents") or []
        return {
            "intent": intent,
            "count": len(agents[:top_k]),
            "agents": agents[:top_k],
        }

    @mcp.tool()
    async def get_agent(slug: str) -> dict[str, Any]:
        """Get the full A2A agent card (endpoint, skills, auth, x402 info) by slug."""
        return await _get(f"/api/v1/a2a/agents/{slug}")

    # --------------------------------------------------------------- unified
    @mcp.tool()
    async def search_all(
        intent: str,
        protocol: str | None = None,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """Unified search across **x402 services, MCP servers and A2A agents**.

        Use this when you don't yet know which resource type fits best — it
        ranks all three together and tags each result with its `protocol`.

        Args:
            intent: What the agent wants to accomplish, plain language.
            protocol: Optional filter, one of "x402", "mcp", "a2a". Omit for all.
            top_k: Max number of results to return (default 8, max 50).
        """
        top_k = max(1, min(int(top_k), 50))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if protocol:
            params["protocol"] = protocol
        return await _get("/api/v1/resources/search", params)

    # ----------------------------------------------------------------- stats
    @mcp.tool()
    async def stats() -> dict[str, Any]:
        """Directory-wide stats across all protocols: x402 services, MCP servers, A2A agents."""
        out: dict[str, Any] = {}
        try:
            out["x402"] = await _get("/api/v1/stats")
        except Exception as e:  # noqa: BLE001
            out["x402"] = {"error": str(e)}
        try:
            out["mcp"] = await _get("/api/v1/mcp/stats")
        except Exception as e:  # noqa: BLE001
            out["mcp"] = {"error": str(e)}
        try:
            out["a2a"] = await _get("/api/v1/a2a/stats")
        except Exception as e:  # noqa: BLE001
            out["a2a"] = {"error": str(e)}
        return out

    log.info("agent-tools-mcp server built (api_base=%s)", _api_base())
    return mcp
