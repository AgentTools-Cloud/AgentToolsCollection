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
    "Directory of x402-paid + MCP services on agent-tools.cloud. Built for "
    "AGENT consumers — humans use the website, agents use these tools.\n\n"
    "Typical flow:\n"
    "  1. `search(intent=..., has_mcp=True)` to find candidates by natural\n"
    "     language; or `search(category=..., has_mcp=True)` to browse.\n"
    "  2. Inspect `match_reason` + `confidence` + `tx_30d` on each item to\n"
    "     judge quality. Prefer `popular+healthy` items.\n"
    "  3. `get(slug)` to fetch full details including `call_template` —\n"
    "     a ready-to-paste snippet showing how to invoke the service\n"
    "     (MCP streamable-http and/or x402 HTTP).\n"
    "  4. Use `list_categories` to discover the taxonomy and `stats` for\n"
    "     directory size + health."
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
    intent: str | None = None,
    top_k: int = 5,
    max_price_usd: float | None = None,
    category: str | None = None,
    chain: str | None = None,
    min_confidence: float | None = None,
    has_mcp: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find x402 / MCP services matching an intent or filter set.

    Two usage modes (agents pick whichever fits):
      A. Natural-language: `search(intent="fetch tweets for @user")`
      B. Pure browse:      `search(has_mcp=True, category="defi", top_k=10)`
         At least one of `intent`, `category`, `chain`, `has_mcp`,
         `min_confidence` must be supplied — otherwise the call is
         rejected (we won't dump 2300+ rows).

    Results are ranked by:
        (health=ok AND tx_30d>0) → health=ok → has-quality-signal →
        confidence → tx_30d → recency.
    So the highest-quality real-traffic services appear first.

    Each item includes (when available):
      - confidence    : 0.0–1.0 x402scan quality score.
      - tx_30d        : 30-day x402 payment count (proxy for real usage).
      - match_snippet : FTS snippet showing where `intent` hit ([[token]]).
      - match_reason  : list[str] of human-readable ranking signals.
      - mcp_url       : populated when the service exposes an MCP endpoint
                        (you can call it directly via streamable-http).
    Agents should prefer items with non-null confidence and tx_30d > 0
    unless the user explicitly wants experimental endpoints.

    Args:
        intent: What the agent wants to do (English or Chinese). Optional
            when at least one structured filter is set. Synonym expansion
            covers twitter↔X↔推特, whale↔巨鲸, price↔价格 etc.
        top_k: Max services to return (default 5, hard cap 25).
        max_price_usd: Upper bound on per-call price in USD.
        category: Filter (see `list_categories`).
        chain: "base", "polygon", "solana", "arbitrum", ...
        min_confidence: Minimum confidence (0.0–1.0). 0.8+ keeps only
            services x402scan rates as high-quality.
        has_mcp: When true, return only services with a callable MCP
            endpoint. Use this when the agent wants to chain another MCP
            server rather than perform raw HTTP+x402.
    """
    if not any((intent, category, chain, has_mcp,
                min_confidence is not None)):
        return {
            "error": "no_filter",
            "message": (
                "Provide at least one of: intent, category, chain, "
                "has_mcp=true, min_confidence. The directory has 2000+ "
                "services — pick a starting point."
            ),
        }
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


def _build_call_template(row: dict) -> dict[str, Any]:
    """Return ready-to-paste call snippets for both MCP and x402 HTTP modes.

    Agents typically need: (a) the exact endpoint URL, (b) which transport
    (MCP streamable-http vs raw HTTP + 402 handshake), (c) a code skeleton
    they can fill in. We hand them all three in one place.
    """
    out: dict[str, Any] = {}
    mcp_url = (row.get("mcp_url") or "").strip()
    if mcp_url:
        out["mcp"] = {
            "transport": "streamable-http",
            "url": mcp_url,
            "python": (
                "from mcp import ClientSession\n"
                "from mcp.client.streamable_http import streamablehttp_client\n"
                "async with streamablehttp_client(\n"
                f"    {mcp_url!r}\n"
                ") as (r, w, _):\n"
                "    async with ClientSession(r, w) as s:\n"
                "        await s.initialize()\n"
                "        tools = await s.list_tools()\n"
                "        # await s.call_tool('<tool_name>', { ... })"
            ),
            "inspector_cli": (
                f"npx @modelcontextprotocol/inspector --transport http {mcp_url}"
            ),
            "claude_desktop_config": {
                row.get("slug") or "service": {
                    "type": "streamable-http",
                    "url": mcp_url,
                }
            },
        }

    url = (row.get("url") or "").strip()
    if url:
        chains = row.get("chains") or []
        price_min = row.get("price_min")
        price_hint = (
            f"(≈${price_min}/call in USDC)" if price_min is not None
            else "(price advertised in 402 response)"
        )
        out["http_x402"] = {
            "url": url,
            "flow": [
                f"1. GET/POST {url}  →  receives HTTP 402 + accepts[] header",
                "2. Pick a row from accepts[] (network + maxAmountRequired).",
                "3. Sign an EIP-3009 USDC transferWithAuthorization payload.",
                "4. Retry with header `X-PAYMENT: <base64 payload>`.",
                "5. Service settles via facilitator and returns the result.",
            ],
            "curl_probe": (
                f'curl -sS -i -X POST "{url}"  '
                "# expect HTTP/1.1 402 Payment Required " + price_hint
            ),
            "well_known": row.get("well_known_url"),
            "chains": chains,
            "facilitator": row.get("facilitator"),
        }

    if not out:
        out["note"] = "Service has no callable endpoint registered yet."
    return out


@discover_mcp.tool()
async def get(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Get full details + ready-to-paste call template for a service.

    Returns the service row plus a `call_template` field:
      - call_template.mcp        : how to call via MCP streamable-http
                                   (python snippet, inspector CLI line,
                                   Claude Desktop config fragment).
      - call_template.http_x402  : 5-step HTTP+402 payment flow with the
                                   exact endpoint URL and a curl probe.
    Use this AFTER `search` to grab the snippet — no need for the agent
    to hand-craft an x402 client.

    Args:
        slug: Service slug as returned by `search` items.
    """
    with _open() as conn:
        row = directory_db.get_by_slug(conn, slug)
    if row is None:
        _log_call("get", ctx=ctx, args={"slug": slug}, result_n=0)
        return {"error": "not_found", "slug": slug}
    row["call_template"] = _build_call_template(row)
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


_REGISTER_RATE_LIMIT_PER_IP_PER_DAY = 5


@discover_mcp.tool()
async def register(
    url: str,
    name: str | None = None,
    description: str | None = None,
    mcp_url: str | None = None,
    category: str | None = None,
    chains: list[str] | None = None,
    price_min_usdc: float | None = None,
    price_max_usdc: float | None = None,
    contact: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Self-register an x402 / MCP service in the agent-tools directory.

    Service owners and agents may submit new services here. Submissions
    land in a pending queue and are reviewed by a human before they show
    up in `search` results. Listing is FREE.

    Dedup: if a service with the same canonical origin (scheme://host)
    already exists in the directory we return its slug instead of
    creating a duplicate submission. Same goes for a still-pending
    submission with the same origin.

    Rate limit: at most 5 pending submissions per client IP per 24h.
    Hits beyond that get `{error: rate_limited}` — try again later or
    email contact@agent-tools.cloud for bulk imports.

    Args:
        url: Public HTTPS URL of the service (the x402-payable endpoint
            or its homepage). Required.
        name: Human-friendly name. Defaults to the URL hostname.
        description: One-paragraph description (max ~2000 chars).
        mcp_url: If the service speaks MCP, its streamable-http endpoint.
        category: Free-form (e.g. "defi", "search", "social"). Use
            `list_categories` to align with existing taxonomy.
        chains: Networks the service accepts payment on
            (e.g. ["base", "solana"]).
        price_min_usdc: Lower bound of per-call price in USDC.
        price_max_usdc: Upper bound of per-call price in USDC.
        contact: Optional email / handle the directory team can reach
            you on for clarifications.
    """
    # ---- 1. Validation -------------------------------------------------
    if not url or not isinstance(url, str):
        return {"error": "invalid_url", "message": "url is required"}
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "invalid_url", "message": "url must start with http:// or https://"}
    if len(url) > 500:
        return {"error": "invalid_url", "message": "url too long (max 500)"}
    if mcp_url:
        mcp_url = mcp_url.strip()
        if not (mcp_url.startswith("http://") or mcp_url.startswith("https://")):
            return {"error": "invalid_mcp_url",
                    "message": "mcp_url must start with http:// or https://"}
    if description and len(description) > 2000:
        description = description[:2000]

    client_name = _client_name_from_ctx(ctx)
    client_ip = _current_client_ip.get()

    # ---- 2. Dedup ------------------------------------------------------
    with _open() as conn:
        existing = directory_db.find_service_by_url(conn, url)
        if not existing and mcp_url:
            existing = directory_db.find_service_by_url(conn, mcp_url)
        pending = directory_db.find_pending_submission(conn, url)
        if not pending and mcp_url:
            pending = directory_db.find_pending_submission(conn, mcp_url)

    if existing:
        _log_call("register", ctx=ctx,
                  args={"url": url, "outcome": "already_listed"},
                  result_n=0, result_slug=existing.get("slug"))
        return {
            "status": "already_listed",
            "message": "A service with this URL is already in the directory.",
            "slug": existing.get("slug"),
            "url": existing.get("url"),
        }
    if pending:
        _log_call("register", ctx=ctx,
                  args={"url": url, "outcome": "already_pending"}, result_n=0)
        return {
            "status": "already_pending",
            "message": "A submission for this URL is already awaiting review.",
            "submission_id": pending.get("id"),
        }

    # ---- 3. Rate limit -------------------------------------------------
    if client_ip:
        with _open() as conn:
            recent = directory_db.count_recent_submissions(conn, client_ip)
        if recent >= _REGISTER_RATE_LIMIT_PER_IP_PER_DAY:
            _log_call("register", ctx=ctx,
                      args={"url": url, "outcome": "rate_limited",
                            "recent": recent}, result_n=0)
            return {
                "status": "rate_limited",
                "message": (
                    f"You have {recent} pending submissions in the last 24h "
                    f"(limit {_REGISTER_RATE_LIMIT_PER_IP_PER_DAY}). "
                    "Wait for review or email contact@agent-tools.cloud."
                ),
            }

    # ---- 4. Insert -----------------------------------------------------
    payload = {
        "url": url,
        "name": name,
        "description": description,
        "mcp_url": mcp_url,
        "category": category,
        "chains": chains,
        "price_min_usdc": price_min_usdc,
        "price_max_usdc": price_max_usdc,
        "contact": contact,
        "_client_name": client_name,
        "_client_ip": client_ip,
        "_source": "mcp-register",
    }
    try:
        with directory_db.writer(DB_PATH) as wc:
            sub_id = directory_db.create_submission(wc, payload)
    except Exception as e:
        log.exception("register failed: %r", e)
        return {"status": "error", "message": "submission_failed"}

    _log_call("register", ctx=ctx,
              args={"url": url, "outcome": "pending"},
              result_n=1, result_slug=str(sub_id))
    return {
        "status": "pending",
        "submission_id": sub_id,
        "message": (
            "Submission received. A human will review and (if approved) "
            "your service will appear in `search` within 1–2 days. "
            "Listing is free."
        ),
    }


log.info("directory MCP app built (db=%s, tools=5)", DB_PATH)
