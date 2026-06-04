"""FastAPI router for the agent-tools directory site."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl

from . import ask as directory_ask
from . import cards, db
from . import a2a as directory_a2a
from . import resources as directory_resources
from . import limits

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()


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
async def home(
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
        cats = db.list_categories(c)
        s = db.stats(c)
    return TEMPLATES.TemplateResponse(request, "categories.html", {"request": request, "categories": cats, "stats": s}
    )


@router.get("/submit", response_class=HTMLResponse, include_in_schema=False)
async def submit_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "submit.html", {"request": request})


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
async def about_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "about.html", {"request": request})


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
    health: str | None = None,
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
    health: str | None = Query(default=None),
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
    health: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Search MCP-callable services (x402 services that expose an mcp_url)."""
    with _conn() as c:
        rows = db.search(c, q=q, chain=chain, health=health,
                         has_mcp=True, limit=limit, offset=offset)
    return {
        "query": q,
        "count": len(rows),
        "servers": [directory_resources.normalize_service(r, as_mcp=True) for r in rows],
    }


@router.get("/api/v1/mcp/servers/{slug}", tags=["mcp"])
async def api_mcp_server(slug: str):
    with _conn() as c:
        row = db.get_by_slug(c, slug)
    if not row or not (row.get("mcp_url") or "").strip():
        raise HTTPException(status_code=404, detail="MCP server not found")
    return cards.build_service_card(row)


@router.get("/api/v1/resources/search", tags=["directory"])
async def api_resources_search(
    q: str | None = Query(default=None, max_length=800),
    protocol: str | None = Query(default=None, pattern="^(x402|mcp|a2a)$"),
    chain: str | None = Query(default=None),
    health: str | None = Query(default=None),
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
    contact: str | None = Field(default=None, max_length=200)


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
            "message": "A submission for this URL is already awaiting review.",
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
    return {"status": "pending", "submission_id": sub_id}


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
