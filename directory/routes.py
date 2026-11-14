"""FastAPI router for the agent-tools directory site."""

from __future__ import annotations

import secrets
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBearer
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, SecretStr
from starlette.concurrency import run_in_threadpool

from . import ask as directory_ask
from . import cards, db
from .articles import ArticleError
from . import a2a as directory_a2a
from . import a2a_research
from . import crawlers as directory_crawlers
from . import jobs as directory_jobs
from . import resources as directory_resources
from . import reverify_x402 as directory_reverify
from . import mailer as directory_mailer
from . import limits
from . import search_terms
from . import measured as directory_measured
from . import auth as directory_auth
from . import ownership as directory_ownership
from . import public_http as directory_public_http

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Build version — bump on deploy to bust browser/CDN HTML caches.
BUILD_VERSION = "2026-09-03.1"
TEMPLATES.env.globals["build_version"] = BUILD_VERSION
# Anonymous visitors cost no query: this returns before touching
# the DB when the session cookie is absent or unsigned.
from .auth import current_user as _current_user
TEMPLATES.env.globals["current_user"] = _current_user
from .auth import may_edit_listing as _may_edit_listing
TEMPLATES.env.globals["may_edit_listing"] = _may_edit_listing
TEMPLATES.env.globals["url_host"] = lambda u: db._host_and_path(u or "")[0]
TEMPLATES.env.filters["ymd"] = lambda ts: (
    time.strftime("%Y-%m-%d", time.gmtime(int(ts))) if ts else "")

router = APIRouter()

LEGACY_MCP_SLUG_REDIRECTS = {
    "lordevo-agent-shop": "lordevo-shop-x402-website-checks-shop-lordevo-com",
}

# Single source of truth for the liveness vocabulary, surfaced on every
# `health` filter (services / a2a / mcp) and echoed by the `health_status`
# field on each result.
SEARCH_SORT_PATTERN = "^(default|relevance|quality|newest|name)$"
SEARCH_SORT_DOC = (
    "Result order: default (text relevance with q, health-and-demand without q), "
    "relevance (title/description matches first; health and measured quality "
    "break ties; falls back to default without q), quality, newest, or name."
)

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
# Ranked search cannot satisfy its ORDER BY from an index, so a deep offset
# forces a proportionally larger sort. Cap it; bulk readers use /api/v1/export.
MAX_SEARCH_OFFSET = limits.env_int("AGENT_TOOLS_MAX_SEARCH_OFFSET", 1000)


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


# The homepage aggregates walk whole tables -- mcp_stats alone reads every row
# to bucket distinct domains -- but the numbers only move when the crawl and
# health timers run, hours apart.
# Homepage traffic is sparse: measured over a full day, visits arrive 380s
# apart on average, so a 300s window expired before the next visitor and 55%
# of them still paid for a rebuild. At 1800s that falls to 1%, and the figures
# are still eight times fresher than the four-hour job that changes them.
_HOME_CACHE_TTL = limits.env_int("AGENT_TOOLS_HOME_CACHE_TTL", 1800)
_home_cache: dict = {"at": 0.0, "payload": None}
_home_cache_lock = threading.Lock()


def _home_payload():
    now = time.time()
    # Held across the rebuild so a burst on an expired entry recomputes once
    # rather than once per waiting thread.
    with _home_cache_lock:
        with closing(_conn()) as c:
            c.execute("BEGIN")
            revision = db.get_meta(c, "owner_catalog_revision", "")
            cached = _home_cache["payload"]
            if (cached is not None and now - _home_cache["at"] < _HOME_CACHE_TTL
                    and _home_cache.get("revision", "") == revision):
                return cached
            payload = {
                "stats": db.stats(c),
                "mcp_stats": db.mcp_stats(c),
                "a2a_stats": db.a2a_stats(c),
                "top_mcp": db.top_rated(c, "mcp", 5),
                "top_a2a": db.top_rated(c, "a2a", 5),
                "top_x402": db.top_rated(c, "x402", 5),
                "grade_mix": db.grade_mix_all(c),
            }
        _home_cache["at"], _home_cache["payload"] = now, payload
        _home_cache["revision"] = revision
        return payload


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    article_store = getattr(request.app.state, "article_store", None)
    latest_articles = article_store.list_published(page_size=3)["items"] if article_store is not None else []
    # The daily /stats snapshot feeds the hero panel and the findings; the page
    # falls back to the live counters when no snapshot exists yet.
    try:
        snap, history = _stats_payload()
    except Exception:
        snap, history = None, []
    headlines = band = None
    if snap:
        from . import stats as stats_mod
        try:
            headlines = stats_mod.headlines(snap)
        except Exception:
            headlines = None
        try:
            band = stats_mod.home_band(snap, history)
        except Exception:  # noqa: BLE001 - the band is optional
            import logging
            logging.getLogger(__name__).exception("home band")
            band = None
    from . import promo
    return TEMPLATES.TemplateResponse(
        request, "home.html", {"request": request, "latest_articles": latest_articles,
                               "snap": snap, "headlines": headlines, "band": band,
                               "promo": promo.slots("home-after-index", "home-before-blog"),
                               **_home_payload()})


@router.get("/x402", response_class=HTMLResponse, include_in_schema=False)
def x402_page(
    request: Request,
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    chain: str | None = Query(default=None),
    region: str | None = Query(default=None),
    health: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
    view: str | None = Query(default=None),
):
    with _conn() as c:
        services = db.attach_ratings("x402", db.search(
            c, q=q, category=category, chain=chain,
            region=region, health=health, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in services))
        cats = db.list_categories(c)
        s = db.stats(c)
        leaders = db.top_rated(c, "x402", 20)
    if not request.query_params.get("ref"):  # our own links (topic pages) are not demand
        search_terms.record("x402", q, len(services), "web")
    return TEMPLATES.TemplateResponse(request, "index.html", {
            "request": request, "services": services, "categories": cats,
            "stats": s, "q": q or "", "active_category": category,
            "active_chain": chain, "active_region": region, "active_health": health,
            "active_sort": sort,
            "leaders": leaders, "view": view,
            "exact_name_groups": exact_name_groups,
        },
    )


def _privacy_suppressed_response(kind: str, slug: str, detail: str):
    with _conn() as conn:
        suppressed = db.find_privacy_suppression_any(
            conn, kind, {"slug": slug})
    if suppressed is None:
        return None
    return Response(
        content=json.dumps({"detail": detail}, separators=(",", ":")),
        status_code=404,
        media_type="application/json",
        headers={
            "X-Agent-Tools-Privacy-Suppressed": "1",
        },
    )


def _retired_response(request: Request, kind: str, slug: str):
    detail = {"x402": "service not found", "mcp": "mcp server not found",
              "a2a": "a2a agent not found"}[kind]
    suppressed = _privacy_suppressed_response(kind, slug, detail)
    if suppressed is not None:
        return suppressed
    with _conn() as conn:
        retired = db.get_retired_listing(conn, kind, slug)
        if retired:
            retired["replacement"] = directory_auth.owner_lifecycle.replacement(conn, kind, retired)
    if retired is None:
        return None
    retired["retired_display"] = time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(retired["retired_at"]))
    return TEMPLATES.TemplateResponse(
        request, "retired.html", {"request": request, "retired": retired},
        status_code=410,
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store",
                 "CDN-Cache-Control": "no-store", "Cloudflare-CDN-Cache-Control": "no-store"},
    )


# Detail pages offered to search engines are exactly the sitemap set: reachable,
# readiness A or B, and the best-scored listing for its endpoint. Every other
# detail page stays reachable and linkable but asks not to be indexed, so the
# thin and duplicate pages do not drag down the ones worth indexing.
_DETAIL_INDEX_SQL = {
    "x402": ("services", "url", "url IN (?, ?)"),
    "mcp": ("mcp_servers", "endpoint_url", "endpoint_url IN (?, ?)"),
    "a2a": ("a2a_agents", "COALESCE(endpoint_url, card_url)",
            "(endpoint_url IN (?, ?) OR (endpoint_url IS NULL AND card_url IN (?, ?)))"),
}


def _detail_noindex(kind: str, listing_id) -> bool:
    table, endpoint, match = _DETAIL_INDEX_SQL[kind]
    try:
        with _conn() as c:
            row = c.execute(
                f"SELECT health, readiness_grade, COALESCE(quality_score, 0) q, "
                f"{endpoint} ep FROM {table} WHERE id=?", (listing_id,)).fetchone()
            if row is None:
                return True
            if row["health"] != "ok" or row["readiness_grade"] not in ("A", "B"):
                return True
            if not row["ep"]:
                return False
            base = str(row["ep"]).rstrip("/")
            variants = (base, base + "/") * match.count("IN (?, ?)")
            better = c.execute(
                f"SELECT 1 FROM {table} WHERE {match} AND id != ? AND health='ok' "
                "AND readiness_grade IN ('A','B') AND (COALESCE(quality_score, 0) > ? "
                "OR (COALESCE(quality_score, 0) = ? AND id < ?)) LIMIT 1",
                (*variants, listing_id, row["q"], row["q"], listing_id)).fetchone()
            return better is not None
    except sqlite3.Error:
        return False


# Whole sources deleted on purpose: every listing they produced is gone for
# good (2026-09-14 Glama purge, 4,199 rows; its slugs all start "glama-").
# Only these get 410; any other unknown slug stays a plain 404 so it cannot be
# told apart from a privacy-suppressed one.
_PURGED_MCP_PREFIXES = ("glama-",)


def _detail_gone(request: Request, detail: str):
    """410 so search engines drop a deliberately deleted listing faster than
    they drop a 404."""
    headers = {"X-Robots-Tag": "noindex, nofollow"}
    if "text/html" in request.headers.get("accept", ""):
        return TEMPLATES.TemplateResponse(request, "404.html", {"request": request},
                                          status_code=410, headers=headers)
    return JSONResponse({"detail": detail}, status_code=410, headers=headers)


def _owned_detail_headers(row):
    if row.get("owner_verified"):
        return {"Cache-Control": "private, no-store", "CDN-Cache-Control": "no-store",
                "Cloudflare-CDN-Cache-Control": "no-store"}
    return {}


@router.get("/services/{slug}", response_class=HTMLResponse, include_in_schema=False)
def service_detail(request: Request, slug: str):
    with _conn() as c:
        svc = db.get_by_slug(c, slug)
    if not svc:
        # Retired or privacy-suppressed slugs keep their 410/404 page; only a
        # slug that simply moved to another row is redirected.
        retired = _retired_response(request, "x402", slug)
        if retired is not None:
            return retired
        with _conn() as c:
            moved = db.resolve_moved_service_slug(c, slug)
        if moved:
            return Response(status_code=301,
                            headers={"Location": f"/services/{moved}"})
        raise HTTPException(404, "service not found")
    from . import paid_verify
    with _conn() as c:
        paid_summary = paid_verify.public_summary(c, int(svc["id"]))
    return TEMPLATES.TemplateResponse(request, "service.html", {
        "request": request, "svc": svc, "rating": db.score_breakdown(svc),
        "noindex": _detail_noindex("x402", svc["id"]), "meta_desc": directory_measured.meta_description("x402", svc),
        "paid_summary": paid_summary,
        "call_template": cards.build_call_template(svc),
        "interface_declaration": directory_auth.owner_declarations.public_state(svc)},
        headers=_owned_detail_headers(svc))


