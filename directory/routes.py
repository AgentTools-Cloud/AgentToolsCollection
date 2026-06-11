"""FastAPI router for the agent-tools directory site."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl
from starlette.concurrency import run_in_threadpool

from . import ask as directory_ask
from . import cards, db
from . import a2a as directory_a2a
from . import crawlers as directory_crawlers
from . import jobs as directory_jobs
from . import resources as directory_resources
from . import reverify_x402 as directory_reverify
from . import mailer as directory_mailer
from . import limits

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Build version — bump on deploy to bust browser/CDN HTML caches.
BUILD_VERSION = "2026-06-04.17"
TEMPLATES.env.globals["build_version"] = BUILD_VERSION

router = APIRouter()

# Single source of truth for the liveness vocabulary, surfaced on every
# `health` filter (services / a2a / mcp) and echoed by the `health_status`
# field on each result.
HEALTH_DOC = (
    "Filter by liveness from the most recent probe. One of: "
    "`ok` — the endpoint answered a live probe. For MCP, a successful "
    "`initialize` handshake counts as ok, and so does an intentional "
    "401/402/403 auth challenge (BYO API key / OAuth / paid access), because "
    "the remote server is reachable and credential-gated rather than broken; "
    "`degraded` — reachable but the probe could not fully complete for a "
    "non-auth reason, such as a protocol/method mismatch or other 4xx reply "
    "to the MCP probe; "
    "`down` — unreachable, timed out, or returned 5xx; "
    "`unknown` — not probed yet. "
    "Omit to include every status. Results are always ranked "
    "ok > degraded > unknown > down. The same value is returned per result as "
    "`health_status`, alongside `http_status` and `latency_ms`."
)


def _conn():
    return db.connect(read_only=True)


ASK_RATE_LIMITS = (
    ("minute", limits.env_int("AGENT_TOOLS_ASK_RATE_LIMIT_PER_MINUTE", 10), 60),
    ("day", limits.env_int("AGENT_TOOLS_ASK_RATE_LIMIT_PER_DAY", 200), 86400),
)
SUBMIT_RATE_LIMIT_PER_DAY = limits.env_int("AGENT_TOOLS_SUBMIT_RATE_LIMIT_PER_DAY", 5)


def _enforce_ask_limit(request: Request, use_llm: bool) -> None:
    if not use_llm:
        return
    state = limits.check_ip_limits(
        limits.client_ip_from_request(request),
        "ask",
        ASK_RATE_LIMITS,
    )
    if state:
        limits.raise_rate_limited(state, "Too many LLM-backed ask requests. Try again later or set use_llm=false.")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    with _conn() as c:
        s = db.stats(c)
        ms = db.mcp_stats(c)
        a2s = db.a2a_stats(c)
    return TEMPLATES.TemplateResponse(request, "home.html", {
            "request": request, "stats": s, "mcp_stats": ms, "a2a_stats": a2s,
        },
    )


@router.get("/x402", response_class=HTMLResponse, include_in_schema=False)
async def x402_page(
    request: Request,
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    chain: str | None = Query(default=None),
    region: str | None = Query(default=None),
    health: str | None = Query(default=None),
):
    with _conn() as c:
        services = db.search(c, q=q, category=category, chain=chain,
                             region=region, health=health, limit=60)
        cats = db.list_categories(c)
        s = db.stats(c)
    return TEMPLATES.TemplateResponse(request, "index.html", {
            "request": request, "services": services, "categories": cats,
            "stats": s, "q": q or "", "active_category": category,
            "active_chain": chain, "active_region": region, "active_health": health,
        },
    )


@router.get("/services/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def service_detail(request: Request, slug: str):
    with _conn() as c:
        svc = db.get_by_slug(c, slug)
    if not svc:
        raise HTTPException(404, "service not found")
    return TEMPLATES.TemplateResponse(request, "service.html", {"request": request, "svc": svc})


@router.get("/categories", response_class=HTMLResponse, include_in_schema=False)
async def categories_page(request: Request):
    with _conn() as c:
        x402_cats = db.list_categories(c)
        mcp_cats = db.mcp_categories(c)
        a2a_cats = db.a2a_categories(c)
        s = db.stats(c)
        ms = db.mcp_stats(c)
        a2s = db.a2a_stats(c)
    return TEMPLATES.TemplateResponse(request, "categories.html", {
            "request": request,
            "x402_cats": x402_cats, "mcp_cats": mcp_cats, "a2a_cats": a2a_cats,
            "stats": s, "mcp_stats": ms, "a2a_stats": a2s,
        },
    )


@router.get("/submit", response_class=HTMLResponse, include_in_schema=False)
async def submit_page(request: Request, type: str | None = Query(default=None)):
    active = type if type in ("x402", "mcp", "a2a") else "x402"
    return TEMPLATES.TemplateResponse(request, "submit.html", {"request": request, "active_type": active})


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
async def about_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "about.html", {"request": request})


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
async def terms_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "terms.html", {"request": request})


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "privacy.html", {"request": request})


@router.get("/mcp", response_class=HTMLResponse, include_in_schema=False)
async def mcp_page(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
):
    with _conn() as c:
        servers = db.search_mcp(c, q=q, health=health, x402_only=bool(x402), limit=60)
        s = db.mcp_stats(c)
    return TEMPLATES.TemplateResponse(request, "mcp.html", {
            "request": request, "servers": servers, "stats": s, "q": q or "",
            "active_health": health, "active_x402": bool(x402),
        },
    )


@router.get("/mcp/servers/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def mcp_detail(request: Request, slug: str):
    with _conn() as c:
        m = db.get_mcp_by_slug(c, slug)
    if not m:
        raise HTTPException(404, "mcp server not found")
    return TEMPLATES.TemplateResponse(request, "mcp_server.html", {"request": request, "m": m})


@router.get("/_partials/mcp", response_class=HTMLResponse, include_in_schema=False)
async def mcp_partial(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
):
    with _conn() as c:
        servers = db.search_mcp(c, q=q, health=health, x402_only=bool(x402), limit=60)
    return TEMPLATES.TemplateResponse(request, "_mcp_grid.html", {"request": request, "servers": servers})


@router.get("/a2a", response_class=HTMLResponse, include_in_schema=False)
async def a2a_page(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
):
    with _conn() as c:
        agents = db.search_a2a(c, q=q, health=health, x402_only=bool(x402), limit=60)
        s = db.a2a_stats(c)
    return TEMPLATES.TemplateResponse(request, "a2a.html", {
            "request": request, "agents": agents, "stats": s, "q": q or "",
            "active_health": health, "active_x402": bool(x402),
        },
    )


@router.get("/a2a/agents/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def a2a_detail(request: Request, slug: str):
    with _conn() as c:
        a = db.get_a2a_by_slug(c, slug)
    if not a:
        raise HTTPException(404, "a2a agent not found")
    return TEMPLATES.TemplateResponse(request, "a2a_agent.html", {"request": request, "a": a})


@router.get("/_partials/a2a", response_class=HTMLResponse, include_in_schema=False)
async def a2a_partial(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
):
    with _conn() as c:
        agents = db.search_a2a(c, q=q, health=health, x402_only=bool(x402), limit=60)
    return TEMPLATES.TemplateResponse(request, "_a2a_grid.html", {"request": request, "agents": agents})


@router.get("/_partials/services", response_class=HTMLResponse, include_in_schema=False)
async def services_partial(
    request: Request,
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    chain: str | None = Query(default=None),
    region: str | None = Query(default=None),
    health: str | None = Query(default=None),
):
    with _conn() as c:
        services = db.search(c, q=q, category=category, chain=chain,
                             region=region, health=health, limit=60)
    return TEMPLATES.TemplateResponse(request, "_service_grid.html", {"request": request, "services": services}
    )


@router.get("/api/v1/search", tags=["directory"])
async def api_search(
    q: str | None = Query(default=None, description="Free-text query."),
    category: str | None = None,
    chain: str | None = Query(default=None, description='e.g. "base", "solana"'),
    region: str | None = None,
    health: str | None = Query(default=None, description=HEALTH_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Agent-friendly search across the x402 service directory."""
    with _conn() as c:
        services = db.search(c, q=q, category=category, chain=chain,
                             region=region, health=health, limit=limit, offset=offset)
    for svc in services:
        svc.pop("source", None)
        svc.pop("source_id", None)
        if svc.get("slug"):
            svc["service_card_url"] = f"/api/v1/services/{svc['slug']}"
    return {"count": len(services), "services": services}


