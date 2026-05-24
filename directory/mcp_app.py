"""FastMCP instance exposing the agent-tools directory as MCP tools.

Mounted at `/mcp-discovery` from server.py. UNGATED (free) so any MCP client
can browse and search the x402 service directory without payment.

Tools mirror the ones in the `agent-tools-mcp` PyPI package (the stdio variant
agents install locally), but run server-side directly against the SQLite DB —
no self-HTTP roundtrip.
"""
from __future__ import annotations

import contextvars
import logging
import os
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
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


# ---- Client identity capture --------------------------------------------------
#
# Tool calls land in async tasks spawned by the streamable-http transport, so
# we stash the HTTP peer info into a ContextVar set by an ASGI middleware
# wrapper around `discover_mcp.streamable_http_app()`. `ctx.session.client_params
# .clientInfo` gives us the MCP-level client name (e.g. "claude-desktop").

_current_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_tools_client_ip", default=None,
)


def _extract_peer_ip(scope: dict) -> str | None:
    """Return the best-guess peer IP for an ASGI scope.

    Priority: CF-Connecting-IP (Cloudflare) → X-Forwarded-For (first hop) →
    direct ASGI client.host.
    """
    headers = scope.get("headers") or []
    cf = xff = None
    for k, v in headers:
        if k == b"cf-connecting-ip":
            cf = v.decode("latin-1", "replace").strip()
        elif k == b"x-forwarded-for" and xff is None:
            # Use the *first* IP — that's the original client per RFC 7239.
            xff = v.decode("latin-1", "replace").split(",", 1)[0].strip()
    if cf:
        return cf
    if xff:
        return xff
    client = scope.get("client")
    if client and isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return None


def wrap_with_client_capture(app):
    """ASGI middleware: stash the per-request client IP into a ContextVar.

    Tools later read `_current_client_ip.get()` inside `_log_call`.
    """
    async def wrapped(scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            ip = _extract_peer_ip(scope)
            token = _current_client_ip.set(ip)
            try:
                await app(scope, receive, send)
            finally:
                _current_client_ip.reset(token)
        else:  # lifespan etc.
            await app(scope, receive, send)
    return wrapped


def _client_name_from_ctx(ctx: Context | None) -> str | None:
    """Read MCP-level clientInfo name/version from the session."""
    if ctx is None:
        return None
    try:
        params = ctx.session.client_params
    except Exception:
        return None
    if params is None or params.clientInfo is None:
        return None
    info = params.clientInfo
    name = getattr(info, "name", None) or "unknown"
    ver = getattr(info, "version", None)
    return f"{name}/{ver}" if ver else name


def _open():
    return directory_db.connect(DB_PATH, read_only=True)


def _log_call(tool, ctx: Context | None = None, args=None,
              result_n=None, result_slug=None):
    """Best-effort usage log. Never raises."""
    try:
        client_name = _client_name_from_ctx(ctx)
        client_ip = _current_client_ip.get()
        with directory_db.writer(DB_PATH) as wc:
            directory_db.log_tool_call(
                wc, tool, args=args, result_n=result_n, result_slug=result_slug,
                client_name=client_name, client_ip=client_ip,
            )
    except Exception as e:
        log.warning("tool_call log failed: %r", e)


# ---- Tools --------------------------------------------------------------------


@discover_mcp.tool()
async def search(
    intent: str,
    top_k: int = 5,
    max_price_usd: float | None = None,
    category: str | None = None,
    chain: str | None = None,
    min_confidence: float | None = None,
    has_mcp: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find x402 paid services matching a natural-language intent.

    Results are ranked by: health → has-quality-signal → confidence
    → 30-day transaction volume → last-updated. So the highest-quality
    real-traffic services appear first.

    Each item in the response includes (when available):
      - confidence    : 0.0–1.0 x402scan quality score (higher = more reliable).
      - tx_30d        : 30-day x402 payment count (proxy for real usage).
      - match_snippet : FTS snippet showing where your `intent` hit
                        (matched tokens wrapped in [[ ]]).
      - match_reason  : list[str] of human-readable ranking signals — agents
                        can show these to users so they understand WHY a
                        service was picked.
    Agents should prefer items with non-null confidence and tx_30d > 0 unless
    the user explicitly wants experimental endpoints.

    Args:
        intent: What the agent wants to do (English or Chinese), e.g.
            "fetch user tweets", "check on-chain whale activity".
        top_k: Max number of services (default 5, max 25).
        max_price_usd: Hard upper bound on per-call price in USD.
        category: Optional category filter (see `list_categories`).
        chain: Optional chain filter ("base", "polygon", "arbitrum", ...).
        min_confidence: Optional minimum confidence threshold (0.0–1.0).
            E.g. 0.8 keeps only services x402scan rates as high-quality.
        has_mcp: When true, only return services that expose a callable
            MCP endpoint (i.e. `mcp_url` is set).
    """
    top_k = max(1, min(int(top_k), 25))
    with _open() as conn:
        items = directory_db.search(
            conn,
            q=intent or None,
            category=category,
            chain=chain,
            min_confidence=min_confidence,
            has_mcp=has_mcp,
            limit=top_k * 3,  # over-fetch for price filter
        )
    if max_price_usd is not None:
        items = [
            s for s in items
            if (s.get("price_min") is None) or float(s["price_min"]) <= max_price_usd
        ]
    items = items[:top_k]
    out = {"intent": intent, "count": len(items), "items": items}
    _log_call(
        "search", ctx=ctx,
        args={
            "intent": intent, "top_k": top_k, "max_price_usd": max_price_usd,
            "category": category, "chain": chain,
            "min_confidence": min_confidence, "has_mcp": has_mcp,
        },
        result_n=out["count"],
    )
    return out


@discover_mcp.tool()
async def get(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Get full details (URL, price, schema, call template) of a service by slug.

    Returned dict includes `confidence` (0–1, x402scan quality score) and
    `tx_30d` (30-day x402 payment count) when available — use these to
    judge whether a service is production-ready before calling it.
    """
    with _open() as conn:
        row = directory_db.get_by_slug(conn, slug)
    if row is None:
        _log_call("get", ctx=ctx, args={"slug": slug}, result_n=0)
        return {"error": "not_found", "slug": slug}
    _log_call("get", ctx=ctx, args={"slug": slug}, result_n=1, result_slug=slug)
    return row


@discover_mcp.tool()
async def list_categories(ctx: Context | None = None) -> dict[str, Any]:
    """List all available service categories in the directory."""
    with _open() as conn:
        cats = directory_db.list_categories(conn)
    out = {"items": cats, "count": len(cats)}
    _log_call("list_categories", ctx=ctx, result_n=out["count"])
    return out


@discover_mcp.tool()
async def stats(ctx: Context | None = None) -> dict[str, Any]:
    """High-level stats about the directory: total services, healthy count, sources."""
    with _open() as conn:
        out = directory_db.stats(conn)
    _log_call("stats", ctx=ctx, result_n=out.get("total"))
    return out


log.info("directory MCP app built (db=%s, tools=4)", DB_PATH)