_CATEGORY_PAGE_SIZE = 60
_CATEGORY_CACHE_TTL = 300
_category_cache: dict = {}
_category_cache_lock = threading.Lock()
_CATEGORY_KINDS = {
    "x402": ("x402 Services", "/x402", "category"),
    "mcp": ("MCP Topics", "/mcp", "q"),
    "a2a": ("A2A Skill Tags", "/a2a", "q"),
}


def _category_rows(kind: str):
    # Three aggregate entries per worker, never one cache entry per query.
    with _category_cache_lock:
        with closing(_conn()) as c:
            c.execute("BEGIN")
            revision = db.get_meta(c, "owner_catalog_revision", "")
            cached = _category_cache.get(kind)
            if (cached and time.monotonic() - cached[0] < _CATEGORY_CACHE_TTL
                    and len(cached) > 2 and cached[2] == revision):
                return cached[1]
            loader = {"x402": db.list_categories, "mcp": db.mcp_categories,
                      "a2a": db.a2a_categories}[kind]
            rows = sorted(loader(c), key=lambda r: (-r["count"], r["category"]))
        _category_cache[kind] = (time.monotonic(), rows, revision)
        return rows


@router.get("/categories", response_class=HTMLResponse, include_in_schema=False)
def categories_page(
    request: Request,
    kind: str = Query(default="x402", pattern="^(x402|mcp|a2a)$"),
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1, le=100000),
):
    query = q.strip()
    rows = _category_rows(kind)
    matched = [r for r in rows if query.casefold() in r["category"].casefold()]
    pages = max(1, (len(matched) + _CATEGORY_PAGE_SIZE - 1) // _CATEGORY_PAGE_SIZE)
    page = min(page, pages)
    start = (page - 1) * _CATEGORY_PAGE_SIZE
    title, target, param = _CATEGORY_KINDS[kind]
    items = [{**r, "url": target + "?" + urlencode({param: r["category"]})}
             for r in matched[start:start + _CATEGORY_PAGE_SIZE]]
    def page_url(number):
        return "/categories?" + urlencode({"kind": kind, "q": query, "page": number})
    return TEMPLATES.TemplateResponse(request, "categories.html", {
        "request": request, "kind": kind, "kinds": _CATEGORY_KINDS,
        "q": query, "title": title, "items": items, "total": len(matched),
        "all_total": len(rows), "page": page, "pages": pages,
        "first": start + 1 if items else 0, "last": start + len(items),
        "previous_url": page_url(page - 1) if page > 1 else None,
        "next_url": page_url(page + 1) if page < pages else None,
    })


@router.get("/submit", response_class=HTMLResponse, include_in_schema=False)
def submit_page(request: Request, type: str | None = Query(default=None)):
    active = type if type in ("x402", "mcp", "a2a") else "x402"
    return TEMPLATES.TemplateResponse(request, "submit.html", {"request": request, "active_type": active})


TEMPLATES.env.filters["num"] = lambda v: (
    f"{v:,}" if isinstance(v, int) else (f"{v:,.1f}" if isinstance(v, float) else (v if v is not None else "–")))

_stats_cache: dict = {"at": 0.0, "snap": None, "history": []}
_stats_lock = threading.Lock()
STATS_VIEWS = ("all", "x402", "mcp", "a2a")
STATS_PERIODS = (7, 30, 90)


def _stats_payload() -> tuple[dict | None, list]:
    """Latest stored snapshot + KPI history, cached in-process for 10 minutes."""
    now = time.time()
    with _stats_lock:
        if _stats_cache["snap"] is not None and now - _stats_cache["at"] < 600:
            return _stats_cache["snap"], _stats_cache["history"]
        with _conn() as c:
            snap = db.latest_stats_snapshot(c)
            history = db.stats_history(c, 90)
        _stats_cache.update(at=now, snap=snap, history=history)
        return snap, history


def _kpi_trend(history, view, metric, period):
    """(sparkline points, change, day the change is measured from).

    Only snapshots inside the period count. When the stored history is shorter
    than the period the change is measured from the first stored day, and the
    page says so instead of claiming "vs 30d ago".
    """
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - period * 86400))
    points = [(d, kp.get(view, {}).get(metric)) for d, kp in history if d >= cutoff]
    points = [(d, v) for d, v in points if isinstance(v, (int, float))]
    if len(points) < 2:
        return [v for _d, v in points], None, None
    return [v for _d, v in points], points[-1][1] - points[0][1], points[0][0]


@router.get("/stats", response_class=HTMLResponse, include_in_schema=False)
def stats_page(request: Request,
               view: str = Query(default="all"),
               period: int = Query(default=7)):
    view = view if view in STATS_VIEWS else "all"
    period = period if period in STATS_PERIODS else 7
    if view != "all":
        # One URL per protocol: /stats/mcp, /stats/x402, /stats/a2a.
        suffix = f"?period={period}" if period != 7 else ""
        return RedirectResponse(f"/stats/{view}{suffix}", status_code=301)
    return _render_stats(request, "all", period)


def _render_stats(request: Request, view: str, period: int):
    from . import stats as stats_mod
    snap, history = _stats_payload()
    if snap is None:
        return TEMPLATES.TemplateResponse(
            request, "stats.html", {"request": request, "snap": None}, status_code=503)
    kpis = {}
    real = [d for d, kp in history if not kp.get("backfill")]
    first_real = real[0] if real else None
    full_from = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (period - 1) * 86400))
    for metric in ("indexed", "reachable", "verified", "ready"):
        series, delta, since = _kpi_trend(history, view, metric, period)
        kpis[metric] = {"value": snap["kpis"][view][metric], "series": series, "delta": delta,
                        "since": since,
                        "full": bool(since and since <= full_from),
                        "backfilled": bool(since and first_real and since < first_real)}
    return TEMPLATES.TemplateResponse(request, "stats.html", {
        "request": request, "snap": snap, "view": view, "period": period,
        "kpis": kpis, "headlines": stats_mod.headlines(snap, view),
        "first_day": first_real,
        "backfill_used": any(k["backfilled"] for k in kpis.values()),
        "kinds": [k for k in stats_mod.KINDS if view in ("all", k)],
        "kind_label": stats_mod.KIND_LABEL,
        "page_title": (stats_mod.KIND_LABEL[view] + " statistics") if view != "all" else "The state of agent tools",
        "page_path": "/stats" if view == "all" else f"/stats/{view}",
        "month_title": time.strftime("%B %Y", time.strptime(snap["as_of"][:7], "%Y-%m")),
    })


FULL_DOC = ("Add full=1 with an account (browser sign-in, or Authorization: Bearer <key>; "
            "a key is free from POST /api/v1/keys) for the complete lists.")
_PUBLIC_JSON = {"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"}
_PRIVATE_JSON = {"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"}


def _full_access(request: Request, full: bool) -> bool:
    """True when the caller asked for full=1 and has an account; 401 otherwise."""
    if not full:
        return False
    if directory_auth.current_user(request) is None:
        raise HTTPException(status_code=401, detail={
            "error": "account_required",
            "message": "full=1 needs an account. Without it the public lists are returned.",
            "browser": "https://agent-tools.cloud/auth/github/login",
            "get_a_key": {"method": "POST", "url": "https://agent-tools.cloud/api/v1/keys"},
            "send": "Authorization: Bearer <key>"})
    return True


@router.get("/stats.json", tags=["directory"], summary="Full daily statistics snapshot")
def stats_json(request: Request, full: bool = Query(default=False, description=FULL_DOC)):
    """Everything behind /stats: funnel, sources, protocol versions, readiness,
    uptime and latency, x402 prices/networks/payment evidence, access, safety,
    topic coverage, growth and search demand. Recomputed once a day. CC BY 4.0.
    Without an account the topic and search lists are shortened."""
    from . import stats as stats_mod
    is_full = _full_access(request, full)
    snap, _h = _stats_payload()
    if snap is None:
        raise HTTPException(status_code=503, detail="statistics not computed yet")
    if is_full:
        return JSONResponse(snap, headers=_PRIVATE_JSON)
    return JSONResponse(stats_mod.public_snapshot(snap), headers=_PUBLIC_JSON)


@router.get("/stats/searches.json", tags=["directory"],
            summary="What people search for (aggregate; terms with >= 5 searches)")
def stats_searches_json(request: Request, full: bool = Query(default=False, description=FULL_DOC)):
    """Most searched terms and terms that mostly found nothing over the last 30
    days, across web, API and MCP searches. No visitor data is kept with the
    counts. Public: first 10 of each list; with an account: up to 100."""
    from . import stats as stats_mod
    is_full = _full_access(request, full)
    snap, _h = _stats_payload()
    data = stats_mod.searches_view(snap, full=is_full)
    data["license"] = "CC BY 4.0"
    return JSONResponse(data, headers=_PRIVATE_JSON if is_full else _PUBLIC_JSON)


@router.get("/stats/usable", response_class=HTMLResponse, include_in_schema=False)
def stats_usable_page(request: Request):
    from . import stats as stats_mod
    snap, _h = _stats_payload()
    data = stats_mod.usable(snap)
    return TEMPLATES.TemplateResponse(request, "stats_usable.html", {
        "request": request, "u": data, "snap": snap,
    }, status_code=200 if data["available"] else 503)


@router.get("/stats/usable.json", tags=["directory"],
            summary="How many agent tools actually work, and why the rest do not")
def stats_usable_json():
    """One-sentence answer, per-protocol reachable/verified/ready shares, median
    latency, 30-day uptime and the outcome of every listing's latest probe
    (answered, asks for credentials, 404, 5xx, no response). Daily. CC BY 4.0."""
    from . import stats as stats_mod
    snap, _h = _stats_payload()
    data = stats_mod.usable(snap)
    if not data["available"]:
        raise HTTPException(status_code=503, detail="statistics not computed yet")
    data["license"] = "CC BY 4.0"
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=600",
                                       "Access-Control-Allow-Origin": "*"})


@router.get("/stats/topics", include_in_schema=False)
def stats_topics_index():
    return RedirectResponse("/stats#topics", status_code=302)


@router.get("/stats/topics/{topic}.json", tags=["directory"],
            summary="One topic across x402, MCP and A2A: supply, readiness, top listings, searches")
def stats_topic_json(request: Request, topic: str,
                     full: bool = Query(default=False, description=FULL_DOC)):
    """Reachable listings mentioning the topic word per protocol, how many are
    ready (A/B), median readiness and latency, the best listings per protocol
    (3 public, 10 with an account), supply gaps and matching search terms.
    Daily. CC BY 4.0."""
    from . import stats as stats_mod
    is_full = _full_access(request, full)
    snap, _h = _stats_payload()
    data = stats_mod.topic_page(snap, topic.lower(), full=is_full)
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown topic; one of: {', '.join(stats_mod.TOPICS)}")
    data["license"] = "CC BY 4.0"
    return JSONResponse(data, headers=_PRIVATE_JSON if is_full else _PUBLIC_JSON)


@router.get("/stats/topics/{topic}", response_class=HTMLResponse, include_in_schema=False)
def stats_topic_page(request: Request, topic: str):
    from . import stats as stats_mod
    snap, _h = _stats_payload()
    data = stats_mod.topic_page(snap, topic.lower())
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown topic")
    return TEMPLATES.TemplateResponse(request, "stats_topic.html", {
        "request": request, "t": data, "topics": [r["topic"] for r in snap["topics"]["rows"]]})


def _report_or_404(month: str):
    from . import stats as stats_mod
    with _conn() as c:
        try:
            data = stats_mod.monthly_report(c, month)
        except Exception:  # noqa: BLE001 - old snapshot shapes
            data = None
    if data is None:
        raise HTTPException(status_code=404, detail="No report for that month")
    return data


