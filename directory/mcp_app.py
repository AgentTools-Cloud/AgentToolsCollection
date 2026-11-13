"""FastMCP instance exposing the agent-tools directory as MCP tools.

Mounted at `/mcp-discovery` from server.py. UNGATED (free) so any MCP client
can browse and search the x402 service directory without payment.

Tools mirror the ones in the `agent-tools-mcp` PyPI package (the stdio variant
agents install locally), but run server-side directly against the SQLite DB —
no self-HTTP roundtrip.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import secrets
from typing import Any

from fastapi import HTTPException, Request
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from . import ask as directory_ask
from . import a2a as directory_a2a
from . import cards
from . import db as directory_db
from . import jobs as directory_jobs
from . import limits
from . import paid_verify
from . import ownership as directory_ownership
from . import resources as directory_resources
from . import safety_service

log = logging.getLogger("mcpserver.directory.mcp")

DB_PATH = os.getenv("AGENT_TOOLS_DB_PATH", directory_db.DEFAULT_DB_PATH)
ASK_RATE_LIMITS = (
    ("minute", limits.env_int("AGENT_TOOLS_ASK_RATE_LIMIT_PER_MINUTE", 10), 60),
    ("day", limits.env_int("AGENT_TOOLS_ASK_RATE_LIMIT_PER_DAY", 200), 86400),
)

_INSTRUCTIONS = (
    "Directory of x402-paid + MCP services on agent-tools.cloud. Built for "
    "AGENT consumers — humans use the website, agents use these tools.\n\n"
    "Typical flow:\n"
    "  1. `ask_services(intent=...)` for an LLM-ranked recommendation, or\n"
    "     `search(intent=..., has_mcp=True)` to manually inspect candidates.\n"
    "  2. Inspect `match_reason` + `confidence` + `tx_30d` on each item to\n"
    "     judge quality. Prefer `popular+healthy` items.\n"
    "  3. `get(slug)` to fetch the service card: payment, call, quality,\n"
    "     resource samples, and ready-to-paste MCP/x402 call hints.\n"
    "  4. Use `list_categories` to discover the taxonomy and `stats` for\n"
    "     directory size + health.\n"
    "  5. `scan_mcp_safety(endpoint_url)` screens an MCP server for malware /\n"
    "     prompt-injection lures: returns our latest stored verdict if it is\n"
    "     already indexed, otherwise probes + scans it live and adds it to the\n"
    "     directory.\n"
    "  6. `prepare_paid_verification` -> sign `typed_data` in your wallet ->\n"
    "     `execute_paid_verification`: pay a service once and record the\n"
    "     result. Needs an API key (`create_api_key` or POST /api/v1/keys)."
    "\n  7. With an account API key in the Authorization: Bearer header, use "
    "`claim_service_url(url)` -> publish the returned token -> "
    "`verify_service_url(claim_id, token)` -> `get_owned_listing` / "
    "`edit_owned_listing`. A claim covers only its exact public HTTPS URL, "
    "not the host or sibling services."
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Adds or rescans a directory listing; repeating it does not create duplicates.
_SCAN = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=True, openWorldHint=True)
_SUBMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                          idempotentHint=False, openWorldHint=True)
_QUOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=False, openWorldHint=False)
# Spends the caller's funds irreversibly, so clients should confirm first.
_PAYMENT = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                           idempotentHint=False, openWorldHint=True)
_CLAIM_URL = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=False, openWorldHint=False)
_VERIFY_URL = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                              idempotentHint=True, openWorldHint=True)
_OWNER_EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                              idempotentHint=True, openWorldHint=False)
# Creates a new anonymous account + key; every call makes another one.
_MINT_KEY = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=False)

_TS = TransportSecuritySettings(enable_dns_rebinding_protection=False)
discover_mcp = FastMCP(
    name="agent-tools",
    instructions=_INSTRUCTIONS,
    streamable_http_path="/",
    transport_security=_TS,
    # uvicorn runs 2 workers; a stateful session would live in only one of them
    stateless_http=True,
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
# Raw `Authorization` header of the current MCP HTTP request. Only read by the
# authenticated account tools; never logged.
_current_authorization: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_tools_authorization", default=None,
)


def _extract_authorization(scope: dict) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            return v.decode("latin-1", "replace")
    return None


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


# ---- MCP method-level telemetry ---------------------------------------------
#
# nginx only logs `POST /mcp-discovery/` — the JSON-RPC method lives in the
# request body, so it cannot tell an `initialize`/`tools/list` handshake apart
# from a real `tools/call`. We peek the (small, capped) body here and bump a
# daily per-method counter. Bounded rows (days x methods); best-effort, never
# affects request handling.

_MCP_PEEK_CAP = 256 * 1024  # never buffer more than this just to read the method
_MCP_BATCH_METHOD_CAP = 64
_MCP_KNOWN_METHODS = {
    "initialize", "notifications/initialized", "notifications/cancelled",
    "ping", "tools/list", "tools/call", "resources/list", "resources/read",
    "resources/templates/list", "prompts/list", "prompts/get",
    "completion/complete", "logging/setLevel", "roots/list",
    "sampling/createMessage", "elicitation/create",
}


def _methods_from_body(body: bytes) -> list[str]:
    if not body:
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    items = data if isinstance(data, list) else [data]
    out = []
    for it in items[:_MCP_BATCH_METHOD_CAP]:
        if isinstance(it, dict):
            m = it.get("method")
            if isinstance(m, str) and m:
                out.append(m if m in _MCP_KNOWN_METHODS else "_other")
    return out


def _client_info_from_body(body: bytes) -> str | None:
    """clientInfo name/version out of an `initialize` request body."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    for it in (data if isinstance(data, list) else [data]):
        if not isinstance(it, dict) or it.get("method") != "initialize":
            continue
        info = (it.get("params") or {}).get("clientInfo") or {}
        name = info.get("name")
        if not isinstance(name, str) or not name:
            continue
        ver = info.get("version")
        return f"{name}/{ver}" if ver else name
    return None


