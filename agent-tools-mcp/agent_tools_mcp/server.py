"""Stdio MCP server exposing the agent-tools.cloud x402 directory.

Tools:
- search(intent, top_k, max_price_usd, category) - natural-language service discovery
- get(slug)                                       - full details + call template
- list_categories()                               - browse by category
- stats()                                         - directory-wide stats

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
    return f"agent-tools-mcp/{__version__} (+https://github.com/JoursBleu/agent-tools-mcp)"


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="agent-tools",
        instructions=(
            "Use these tools to find and inspect x402 paid services from the "
            "agent-tools.cloud directory. Call `search` first with a natural "
            "language intent, then `get` to fetch full call details."
        ),
    )

    @mcp.tool()
    async def search(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Find x402 paid services matching a natural-language intent.

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
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
        ) as cx:
            r = await cx.get(f"{_api_base()}/api/v1/search", params=params)
            r.raise_for_status()
            data = r.json()

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
        """Get full details (URL, price, schema, call template) of a service by slug."""
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
        ) as cx:
            r = await cx.get(f"{_api_base()}/api/v1/services/{slug}")
            r.raise_for_status()
            return r.json()

    @mcp.tool()
    async def list_categories() -> dict[str, Any]:
        """List all available service categories in the directory."""
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
        ) as cx:
            r = await cx.get(f"{_api_base()}/api/v1/categories")
            r.raise_for_status()
            return r.json()

    @mcp.tool()
    async def stats() -> dict[str, Any]:
        """High-level stats about the directory: total services, healthy count, sources."""
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
        ) as cx:
            r = await cx.get(f"{_api_base()}/api/v1/stats")
            r.raise_for_status()
            return r.json()

    log.info("agent-tools-mcp server built (api_base=%s)", _api_base())
    return mcp