@router.get("/stats/reports", response_class=HTMLResponse, include_in_schema=False)
def stats_reports_page(request: Request):
    from . import stats as stats_mod
    with _conn() as c:
        months = stats_mod.months(c)
    current = time.strftime("%Y-%m", time.gmtime())
    return TEMPLATES.TemplateResponse(request, "stats_reports.html", {
        "request": request, "months": months, "current": current})


@router.get("/stats/reports.json", tags=["directory"], summary="Monthly reports available")
def stats_reports_json():
    """Months with a report. A month is complete once it has ended; the
    current month is shown as month-to-date."""
    from . import stats as stats_mod
    with _conn() as c:
        months = stats_mod.months(c)
    current = time.strftime("%Y-%m", time.gmtime())
    return JSONResponse({"reports": [
        {"month": m, "complete": m < current,
         "page": f"https://agent-tools.cloud/stats/reports/{m}",
         "json": f"https://agent-tools.cloud/stats/reports/{m}.json"} for m in months]},
        headers={"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@router.get("/stats/reports/{month}.json", tags=["directory"], summary="Monthly report (start vs end of month)")
def stats_report_json(month: str):
    """Start-of-month vs end-of-month counts per protocol, new listings,
    findings, x402 prices, protocol versions, probe outcomes and the most
    searched terms (aggregate, >= 5 searches). CC BY 4.0."""
    return JSONResponse(_report_or_404(month), headers={
        "Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@router.get("/stats/reports/{month}", response_class=HTMLResponse, include_in_schema=False)
def stats_report_page(request: Request, month: str):
    from . import stats as stats_mod
    return TEMPLATES.TemplateResponse(request, "stats_report.html", {
        "request": request, "r": _report_or_404(month), "kind_label": stats_mod.KIND_LABEL})


@router.get("/stats/daily.csv", tags=["directory"], summary="Headline metrics for every stored day (CSV)",
            response_class=Response)
def stats_daily_csv(request: Request, full: bool = Query(default=False, description=FULL_DOC)):
    """Headline metrics per day, long format: day,kind,metric,value. Public:
    the last 90 days; with an account and full=1: every stored day."""
    import csv
    import io
    from . import stats as stats_mod
    is_full = _full_access(request, full)
    since = "0000" if is_full else time.strftime("%Y-%m-%d", time.gmtime(time.time() - 89 * 86400))
    with _conn() as c:
        try:
            rows = c.execute("SELECT day, json FROM stats_snapshots WHERE day >= ? ORDER BY day",
                             (since,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(("day", "kind", "metric", "value"))
    for day, raw in rows:
        for kind, metric, value in stats_mod.flatten(json.loads(raw)):
            w.writerow((day, kind, metric, value))
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'inline; filename="agent-tools-stats-daily.csv"',
        **(_PRIVATE_JSON if is_full else _PUBLIC_JSON)})


@router.get("/stats/{kind}", response_class=HTMLResponse, include_in_schema=False)
def stats_kind_page(request: Request, kind: str, period: int = Query(default=7)):
    if kind not in ("x402", "mcp", "a2a"):
        raise HTTPException(status_code=404, detail="Not found")
    return _render_stats(request, kind, period if period in STATS_PERIODS else 7)


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def about_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "about.html", {"request": request})


@router.get("/docs/claim", response_class=HTMLResponse, include_in_schema=False)
def claim_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "claim.html", {"request": request})


# Both were public briefly: /ownership named after the internal table, /claim
# without saying it is documentation. Redirect straight to the final URL rather
# than chaining.
@router.get("/docs", include_in_schema=False)
@router.get("/claim", include_in_schema=False)
@router.get("/ownership", include_in_schema=False)
def claim_docs_redirect() -> Response:
    return Response(status_code=301, headers={"Location": "/docs/claim"})


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "terms.html", {"request": request})


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "privacy.html", {"request": request})


@router.get("/mcp", response_class=HTMLResponse, include_in_schema=False)
def mcp_page(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
    access: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
    view: str | None = Query(default=None),
):
    with _conn() as c:
        servers = db.attach_ratings("mcp", db.search_mcp(
            c, q=q, health=health, x402_only=bool(x402),
            access=access, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in servers))
        s = db.mcp_stats(c)
        leaders = db.top_rated(c, "mcp", 20)
    if not request.query_params.get("ref"):  # our own links (topic pages) are not demand
        search_terms.record("mcp", q, len(servers), "web")
    return TEMPLATES.TemplateResponse(request, "mcp.html", {
            "request": request, "servers": servers, "stats": s, "q": q or "",
            "active_health": health, "active_x402": bool(x402),
            "active_access": access, "active_sort": sort,
            "leaders": leaders, "view": view,
            "exact_name_groups": exact_name_groups,
        },
    )


@router.get("/mcp/servers/{slug}", response_class=HTMLResponse, include_in_schema=False)
def mcp_detail(request: Request, slug: str):
    if replacement_slug := LEGACY_MCP_SLUG_REDIRECTS.get(slug):
        return Response(
            status_code=301,
            headers={"Location": f"/mcp/servers/{replacement_slug}"},
        )
    with _conn() as c:
        m = db.get_mcp_by_slug(c, slug)
    if not m:
        retired = _retired_response(request, "mcp", slug)
        if retired is not None:
            return retired
        if slug.startswith(_PURGED_MCP_PREFIXES):
            return _detail_gone(request, "mcp server not found")
        raise HTTPException(404, "mcp server not found")
    return TEMPLATES.TemplateResponse(request, "mcp_server.html", {
        "request": request, "m": m, "quality": db.rate_row("mcp", m),
        "noindex": _detail_noindex("mcp", m["id"]), "meta_desc": directory_measured.meta_description("mcp", m)},
        headers=_owned_detail_headers(m))


@router.get("/_partials/mcp", response_class=HTMLResponse, include_in_schema=False)
def mcp_partial(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
    access: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
    view: str | None = Query(default=None),
):
    with _conn() as c:
        servers = db.attach_ratings("mcp", db.search_mcp(
            c, q=q, health=health, x402_only=bool(x402), access=access, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in servers))
    return TEMPLATES.TemplateResponse(request, "_mcp_grid.html", {
        "request": request, "servers": servers, "view": view,
        "exact_name_groups": exact_name_groups,
    })


@router.get("/a2a", response_class=HTMLResponse, include_in_schema=False)
def a2a_page(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
    access: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
    view: str | None = Query(default=None),
):
    with _conn() as c:
        agents = db.attach_ratings("a2a", db.search_a2a(
            c, q=q, health=health, x402_only=bool(x402),
            access=access, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in agents))
        s = db.a2a_stats(c)
        leaders = db.top_rated(c, "a2a", 20)
    if not request.query_params.get("ref"):  # our own links (topic pages) are not demand
        search_terms.record("a2a", q, len(agents), "web")
    return TEMPLATES.TemplateResponse(request, "a2a.html", {
            "request": request, "agents": agents, "stats": s, "q": q or "",
            "active_health": health, "active_x402": bool(x402),
            "active_access": access, "active_sort": sort,
            "leaders": leaders, "view": view,
            "exact_name_groups": exact_name_groups,
        },
    )


@router.get("/a2a/agents/{slug}", response_class=HTMLResponse, include_in_schema=False)
def a2a_detail(request: Request, slug: str):
    with _conn() as c:
        a = db.get_a2a_by_slug(c, slug)
    if not a:
        retired = _retired_response(request, "a2a", slug)
        if retired is not None:
            return retired
        raise HTTPException(404, "a2a agent not found")
    return TEMPLATES.TemplateResponse(request, "a2a_agent.html", {
        "request": request, "a": a, "quality": db.rate_row("a2a", a),
        "noindex": _detail_noindex("a2a", a["id"]), "meta_desc": directory_measured.meta_description("a2a", a)},
        headers=_owned_detail_headers(a))


@router.get("/_partials/a2a", response_class=HTMLResponse, include_in_schema=False)
def a2a_partial(
    request: Request,
    q: str | None = Query(default=None),
    health: str | None = Query(default=None),
    x402: str | None = Query(default=None),
    access: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
    view: str | None = Query(default=None),
):
    with _conn() as c:
        agents = db.attach_ratings("a2a", db.search_a2a(
            c, q=q, health=health, x402_only=bool(x402), access=access, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in agents))
    return TEMPLATES.TemplateResponse(request, "_a2a_grid.html", {
        "request": request, "agents": agents, "view": view,
        "exact_name_groups": exact_name_groups,
    })


@router.get("/_partials/services", response_class=HTMLResponse, include_in_schema=False)
def services_partial(
    request: Request,
    view: str | None = Query(default=None),
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    chain: str | None = Query(default=None),
    region: str | None = Query(default=None),
    health: str | None = Query(default=None),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN),
):
    with _conn() as c:
        services = db.attach_ratings("x402", db.search(
            c, q=q, category=category, chain=chain,
            region=region, health=health, sort=sort, limit=60))
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in services))
    return TEMPLATES.TemplateResponse(request, "_service_grid.html", {
        "request": request, "services": services, "view": view,
        "exact_name_groups": exact_name_groups,
    })


@router.get("/api/v1/search", tags=["directory"])
def api_search(
    q: str | None = Query(default=None, description="Free-text query."),
    category: str | None = None,
    chain: str | None = Query(default=None, description='e.g. "base", "solana"'),
    region: str | None = None,
    health: str | None = Query(default=None, description=HEALTH_DOC),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN,
                      description=SEARCH_SORT_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=MAX_SEARCH_OFFSET),
):
    """Agent-friendly search across the x402 service directory."""
    with _conn() as c:
        services = db.search(c, q=q, category=category, chain=chain,
                             region=region, health=health, sort=sort,
                             limit=limit, offset=offset)
        db.attach_sources(c, "x402", services)
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in services))
    if not offset:
        search_terms.record("x402", q, len(services), "api")
    services = [cards.public_service_row(svc) for svc in services]
    for svc in services:
        svc.pop("source", None)
        svc.pop("source_id", None)
        svc.pop("search_rank", None)
        svc.pop("search_priority", None)
        if svc.get("slug"):
            svc["service_card_url"] = f"/api/v1/services/{svc['slug']}"
    return {
        "count": len(services),
        "sort": sort,
        "services": services,
        "exact_name_matches": next(
            (group for group in exact_name_groups if group["query_exact"]), None),
        "exact_name_groups": exact_name_groups,
    }


@router.get("/api/v1/services/{slug}", tags=["directory"])
def api_service(slug: str):
    with _conn() as c:
        svc = db.get_by_slug(c, slug)
    if not svc:
        suppressed = _privacy_suppressed_response(
            "x402", slug, "service not found")
        if suppressed is not None:
            return suppressed
        with _conn() as c:
            retired = db.get_retired_listing(c, "x402", slug)
        if retired is not None:
            raise HTTPException(status_code=410, detail={
                "status": "retired", "slug": slug, "reason": retired["reason"]})
        raise HTTPException(404, "service not found")
    from . import paid_verify
    card = cards.build_service_card(svc)
    # Display-only conformance breakdown; search order uses the quality grade.
    card["readiness"] = svc.get("readiness")
    with _conn() as c:
        card["paid_verification"] = paid_verify.public_summary(c, int(svc["id"]))
    return card


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
def api_categories():
    with _conn() as c:
        return {"categories": db.list_categories(c)}