def _record_mcp_methods(body: bytes) -> None:
    """Best-effort: bump the daily counter for each JSON-RPC method in `body`,
    and remember the caller's clientInfo when this is an `initialize`.
    Never raises — telemetry must not affect the MCP request."""
    methods = _methods_from_body(body)
    client_name = _client_info_from_body(body)
    if not methods and not client_name:
        return
    try:
        with directory_db.writer(DB_PATH) as wc:
            for m in methods:
                directory_db.bump_mcp_method(wc, m)
            if client_name:
                directory_db.remember_mcp_client(wc, _current_client_ip.get(), client_name)
    except Exception as e:
        log.debug("mcp method telemetry skipped: %r", e)


async def _peek_and_replay(receive):
    """Buffer the request body (capped), record method telemetry, and return a
    receive() that replays the buffered ASGI events then defers to the original.
    Bounded so a large POST never blows memory; if the cap is hit we skip the
    telemetry and still replay everything read."""
    messages: list[dict] = []
    total = 0
    capped = False
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") == "http.request":
            total += len(message.get("body", b"") or b"")
            if total > _MCP_PEEK_CAP:
                capped = True
                break
            if not message.get("more_body"):
                break
        else:
            break
    if not capped:
        body = b"".join(
            m.get("body", b"") or b""
            for m in messages
            if m.get("type") == "http.request"
        )
        _record_mcp_methods(body)
    queue = iter(messages)

    async def replay():
        try:
            return next(queue)
        except StopIteration:
            return await receive()

    return replay