@router.get("/api/v1/services/{slug}", tags=["directory"])
async def api_service(slug: str):
    with _conn() as c:
        svc = db.get_by_slug(c, slug)
    if not svc:
        raise HTTPException(404, "service not found")
    return cards.build_service_card(svc)


class AskPayload(BaseModel):
    query: str = Field(min_length=1, max_length=800)
    limit: int = Field(default=5, ge=1, le=10)
    candidate_limit: int = Field(default=30, ge=5, le=50)
    category: str | None = None
    chain: str | None = None
    max_price_usd: float | None = Field(default=None, ge=0)
    require_healthy: bool = True
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    has_mcp: bool = False
    use_llm: bool = True


_VIEW_KINDS = {"mcp", "a2a", "service"}


@router.post("/api/v1/track/view", tags=["directory"], include_in_schema=False)
async def track_view(request: Request):
    """Record a real-browser view of a detail page.

    Fired by a tiny JS beacon in the detail-page templates, so crawlers that
    don't run JS never trigger it — the counts are real human views. This
    endpoint is POST + under /api/, so Cloudflare never caches it (always hits
    origin) even though the detail HTML itself is CDN-cached.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind") or "")[:16]
    slug = str(body.get("slug") or "")[:200]
    ref = (str(body.get("ref"))[:300] if body.get("ref") else None)
    if kind not in _VIEW_KINDS or not slug:
        return Response(status_code=204)
    ua = (request.headers.get("user-agent") or "")[:300]
    client_ip = limits.client_ip_from_request(request)
    try:
        with db.writer() as c:
            db.log_page_view(c, kind, slug, ref=ref, client_ip=client_ip, ua=ua)
    except Exception:
        pass
    return Response(status_code=204)


@router.post("/api/v1/ask", tags=["directory"])
async def api_ask(request: Request, payload: AskPayload):
    """Ask for the best x402/MCP services for an intent.

    The endpoint retrieves directory candidates first, then asks the configured
    LLM to rank only those candidates. If the LLM is unavailable, it returns a
    deterministic directory-ranked fallback.
    """
    _enforce_ask_limit(request, payload.use_llm)
    return await directory_ask.answer_query(
        payload.query,
        limit=payload.limit,
        candidate_limit=payload.candidate_limit,
        category=payload.category,
        chain=payload.chain,
        max_price_usd=payload.max_price_usd,
        health="ok" if payload.require_healthy else None,
        min_confidence=payload.min_confidence,
        has_mcp=payload.has_mcp,
        use_llm=payload.use_llm,
    )


@router.get("/api/v1/ask", tags=["directory"])
async def api_ask_get(
    request: Request,
    q: str = Query(min_length=1, max_length=800),
    limit: int = Query(default=5, ge=1, le=10),
    category: str | None = None,
    chain: str | None = None,
    max_price_usd: float | None = Query(default=None, ge=0),
    require_healthy: bool = True,
    use_llm: bool = True,
):
    _enforce_ask_limit(request, use_llm)
    return await directory_ask.answer_query(
        q,
        limit=limit,
        category=category,
        chain=chain,
        max_price_usd=max_price_usd,
        health="ok" if require_healthy else None,
        use_llm=use_llm,
    )


@router.get("/api/v1/categories", tags=["directory"])
async def api_categories():
    with _conn() as c:
        return {"categories": db.list_categories(c)}


@router.get("/api/v1/a2a/search", tags=["a2a"])
async def api_a2a_search(
    q: str | None = Query(default=None, max_length=800),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    x402_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    with _conn() as c:
        rows = db.search_a2a(c, q=q, health=health, x402_only=x402_only,
                             limit=limit, offset=offset)
    return {
        "query": q,
        "count": len(rows),
        "agents": [directory_a2a.public_agent(r) for r in rows],
    }


@router.get("/api/v1/a2a/agents/{slug}", tags=["a2a"])
async def api_a2a_agent(slug: str):
    with _conn() as c:
        row = db.get_a2a_by_slug(c, slug)
    if not row:
        raise HTTPException(status_code=404, detail="A2A agent not found")
    return directory_a2a.public_agent(row)


@router.get("/api/v1/a2a/stats", tags=["a2a"])
async def api_a2a_stats():
    with _conn() as c:
        return db.a2a_stats(c)


@router.get("/api/v1/mcp/search", tags=["mcp"])
async def api_mcp_search(
    q: str | None = Query(default=None, max_length=800),
    chain: str | None = Query(default=None),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Search MCP servers.

    Unions the standalone MCP directory (PulseMCP / official registry import)
    with x402 services that also expose an mcp_url.
    """
    pull = limit + offset
    servers: list[dict] = []
    seen: set[str] = set()
    with _conn() as c:
        for r in db.search_mcp(c, q=q, health=health, limit=pull):
            item = directory_resources.normalize_mcp_server(r)
            key = (item.get("endpoint_url") or item.get("slug") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            servers.append(item)
        for r in db.search(c, q=q, chain=chain, health=health,
                           has_mcp=True, limit=pull):
            item = directory_resources.normalize_service(r, as_mcp=True)
            key = (item.get("endpoint_url") or item.get("slug") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            servers.append(item)
    window = servers[offset:offset + limit]
    return {
        "query": q,
        "count": len(window),
        "total_matched": len(servers),
        "servers": window,
    }


@router.get("/api/v1/mcp/stats", tags=["mcp"])
async def api_mcp_stats():
    with _conn() as c:
        return db.mcp_stats(c)


@router.get("/api/v1/mcp/servers/{slug}", tags=["mcp"])
async def api_mcp_server(slug: str):
    with _conn() as c:
        mcp = db.get_mcp_by_slug(c, slug)
        if mcp:
            # Hide crawl provenance from the public surface (same as the
            # a2a/services detail endpoints) while keeping it in the DB.
            for _k in ("source", "source_id", "source_url"):
                mcp.pop(_k, None)
            return mcp
        row = db.get_by_slug(c, slug)
    if not row or not (row.get("mcp_url") or "").strip():
        raise HTTPException(status_code=404, detail="MCP server not found")
    return cards.build_service_card(row)


@router.get("/api/v1/resources/search", tags=["directory"])
async def api_resources_search(
    q: str | None = Query(default=None, max_length=800),
    protocol: str | None = Query(default=None, pattern="^(x402|mcp|a2a)$"),
    chain: str | None = Query(default=None),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Unified search across x402 services, MCP servers and A2A agents."""
    with _conn() as c:
        return directory_resources.unified_search(
            c, q=q, protocol=protocol, chain=chain, health=health,
            limit=limit, offset=offset,
        )


@router.get("/api/v1/broker/recommend", tags=["directory"])
async def api_broker_recommend(
    request: Request,
    q: str = Query(min_length=1, max_length=800),
    max_price_usd: float | None = Query(default=None, ge=0),
    chain: str | None = Query(default=None),
    require_healthy: bool = True,
    limit: int = Query(default=5, ge=1, le=20),
):
    """Discovery + scoring broker: rank payable x402 endpoints for an intent.

    Does not settle payments; returns call_hint + pay_hint per recommendation.
    """
    with _conn() as c:
        return directory_resources.broker_recommend(
            c, q=q, max_price_usd=max_price_usd, chain=chain,
            require_healthy=require_healthy, limit=limit,
        )


@router.get("/api/v1/stats", tags=["directory"])
async def api_stats():
    with _conn() as c:
        return db.stats(c)


class SubmissionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = None
    chains: list[str] | None = None
    price_usdc: float | None = Field(default=None, ge=0)
    contact: str = Field(min_length=3, max_length=200,
                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/api/v1/submit", tags=["directory"])
async def api_submit(request: Request, payload: SubmissionPayload):
    url = str(payload.url).strip()
    client_ip = limits.client_ip_from_request(request)
    with _conn() as c:
        existing = db.find_service_by_url(c, url)
        pending = db.find_pending_submission(c, url)
        recent = db.count_recent_submissions(c, client_ip)
    if existing:
        return {
            "status": "already_listed",
            "message": "A service with this URL is already in the directory.",
            "slug": existing.get("slug"),
            "url": existing.get("url"),
        }
    if pending:
        return {
            "status": "already_pending",
            "message": "A submission for this URL is already submitted and auto-verifying.",
            "submission_id": pending.get("id"),
        }
    if recent >= SUBMIT_RATE_LIMIT_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": "Too many pending submissions from this IP in the last 24h.",
                "limit": SUBMIT_RATE_LIMIT_PER_DAY,
            },
        )
    state = limits.check_ip_limits(
        client_ip,
        "submit",
        (("day", SUBMIT_RATE_LIMIT_PER_DAY, 86400),),
    )
    if state:
        limits.raise_rate_limited(state, "Too many service submissions from this IP.")
    submission = payload.model_dump(mode="json")
    submission["url"] = url
    submission["_client_ip"] = client_ip
    submission["_source"] = "rest-submit"
    with db.writer() as c:
        sub_id = db.create_submission(c, submission)
    # Auto-review immediately — there is no human gate. x402 verification
    # decides: verified -> listed now, rejected -> dropped, uncertain ->
    # stays pending and is retried automatically by the crawl timer.
    try:
        review = await run_in_threadpool(
            directory_jobs.review_submission, sub_id, "auto-review (on-submit)")
    except Exception:
        review = {"status": "pending", "submission_id": sub_id}
    rstatus = review.get("status")
    if rstatus == "listed":
        return {
            "status": "listed",
            "submission_id": sub_id,
            "slug": review.get("slug"),
            "message": "Auto-verified x402 support — your service is now live in the directory.",
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
        "message": "Submitted; auto-verification was inconclusive and will be retried automatically.",
        "evidence": review.get("evidence"),
    }


class McpSubmissionPayload(BaseModel):
    url: HttpUrl
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    transport: str | None = Field(default=None, max_length=40)
    contact: str = Field(min_length=3, max_length=200,
                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class A2ASubmissionPayload(BaseModel):
    url: HttpUrl
    contact: str = Field(min_length=3, max_length=200,
                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _enforce_submit_limit(request: Request, scope: str) -> str:
    """Shared per-IP rate limit for the manual submit endpoints."""
    client_ip = limits.client_ip_from_request(request)
    state = limits.check_ip_limits(
        client_ip, scope, (("day", SUBMIT_RATE_LIMIT_PER_DAY, 86400),))
    if state:
        limits.raise_rate_limited(state, "Too many submissions from this IP today.")
    return client_ip


@router.post("/api/v1/mcp/submit", tags=["mcp"])
async def api_submit_mcp(request: Request, payload: McpSubmissionPayload):
    """Index a single MCP server by its streamable-http endpoint URL."""
    _enforce_submit_limit(request, "submit-mcp")
    endpoint = str(payload.url).strip()
    name = (payload.name or "").strip() or directory_crawlers._host_slug(endpoint).replace("-", " ").title()
    slug = directory_a2a._slugify(name) or directory_crawlers._host_slug(endpoint)
    probe = await run_in_threadpool(directory_crawlers.probe_mcp_health, endpoint)
    # Also probe for x402: an MCP server can be a paid (402) endpoint, in which
    # case it must ALSO land in the x402 services catalog (delivery=mcp).
    x402 = await run_in_threadpool(
        directory_reverify.verify_and_mirror, endpoint,
        slug=slug, name=name,
        description=(payload.description or "").strip() or None,
        homepage=endpoint, delivery="mcp", source="manual", source_id=endpoint)
    row = {
        "slug": slug,
        "name": name,
        "description": (payload.description or "").strip() or None,
        "homepage_url": endpoint,
        "endpoint_url": endpoint,
        "transport": (payload.transport or "streamable-http").strip(),
        "x402_supported": bool(x402.get("x402")),
        "source": "manual",
        "source_id": endpoint,
        "source_url": endpoint,
        "health": probe.get("status"),
        "health_checked": int(time.time()),
        "latency_ms": probe.get("latency_ms"),
        "http_status": probe.get("http_status"),
        "last_success_at": int(time.time()) if probe.get("status") == "ok" else None,
        "confidence": 0.5,
    }
    with db.writer() as c:
        created, server_id = db.upsert_mcp_server(c, row)
    await run_in_threadpool(
        directory_mailer.send_admin_notification,
        "MCP submission", name, "listed" if created else "updated",
        [("Endpoint", endpoint),
         ("Transport", (payload.transport or "streamable-http").strip()),
         ("Contact", (payload.contact or "").strip() or "—"),
         ("Health", probe.get("status")),
         ("x402", "yes — mirrored to x402 catalog" if x402.get("x402") else "no"),
         ("View", f"{directory_mailer.SITE}/mcp/servers/{slug}")])
    return {
        "status": "listed" if created else "updated",
        "slug": slug,
        "health": probe.get("status"),
        "x402": bool(x402.get("x402")),
        "x402_service_slug": x402.get("service_slug"),
        "view_url": f"/mcp/servers/{slug}",
        "message": ("Indexed your MCP server."
                    if created else "This MCP server was already indexed; refreshed it.")
        + (" Verified x402 payment support — also listed in the x402 services catalog."
           if x402.get("x402") else ""),
    }


@router.post("/api/v1/a2a/submit", tags=["a2a"])
async def api_submit_a2a(request: Request, payload: A2ASubmissionPayload):
    """Index an A2A agent by fetching its well-known Agent Card."""
    _enforce_submit_limit(request, "submit-a2a")
    url = str(payload.url).strip()
    card, card_url = await run_in_threadpool(directory_a2a.fetch_agent_card, url)
    if not card:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "no_agent_card",
                "message": ("No A2A Agent Card found at this URL. Make sure "
                            "/.well-known/agent-card.json is reachable."),
            },
        )
    row = directory_a2a.card_to_row(card, card_url, source="manual")
    # Probe for x402: an A2A agent can be a paid (402) endpoint, in which case
    # it must ALSO land in the x402 services catalog (delivery=a2a).
    verify_target = row.get("endpoint_url") or url
    x402 = await run_in_threadpool(
        directory_reverify.verify_and_mirror, verify_target,
        slug=row["slug"], name=row.get("name"),
        description=row.get("description"),
        homepage=row.get("homepage_url"), delivery="a2a",
        source="manual", source_id=row.get("source_id"))
    if x402.get("x402"):
        # real 402 probe is authoritative; never downgrade card self-declaration
        row["x402_supported"] = True
    with db.writer() as c:
        created, agent_id = db.upsert_a2a_agent(c, row)
    slug = row["slug"]
    await run_in_threadpool(
        directory_mailer.send_admin_notification,
        "A2A submission", row.get("name") or slug, "listed" if created else "updated",
        [("Card URL", card_url),
         ("Endpoint", row.get("endpoint_url") or "—"),
         ("Contact", (payload.contact or "").strip() or "—"),
         ("x402", "yes — mirrored to x402 catalog" if x402.get("x402") else "no"),
         ("View", f"{directory_mailer.SITE}/a2a/agents/{slug}")])
    return {
        "status": "listed" if created else "updated",
        "slug": slug,
        "name": row.get("name"),
        "x402": bool(x402.get("x402")),
        "x402_service_slug": x402.get("service_slug"),
        "view_url": f"/a2a/agents/{slug}",
        "message": ("Indexed your A2A agent from its Agent Card."
                    if created else "This agent was already indexed; refreshed its card.")
        + (" Verified x402 payment support — also listed in the x402 services catalog."
           if x402.get("x402") else ""),
    }


@router.get("/.well-known/agent-tools.json", tags=["discovery"])
async def well_known():
    return {
        "name": "agent-tools.cloud",
        "type": "x402-service-directory",
        "version": "0.5",
        "description": (
            "Free directory and MCP discovery layer for x402 paid APIs. "
            "The previously hosted paid Qwen relay and vertical paid endpoints "
            "were retired on 2026-05-25; this host is discovery-only."
        ),
        "paid_relay": False,
        "capabilities": [
            "search_services",
            "ask_services",
            "get_service_card",
            "list_categories",
            "submit_service",
            "mcp_discovery",
            "search_a2a_agents",
            "a2a_jsonrpc",
            "search_mcp_servers",
            "unified_resource_search",
            "broker_recommend",
        ],
        "endpoints": {
            "search": {
                "method": "GET",
                "url": "https://agent-tools.cloud/api/v1/search",
                "query": ["q", "category", "chain", "region", "health", "limit", "offset"],
                "returns": "ranked directory rows with service_card_url",
            },
            "ask": {
                "method": "POST",
                "url": "https://agent-tools.cloud/api/v1/ask",
                "body": {
                    "query": "find a current weather API that accepts x402",
                    "limit": 5,
                    "chain": "base",
                    "max_price_usd": 0.01,
                    "require_healthy": True,
                },
                "returns": "LLM-ranked recommendations grounded in directory candidates",
            },
            "get_service_card": {
                "method": "GET",
                "url_template": "https://agent-tools.cloud/api/v1/services/{slug}",
                "returns": "agent-readable service card with payment, call and quality metadata",
            },
            "categories": "https://agent-tools.cloud/api/v1/categories",
            "stats": "https://agent-tools.cloud/api/v1/stats",
            "submit": "https://agent-tools.cloud/api/v1/submit",
            "mcp": {
                "transport": "streamable-http",
                "url": "https://agent-tools.cloud/mcp-discovery/",
                "tools": ["search", "ask_services", "get", "list_categories", "stats", "register"],
            },
            "a2a": {
                "agent_card": "https://agent-tools.cloud/.well-known/agent-card.json",
                "jsonrpc": "https://agent-tools.cloud/a2a",
                "search": "https://agent-tools.cloud/api/v1/a2a/search",
                "get_agent": "https://agent-tools.cloud/api/v1/a2a/agents/{slug}",
            },
            "mcp_servers": {
                "search": "https://agent-tools.cloud/api/v1/mcp/search",
                "get_server": "https://agent-tools.cloud/api/v1/mcp/servers/{slug}",
                "stats": "https://agent-tools.cloud/api/v1/mcp/stats",
            },
            "resources_search": {
                "method": "GET",
                "url": "https://agent-tools.cloud/api/v1/resources/search",
                "query": ["q", "protocol", "chain", "health", "limit", "offset"],
                "returns": "unified x402 / mcp / a2a resources in one normalised shape",
            },
            "broker_recommend": {
                "method": "GET",
                "url": "https://agent-tools.cloud/api/v1/broker/recommend",
                "query": ["q", "max_price_usd", "chain", "require_healthy", "limit"],
                "returns": "scored payable endpoints with call_hint + pay_hint (no settlement)",
            },
            "x402_manifest": "https://agent-tools.cloud/.well-known/x402",
            "openapi": "https://agent-tools.cloud/openapi.json",
        },
        "agent_hint": (
            "Use POST /api/v1/ask for intent-level recommendations. Use "
            "GET /api/v1/search for faceted retrieval and then GET "
            "/api/v1/services/{slug} for the service card before paying or calling."
        ),
    }


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml() -> Response:
    """Dynamic sitemap listing every MCP server / A2A agent / x402 service
    detail page plus the main landing pages, so search engines and AI crawlers
    can discover the full directory."""
    base = "https://agent-tools.cloud"
    static_pages = [
        ("/", "1.0", "daily"),
        ("/mcp", "0.9", "daily"),
        ("/a2a", "0.9", "daily"),
        ("/x402", "0.9", "daily"),
        ("/categories", "0.6", "weekly"),
        ("/about", "0.4", "monthly"),
        ("/terms", "0.2", "yearly"),
        ("/privacy", "0.2", "yearly"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    def _url(loc, lastmod=None, priority=None, changefreq=None):
        out = ["  <url>", f"    <loc>{loc}</loc>"]
        if lastmod:
            out.append(f"    <lastmod>{lastmod}</lastmod>")
        if changefreq:
            out.append(f"    <changefreq>{changefreq}</changefreq>")
        if priority:
            out.append(f"    <priority>{priority}</priority>")
        out.append("  </url>")
        return "\n".join(out)

    def _iso(ts):
        if not ts:
            return None
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
        except (TypeError, ValueError, OverflowError):
            return None

    for path, prio, freq in static_pages:
        parts.append(_url(base + path, priority=prio, changefreq=freq))

    with _conn() as c:
        for slug, ts in c.execute(
                "SELECT slug, updated_at FROM mcp_servers ORDER BY slug"):
            parts.append(_url(f"{base}/mcp/servers/{slug}", _iso(ts), "0.7"))
        for slug, ts in c.execute(
                "SELECT slug, updated_at FROM a2a_agents ORDER BY slug"):
            parts.append(_url(f"{base}/a2a/agents/{slug}", _iso(ts), "0.7"))
        for slug, ts in c.execute(
                "SELECT slug, updated_at FROM services ORDER BY slug"):
            parts.append(_url(f"{base}/services/{slug}", _iso(ts), "0.7"))

    parts.append("</urlset>")
    xml = "\n".join(parts) + "\n"
    return Response(content=xml, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    """Welcome search engines and AI agents; point them at the sitemap."""
    body = (
        "# agent-tools.cloud - open directory of MCP servers, A2A agents and\n"
        "# x402 services. Crawlers and AI agents are welcome to index and use\n"
        "# the public catalogue and JSON API.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://agent-tools.cloud/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=3600"})