@router.get("/api/v1/a2a/search", tags=["a2a"])
def api_a2a_search(
    q: str | None = Query(default=None, max_length=800),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    x402_only: bool = Query(default=False),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN,
                      description=SEARCH_SORT_DOC),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=MAX_SEARCH_OFFSET),
):
    with _conn() as c:
        rows = db.search_a2a(c, q=q, health=health, x402_only=x402_only,
                             sort=sort, limit=limit, offset=offset)
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in rows))
    if not offset:
        search_terms.record("a2a", q, len(rows), "api")
    return {
        "query": q,
        "sort": sort,
        "count": len(rows),
        "agents": [directory_a2a.public_agent(r) for r in rows],
        "exact_name_matches": next(
            (group for group in exact_name_groups if group["query_exact"]), None),
        "exact_name_groups": exact_name_groups,
    }


@router.get("/api/v1/a2a/agents/{slug}", tags=["a2a"])
def api_a2a_agent(slug: str):
    with _conn() as c:
        row = db.get_a2a_by_slug(c, slug)
    if not row:
        suppressed = _privacy_suppressed_response(
            "a2a", slug, "A2A agent not found")
        if suppressed is not None:
            return suppressed
        with _conn() as c:
            retired = db.get_retired_listing(c, "a2a", slug)
        if retired is not None:
            raise HTTPException(status_code=410, detail={
                "status": "retired", "slug": slug, "reason": retired["reason"]})
        raise HTTPException(status_code=404, detail="A2A agent not found")
    return directory_a2a.public_agent(row)


@router.get("/api/v1/a2a/stats", tags=["a2a"])
def api_a2a_stats():
    with _conn() as c:
        return db.a2a_stats(c)


@router.get("/api/v1/mcp/search", tags=["mcp"])
def api_mcp_search(
    q: str | None = Query(default=None, max_length=800),
    chain: str | None = Query(default=None),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN,
                      description=SEARCH_SORT_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=MAX_SEARCH_OFFSET),
):
    """Search MCP servers.

    Unions the standalone MCP directory (PulseMCP / official registry import)
    with x402 services that also expose an mcp_url.
    """
    effective_sort = db.effective_search_sort(q, sort)
    pull = limit + offset
    servers: list[dict] = []
    seen: set[str] = set()
    with _conn() as c:
        c.execute("BEGIN")
        mcp_keys, service_keys = db.mcp_search_keys(c, q=q, chain=chain, health=health)
        total_matched = len(mcp_keys | service_keys)
        for r in db.search_mcp(c, q=q, health=health, sort=effective_sort, limit=pull):
            item = directory_resources.normalize_mcp_server(
                r, include_sort_meta=effective_sort != "default")
            key = (item.get("endpoint_url") or item.get("slug") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            servers.append(item)
        service_target = min(pull, len(service_keys - mcp_keys))
        if effective_sort == "default" and len(servers) >= pull:
            service_target = 0
        service_count = service_offset = 0
        chunk = max(pull, 50)
        while service_count < service_target:
            rows = db.search(c, q=q, chain=chain, health=health,
                             has_mcp=True, sort=effective_sort,
                             limit=chunk, offset=service_offset)
            if not rows:
                break
            service_offset += len(rows)
            for row in rows:
                key = ((row.get("mcp_url") or "").strip()
                       or row.get("url") or row.get("slug") or "").lower()
                if key in seen or key in mcp_keys or key not in service_keys:
                    continue
                seen.add(key)
                servers.append(directory_resources.normalize_service(
                    row, as_mcp=True, include_sort_meta=effective_sort != "default"))
                service_count += 1
                if service_count >= service_target:
                    break
            if len(rows) < chunk:
                break
    if effective_sort != "default":
        directory_resources.sort_resources(servers, effective_sort, q)
    window = servers[offset:offset + limit]
    if not offset:
        search_terms.record("mcp", q, len(window), "api")
    directory_resources.strip_sort_metadata(window)
    with _conn() as c:
        exact_name_groups = directory_resources.exact_name_groups(
            c, q, (item.get("name") for item in window))
    return {
        "query": q,
        "sort": sort,
        "count": len(window),
        "total_matched": total_matched,
        "servers": window,
        "exact_name_matches": next(
            (group for group in exact_name_groups if group["query_exact"]), None),
        "exact_name_groups": exact_name_groups,
    }


@router.get("/api/v1/mcp/stats", tags=["mcp"])
def api_mcp_stats():
    with _conn() as c:
        return db.mcp_stats(c)


@router.get("/api/v1/mcp/servers/{slug}", tags=["mcp"])
def api_mcp_server(slug: str):
    suppressed = _privacy_suppressed_response(
        "mcp", slug, "MCP server not found")
    if suppressed is not None:
        return suppressed
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
        with _conn() as c:
            retired = db.get_retired_listing(c, "mcp", slug)
        if retired is not None:
            raise HTTPException(status_code=410, detail={
                "status": "retired", "slug": slug, "reason": retired["reason"]})
        raise HTTPException(status_code=404, detail="MCP server not found")
    return cards.build_service_card(row)


@router.get("/api/v1/resources/search", tags=["directory"])
def api_resources_search(
    q: str | None = Query(default=None, max_length=800),
    protocol: str | None = Query(default=None, pattern="^(x402|mcp|a2a)$"),
    chain: str | None = Query(default=None),
    health: str | None = Query(default=None, description=HEALTH_DOC),
    sort: str = Query(default="default", pattern=SEARCH_SORT_PATTERN,
                      description=SEARCH_SORT_DOC),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=MAX_SEARCH_OFFSET),
):
    """Unified search across x402 services, MCP servers and A2A agents."""
    with _conn() as c:
        out = directory_resources.unified_search(
            c, q=q, protocol=protocol, chain=chain, health=health,
            sort=sort, limit=limit, offset=offset,
        )
    if not offset:
        search_terms.record(protocol or "all", q, out.get("count") or 0, "api")
    return out


@router.get("/api/v1/export", tags=["directory"])
def api_export(
    kind: str = Query(default="mcp", pattern="^(x402|mcp|a2a)$"),
    after_id: int = Query(default=0, ge=0,
                          description="Return rows with id greater than this."),
    limit: int = Query(default=500, ge=1, le=1000),
):
    """Keyset-paginated bulk export of the public catalogue.

    Ranked search caps `offset`; use this to walk the whole catalogue cheaply.
    Pass the returned `next_after_id` back as `after_id` until `count` is 0.
    """
    with _conn() as c:
        rows = db.export_listings(c, kind, after_id=after_id, limit=limit)
    items: list[dict] = []
    for row in rows:
        sources = row.get("sources")
        if kind == "a2a":
            item = directory_a2a.public_agent(row)
        elif kind == "mcp":
            item = directory_resources.normalize_mcp_server(row)
        else:
            item = cards.public_service_row(row)
            item.pop("source", None)
            item.pop("source_id", None)
            if item.get("slug"):
                item["service_card_url"] = f"/api/v1/services/{item['slug']}"
        item["id"] = row["id"]
        if sources is not None:
            item["sources"] = sources
        items.append(item)
    return {
        "kind": kind,
        "count": len(items),
        "next_after_id": rows[-1]["id"] if rows else None,
        "items": items,
    }


@router.get("/api/v1/broker/recommend", tags=["directory"])
def api_broker_recommend(
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
def api_stats():
    """x402 totals and chains (legacy fields) plus `snapshot`, the daily digest
    across x402, MCP and A2A. The full snapshot is at /stats.json."""
    from . import stats as stats_mod
    with _conn() as c:
        out = db.stats(c)
    snap, _h = _stats_payload()
    out["snapshot"] = stats_mod.summary(snap)
    return out


@router.get("/api/v1/methodology", tags=["directory"], summary="How every score and statistic is computed")
def api_methodology():
    """Quality score components and grade bands, readiness criteria and weights,
    payment-evidence grades, probing schedule and statistics definitions."""
    from . import methodology
    return JSONResponse(methodology.build(), headers={"Cache-Control": "public, max-age=3600",
                                                      "Access-Control-Allow-Origin": "*"})


@router.get("/api/v1/stats/summary", tags=["directory"])
def api_stats_summary(request: Request,
                      view: str = Query(default="all", pattern="^(all|mcp|x402|a2a)$",
                                        description="all, mcp, x402 or a2a"),
                      full: bool = Query(default=False, description=FULL_DOC)):
    """Daily digest: counts, findings and key facts for one protocol or all."""
    from . import stats as stats_mod
    is_full = _full_access(request, full)
    snap, _h = _stats_payload()
    out = stats_mod.summary(snap, view, full=is_full)
    return JSONResponse(out, headers=_PRIVATE_JSON if is_full else {"Access-Control-Allow-Origin": "*"})


class SubmissionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = None
    chains: list[str] | None = None
    price_usdc: float | None = Field(default=None, ge=0)
    contact: str = Field(min_length=3, max_length=200,
                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _unsafe_url_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "unsafe_url", "message": message},
    )


def _privacy_submission_error(kind: str) -> HTTPException:
    detail = {"x402": "service not found", "mcp": "mcp server not found",
              "a2a": "a2a agent not found"}[kind]
    return HTTPException(
        status_code=404,
        detail=detail,
        headers={
            "X-Agent-Tools-Privacy-Suppressed": "1",
            "Cache-Control": "no-store",
        },
    )


def _retired_submission_error() -> HTTPException:
    return HTTPException(
        status_code=410,
        detail="This endpoint was retired by its operator.",
    )


def _assert_submission_allowed(kind: str, row: dict) -> None:
    try:
        with _conn() as conn:
            db.assert_listing_not_retired(conn, kind, row)
    except db.PrivacySuppressedListingError as exc:
        raise _privacy_submission_error(kind) from exc
    except db.RetiredListingError as exc:
        raise _retired_submission_error() from exc


def _existing_submission_response(existing, pending):
    if existing:
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
        return {
            "status": "already_pending",
            "message": (
                "A submission for this URL is already submitted and auto-verifying."),
            "submission_id": pending.get("id"),
        }
    return None


async def _require_public_url(url: str) -> None:
    try:
        await run_in_threadpool(directory_public_http.validate_url, url)
    except directory_public_http.UnsafeURL as exc:
        raise _unsafe_url_error(str(exc)) from exc


