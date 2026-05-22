"""FastMCP instance exposing the agent-tools directory as MCP tools.

Mounted at `/mcp-discovery` from server.py. UNGATED (free) so any MCP client
can browse and search the x402 service directory without payment.

Tools mirror the ones in the `agent-tools-mcp` PyPI package (the stdio variant
agents install locally), but run server-side directly against the SQLite DB —
no self-HTTP roundtrip.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import db as directory_db

log = logging.getLogger("mcpserver.directory.mcp")

DB_PATH = os.getenv("AGENT_TOOLS_DB_PATH", directory_db.DEFAULT_DB_PATH)

_INSTRUCTIONS = (
    "Search and inspect x402 paid services from the agent-tools.cloud directory.\n"
    "Start with `search` (natural-language intent), then `get` for full call details.\n"
    "Use `list_categories` to browse and `stats` for directory size + health."
)

_TS = TransportSecuritySettings(enable_dns_rebinding_protection=False)
discover_mcp = FastMCP(
    name="agent-tools",
    instructions=_INSTRUCTIONS,
    streamable_http_path="/",
    transport_security=_TS,
)


def _open():
    return directory_db.connect(DB_PATH, read_only=True)


def _log(tool, args=None, result_n=None, result_slug=None):
    """Best-effort usage log. Never raises."""
    try:
        with directory_db.writer(DB_PATH) as wc:
            directory_db.log_tool_call(
                wc, tool, args=args, result_n=result_n, result_slug=result_slug,
            )
    except Exception as e:
        log.warning("tool_call log failed: %r", e)


@discover_mcp.tool()
async def search(
    intent: str,
    top_k: int = 5,
    max_price_usd: float | None = None,
    category: str | None = None,
    chain: str | None = None,
) -> dict[str, Any]:
    """Find x402 paid services matching a natural-language intent.

    Args:
        intent: What the agent wants to do (English or Chinese), e.g.
            "fetch user tweets", "check on-chain whale activity".
        top_k: Max number of services (default 5, max 25).
        max_price_usd: Hard upper bound on per-call price in USD.
        category: Optional category filter (see `list_categories`).
        chain: Optional chain filter ("base", "polygon", "arbitrum", ...).
    """
    top_k = max(1, min(int(top_k), 25))
    with _open() as conn:
        rows = directory_db.search(
            conn,
            q=intent or None,
            category=category,
            chain=chain,
            limit=top_k * 3,  # over-fetch for price filter
        )
    items = [directory_db.row_to_dict(r) for r in rows]
    if max_price_usd is not None:
        items = [
            s for s in items
            if (s.get("price_usd") is None) or float(s["price_usd"]) <= max_price_usd
        ]
    out = {"intent": intent, "count": len(items[:top_k]), "items": items[:top_k]}
    _log("search", args={"intent": intent, "top_k": top_k, "max_price_usd": max_price_usd,
                          "category": category, "chain": chain},
         result_n=out["count"])
    return out


@discover_mcp.tool()
async def get(slug: str) -> dict[str, Any]:
    """Get full details (URL, price, schema, call template) of a service by slug."""
    with _open() as conn:
        row = directory_db.get_by_slug(conn, slug)
    if row is None:
        _log("get", args={"slug": slug}, result_n=0)
        return {"error": "not_found", "slug": slug}
    out = directory_db.row_to_dict(row)
    _log("get", args={"slug": slug}, result_n=1, result_slug=slug)
    return out


@discover_mcp.tool()
async def list_categories() -> dict[str, Any]:
    """List all available service categories in the directory."""
    with _open() as conn:
        cats = directory_db.list_categories(conn)
    out = {"items": cats, "count": len(cats)}
    _log("list_categories", result_n=out["count"])
    return out


@discover_mcp.tool()
async def stats() -> dict[str, Any]:
    """High-level stats about the directory: total services, healthy count, sources."""
    with _open() as conn:
        out = directory_db.stats(conn)
    _log("stats", result_n=out.get("total"))
    return out


log.info("directory MCP app built (db=%s, tools=4)", DB_PATH)
