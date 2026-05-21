"""FastAPI router for the agent-tools directory site."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl

from . import db

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()


def _conn():
    return db.connect(read_only=False)


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
    return {"count": len(services), "services": services}


@router.get("/api/v1/services/{slug}", tags=["directory"])
async def api_service(slug: str):
    with _conn() as c:
        svc = db.get_by_slug(c, slug)
    if not svc:
        raise HTTPException(404, "service not found")
    return svc


@router.get("/api/v1/categories", tags=["directory"])
async def api_categories():
    with _conn() as c:
        return {"categories": db.list_categories(c)}


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
async def api_submit(payload: SubmissionPayload):
    with db.writer() as c:
        c.execute(
            "INSERT INTO submissions (payload, status, created_at) VALUES (?, 'pending', ?)",
            (payload.model_dump_json(), int(time.time())),
        )
    return {"status": "received"}


@router.get("/.well-known/agent-tools.json", tags=["discovery"])
async def well_known():
    return {
        "name": "agent-tools.cloud",
        "description": "Global directory of 470+ x402 paid APIs and MCP services, "
                       "plus a pay-per-call Qwen3.6-35B-A3B inference relay at "
                       "flat $0.001 USDC / call on Base (x402 v2).",
        "endpoints": {
            "search": "https://agent-tools.cloud/api/v1/search",
            "categories": "https://agent-tools.cloud/api/v1/categories",
            "stats": "https://agent-tools.cloud/api/v1/stats",
            "submit": "https://agent-tools.cloud/api/v1/submit",
            "openapi": "https://agent-tools.cloud/openapi.json",
        },
        "agent_hint": (
            "Use GET /api/v1/search?q=<query>&category=<cat>&chain=<chain> "
            "to discover x402-payable APIs. Each result includes the "
            "service URL, /.well-known/x402.json (if any), MCP/OpenAPI "
            "endpoints, price range in USDC, and current health status."
        ),
    }