@router.post(
    "/api/v1/submit",
    tags=["directory"],
    summary="Submit a service for listing",
    description=(
        "Submit a new x402 service. This endpoint only *creates* listings; "
        "re-submitting a URL that is already listed returns `already_listed` "
        "and changes nothing.\n\n"
        "Submit each service once, by its main endpoint: the other paid "
        "endpoints it advertises in `/.well-known/x402` are read and listed "
        "under the same entry. Submissions are rate limited per client; "
        "rotating IP addresses, accounts or contacts to exceed the limit, or "
        "splitting one origin into many submissions, may lead to the entries "
        "being merged and further submissions for that origin being refused "
        "(https://agent-tools.cloud/terms, section 5).\n\n"
        "To correct an existing listing, verify control of its domain and "
        "edit it: in a browser at <https://agent-tools.cloud/account>, or from "
        "code with `POST /api/v1/keys` -> `POST /api/v1/claims` -> "
        "`PATCH /api/v1/listings/{kind}/{slug}` (https://agent-tools.cloud/docs/claim)."
    ),
)
async def api_submit(request: Request, payload: SubmissionPayload):
    url = str(payload.url).strip()
    submission = payload.model_dump(mode="json")
    submission["url"] = url
    submission["_source"] = "rest-submit"
    _assert_submission_allowed("x402", submission)
    await _require_public_url(url)
    if directory_crawlers.url_retired(url):
        raise HTTPException(
            status_code=410,
            detail="This endpoint was retired by its operator.",
        )
    client_ip = limits.client_ip_from_request(request)
    with _conn() as c:
        existing = db.find_service_by_url(c, url)
        pending = db.find_pending_submission(c, url)
        recent = db.count_recent_submissions(c, client_ip)
    duplicate = _existing_submission_response(existing, pending)
    if duplicate:
        return duplicate
    if recent >= SUBMIT_RATE_LIMIT_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": "Too many pending submissions from this IP in the last 24h.",
                "limit": SUBMIT_RATE_LIMIT_PER_DAY,
            },
        )
    state = await run_in_threadpool(
        limits.check_ip_limits,
        client_ip,
        "submit",
        (("day", SUBMIT_RATE_LIMIT_PER_DAY, 86400),),
    )
    if state:
        limits.raise_rate_limited(state, "Too many service submissions from this IP.")
    submission["_client_ip"] = client_ip

    def persist(conn):
        db.assert_listing_not_retired(conn, "x402", submission)
        duplicate = _existing_submission_response(
            db.find_service_by_url(conn, url), db.find_pending_submission(conn, url))
        if duplicate:
            return duplicate
        if db.count_recent_submissions(conn, client_ip) >= SUBMIT_RATE_LIMIT_PER_DAY:
            raise HTTPException(status_code=429, detail={
                "error": "rate_limited",
                "message": "Too many pending submissions from this IP in the last 24h.",
                "limit": SUBMIT_RATE_LIMIT_PER_DAY,
            })
        return db.create_submission(conn, submission)

    try:
        stored = await run_in_threadpool(_submission_write, persist)
    except db.PrivacySuppressedListingError as exc:
        raise _privacy_submission_error("x402") from exc
    except db.RetiredListingError as exc:
        raise _retired_submission_error() from exc
    if isinstance(stored, dict):
        return stored
    sub_id = stored
    # Auto-review immediately — there is no human gate. x402 verification
    # decides: verified -> listed now, rejected -> dropped, uncertain ->
    # stays pending and is retried automatically by the crawl timer.
    try:
        review = await run_in_threadpool(
            directory_jobs.review_submission, sub_id, "auto-review (on-submit)", True)
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
    if rstatus == "owner_locked":
        raise HTTPException(status_code=409, detail={
            "error": "owner_verified",
            "message": "A verified owner controls this endpoint.",
            "submission_id": sub_id,
        })
    if rstatus == "rate_limited":
        retry = int(review.get("retry_after_seconds") or 86400)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": ("Too many submissions for this payTo address in "
                            "the last 24h. Submit each service once; its other "
                            "paid endpoints are read from /.well-known/x402."),
                "window": "day",
                "submission_id": sub_id,
                "retry_after_seconds": retry,
            },
            headers={"Retry-After": str(retry)},
        )
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


def _submission_write(operation, *, busy_error="submission_busy"):
    deadline = time.monotonic() + 2.5
    last_error = None
    while time.monotonic() < deadline:
        timeout = min(0.25, max(0.0, deadline - time.monotonic()) / 3)
        try:
            with db.writer(immediate=True, timeout=timeout) as conn:
                return operation(conn)
        except sqlite3.OperationalError as exc:
            if not db.is_locked_error(exc):
                raise
            last_error = exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise HTTPException(
        status_code=503,
        detail={"error": busy_error, "message": "Please retry shortly."},
        headers={"Retry-After": "2", "Cache-Control": "private, no-store"},
    ) from last_error


def _owner_locked(kind: str, row: dict) -> HTTPException:
    """Refuse a submission aimed at a listing whose owner proved control.

    Verification exists so an operator can correct their own entry; leaving the
    anonymous path able to overwrite it afterwards would hand that correction to
    whoever posts last.
    """
    slug = row.get("slug") or ""
    view = {"mcp": "/mcp/servers/", "a2a": "/a2a/agents/"}.get(kind, "/services/")
    return HTTPException(
        status_code=409,
        detail={
            "error": "owner_verified",
            "message": (
                "This listing has a verified owner, so an unauthenticated "
                "submission can no longer change it. To edit it, prove control "
                "of the domain: in a browser at claim_url, or from code by "
                "following the steps under api. Listings without a verified "
                "owner are unaffected, and any URL that is not listed yet can "
                "still be submitted here."),
            "slug": slug,
            "view_url": view + slug,
            "edit_url": f"/listings/{kind}/{slug}/edit",
            "claim_url": "https://agent-tools.cloud/account",
            "claim_docs": "https://agent-tools.cloud/docs/claim",
            "api": directory_ownership.API_FLOW,
        },
    )


# --------------------------------------------------------- agent self-service
# Signing in with a browser is not something an agent can do, and the guard on
# claimed listings would otherwise lock an operator out of their own entry. A
# key is a second way to present an identity, never a second set of powers:
# everything below still ends at domain_ownership.

KEY_MINT_LIMIT_PER_DAY = limits.env_int("AGENT_TOOLS_KEY_MINT_PER_DAY", 5)
CLAIM_LIMIT_PER_DAY = limits.env_int("AGENT_TOOLS_CLAIM_PER_DAY", 20)

_PUBLISH_HINT = {
    "wellknown_file": "Serve the token as the whole body of "
                      "https://{host}/.well-known/agent-tools-verify.txt",
    "descriptor": "Add an agentToolsVerify field holding the token to the JSON "
                  "at https://{host}/.well-known/x402 or your agent card",
    "dns_txt": "Publish a TXT record at _agent-tools.{host} with the value "
               "atc-verify=<token>",
}


class KeyRequest(BaseModel):
    name: str | None = Field(default=None, max_length=60)


class ClaimRequest(BaseModel):
    host: str = Field(min_length=3, max_length=253)
    method: str = Field(default="wellknown_file")
    contact: str | None = Field(
        default=None, max_length=200,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ClaimVerifyRequest(BaseModel):
    token: str = Field(min_length=8, max_length=200)
    gateway_api_key: SecretStr | None = Field(
        default=None, min_length=8, max_length=200,
        description=("Optional and normally unnecessary: Agent Tools uses its own key for "
                     "hosted MCP gateways (currently server.smithery.ai). If sent, used for "
                     "this single check only and never stored or logged."))


class ServiceClaimRequest(BaseModel):
    model_config = {"extra": "forbid", "json_schema_extra": {"examples": [
        {"url": "https://provider.example/team/service/mcp", "method": "mcp_resource"},
        {"url": "https://github.com/owner/repository"},
        {"url": "https://www.npmjs.com/package/@scope/package"},
    ]}}
    url: str = Field(min_length=8, max_length=2000)
    method: str | None = Field(default=None, max_length=40)


class ServiceClaimResponse(BaseModel):
    claim_id: int
    url: str
    host: str
    scope: str
    method: str
    token: str
    expires_at: int
    publish: str
    document: dict[str, str]
    next: dict


_SERVICE_CLAIM_AUTH = HTTPBearer(auto_error=False, scheme_name="AccountAPIKey",
                                description="An existing account API key. Signed-in account cookies also work.")
_SERVICE_CLAIM_ERRORS = {
    401: {"description": "An authenticated account is required."},
    403: {"description": "The claim belongs to another account or a cookie write is cross-origin."},
    404: {"description": "No matching URL claim exists."},
    422: {"description": "The target or method is invalid, the claim expired, or public proof is missing."},
    429: {"description": "Claim creation or verification rate limit reached."},
    503: {"description": "The database is busy; retry after the response's Retry-After interval."},
}


def _limit_service_claim(request: Request, user, *, verify: bool = False) -> None:
    windows = (("minute", 2, 60), ("day", 20, 86400)) if verify else (("day", CLAIM_LIMIT_PER_DAY, 86400),)
    scope = "verify-url-claim" if verify else "open-url-claim"
    for identity in ("owner:%s" % user["id"], limits.client_ip_from_request(request)):
        state = limits.check_ip_limits(identity, scope, windows)
        if state:
            limits.raise_rate_limited(state, "Too many service claim requests. Try again later.")


def _require_api_user(request: Request):
    """The account behind the request, or a 401 that says how to get one."""
    user = directory_auth.current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "no_credentials",
                "message": ("Present an API key as Authorization: Bearer <key>. "
                            "A key is free and can run account-scoped actions such as "
                            "sponsored paid verification; listing edits still require "
                            "a verified domain or exact service URL."),
                "get_a_key": {"method": "POST", "url": "/api/v1/keys"},
            })
    return user


@router.post("/api/v1/keys", tags=["ownership"], status_code=201)
async def api_mint_key(request: Request, payload: KeyRequest | None = None):
    """Mint an API key. No existing credentials are needed.

    The key can authenticate account-scoped actions such as sponsored paid
    verification, read its own account, and open a claim. Every listing edit
    still requires a verified claim on the host that serves the listing.
    """
    ip = limits.client_ip_from_request(request)
    state = limits.check_ip_limits(
        ip, "mint-key", (("day", KEY_MINT_LIMIT_PER_DAY, 86400),))
    if state:
        limits.raise_rate_limited(state, "Too many keys minted from this IP today.")

    name = (payload.name if payload else None) or None
    with db.writer() as conn:
        puid = secrets.token_hex(16)
        uid = db.upsert_user(conn, provider="domain", provider_uid=puid,
                             login=db.key_user_login(puid))
        raw = db.create_api_key(conn, uid, name=name)
    return {
        "api_key": raw,
        "prefix": raw[:15],
        "message": ("Store this now -- it is not shown again. It can authenticate "
                "account-scoped actions such as sponsored paid verification; "
                "listing edits require a verified domain."),
        "next": {
            "method": "POST", "url": "/api/v1/claims",
            "auth": "Authorization: Bearer <api_key>",
            "body": {"host": "example.com", "method": "wellknown_file"},
        },
    }


@router.post("/api/v1/claims", tags=["ownership"], status_code=201)
async def api_open_claim(request: Request, payload: ClaimRequest):
    """Open a claim on a host. Returns the token to publish there."""
    user = _require_api_user(request)
    ip = limits.client_ip_from_request(request)
    state = limits.check_ip_limits(
        ip, "open-claim", (("day", CLAIM_LIMIT_PER_DAY, 86400),))
    if state:
        limits.raise_rate_limited(state, "Too many claims from this IP today.")

    if payload.method not in directory_ownership.METHODS:
        raise HTTPException(422, {
            "error": "unknown_method",
            "message": "method must be one of %s"
                       % (list(directory_ownership.METHODS),),
        })
    host = payload.host.strip().lower()
    try:
        with db.writer() as conn:
            claim_id, token = directory_ownership.issue(
                conn, int(user["id"]), host, payload.method)
            if payload.contact:
                # Unverified on purpose: a courtesy address for the one notice
                # we send if the claim is later displaced, not a credential.
                conn.execute(
                    "UPDATE users SET email=?, email_verified=0 WHERE id=?",
                    (payload.contact.strip(), int(user["id"])))
    except directory_ownership.ClaimError as exc:
        raise HTTPException(422, {"error": "claim_refused", "message": str(exc)})

    return {
        "claim_id": claim_id,
        "host": host,
        "method": payload.method,
        "token": token,
        "publish": _PUBLISH_HINT[payload.method].format(host=host),
        "next": {
            "method": "POST", "url": "/api/v1/claims/%d/verify" % claim_id,
            "auth": "Authorization: Bearer <api_key>",
            "body": {"token": token},
        },
    }