def wrap_with_client_capture(app):
    """ASGI middleware: stash the per-request client IP into a ContextVar.

    Tools later read `_current_client_ip.get()` inside `_log_call`.
    """
    async def wrapped(scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            ip = _extract_peer_ip(scope)
            token = _current_client_ip.set(ip)
            auth_token = _current_authorization.set(_extract_authorization(scope))
            try:
                if scope.get("type") == "http" and scope.get("method") == "POST":
                    receive = await _peek_and_replay(receive)
                await app(scope, receive, send)
            finally:
                _current_client_ip.reset(token)
                _current_authorization.reset(auth_token)
        else:  # lifespan etc.
            await app(scope, receive, send)
    return wrapped


def _remembered_client_name() -> str | None:
    """clientInfo recorded at `initialize` time, looked up by peer IP."""
    ip = _current_client_ip.get()
    if not ip:
        return None
    try:
        with _open() as c:
            return directory_db.recent_mcp_client(c, ip)
    except Exception:
        return None


def _client_name_from_ctx(ctx: Context | None) -> str | None:
    """Read MCP-level clientInfo name/version from the session.

    Stateless transport: the session never carries it, so fall back to what the
    `initialize` request recorded."""
    if ctx is None:
        return _remembered_client_name()
    try:
        params = ctx.session.client_params
    except Exception:
        return None
    if params is None or params.clientInfo is None:
        return _remembered_client_name()
    info = params.clientInfo
    name = getattr(info, "name", None) or "unknown"
    ver = getattr(info, "version", None)
    return f"{name}/{ver}" if ver else name


def _open():
    return directory_db.connect(DB_PATH, read_only=True)


_SEARCH_TOOL_KIND = {"search": "x402", "search_mcp_servers": "mcp",
                     "search_a2a_agents": "a2a", "search_resources": None}


def _log_call(tool, ctx: Context | None = None, args=None,
              result_n=None, result_slug=None):
    """Best-effort usage log. Never raises."""
    if tool in _SEARCH_TOOL_KIND and args:
        from . import search_terms
        kind = _SEARCH_TOOL_KIND[tool] or (args.get("protocol") or "all")
        search_terms.record(kind, args.get("intent"), result_n or 0, "mcp")
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


@discover_mcp.tool(annotations=_READ_ONLY)
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
    items = [cards.public_service_row(item) for item in items[:top_k]]
    for item in items:
        if item.get("slug"):
            item["service_card_url"] = f"/api/v1/services/{item['slug']}"
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


@discover_mcp.tool(annotations=_READ_ONLY)
async def ask_services(
    intent: str,
    top_k: int = 5,
    max_price_usd: float | None = None,
    category: str | None = None,
    chain: str | None = None,
    require_healthy: bool = True,
    min_confidence: float | None = None,
    has_mcp: bool = False,
    use_llm: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Ask for the best x402/MCP services for an agent intent.

    This is the high-level discovery tool: it retrieves candidates from the
    directory, asks the configured backend LLM to rank only those candidates,
    and returns service cards for the selected recommendations. If the LLM is
    unavailable, it falls back to the directory ranker.

    Args:
        intent: Natural-language job the agent wants to accomplish.
        top_k: Max recommendations to return (1-10).
        max_price_usd: Optional per-call budget cap.
        category: Optional directory category filter.
        chain: Optional payment network filter, e.g. "base" or "solana".
        require_healthy: When true, only consider services marked health=ok.
        min_confidence: Optional x402scan quality floor (0.0-1.0).
        has_mcp: When true, only consider services with MCP endpoints.
        use_llm: Set false for deterministic retrieval-only fallback.
    """
    if use_llm:
        state = limits.check_ip_limits(
            _current_client_ip.get(),
            "mcp-ask",
            ASK_RATE_LIMITS,
            db_path=DB_PATH,
        )
        if state:
            return {
                "error": "rate_limited",
                "message": "Too many LLM-backed ask_services calls. Try again later or set use_llm=false.",
                "window": state.get("window"),
                "limit": state.get("limit"),
                "retry_after_seconds": state.get("retry_after"),
            }
    out = await directory_ask.answer_query(
        intent,
        db_path=DB_PATH,
        limit=top_k,
        category=category,
        chain=chain,
        max_price_usd=max_price_usd,
        health="ok" if require_healthy else None,
        min_confidence=min_confidence,
        has_mcp=has_mcp,
        use_llm=use_llm,
    )
    recs = out.get("recommendations") or []
    _log_call(
        "ask_services", ctx=ctx,
        args={
            "intent": intent, "top_k": top_k,
            "max_price_usd": max_price_usd, "category": category,
            "chain": chain, "require_healthy": require_healthy,
            "min_confidence": min_confidence, "has_mcp": has_mcp,
            "use_llm": use_llm,
        },
        result_n=len(recs),
        result_slug=(recs[0].get("slug") if recs else None),
    )
    return out

@discover_mcp.tool(annotations=_READ_ONLY)
async def get(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Get full details + ready-to-paste call template for a service.

    Returns the service card with payment, call, quality, resource samples,
    and ready-to-use MCP/x402 call hints. Use this after `search` or
    `ask_services` before paying/calling an external service.

    Args:
        slug: Service slug as returned by `search` items.
    """
    with _open() as conn:
        row = directory_db.get_by_slug(conn, slug)
        suppressed = directory_db.find_privacy_suppression_any(
            conn, "x402", {"slug": slug}) if row is None else None
    if row is None:
        if suppressed is not None:
            return {"error": "not_found", "slug": slug}
        _log_call("get", ctx=ctx, args={"slug": slug}, result_n=0)
        return {"error": "not_found", "slug": slug}
    row = cards.build_service_card(row)
    row["call_template"] = row.get("call", {}).get("template")
    _log_call("get", ctx=ctx, args={"slug": slug}, result_n=1, result_slug=slug)
    return row


@discover_mcp.tool(annotations=_READ_ONLY)
async def list_categories(ctx: Context | None = None) -> dict[str, Any]:
    """List all available service categories in the directory."""
    with _open() as conn:
        cats = directory_db.list_categories(conn)
    out = {"items": cats, "count": len(cats)}
    _log_call("list_categories", ctx=ctx, result_n=out["count"])
    return out


@discover_mcp.tool(annotations=_READ_ONLY)
async def stats(ctx: Context | None = None) -> dict[str, Any]:
    """High-level stats about the directory: x402 total, healthy count and chains,
    plus `snapshot`, the daily digest across x402, MCP and A2A (see directory_stats)."""
    from . import stats as directory_stats_mod
    with _open() as conn:
        out = directory_db.stats(conn)
    try:
        out["snapshot"] = directory_stats_mod.summary(directory_stats_mod.latest(_open))
    except Exception:
        out["snapshot"] = {"available": False}
    _log_call("stats", ctx=ctx, result_n=out.get("total"))
    return out


@discover_mcp.tool(annotations=_READ_ONLY)
async def get_methodology(ctx: Context | None = None) -> dict[str, Any]:
    """How every score and statistic on agent-tools.cloud is computed.

    Returns the quality-score components and grade bands per protocol, the nine
    readiness criteria with weights, payment-evidence grades, the probing
    schedule and the definitions behind the daily statistics.
    """
    from . import methodology
    _log_call("get_methodology", ctx=ctx)
    return methodology.build()


_article_store_cache: dict = {}


def _article_store():
    """The published-article store, or None when the Blog is disabled."""
    if os.getenv("ARTICLES_ENABLED", "0") != "1":
        return None
    if "store" not in _article_store_cache:
        from pathlib import Path
        from .articles import ArticleStore
        _article_store_cache["store"] = ArticleStore(os.getenv(
            "ARTICLE_DB_PATH", str(Path(directory_db.DEFAULT_DB_PATH).with_name("articles.db"))))
    return _article_store_cache["store"]


@discover_mcp.tool(annotations=_READ_ONLY)
async def list_articles(
    query: str = "",
    tag: str = "",
    page: int = 1,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List published Blog articles, newest first (metadata only).

    Args:
        query: Optional words to match in title, summary or body.
        tag: Optional tag to filter by.
        page: Page number, 20 articles per page.

    Use get_article(slug) for the full Markdown body.
    """
    from .article_routes import public_article_list
    from .articles import ArticleError
    store = _article_store()
    if store is None:
        return {"items": [], "total": 0, "note": "The Blog is not enabled on this host."}
    try:
        out = public_article_list(store, query=(query or "")[:200], tag=(tag or "")[:40],
                                  page=max(1, min(int(page or 1), 10000)))
    except ArticleError as error:
        return {"error": error.code}
    _log_call("list_articles", ctx=ctx, args={"query": query, "tag": tag}, result_n=out["total"])
    return out


@discover_mcp.tool(annotations=_READ_ONLY)
async def get_article(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Read one published Blog article: metadata plus the full Markdown body.

    Args:
        slug: The article slug, as returned by list_articles.
    """
    from .article_routes import public_article_json
    from .articles import ArticleError
    store = _article_store()
    if store is None:
        return {"error": "blog_disabled"}
    try:
        state = store.public_state(str(slug)[:100])
    except ArticleError as error:
        return {"error": error.code}
    if state["status"] != "published":
        return {"error": "withdrawn" if state["status"] == "withdrawn" else "not_found", "slug": slug}
    _log_call("get_article", ctx=ctx, result_slug=slug)
    return public_article_json(state["article"], full=True)


@discover_mcp.tool(annotations=_READ_ONLY)
async def directory_stats(
    view: str = "all",
    topic: str | None = None,
    month: str | None = None,
    api_key: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Daily statistics on the agent-tool ecosystem as seen by our probes.

    Args:
        view: "all", "mcp", "x402" or "a2a" to scope the figures to one protocol.
        topic: Optional topic word (e.g. "payment", "weather") to get that topic
            instead: supply per protocol, readiness, best listings, searches.
        month: Optional "YYYY-MM" to get that monthly report instead.
        api_key: Optional account key (free from create_api_key) for complete
            lists when you cannot send the Authorization header.

    Returns counts (indexed, reachable, protocol-verified, readiness A/B), one
    finding sentence per topic, key facts (x402 prices and networks, MCP and A2A
    protocol versions, MCP auth, A2A card signatures, how every listing's
    latest probe ended), the most searched terms (view "all"), the snapshot
    time, and links to the full JSON snapshot, the daily CSV, the methodology,
    the "how many work" page, per-protocol and per-topic pages and the monthly
    reports. With an account API key (Authorization: Bearer header or
    `api_key`; free from `create_api_key`) the topic and search lists are
    complete (10 listings per protocol, up to 100 terms); without one they
    are shortened.
    """
    from . import stats as directory_stats_mod
    full = _has_account() or (bool((api_key or "").strip())
                              and _pv_user_id(api_key) is not None)
    snap = directory_stats_mod.latest(_open)
    if topic:
        out = directory_stats_mod.topic_page(snap, topic.strip().lower(), full=full) or {
            "error": "unknown_topic", "topics": list(directory_stats_mod.TOPICS)}
    elif month:
        with _open() as conn:
            try:
                out = directory_stats_mod.monthly_report(conn, month.strip())
            except Exception:  # noqa: BLE001
                out = None
        out = out or {"error": "no_report", "reports": "https://agent-tools.cloud/stats/reports.json"}
    else:
        out = directory_stats_mod.summary(snap, view, full=full)
    _log_call("directory_stats", ctx=ctx, args={"view": view, "topic": topic, "month": month},
              result_n=None if (topic or month) else
              (out.get("kpis") or {}).get(out.get("view"), {}).get("indexed"))
    return out


def _has_account() -> bool:
    """Whether the current MCP request carries a valid account API key."""
    authorization = _current_authorization.get() or ""
    if not authorization.lower().startswith("bearer "):
        return False
    from . import auth as directory_auth
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                       "headers": [(b"authorization", authorization.encode("latin-1"))]})
    try:
        return directory_auth.current_user(request) is not None
    except Exception:  # noqa: BLE001
        return False


@discover_mcp.tool(annotations=_READ_ONLY)
async def search_mcp_servers(
    intent: str | None = None,
    top_k: int = 5,
    chain: str | None = None,
    require_healthy: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find MCP servers in the directory.

    Searches the standalone MCP directory (PulseMCP / official MCP registry
    import) unioned with x402 services that also expose an MCP endpoint.
    Returns normalised entries with a ready-to-use streamable-http
    `call_hint.mcp.url`.

    Args:
        intent: Natural-language description of the tool/capability needed.
        top_k: Max servers to return (1-20).
        chain: Optional payment-network filter for paid MCP servers.
        require_healthy: When true, only return servers marked health=ok.
    """
    top_k = max(1, min(20, top_k))
    health = "ok" if require_healthy else None
    servers: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _open() as conn:
        for r in directory_db.search_mcp(conn, q=intent, health=health, limit=top_k):
            item = directory_resources.normalize_mcp_server(r)
            key = (item.get("endpoint_url") or item.get("slug") or "").lower()
            if key not in seen:
                seen.add(key); servers.append(item)
        for r in directory_db.search(
            conn, q=intent, chain=chain, health=health,
            has_mcp=True, limit=top_k,
        ):
            item = directory_resources.normalize_service(r, as_mcp=True)
            key = (item.get("endpoint_url") or item.get("slug") or "").lower()
            if key not in seen:
                seen.add(key); servers.append(item)
    servers = servers[:top_k]
    _log_call("search_mcp_servers", ctx=ctx,
              args={"intent": intent, "top_k": top_k, "chain": chain,
                    "require_healthy": require_healthy},
              result_n=len(servers),
              result_slug=(servers[0].get("slug") if servers else None))
    return {"intent": intent, "count": len(servers), "servers": servers}


@discover_mcp.tool(annotations=_READ_ONLY)
async def get_mcp_server(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Get the full card for one MCP server by slug.

    Args:
        slug: MCP server slug as returned by `search_mcp_servers` items.
    """
    with _open() as conn:
        suppressed = directory_db.find_privacy_suppression_any(
            conn, "mcp", {"slug": slug})
        if suppressed is not None:
            return {"error": "not_found", "message": f"No MCP server with slug {slug!r}"}
        mcp = directory_db.get_mcp_by_slug(conn, slug)
        if mcp:
            _log_call("get_mcp_server", ctx=ctx, args={"slug": slug},
                      result_n=1, result_slug=slug)
            return mcp
        row = directory_db.get_by_slug(conn, slug)
    if not row or not (row.get("mcp_url") or "").strip():
        _log_call("get_mcp_server", ctx=ctx, args={"slug": slug}, result_n=0)
        return {"error": "not_found", "message": f"No MCP server with slug {slug!r}"}
    card = cards.build_service_card(row)
    _log_call("get_mcp_server", ctx=ctx, args={"slug": slug},
              result_n=1, result_slug=slug)
    return card


@discover_mcp.tool(annotations=_READ_ONLY)
async def search_a2a_agents(
    intent: str | None = None,
    top_k: int = 5,
    x402_only: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find A2A agents you can delegate a task to.

    Args:
        intent: Natural-language description of the task to delegate.
        top_k: Max agents to return (1-20).
        x402_only: When true, only return agents that advertise x402 payment.
    """
    top_k = max(1, min(20, top_k))
    with _open() as conn:
        rows = directory_db.search_a2a(conn, q=intent, x402_only=x402_only, limit=top_k)
    agents = [directory_a2a.public_agent(r) for r in rows]
    _log_call("search_a2a_agents", ctx=ctx,
              args={"intent": intent, "top_k": top_k, "x402_only": x402_only},
              result_n=len(agents),
              result_slug=(agents[0].get("slug") if agents else None))
    return {"intent": intent, "count": len(agents), "agents": agents}


@discover_mcp.tool(annotations=_READ_ONLY)
async def search_resources(
    intent: str | None = None,
    protocol: str | None = None,
    top_k: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Unified search across x402 services, MCP servers and A2A agents.

    Args:
        intent: Natural-language query.
        protocol: Optional filter: "x402", "mcp" or "a2a".
        top_k: Max resources to return (1-50).
    """
    top_k = max(1, min(50, top_k))
    with _open() as conn:
        out = directory_resources.unified_search(
            conn, q=intent, protocol=protocol, limit=top_k,
        )
    _log_call("search_resources", ctx=ctx,
              args={"intent": intent, "protocol": protocol, "top_k": top_k},
              result_n=out.get("count"))
    return out


_REGISTER_RATE_LIMIT_PER_IP_PER_DAY = 5


@discover_mcp.tool(annotations=_SUBMIT)
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
    are auto-reviewed instantly by x402 verification (no human gate): if
    the URL proves x402 payment support it is listed immediately and shows
    up in `search`; otherwise it is rejected or retried automatically.
    Listing is FREE.

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
            "message": (
                "A service with this URL is already in the directory. Submitting "
                "it again will not change it: verify domain ownership to edit."),
            "claim_url": "https://agent-tools.cloud/account",
            "claim_docs": "https://agent-tools.cloud/docs/claim",
            "api": directory_ownership.API_FLOW,
            "slug": existing.get("slug"),
            "url": existing.get("url"),
        }
    if pending:
        _log_call("register", ctx=ctx,
                  args={"url": url, "outcome": "already_pending"}, result_n=0)
        return {
            "status": "already_pending",
            "message": (
                "A submission for this URL is already submitted and auto-verifying."),
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
                    "Wait for the auto-verifier to clear them or email contact@agent-tools.cloud."
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

    # Auto-review immediately (no human gate): verify x402 support and
    # publish / reject / retry accordingly.
    try:
        review = await asyncio.to_thread(
            directory_jobs.review_submission, sub_id, "auto-review (mcp-register)", True)
    except Exception as e:
        log.warning("register auto-review failed: %r", e)
        review = {"status": "pending", "submission_id": sub_id}

    rstatus = review.get("status")
    _log_call("register", ctx=ctx,
              args={"url": url, "outcome": rstatus},
              result_n=1, result_slug=str(review.get("slug") or sub_id))
    if rstatus == "listed":
        return {
            "status": "listed",
            "submission_id": sub_id,
            "slug": review.get("slug"),
            "message": "Auto-verified x402 support — your service is now live in `search`. Listing is free.",
        }
    if rstatus == "rejected":
        return {
            "status": "rejected",
            "submission_id": sub_id,
            "message": "Auto-review could not confirm x402 payment support for this URL.",
            "evidence": review.get("evidence"),
        }
    return {
        "status": "pending",
        "submission_id": sub_id,
        "message": (
            "Submitted; auto-verification was inconclusive and will be retried "
            "automatically. Listing is free."
        ),
        "evidence": review.get("evidence"),
    }


# ---- Safety scanning ----------------------------------------------------------


@discover_mcp.tool(annotations=_SCAN)
async def scan_mcp_safety(
    endpoint_url: str,
    name: str = "",
    description: str = "",
    tools_text: str = "",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Check an MCP server for malware / prompt-injection lures by its endpoint URL.

    Give the server's streamable-http endpoint URL. Two paths:

      * **Already in the agent-tools directory** → returns our LATEST stored
        rule verdict. Every indexed server is re-scanned hourly, so you get a
        consistent, continuously-refreshed answer without re-probing.
      * **Not yet indexed** → we probe the endpoint live, statically scan its
        advertised tools + metadata, and return the fresh verdict. It is ADDED
        to the directory (so the next caller gets the stored verdict instantly)
        only if it answered or you supplied metadata; documentation hosts such
        as example.com / *.test / *.invalid are scanned but never listed.

    Two dimensions are reported. `verdict` is authoritative and comes from
    deterministic static rules — pure pattern-matching over the *advertised*
    text only, NO code execution. It flags the social-engineering / RCE tricks
    listing-spam servers use:

      * `curl … | bash` and `base64 -d | sh` install lures
      * `eval "$(curl …)"` / PowerShell `IEX(...DownloadString)` cradles
      * base64 blobs that decode to a shell command
      * bare-IP payload hosts and cheap throwaway TLDs
      * prompt-injection / credential-exfiltration phrasing
        ("ignore previous instructions", "send your .env / api key")
      * MCP tool-poisoning coercion — descriptions that hijack an agent's
        tool-calling ("always call this tool first", "before using any other
        tool you must…"), hidden `<IMPORTANT>` instructions, "list all API
        keys / include secrets in your response", and coercion to read &
        forward `.key`/`.pem`/`.ssh`/`.env` files

    Source-code-oriented rules (SQL / command / code injection) are deliberately
    not applied to natural-language descriptions, to avoid false positives.

    `llm_reference` is an advisory frontier-LLM second opinion over the same text.
    Because the LLM is slow it is computed LIVE on this call only and is never
    stored (the hourly job never runs it), so it may be null on timeout. It
    never overrides the rule verdict; when it is *more* severe than the rules an
    `advisory` note is attached as a safety-net signal. Security/defense
    products that merely *name* these attacks are not flagged.

    Args:
        endpoint_url: The MCP server's streamable-http URL (required). This is
            the identity we look up / index by.
        name: Optional advertised name (used when the server is new and gets
            added; falls back to the URL host).
        description: Optional description / README blurb (scanned when new).
        tools_text: Optional tool names + descriptions; used only if the live
            probe cannot fetch the server's tools/list.

    Returns:
        { verdict: "clean"|"suspicious"|"malicious", score: 0-100,
          reasons: [{rule, weight, snippet}],
          llm_reference: {model, verdict, reason, confidence} | null,
          advisory: str | null, slug, name, endpoint_url,
          source: "stored" (existing) | "new_scan" (just added), indexed: bool }
    """
    out = await asyncio.to_thread(
        safety_service.scan_endpoint, endpoint_url, name, description, tools_text, DB_PATH)
    if isinstance(out, dict) and not out.get("error"):
        _log_call("scan_mcp_safety", ctx=ctx,
                  args={"endpoint_url": endpoint_url, "outcome": out.get("source")},
                  result_n=1, result_slug=out.get("verdict"))
    return out


async def _ownership_api(tool_name: str, method: str, path: str, handler,
                         *arguments, ctx: Context | None = None) -> dict[str, Any]:
    authorization = _current_authorization.get() or ""
    scheme, _, raw_key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_key.strip():
        return {"error": "unauthorized", "status_code": 401,
                "message": ("Send an account API key in the MCP Authorization: Bearer header. "
                            "Claim and listing tools read only that header, not an api_key "
                            "argument; if your client cannot set headers, use the REST "
                            "endpoints in `rest` with the same key."),
                "get_a_key": {"method": "POST", "url": "https://agent-tools.cloud/api/v1/keys",
                              "mcp_tool": "create_api_key"},
                "rest": "https://agent-tools.cloud/docs/claim"}
    request = Request({
        "type": "http", "method": method, "path": path, "root_path": "", "scheme": "https",
        "server": ("agent-tools.cloud", 443), "client": (_current_client_ip.get() or "unknown", 0),
        "query_string": b"", "headers": [(b"authorization", authorization.encode("latin-1"))],
    })
    try:
        response = await handler(request, *arguments)
        result = json.loads(response.body) if hasattr(response, "body") else response
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "request_rejected", "message": str(exc.detail)}
        result = {**detail, "status_code": exc.status_code}
        if exc.status_code == 401:
            result["error"] = "unauthorized"
        if exc.headers and exc.headers.get("Retry-After"):
            result["retry_after_seconds"] = int(exc.headers["Retry-After"])
    except ValidationError:
        result = {"error": "invalid_arguments", "status_code": 422,
                  "message": "Invalid claim arguments; supply the complete URL or issued claim id and token."}
    _log_call(tool_name, ctx=ctx, args={"outcome": result.get("error") or result.get("status") or "ok"},
              result_n=0 if result.get("error") else 1)
    return result


@discover_mcp.tool(annotations=_CLAIM_URL)
async def claim_service_url(url: str, method: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
    """Issue a fresh ownership token for one exact public HTTPS URL.

    Authenticate in the MCP Authorization: Bearer header. Publishing and
    verifying the token is required before editing; URL input alone grants no rights.

    Args:
        url: Complete public HTTPS service URL, including its query when present.
        method: Optional proof method; otherwise inferred from the complete URL.
    """
    from . import routes

    async def open_claim(request):
        return await routes.api_open_service_claim(request, routes.ServiceClaimRequest(url=url, method=method))

    result = await _ownership_api("claim_service_url", "POST", "/api/v1/listing-claims", open_claim, ctx=ctx)
    if not result.get("error"):
        result["mcp_next"] = {"tool": "verify_service_url", "arguments": {
            "claim_id": result["claim_id"], "token": result["token"]}}
    return result


@discover_mcp.tool(annotations=_VERIFY_URL)
async def verify_service_url(claim_id: int, token: str,
                             ctx: Context | None = None) -> dict[str, Any]:
    """Verify public ownership proof for your own exact service URL claim.

    This reads public proof only. It never calls business tools, requests
    private service credentials, or grants whole-platform permissions.

    Args:
        claim_id: Claim id returned by claim_service_url.
        token: The issued token after publishing it in the instructed location.
    """
    from . import routes

    async def verify_claim(request):
        return await routes.api_verify_claim(request, claim_id, routes.ClaimVerifyRequest(token=token))

    result = await _ownership_api("verify_service_url", "POST",
                                  "/api/v1/listing-claims/%s/verify" % claim_id, verify_claim, ctx=ctx)
    if not result.get("error"):
        result["mcp_next"] = {"tool": "get_owned_listing", "note": "Choose a kind and slug from the returned listings."}
    return result


@discover_mcp.tool(annotations=_READ_ONLY)
async def get_owned_listing(kind: str, slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Read editable metadata for a listing covered by your verified claim.

    Args:
        kind: Listing type: x402, mcp, or a2a.
        slug: Slug from the verified claim's listings or directory search.
    """
    from . import routes
    return await _ownership_api("get_owned_listing", "GET", "/api/v1/listings/%s/%s" % (kind, slug),
                                routes.api_get_listing, kind, slug, ctx=ctx)


@discover_mcp.tool(annotations=_OWNER_EDIT)
async def edit_owned_listing(kind: str, slug: str, fields: dict[str, Any],
                             ctx: Context | None = None) -> dict[str, Any]:
    """Edit only metadata covered by your verified claim, with the public audit trail.

    An endpoint change requires verified control of the exact destination or
    its whole host and clears measurements tied to the old primary endpoint.

    Args:
        kind: Listing type: x402, mcp, or a2a.
        slug: Slug of the listing you are authorized to edit.
        fields: Editable fields to change; measured fields are rejected.
    """
    from . import routes
    return await _ownership_api("edit_owned_listing", "PATCH", "/api/v1/listings/%s/%s" % (kind, slug),
                                routes.api_patch_listing, kind, slug, fields, ctx=ctx)


# ---- Sponsored paid verification ----------------------------------------------

_PV_PREPARE_LIMITS = (("minute", 6, 60), ("day", 60, 86400))
_PV_AUTH_ERROR = {
    "error": "unauthorized",
    "message": "API key required: call the create_api_key tool (or POST "
               "https://agent-tools.cloud/api/v1/keys), then send "
               "Authorization: Bearer <key> or api_key.",
}


@discover_mcp.tool(annotations=_MINT_KEY)
async def create_api_key(name: str | None = None,
                         ctx: Context | None = None) -> dict[str, Any]:
    """Create a free account API key. No sign-up, email or existing key needed.

    The key is returned once and cannot be recovered. Store it in your secret
    store; do not print it back to the user or write it to logs. Send it as
    `Authorization: Bearer <key>` (or the `api_key` argument where offered) to
    get complete `directory_stats` lists, run paid verifications and claim
    your own listings. The claim and listing tools read the key only from the
    Authorization header; without header support use the REST claim API. Shares the HTTP limit of POST /api/v1/keys (a few keys
    per IP per day).

    Args:
        name: Optional label to recognise the key later, e.g. your agent name.
    """
    from . import routes

    state = limits.check_ip_limits(
        _current_client_ip.get(), "mint-key",
        (("day", routes.KEY_MINT_LIMIT_PER_DAY, 86400),))
    if state:
        out = {"error": "rate_limited",
               "message": "Too many keys minted from this IP today.",
               "retry_after_seconds": int(state.get("retry_after") or 1)}
        _log_call("create_api_key", ctx=ctx, args={"outcome": "rate_limited"},
                  result_n=0)
        return out
    label = (name or "").strip()[:60] or None

    def mint() -> str:
        with directory_db.writer() as conn:
            puid = secrets.token_hex(16)
            uid = directory_db.upsert_user(
                conn, provider="domain", provider_uid=puid,
                login=directory_db.key_user_login(puid))
            return directory_db.create_api_key(conn, uid, name=label)

    raw = await asyncio.to_thread(mint)
    # Never log the key or its name.
    _log_call("create_api_key", ctx=ctx, args={"outcome": "created"}, result_n=1)
    return {
        "api_key": raw,
        "prefix": raw[:15],
        "message": ("Store this now -- it is not shown again. Do not echo it "
                    "to the user or logs. Send it as Authorization: Bearer "
                    "<api_key>."),
        "use_with": ["directory_stats (complete lists)",
                     "prepare_paid_verification", "claim_service_url"],
    }


def _pv_user_id(api_key: str | None) -> int | None:
    """Account id for the presented API key (argument first, then header)."""
    raw = (api_key or "").strip()
    if not raw:
        scheme, _, tok = (_current_authorization.get() or "").partition(" ")
        if scheme.lower() == "bearer":
            raw = tok.strip()
    if not raw:
        return None
    try:
        with _open() as conn:
            key = directory_db.api_key_row(conn, raw)
            if key is None:
                return None
            user = conn.execute("SELECT id, status FROM users WHERE id=?",
                                (key["user_id"],)).fetchone()
    except Exception:
        return None
    if user is None or user["status"] != "active":
        return None
    directory_db.touch_api_key(key["id"], key["last_used_at"])
    return int(user["id"])


def _pv_error(exc: "paid_verify.VerifyError") -> dict[str, Any]:
    return {"error": exc.code, "message": str(exc)}


@discover_mcp.tool(annotations=_QUOTE)
async def prepare_paid_verification(
    slug: str,
    payer: str,
    api_key: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Quote a sponsored paid verification of an x402 service.

    You pay the service once from your own wallet (Base USDC, <= $1; funds go
    to its payTo). Sign the returned `typed_data` (EIP-712), then call
    `execute_paid_verification` before `valid_before`. We never sign or hold funds.

    Auth: API key from POST https://agent-tools.cloud/api/v1/keys, as the
    `Authorization: Bearer` header or `api_key`.

    Args:
        slug: Service slug from `search` / `get`.
        payer: 0x address of the wallet that will sign and pay.
        api_key: Optional if the Authorization header is sent.

    Returns:
        { id, amount_usd, pay_to, valid_before, typed_data, ... } or { error, message }.
    """
    uid = _pv_user_id(api_key)
    if uid is None:
        return dict(_PV_AUTH_ERROR)
    blocked = limits.check_ip_limits(
        _current_client_ip.get(), "paid_verify_prepare", _PV_PREPARE_LIMITS,
        db_path=directory_db.DEFAULT_DB_PATH)
    if blocked:
        return {"error": "rate_limited",
                "message": "too many paid verification requests",
                "retry_after_seconds": int(blocked.get("retry_after") or 1)}
    with _open() as conn:
        svc = directory_db.get_by_slug(conn, slug)
    if not svc:
        return {"error": "not_found", "message": "service not found"}
    try:
        out = await asyncio.to_thread(paid_verify.prepare, uid, svc, payer)
    except paid_verify.VerifyError as exc:
        out = _pv_error(exc)
    _log_call("prepare_paid_verification", ctx=ctx,
              args={"slug": slug, "outcome": out.get("error") or "prepared"},
              result_n=0 if out.get("error") else 1, result_slug=slug)
    return out


@discover_mcp.tool(annotations=_PAYMENT)
async def execute_paid_verification(
    id: str,
    signature: str,
    api_key: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Send the signed payment to the service once and score the result.

    Aborts without paying if price or recipient changed. Never retried; a
    timeout returns `indeterminate`. status: succeeded | delivery_failed |
    payment_rejected | indeterminate.

    Args:
        id: `id` returned by `prepare_paid_verification`.
        signature: 0x EIP-712 signature of its `typed_data` by the payer.
        api_key: Optional if the Authorization header is sent.
    """
    uid = _pv_user_id(api_key)
    if uid is None:
        return dict(_PV_AUTH_ERROR)
    sig = (signature or "").strip()
    if not 132 <= len(sig) <= 134:
        return {"error": "bad_signature", "message": "signature must be a 65-byte 0x hex string"}
    try:
        out = await asyncio.to_thread(paid_verify.execute, id, uid, sig)
    except paid_verify.VerifyError as exc:
        out = _pv_error(exc)
    _log_call("execute_paid_verification", ctx=ctx,
              args={"outcome": out.get("error") or out.get("status")},
              result_n=0 if out.get("error") else 1)
    return out


@discover_mcp.tool(annotations=_READ_ONLY)
async def get_paid_verification(
    id: str,
    api_key: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read one of your own paid verifications.

    Args:
        id: Verification id.
        api_key: Optional if the Authorization header is sent.
    """
    uid = _pv_user_id(api_key)
    if uid is None:
        return dict(_PV_AUTH_ERROR)
    try:
        out = await asyncio.to_thread(paid_verify.result, id, uid)
    except paid_verify.VerifyError as exc:
        out = _pv_error(exc)
    _log_call("get_paid_verification", ctx=ctx,
              args={"outcome": out.get("error") or out.get("status")},
              result_n=0 if out.get("error") else 1)
    return out


_ARG_LINE = re.compile(r"^ {8}(\w+): (.*)$")


def _docstring_args(doc: str) -> dict[str, str]:
    """Parse the Google-style `Args:` block into {param: one-line description}."""
    out: dict[str, str] = {}
    current = None
    in_args = False
    for line in (doc or "").splitlines():
        if line.strip() == "Args:":
            in_args = True
            continue
        if not in_args:
            continue
        m = _ARG_LINE.match(line)
        if m:
            current = m.group(1)
            out[current] = m.group(2).strip()
        elif current and line.startswith(" " * 12) and line.strip():
            out[current] += " " + line.strip()
        elif line.strip() and not line.startswith(" " * 8):
            break
    return out


# FastMCP leaves inputSchema properties without descriptions; agents read them
# there, not in the tool docstring.
for _tool in discover_mcp._tool_manager.list_tools():
    _descs = _docstring_args(_tool.fn.__doc__ or "")
    for _name, _schema in (_tool.parameters.get("properties") or {}).items():
        if _name in _descs and not _schema.get("description"):
            _schema["description"] = _descs[_name]


log.info("directory MCP app built (db=%s, tools=%s)", DB_PATH, len(discover_mcp._tool_manager.list_tools()))