@router.get("/api/v1/claim-methods", tags=["ownership"])
def api_claim_methods():
    return {"url_methods": directory_ownership.URL_METHODS,
            "claim_url": "/api/v1/listing-claims"}


@router.post("/api/v1/listing-claims", tags=["ownership"], status_code=201,
             response_model=ServiceClaimResponse, responses=_SERVICE_CLAIM_ERRORS,
             dependencies=[Depends(_SERVICE_CLAIM_AUTH)])
async def api_open_service_claim(request: Request, payload: ServiceClaimRequest):
    """Request proof for one exact public HTTPS URL, including its query.

    Supply the complete service URL. It grants no rights until fresh proof
    is published and verified. The token
    expires after 24 hours and never authorizes other URLs on the same host.
    """
    user = _require_api_user(request)
    directory_auth._require_same_origin_write(request)
    _limit_service_claim(request, user)
    try:
        target = directory_ownership.resolve_service_url(payload.url)
        method = payload.method or directory_ownership.default_url_method(target)

        def persist(conn):
            return directory_ownership.issue_url(conn, int(user["id"]), target, method)

        claim_id, token = _submission_write(persist, busy_error="claim_busy")
    except directory_ownership.ClaimError as exc:
        raise HTTPException(422, {"error": "claim_refused", "message": str(exc)}) from None
    return JSONResponse({
        "claim_id": claim_id, "url": target,
        "host": directory_ownership.claim_url_scope(target)[0], "scope": "url",
        "method": method, "token": token,
        "expires_at": int(time.time()) + directory_ownership.URL_PENDING_SECONDS,
        "publish": directory_ownership.url_publish_hint(target, method),
        "document": {directory_ownership.TOKEN_FIELD: token},
        "next": {"method": "POST", "url": "/api/v1/listing-claims/%d/verify" % claim_id,
                 "auth": "Authorization: Bearer <api_key>", "body": {"token": token}},
    }, status_code=201, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listing-claims/{claim_id}/verify", tags=["ownership"],
             responses=_SERVICE_CLAIM_ERRORS, dependencies=[Depends(_SERVICE_CLAIM_AUTH)])
@router.post("/api/v1/claims/{claim_id}/verify", tags=["ownership"])
async def api_verify_claim(request: Request, claim_id: int,
                           payload: ClaimVerifyRequest):
    """Check that the token is live on the host, and hand over the listings."""
    def verify():
        user = _require_api_user(request)
        token = payload.token.strip()
        conn = db.connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT user_id,host,scope_path,method FROM domain_ownership WHERE id=?",
                (claim_id,)).fetchone()
            if row is None:
                raise HTTPException(404, {"error": "no_such_claim"})
            if int(row["user_id"]) != int(user["id"]):
                raise HTTPException(403, {
                    "error": "not_your_claim",
                    "message": "Open your own claim with POST /api/v1/claims.",
                })
            if request.url.path.startswith("/api/v1/listing-claims/") and row["scope_path"] is None:
                raise HTTPException(404, {"error": "no_such_url_claim"})
            if row["scope_path"] is not None:
                directory_auth._require_same_origin_write(request)
                _limit_service_claim(request, user, verify=True)
            if payload.gateway_api_key is not None:
                prepared = directory_ownership.prepare_claim_verification(
                    conn, claim_id, token,
                    gateway_api_key=payload.gateway_api_key.get_secret_value().strip())
            else:
                prepared = directory_ownership.prepare_claim_verification(
                    conn, claim_id, token)
        finally:
            conn.close()

        if prepared.claim is None:
            ok, detail, notification = False, prepared.detail, None
        else:
            def persist(conn):
                ok, detail, notification = directory_ownership.apply_claim_verification(
                    conn, claim_id, token, verification=prepared)
                if ok:
                    db.sync_owner_verified(conn)
                return ok, detail, notification

            ok, detail, notification = _submission_write(persist, busy_error="claim_busy")
        if not ok:
            publish = (directory_ownership.url_publish_hint(
                "https://%s%s" % (row["host"], row["scope_path"]), row["method"])
                if row["scope_path"] is not None else
                _PUBLISH_HINT.get(row["method"], "").format(host=row["host"]))
            raise HTTPException(422, {
                "error": "not_verified",
                "message": detail,
                "publish": publish,
            })

        directory_ownership.notify_claim_displaced(notification)

        conn = db.connect(read_only=True)
        try:
            mine = directory_ownership.listings_for_claim(conn, row)
        finally:
            conn.close()
        result = {
            "status": "verified",
            "host": row["host"],
            "listings": mine,
            "next": {
                "method": "PATCH",
                "url": "/api/v1/listings/{kind}/{slug}",
                "auth": "Authorization: Bearer <api_key>",
            },
        }
        if row["scope_path"] is not None:
            result.update({"scope": "url", "url": "https://%s%s" % (row["host"], row["scope_path"])})
        return result

    return await run_in_threadpool(verify)


def _api_editable(request: Request, kind: str, slug: str):
    """auth._load_editable, with its refusals restated as JSON.

    The authorisation is not restated -- it is the same function the web form
    uses, so the two doors cannot drift apart.
    """
    try:
        user, row, host, owner = directory_auth._load_editable(
            request, kind, slug)
    except HTTPException as exc:
        if exc.status_code == 403 and isinstance(exc.detail, str):
            raise HTTPException(403, {
                "error": "not_verified_owner",
                "message": exc.detail,
                "claim_it": {"method": "POST", "url": "/api/v1/listing-claims"},
            })
        if exc.status_code == 404 and isinstance(exc.detail, str):
            raise HTTPException(404, {"error": "not_found",
                                      "message": exc.detail},
                                headers=exc.headers)
        raise
    if user is None:
        _require_api_user(request)  # raises the 401 that explains how to get a key
    return user, row, host, owner


@router.get("/api/v1/listings/{kind}/{slug}", tags=["ownership"])
async def api_get_listing(request: Request, kind: str, slug: str):
    """Read a listing you own, in the shape PATCH expects back."""
    user, row, host, _owner = _api_editable(request, kind, slug)
    fields = directory_auth.editable_values(kind, row, user_id=user["id"])
    return JSONResponse({
        "kind": kind,
        "slug": slug,
        "host": host,
        "fields": fields,
        "declaration": directory_auth.owner_declarations.public_state(row)
                   if kind == "x402" and "resources" in fields else None,
        "endpoint_fields": list(db._ENDPOINT_FIELDS.get(kind, ())),
        "note": ("Changing an endpoint clears the measured columns: a listing "
                 "cannot carry a reputation to a different address. The new "
                 "endpoint must be an exact URL or whole host you have already verified."),
    }, headers={"Cache-Control": "private, no-store"})


@router.patch("/api/v1/listings/{kind}/{slug}", tags=["ownership"])
async def api_patch_listing(request: Request, kind: str, slug: str,
                            payload: dict[str, object]):
    """Edit a listing you own. Send only the fields you want to change.

    PATCH rather than PUT because the crawler keeps writing the columns you do
    not own; a full replacement would silently revert them.
    """
    user, row, host, owner = _api_editable(request, kind, slug)
    allowed = db._EDITABLE_FIELDS[kind]
    if kind == "x402":
        allowed = tuple(directory_auth.editable_values(kind, row, user_id=user["id"]))
    unknown = sorted(k for k in payload if k not in allowed)
    if unknown:
        raise HTTPException(422, {
            "error": "unknown_fields",
            "message": "These are not editable: %s" % ", ".join(unknown),
            "editable": list(allowed),
        })
    if not payload:
        raise HTTPException(422, {
            "error": "empty_patch",
            "message": "Send at least one field.",
            "editable": list(allowed),
        })

    def op():
        with db.writer(immediate=True) as conn:
            return db.apply_listing_edits(
                conn, kind, row["id"], user["id"], owner["id"], payload,
                via="api")

    try:
        applied, rejected = db.with_retry(op)
    except PermissionError:
        raise HTTPException(403, "Verified ownership or account status changed") from None
    except ValueError as exc:
        raise HTTPException(422, {"error": "invalid_edit", "message": str(exc)}) from None
    table = db._KIND_TABLE[kind]
    with db.connect(read_only=True) as conn:
        fresh = conn.execute("SELECT * FROM %s WHERE id=?" % table,
                             (row["id"],)).fetchone()
    fields = directory_auth.editable_values(kind, fresh, user_id=user["id"])
    body = {
        "kind": kind, "slug": slug, "host": host,
        "applied": applied, "rejected": rejected,
        "fields": fields,
        "declaration": directory_auth.owner_declarations.public_state(fresh)
                   if kind == "x402" and "resources" in fields else None,
    }
    if rejected and not applied:
        raise HTTPException(422, dict(body, error="nothing_applied"))
    return JSONResponse(body, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listings/{kind}/{slug}/verify", tags=["ownership"])
async def api_verify_listing(request: Request, kind: str, slug: str):
    user, row, _host, owner = _api_editable(request, kind, slug)
    result = await directory_auth.verify_owner_listing(request, kind, row, user, owner)
    return JSONResponse(result, status_code=422 if result["status"] == "blocked" else 200,
                        headers={"Cache-Control": "private, no-store"})


class LifecyclePayload(BaseModel):
    model_config = {"extra": "forbid"}
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm: bool = Field(strict=True)
    source_slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,180}$")


class MergePayload(BaseModel):
    model_config = {"extra": "forbid"}
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_slug: str = Field(pattern=r"^[a-z0-9_-]{1,180}$")
    confirm: bool = Field(strict=True)


@router.get("/api/v1/listings/{kind}/{slug}/lifecycle", tags=["ownership"])
async def api_lifecycle_state(request: Request, kind: str, slug: str):
    user = _require_api_user(request)
    return JSONResponse(directory_auth.lifecycle_state(request, kind, slug, user),
                        headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listings/{kind}/{slug}/retire", tags=["ownership"])
async def api_retire_listing(request: Request, kind: str, slug: str, payload: LifecyclePayload):
    user = _require_api_user(request)
    result = await directory_auth.perform_lifecycle(request, user, kind, slug, "retire",
        payload.version, request.headers.get("idempotency-key"), payload.confirm,
        csrf_token=request.headers.get("x-csrf-token"))
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listings/{kind}/{slug}/restore", tags=["ownership"])
async def api_restore_listing(request: Request, kind: str, slug: str, payload: LifecyclePayload):
    user = _require_api_user(request)
    result = await directory_auth.perform_lifecycle(request, user, kind, slug, "restore",
        payload.version, request.headers.get("idempotency-key"), payload.confirm,
        csrf_token=request.headers.get("x-csrf-token"))
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listings/{kind}/{slug}/migration", tags=["ownership"], status_code=202)
async def api_migration_request(request: Request, kind: str, slug: str, payload: LifecyclePayload):
    user = _require_api_user(request)
    result = await directory_auth.perform_lifecycle(request, user, kind, slug, "migration",
        payload.version, request.headers.get("idempotency-key"), payload.confirm,
        source_slug=payload.source_slug, csrf_token=request.headers.get("x-csrf-token"))
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/listings/{kind}/{slug}/merge", tags=["ownership"])
async def api_merge_listing(request: Request, kind: str, slug: str, payload: MergePayload):
    user = _require_api_user(request)
    result = await directory_auth.perform_lifecycle(request, user, kind, slug, "merge",
        payload.version, request.headers.get("idempotency-key"), payload.confirm,
        csrf_token=request.headers.get("x-csrf-token"), target_slug=payload.target_slug,
        target_version=payload.target_version)
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/mcp/submit", tags=["mcp"])
async def api_submit_mcp(request: Request, payload: McpSubmissionPayload):
    """Index a single MCP server by its streamable-http endpoint URL.

    Once a listing's owner has verified control of the domain it serves, this
    endpoint returns 409 `owner_verified` instead of changing it, and the
    response carries the URL to edit it through. Listings with no verified
    owner are unaffected.
    """
    endpoint = str(payload.url).strip()
    _assert_submission_allowed("mcp", {
        "url": endpoint,
        "endpoint_url": endpoint,
        "source": "submission",
        "source_id": endpoint,
    })
    await _require_public_url(endpoint)
    if directory_crawlers.url_retired(endpoint):
        raise HTTPException(
            status_code=410,
            detail="This endpoint was retired by its operator.",
        )
    await run_in_threadpool(_enforce_submit_limit, request, "submit-mcp")
    with db.connect(read_only=True) as _c:
        _owned = db.find_mcp_by_endpoint(_c, endpoint)
    if _owned and _owned.get("owner_verified"):
        raise _owner_locked("mcp", _owned)
    name = (payload.name or "").strip() or directory_crawlers._host_slug(endpoint).replace("-", " ").title()
    slug = directory_a2a._slugify(name) or directory_crawlers._host_slug(endpoint)
    probe = await run_in_threadpool(directory_crawlers.probe_mcp_health, endpoint)
    if probe.get("unsafe_url"):
        raise _unsafe_url_error(probe["unsafe_url"])
    verdict = await run_in_threadpool(directory_crawlers.verify_x402, endpoint)
    row = {
        "slug": slug,
        "name": name,
        "description": (payload.description or "").strip() or None,
        "homepage_url": endpoint,
        "endpoint_url": endpoint,
        "transport": (payload.transport or "streamable-http").strip(),
        "x402_supported": 0,
        "source": "submission",
        "source_id": endpoint,
        "source_url": endpoint,
        "health": probe.get("status"),
        "health_checked": int(time.time()),
        "latency_ms": probe.get("latency_ms"),
        "http_status": probe.get("http_status"),
        "last_success_at": int(time.time()) if probe.get("status") == "ok" else None,
        "confidence": 0.5,
    }
    def persist(conn):
        db.assert_listing_not_retired(conn, "mcp", row)
        locked = db.find_owner_locked_listing(conn, "mcp", endpoint, slug)
        if locked:
            raise _owner_locked("mcp", locked)
        created, server_id = db.upsert_mcp_server(conn, row)
        stored = conn.execute(
            "SELECT endpoint_url FROM mcp_servers WHERE id=?", (server_id,)
        ).fetchone()
        if (stored is None or db._exact_endpoint(stored["endpoint_url"] or "")
                != db._exact_endpoint(endpoint)):
            raise HTTPException(status_code=409, detail={
                "error": "identity_conflict",
                "message": "This submission identity belongs to another endpoint.",
            })
        x402 = directory_reverify.verify_and_mirror(
            endpoint, slug=slug, name=name,
            description=(payload.description or "").strip() or None,
            homepage=endpoint, delivery="mcp", source="submission",
            source_id=endpoint, verdict=verdict, conn=conn,
            protect_owner_kind="mcp", protect_owner_slug=slug,
            protect_owner_url=endpoint,
        )
        if x402.get("owner_locked"):
            locked = x402.get("locked_listing")
            if locked:
                raise _owner_locked(x402.get("locked_kind") or "x402", locked)
            raise HTTPException(status_code=409, detail={"error": "owner_verified"})
        if x402.get("identity_conflict"):
            raise HTTPException(status_code=409, detail={
                "error": "identity_conflict",
                "message": "The verified paid resource conflicts with an existing identity.",
            })
        conn.execute(
            "UPDATE mcp_servers SET x402_supported=? WHERE id=?",
            (1 if x402.get("x402") else 0, server_id),
        )
        return created, x402

    try:
        created, x402 = await run_in_threadpool(_submission_write, persist)
    except db.PrivacySuppressedListingError as exc:
        raise _privacy_submission_error("mcp") from exc
    except db.RetiredListingError as exc:
        raise _retired_submission_error() from exc
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
    """Index an A2A agent by fetching its well-known Agent Card.

    Once a listing's owner has verified control of the domain it serves, this
    endpoint returns 409 `owner_verified` instead of changing it, and the
    response carries the URL to edit it through. Listings with no verified
    owner are unaffected.
    """
    url = str(payload.url).strip()
    _assert_submission_allowed("a2a", {
        "url": url,
        "endpoint_url": url,
        "card_url": url,
        "source": "submission",
        "source_id": url,
    })
    await _require_public_url(url)
    if directory_crawlers.url_retired(url):
        raise HTTPException(
            status_code=410,
            detail="This endpoint was retired by its operator.",
        )
    await run_in_threadpool(_enforce_submit_limit, request, "submit-a2a")
    try:
        card, card_url = await run_in_threadpool(
            directory_a2a.fetch_agent_card, url, None, True)
    except directory_public_http.UnsafeURL as exc:
        raise _unsafe_url_error(str(exc)) from exc
    if not card:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "no_agent_card",
                "message": ("No A2A Agent Card found at this URL. Make sure "
                            "/.well-known/agent-card.json is reachable."),
            },
        )
    row = directory_a2a.card_to_row(card, card_url, source="submission")
    if row.get("endpoint_url"):
        await _require_public_url(row["endpoint_url"])
    _assert_submission_allowed("a2a", row)
    with db.connect(read_only=True) as _c:
        _owned = db.find_a2a_by_card_url(_c, card_url)
    if _owned and _owned.get("owner_verified"):
        raise _owner_locked("a2a", _owned)
    verify_target = row.get("endpoint_url") or url
    verdict = await run_in_threadpool(
        directory_crawlers.verify_x402, verify_target
    )
    def persist(conn):
        db.assert_listing_not_retired(conn, "a2a", row)
        locked = db.find_owner_locked_listing(
            conn, "a2a", card_url, row["slug"], row.get("endpoint_url"))
        if locked:
            raise _owner_locked("a2a", locked)
        created, agent_id = db.upsert_a2a_agent(conn, row)
        stored = conn.execute(
            "SELECT endpoint_url,card_url FROM a2a_agents WHERE id=?",
            (agent_id,),
        ).fetchone()
        if (stored is None
                or db._exact_endpoint(stored["endpoint_url"] or "")
                != db._exact_endpoint(row.get("endpoint_url") or "")
                or db._exact_endpoint(stored["card_url"] or "")
                != db._exact_endpoint(card_url)):
            raise HTTPException(status_code=409, detail={
                "error": "identity_conflict",
                "message": "This Agent Card identity belongs to another endpoint.",
            })
        x402 = directory_reverify.verify_and_mirror(
            verify_target, slug=row["slug"], name=row.get("name"),
            description=row.get("description"),
            homepage=row.get("homepage_url"), delivery="a2a",
            source="submission", source_id=row.get("source_id"),
            verdict=verdict, conn=conn,
            protect_owner_kind="a2a", protect_owner_slug=row["slug"],
            protect_owner_url=card_url,
            protect_owner_alternate_url=row.get("endpoint_url"),
        )
        if x402.get("owner_locked"):
            locked = x402.get("locked_listing")
            if locked:
                raise _owner_locked(x402.get("locked_kind") or "x402", locked)
            raise HTTPException(status_code=409, detail={"error": "owner_verified"})
        if x402.get("identity_conflict"):
            raise HTTPException(status_code=409, detail={
                "error": "identity_conflict",
                "message": "The verified paid resource conflicts with an existing identity.",
            })
        conn.execute(
            "UPDATE a2a_agents SET x402_supported=? WHERE id=?",
            (1 if x402.get("x402") else 0, agent_id),
        )
        return created, x402

    try:
        created, x402 = await run_in_threadpool(_submission_write, persist)
    except db.PrivacySuppressedListingError as exc:
        raise _privacy_submission_error("a2a") from exc
    except db.RetiredListingError as exc:
        raise _retired_submission_error() from exc
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
def well_known():
    return {
        "name": "agent-tools.cloud",
        "type": "x402-service-directory",
        # Operators find their own listing through this file long before they
        # find the web page, so the claim path has to be discoverable here too.
        "ownership": {
            "docs": "https://agent-tools.cloud/docs/claim",
            "claim_url": "https://agent-tools.cloud/account",
            "methods": ["descriptor", "wellknown_file", "dns_txt"],
            "descriptor_field": "agentToolsVerify",
            "descriptor_paths": ["/.well-known/x402",
                                 "/.well-known/agent-card.json",
                                 "/.well-known/agent.json"],
            "wellknown_file": "/.well-known/agent-tools-verify.txt",
            "dns_txt_name": "_agent-tools.<host>",
            "dns_txt_value": "atc-verify=<token>",
            "scope": "host",
            "recheck_hours": 24,
            "api": {
                "mint_key": "https://agent-tools.cloud/api/v1/keys",
                "mint_key_mcp": "create_api_key tool at https://agent-tools.cloud/mcp-discovery/",
                "open_claim": "https://agent-tools.cloud/api/v1/claims",
                "verify_claim":
                    "https://agent-tools.cloud/api/v1/claims/{claim_id}/verify",
                "edit_listing":
                    "https://agent-tools.cloud/api/v1/listings/{kind}/{slug}",
                "auth": "Authorization: Bearer <api_key>",
            },
        },
        "version": "0.5",
        "description": (
            "Free, agent-readable directory of x402 paid APIs, MCP servers and A2A "
            "agents, probed daily, graded and safety-scanned; plus daily ecosystem "
            "statistics, scoring methodology and a Blog, all available as JSON and MCP "
            "tools. The paid relay hosted here until 2026-05-25 is retired; this host "
            "is discovery-only."
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
            "sponsored_paid_verification",
            "directory_stats",
            "methodology",
            "blog_articles",
        ],
        "endpoints": {
            "search": {
                "method": "GET",
                "url": "https://agent-tools.cloud/api/v1/search",
                "query": ["q", "category", "chain", "region", "health", "limit", "offset"],
                "returns": "ranked directory rows with service_card_url",
            },
            "export": {
                "method": "GET",
                "url": "https://agent-tools.cloud/api/v1/export",
                "query": ["kind", "after_id", "limit"],
                "returns": "keyset-paginated bulk catalogue rows with provenance",
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
            "directory_stats": {
                "summary": "https://agent-tools.cloud/api/v1/stats/summary?view={all|mcp|x402|a2a}",
                "snapshot": "https://agent-tools.cloud/stats.json",
                "daily_csv": "https://agent-tools.cloud/stats/daily.csv",
                "page": "https://agent-tools.cloud/stats",
                "usable": "https://agent-tools.cloud/stats/usable.json",
                "protocol_pages": "https://agent-tools.cloud/stats/{x402|mcp|a2a}",
                "topic": "https://agent-tools.cloud/stats/topics/{topic}.json",
                "monthly_reports": "https://agent-tools.cloud/stats/reports.json",
                "searches": "https://agent-tools.cloud/stats/searches.json",
                "full_access": "public responses shorten topic lists (3 per protocol) and search lists "
                               "(10); add full=1 with Authorization: Bearer <key> (free: POST /api/v1/keys) "
                               "for 10 per protocol, up to 100 terms and all history; MCP directory_stats "
                               "returns the full lists when the MCP request carries a key",
                "updated": "daily, after the 21:30 UTC probe run",
                "license": "CC BY 4.0",
                "returns": "indexed / reachable / protocol-verified / readiness counts, findings, "
                           "x402 prices and networks, protocol versions, access, safety, topics, growth, "
                           "probe outcomes, search demand (terms with >= 5 searches)",
            },
            "methodology": "https://agent-tools.cloud/api/v1/methodology",
            "data_use": {
                "statistics": "CC BY 4.0, link to https://agent-tools.cloud/stats",
                "listings": "free to use incl. commercially, credit agent-tools.cloud with a link; "
                            "third-party content stays with its owners",
                "blog": "copyright of the author; quote excerpts with a link, no full republication",
                "terms": "https://agent-tools.cloud/terms#data",
            },
            "listing_detail_fields": {
                "readiness": "9-criterion readiness score + grade on every detail endpoint; "
                             "feeds up to 15 of 100 quality points for x402 and A2A, "
                             "display only for MCP",
                "popularity": "GitHub stars/last push, npm 30-day downloads, Tranco site rank "
                              "(display only) on MCP and A2A detail endpoints when we have a signal",
                "peer": "readiness percentile and median, latency percentile vs reachable listings of "
                        "the same protocol, and the three criteria that cost the most points",
            },
            "blog": {
                "list": "https://agent-tools.cloud/api/v1/blog?q=&tag=&page=",
                "article": "https://agent-tools.cloud/api/v1/blog/{slug}",
                "markdown": "https://agent-tools.cloud/articles/{slug}.md",
                "rss": "https://agent-tools.cloud/articles/feed.xml",
            },
            "submit": "https://agent-tools.cloud/api/v1/submit",
            "mcp": {
                "transport": "streamable-http",
                "url": "https://agent-tools.cloud/mcp-discovery/",
                "tools": ["search", "ask_services", "get", "list_categories", "stats",
                          "directory_stats", "get_methodology", "list_articles", "get_article",
                          "register"],
            },
            "a2a": {
                "agent_card": "https://agent-tools.cloud/.well-known/agent-card.json",
                "jsonrpc": "https://agent-tools.cloud/a2a",
                "analyst_skills": ["ask_analyst", "research_report", "market_scan", "compare",
                                   "listing_audit"],
                "raw_reports": list(a2a_research.SKILL_IDS),
                "input": "plain text -> ask_analyst; data part {skill, question} for another skill",
                "docs": "https://agent-tools.cloud/about#a2a-agent",
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
            "paid_verification": {
                "docs": "https://agent-tools.cloud/about#paid-verification",
                "mcp": {"url": "https://agent-tools.cloud/mcp-discovery/",
                        "tools": ["prepare_paid_verification", "execute_paid_verification",
                                  "get_paid_verification"],
                        "auth": "Authorization: Bearer <api_key> header or api_key argument",
                        "signing": "the MCP server never signs; sign typed_data with your own wallet"},
                "custody": "none: the payer signs an EIP-3009 authorization to the service's own payTo",
                "limits": {"x402_version": 2, "scheme": "exact", "network": "eip155:8453",
                           "asset": "USDC", "max_usd": 1.0, "methods": ["GET", "POST"],
                           "post_body": "declared public JSON probe_body, max 16 KiB"},
                "auth": "Authorization: Bearer <api_key> (POST /api/v1/keys, no signup)",
                "prepare": {"method": "POST",
                            "url_template": "https://agent-tools.cloud/api/v1/services/{slug}/paid-verifications",
                            "body": {"payer": "0x<wallet>"},
                            "returns": "id, amount_usd, pay_to, valid_before, request_method, "
                                       "request_body_sha256, typed_data (EIP-712)"},
                "execute": {"method": "POST",
                            "url_template": "https://agent-tools.cloud/api/v1/paid-verifications/{id}/execute",
                            "body": {"signature": "0x<eip712 signature of typed_data>"},
                            "returns": "status, http_status, latency_ms, settled, tx_hash, score, checks"},
                "result": "https://agent-tools.cloud/api/v1/paid-verifications/{id}",
                "notes": "Request template and price re-checked before paying; relayed once, never retried. "
                         "Settled requires matching Base-USDC AuthorizationUsed and Transfer events; "
                         "evidence is public for 30 days. Signatures and response bodies are not stored.",
            },
            "x402_manifest": "https://agent-tools.cloud/.well-known/x402",
            "openapi": "https://agent-tools.cloud/openapi.json",
        },
        "agent_hint": (
            "Use POST /api/v1/ask for intent-level recommendations. Use "
            "GET /api/v1/search for faceted retrieval and then GET "
            "/api/v1/services/{slug} for the service card before paying or calling. "
            "To prove a paid x402 call delivers, run a sponsored paid verification "
            "(see endpoints.paid_verification)."
        ),
    }


_SITEMAP_BASE = "https://agent-tools.cloud"
# Detail pages that are reachable today and readiness A or B: the ones an agent
# could actually call. Dead listings and the rest stay out of the sitemap (they
# remain crawlable through the site) so thin pages do not dilute the site.
# One URL per endpoint: when several listings point at the same endpoint, only
# the best-scored one is offered.
_SITEMAP_KINDS = {
    "x402": ("services", "url", "/services/"),
    "mcp": ("mcp_servers", "endpoint_url", "/mcp/servers/"),
    "a2a": ("a2a_agents", "COALESCE(endpoint_url, card_url)", "/a2a/agents/"),
}
SITEMAP_CHUNK = 20000   # well under the 50,000-URL / 50 MB protocol limit


def _sitemap_listing_sql(kind: str) -> str:
    table, endpoint, _ = _SITEMAP_KINDS[kind]
    return (
        "SELECT slug, updated_at FROM ("
        f" SELECT id, slug, updated_at, ROW_NUMBER() OVER ("
        f"  PARTITION BY lower(rtrim(COALESCE({endpoint}, slug), '/'))"
        "   ORDER BY COALESCE(quality_score, 0) DESC, id) AS rn"
        f" FROM {table} WHERE health='ok' AND readiness_grade IN ('A','B')"
        ") WHERE rn=1 ORDER BY id")


def _sitemap_counts() -> dict:
    with _conn() as c:
        return {k: c.execute(f"SELECT COUNT(*) FROM ({_sitemap_listing_sql(k)})").fetchone()[0]
                for k in _SITEMAP_KINDS}


def _sitemap_iso(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def _sitemap_url(loc, lastmod=None, priority=None, changefreq=None):
    out = ["  <url>", f"    <loc>{xml_escape(loc)}</loc>"]
    if lastmod:
        out.append(f"    <lastmod>{lastmod}</lastmod>")
    if changefreq:
        out.append(f"    <changefreq>{changefreq}</changefreq>")
    if priority:
        out.append(f"    <priority>{priority}</priority>")
    out.append("  </url>")
    return "\n".join(out)


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request) -> Response:
    """Sitemap index: site pages, then one or more files per protocol."""
    names = ["pages"]
    for kind, n in _sitemap_counts().items():
        names += [f"{kind}-{i}" for i in range(1, max(1, -(-n // SITEMAP_CHUNK)) + 1)]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name in names:
        parts.append(f"  <sitemap>\n    <loc>{_SITEMAP_BASE}/sitemaps/{name}.xml</loc>\n"
                     f"    <lastmod>{today}</lastmod>\n  </sitemap>")
    parts.append("</sitemapindex>")
    return Response(content="\n".join(parts) + "\n", media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemaps/pages.xml", include_in_schema=False)
def sitemap_pages_xml(request: Request) -> Response:
    """Site pages: catalogue, statistics, topics, finished reports, Blog."""
    base = _SITEMAP_BASE
    static_pages = [
        ("/", "1.0", "daily"),
        ("/mcp", "0.9", "daily"),
        ("/a2a", "0.9", "daily"),
        ("/x402", "0.9", "daily"),
        ("/categories", "0.6", "weekly"),
        ("/stats", "0.8", "daily"),
        ("/stats/usable", "0.8", "daily"),
        ("/stats/x402", "0.7", "daily"),
        ("/stats/mcp", "0.7", "daily"),
        ("/stats/a2a", "0.7", "daily"),
        ("/stats/reports", "0.6", "daily"),
        ("/about", "0.4", "monthly"),
        ("/docs/claim", "0.5", "monthly"),
        ("/terms", "0.2", "yearly"),
        ("/privacy", "0.2", "yearly"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    _url, _iso = _sitemap_url, _sitemap_iso

    for path, prio, freq in static_pages:
        parts.append(_url(base + path, priority=prio, changefreq=freq))
    from . import stats as stats_mod
    for topic in stats_mod.TOPICS:
        parts.append(_url(f"{base}/stats/topics/{topic}", priority="0.5", changefreq="daily"))
    try:
        with _conn() as c:
            report_months = stats_mod.months(c)
    except sqlite3.Error:
        report_months = []
    current_month = time.strftime("%Y-%m", time.gmtime())
    for m in report_months:
        if m >= current_month:  # month to date is noindex until it ends
            continue
        parts.append(_url(f"{base}/stats/reports/{m}", priority="0.5", changefreq="daily"))

    article_store = getattr(request.app.state, "article_store", None)
    if article_store is not None:
        parts.append(_url(base + "/articles", priority="0.6", changefreq="weekly"))
        try:
            for article in article_store.sitemap_entries():
                parts.append(_url(base + "/articles/" + article["slug"], _iso(article["updated_at"]), "0.6"))
        except (ArticleError, sqlite3.Error):
            raise HTTPException(503, "文章索引暂不可用", headers={"Cache-Control": "no-cache"}) from None

    parts.append("</urlset>")
    xml = "\n".join(parts) + "\n"
    return Response(content=xml, media_type="application/xml",
                    headers={"Cache-Control": "no-cache, must-revalidate" if article_store is not None
                             else "public, max-age=3600"})


@router.get("/sitemaps/{name}.xml", include_in_schema=False)
def sitemap_listings_xml(name: str) -> Response:
    kind, _, page = name.rpartition("-")
    if kind not in _SITEMAP_KINDS or not page.isdigit() or int(page) < 1:
        raise HTTPException(404)
    prefix = _SITEMAP_KINDS[kind][2]
    with _conn() as c:
        rows = c.execute(_sitemap_listing_sql(kind) + " LIMIT ? OFFSET ?",
                         (SITEMAP_CHUNK, (int(page) - 1) * SITEMAP_CHUNK)).fetchall()
    if not rows and int(page) > 1:
        raise HTTPException(404)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for slug, ts in rows:
        parts.append(_sitemap_url(f"{_SITEMAP_BASE}{prefix}{slug}", _sitemap_iso(ts)))
    parts.append("</urlset>")
    return Response(content="\n".join(parts) + "\n", media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    """Welcome search engines and AI agents; point them at the sitemap."""
    # /auth/ only starts an OAuth redirect. Crawlers followed it 2,243 times in
    # one day and completed it zero times, which was most of our redirect churn.
    # Only the owner edit form and /account need sign-in; every other /listings/
    # path stays crawlable. "*" is RFC 9309 wildcard syntax.
    body = (
        "# agent-tools.cloud - open directory of MCP servers, A2A agents and\n"
        "# x402 services. Crawlers and AI agents are welcome to index and use\n"
        "# the public catalogue and JSON API.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /auth/\n"
        "Disallow: /listings/*/edit\n"
        "Disallow: /account\n"
        "\n"
        "Sitemap: https://agent-tools.cloud/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=3600"})
