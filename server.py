"""
mcpserver — backend for agent-tools.cloud.

Serves the open x402 service directory, the free MCP discovery server at
/mcp-discovery, and /.well-known descriptors.
The paid x402 stack (Qwen relay + verticals + relay.agent-tools.cloud host
split) was retired 2026-05-25 after 30 days with 0 settlements; see git
history for the previous implementation.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from directory import db as directory_db
from directory import log_redact

log_redact.install()
from directory.routes import TEMPLATES, router as directory_router
from directory.auth import router as auth_router
from directory.articles import ArticleStore
from directory.paid_verify_routes import router as paid_verify_router
from directory.claim_invite_routes import router as claim_invite_router
from directory.mcp_app import discover_mcp, wrap_with_client_capture
from metrics import PrometheusMiddleware, metrics_endpoint

load_dotenv()

# --- config ---------------------------------------------------------------

ALLOWED_MODELS = {
    m.strip()
    for m in os.getenv("ALLOWED_MODELS", "Qwen/Qwen3-8B").split(",")
    if m.strip()
}

ARTICLES_ENABLED = os.getenv("ARTICLES_ENABLED", "0") == "1"
ARTICLE_STORE = (ArticleStore(os.getenv("ARTICLE_DB_PATH", str(Path(directory_db.DEFAULT_DB_PATH).with_name("articles.db"))))
                 if ARTICLES_ENABLED else None)
TEMPLATES.env.globals["articles_enabled"] = ARTICLES_ENABLED

# --- FastAPI app ----------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the directory site DB exists. Safe to run repeatedly.
    try:
        directory_db.init_db()
        with contextlib.closing(directory_db.connect(read_only=True)) as conn:
            directory_db.require_privacy_finalized(conn)
    except Exception:
        import logging
        logging.getLogger("mcpserver").exception("directory db init failed")
        raise
    if ARTICLE_STORE is not None:
        ARTICLE_STORE.init()
    # FastMCP streamable-http needs its own session-manager lifespan.
    # Only the free directory-discovery MCP at /mcp-discovery is mounted now
    # (the paid /mcp mount was retired 2026-05-25).
    async with discover_mcp.session_manager.run():
        yield


app = FastAPI(
    title="mcpserver", version="0.2.0", lifespan=lifespan, openapi_url=None
)
app.state.article_store = ARTICLE_STORE


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    """Friendly HTML 404 for browser page requests; keep JSON for API clients.

    Programmatic clients (anything under /api/, .json endpoints, the MCP/A2A
    surfaces, or callers that don't accept HTML) still get a plain JSON 404.
    """
    from directory.routes import TEMPLATES
    path = request.url.path
    accepts_html = "text/html" in request.headers.get("accept", "")
    is_api = (
        path.startswith("/api/")
        or path.startswith("/admin/articles/api")
        or path.startswith("/admin/articles/media")
        or path.startswith("/account/articles/api")
        or path.startswith("/account/articles/media")
        or path.endswith(".md")
        or path.startswith("/mcp-discovery")
        or path.startswith("/.well-known/")
        or path.endswith(".json")
        or path in ("/a2a", "/metrics", "/healthz", "/health")
    )
    if accepts_html and not is_api:
        return TEMPLATES.TemplateResponse(
            request, "404.html", {"build_version": BUILD_VERSION}, status_code=404,
            headers=exc.headers)
    return JSONResponse({"detail": "Not Found"}, status_code=404,
                        headers=exc.headers)


# Free directory-discovery MCP (search/get/list_categories/stats) — ungated.
app.mount("/mcp-discovery", wrap_with_client_capture(discover_mcp.streamable_http_app()))


class _McpDiscoverySlashFix:
    """Let `POST /mcp-discovery` (no trailing slash) reach the mounted MCP app
    without a 307 redirect. Starlette's Mount would redirect the bare prefix to
    `/mcp-discovery/`, but MCP clients that POST `initialize` frequently do not
    re-send the body after a redirect, so external conformance probers (chiark,
    etc.) see a failed handshake. We normalise the path in-place instead."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp-discovery":
            scope = dict(scope)
            scope["path"] = "/mcp-discovery/"
            if scope.get("raw_path") in (b"/mcp-discovery", None):
                scope["raw_path"] = b"/mcp-discovery/"
        await self.app(scope, receive, send)


# Outermost: instrument every request (incl. 402 challenges) for Prometheus.
app.add_middleware(PrometheusMiddleware)
# Above Prometheus: normalise the bare /mcp-discovery path before routing.
app.add_middleware(_McpDiscoverySlashFix)


# Build version — bump to bust browser/CDN HTML caches on each deploy.
BUILD_VERSION = "2026-06-04.17"


# Detail pages are stable, numerous (10k+ MCP/A2A/x402 cards) and hammered by
# crawlers one-by-one — let the CDN cache them. Landing/list pages change with
# the live catalogue and are the real-visitor entry points, so keep them
# no-cache (full revalidate → counted by site-stats).
_CACHEABLE_DETAIL = ("/mcp/servers/", "/a2a/agents/", "/services/")


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith(("/admin/articles", "/account/articles")):
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    # Lightweight security headers on every response (HSTS is handled at the
    # Cloudflare edge). These are cheap, widely-recommended defaults.
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        path = request.url.path
        existing_policy = resp.headers.get("cache-control", "").lower()
        if ("atc_session" in request.headers.get("cookie", "")
                or "private" in existing_policy or "no-store" in existing_policy):
            # Belt and braces: nothing rendered for a signed-in visitor may
            # ever enter a shared cache, whatever the page would otherwise be.
            resp.headers["Cache-Control"] = "private, no-store"
        elif resp.status_code == 200 and path.startswith(_CACHEABLE_DETAIL):
            # stable card → CDN-cacheable; SWR keeps it fresh-ish cheaply
            resp.headers["Cache-Control"] = (
                "public, max-age=600, stale-while-revalidate=3600")
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        resp.headers["X-Build-Version"] = BUILD_VERSION
    return resp



# --- free endpoints -------------------------------------------------------


@app.get("/healthz")
@app.get("/health")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#2563eb'/>"
    "<text x='16' y='23' font-family='Arial,sans-serif' font-size='20' "
    "font-weight='700' fill='white' text-anchor='middle'>A</text></svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response as _Resp
    return _Resp(content=_FAVICON_SVG, media_type="image/svg+xml",
                 headers={"Cache-Control": "public, max-age=604800"})


_LLMS_TXT = """# agent-tools.cloud

> Discovery directory of x402 services, MCP servers and A2A agents.
> Public search and service cards are free. We do not operate listed services.

## Discovery

- Search all types: GET /api/v1/resources/search?q=...
- MCP: GET /api/v1/mcp/search and /api/v1/mcp/servers/{slug}
- A2A: GET /api/v1/a2a/search, /api/v1/a2a/agents/{slug}; reports: POST /a2a
- x402 card: GET /api/v1/services/{slug}
- Recommendations: GET /api/v1/ask?q=...
- Categories: /api/v1/categories, /api/v1/stats
- Daily ecosystem statistics (CC BY 4.0): GET /api/v1/stats/summary?view=all|mcp|x402|a2a,
  full snapshot /stats.json, history /stats/daily.csv, page /stats
- How many tools work, and why not: /stats/usable.json ; topics: /stats/topics/{topic}.json ;
  monthly: /stats/reports.json ; full lists: ?full=1 with a key
- Details add `readiness` and `peer` (percentile vs same-protocol peers);
  MCP/A2A add `popularity` (GitHub, npm, Tranco)
- How scores and statistics are computed: GET /api/v1/methodology

## Blog

- List: GET /api/v1/blog?q=&tag=&page= ; one article with Markdown body: GET /api/v1/blog/{slug}
- Plain Markdown: /articles/{slug}.md ; RSS: /articles/feed.xml

## Data use

- Statistics: CC BY 4.0, link to https://agent-tools.cloud/stats
- Listing data: free to use, including commercially; credit agent-tools.cloud with a link
- Blog articles: copyright of the author; quote excerpts with a link, do not republish in full
- Full terms: https://agent-tools.cloud/terms#data

Read each resource's payment requirements before calling it; headline price ranges are not per-call quotes.

## HTTP MCP

Endpoint: https://agent-tools.cloud/mcp-discovery/
Tools: search, get, search_mcp_servers, get_mcp_server, search_a2a_agents,
search_resources, ask_services, list_categories, stats, directory_stats, get_methodology,
list_articles, get_article, scan_mcp_safety, register,
create_api_key, prepare_paid_verification, execute_paid_verification, get_paid_verification,
claim_service_url, verify_service_url, get_owned_listing, edit_owned_listing.

## Claim and edit an exact service URL

Send Authorization: Bearer <API_KEY>. Obtain a key with POST /api/v1/keys or create_api_key.
claim_service_url(url, method=None) -> publish the returned token ->
verify_service_url(claim_id, token) -> get_owned_listing(kind, slug) ->
edit_owned_listing(kind, slug, fields).
These ownership tools use HTTP MCP; keys are headers, not tool arguments.
REST: POST /api/v1/listing-claims -> publish proof ->
POST /api/v1/listing-claims/{claim_id}/verify -> GET/PATCH /api/v1/listings/{kind}/{slug}.
Only the exact public HTTPS URL, including path and query, is covered.
Other URLs and whole-host rights are separate. Pending tokens expire after 24 hours.
Whole-host claims use /api/v1/claims; shared hosts cannot be claimed.
Steps and proof methods: https://agent-tools.cloud/docs/claim

## Paid verification

Requires an account key and a signed authorization from the buyer's wallet.
Base USDC, at most $1, paid directly to the service. We do not hold funds or sign.
Prepare at POST /api/v1/services/{slug}/paid-verifications; execute at
POST /api/v1/paid-verifications/{id}/execute; read at GET /api/v1/paid-verifications/{id}.
The request is rechecked and sent once, never retried.
MCP: prepare_paid_verification(slug, payer) -> execute_paid_verification(id, signature)
-> get_paid_verification(id).

## References

OpenAPI: /openapi.json
Manifests: /.well-known/mcp.json, /.well-known/agent-tools.json, /.well-known/agent-card.json
Safety verdicts are advisory; verify services before connecting.
Terms: /terms | Privacy: /privacy | Contact: contact@agent-tools.cloud
"""


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    from fastapi.responses import Response as _Resp
    return _Resp(content=_LLMS_TXT, media_type="text/plain; charset=utf-8",
                 headers={"Cache-Control": "public, max-age=3600"})


_HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-tools.cloud — x402 directory + MCP discovery</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="agent-tools.cloud is a free directory and discovery layer for x402 paid APIs, MCP servers and A2A agents, with a built-in safety scanner that checks any MCP server for malware and prompt-injection before you connect. The previously hosted paid Qwen relay and verticals were retired on 2026-05-25.">
<meta name="robots" content="noindex, nofollow">
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 720px; margin: 3rem auto; padding: 0 1.2rem; }
h1 { margin: 0 0 .2em; font-size: 1.5rem; }
h2 { margin-top: 1.8rem; font-size: 1.05rem; border-bottom: 1px solid #8884; padding-bottom: .25em; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }
pre { background: #8881; padding: .8em 1em; border-radius: 6px; overflow-x: auto; }
a { color: #2c7be5; }
.muted { color: #888; font-size: 13px; }
.notice { padding: .8em 1em; border-left: 3px solid #c4732d; background: #c4732d18; border-radius: 4px; }
</style>
</head>
<body>
<h1>agent-tools.cloud</h1>
<p class="notice">
  <b>Paid x402 relay retired on 2026-05-25.</b>
  The previously hosted Qwen3.6-35B-A3B chat ($0.001/call) and the four verticals
  (token signal, on-chain Q&amp;A, DeFi planner, multi-leg portfolio) are no longer served.
  All paid endpoints now return <code>404</code>. This host is directory + MCP discovery only.
</p>

<h2>What is still here (free)</h2>
<ul>
    <li><code>GET /api/v1/search?q=&amp;category=&amp;chain=</code> — directory search across 2300+ indexed x402 services</li>
    <li><code>POST /api/v1/ask</code> — LLM-ranked service recommendations grounded in directory candidates</li>
    <li><code>GET /api/v1/services/{slug}</code> — agent-readable service card with payment, call and quality metadata</li>
    <li><code>GET /api/v1/categories</code>, <code>/api/v1/stats</code></li>
    <li><code>POST /mcp-discovery/</code> — MCP streamable-http with tools <code>search</code> / <code>ask_services</code> / <code>get</code> / <code>list_categories</code> / <code>stats</code> / <code>register</code></li>
  <li><code>GET /.well-known/agent-tools.json</code> — agents.json manifest</li>
  <li><code>GET /.well-known/x402</code>, <code>/.well-known/mcp.json</code>, <code>/healthz</code></li>
</ul>

<h2>MCP client config</h2>
<pre>{
  "mcpServers": {
    "agent-tools": {
      "transport": "streamable-http",
      "url": "https://agent-tools.cloud/mcp-discovery/"
    }
  }
}</pre>

<h2>Source</h2>
<p>
  <a href="https://github.com/JoursBleu/mcpserver" target="_blank" rel="noopener">github.com/JoursBleu/mcpserver</a>
  &middot;
  <a href="https://smithery.ai/servers/kangletian/agent-tools-x402-directory" target="_blank" rel="noopener">smithery.ai listing</a>
</p>
<p class="muted">Last paid call: 2026-05-23. 30-day window showed 0 settled payments &mdash; retiring the paid stack to keep the directory layer clean.</p>
</body>
</html>
"""


@app.get("/.well-known/x402")
def well_known_x402(request: Request) -> dict[str, Any]:
    host = (request.headers.get("host") or "").split(":", 1)[0].lower() or "agent-tools.cloud"
    description = (
        "Free MCP discovery server for x402 paid services across the ecosystem. "
        "Search x402 endpoints, MCP servers and A2A agents, ask for recommendations, get service cards, "
        "read daily ecosystem statistics, "
        "and scan any MCP server for malware / prompt-injection before connecting. "
        "The paid relay previously hosted here has been retired; this host is "
        "now directory + discovery only."
    )
    descriptor: dict[str, Any] = {
        "name": host,
        "description": description,
        "version": "0.4",
        "endpoints": [
            {"path": "/mcp-discovery", "method": "POST", "kind": "mcp-streamable-http", "gated": False},
            {"path": "/api/v1/search", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/ask", "method": "POST", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/services/{slug}", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/categories", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/stats", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/stats/summary", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/methodology", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/blog", "method": "GET", "kind": "rest-json", "gated": False, "category": "content"},
            {"path": "/.well-known/agent-tools.json", "method": "GET", "kind": "agents.json", "gated": False},
            {"path": "/healthz", "method": "GET", "kind": "info", "gated": False},
        ],
        "models": sorted(ALLOWED_MODELS),
        "source": "https://github.com/JoursBleu/mcpserver",
    }
    # What every listed operator does to prove they control the host:
    # publish the token we issued inside the descriptor they already serve.
    token = os.getenv("AGENT_TOOLS_VERIFY_TOKEN")
    if token:
        descriptor["agentToolsVerify"] = token
    return descriptor


@app.get("/.well-known/mcp.json", tags=["discovery"])
def well_known_mcp(request: Request) -> dict[str, Any]:
    """Lightweight MCP discovery doc — points clients to stdio + streamable-http."""
    host = (request.headers.get("host") or "agent-tools.cloud").split(":", 1)[0].lower()
    scheme = "https" if request.url.scheme == "https" else "http"
    base = f"{scheme}://{host}"
    return {
        "name": "agent-tools",
        "description": (
            "Free MCP discovery server for the agentic economy. "
            "Searches a curated directory of x402 paid APIs, MCP servers and A2A agents, "
            "recommends payable/callable endpoints for a given intent, "
            "and scans MCP servers for malware / prompt-injection before you connect. "
            "Core search tools are also available as a stdio MCP via `uvx agent-tools-mcp`."
        ),
        "version": "0.2.0",
        "homepage": "https://agent-tools.cloud",
        "repository": "https://github.com/AgentTools-Cloud/AgentToolsCollection",
        "license": "Apache-2.0",
        "transports": {
            "stdio": {
                "command": "uvx",
                "args": ["agent-tools-mcp"],
                "package": "agent-tools-mcp",
                "registry": "pypi",
            },
            "streamable_http": {
                "url": f"{base}/mcp-discovery",
            },
        },
        "tools": [
            {"name": "search", "description": "Find x402 / MCP services matching an intent or filter set."},
            {"name": "ask_services", "description": "LLM-ranked recommendation of the best x402/MCP services for an intent."},
            {"name": "get", "description": "Get full details + ready-to-paste call template for a service by slug."},
            {"name": "list_categories", "description": "List all directory categories."},
            {"name": "stats", "description": "x402 size, healthy count and chains, plus the daily cross-protocol digest."},
            {"name": "directory_stats", "description": "Daily ecosystem statistics for all, MCP, x402 or A2A: counts, findings, probe outcomes, top searches; topic= or month= for a topic or monthly report. Lists are shortened without an account; send an API key (Authorization: Bearer) for complete lists."},
            {"name": "get_methodology", "description": "How quality grades, readiness scores and statistics are computed."},
            {"name": "list_articles", "description": "List published Blog articles (metadata)."},
            {"name": "get_article", "description": "Read one Blog article with its Markdown body."},
            {"name": "search_mcp_servers", "description": "Search the MCP server directory."},
            {"name": "get_mcp_server", "description": "Get the full card for one MCP server by slug."},
            {"name": "search_a2a_agents", "description": "Find A2A agents you can delegate a task to."},
            {"name": "search_resources", "description": "Unified search across x402 services, MCP servers and A2A agents."},
            {"name": "scan_mcp_safety", "description": "Check an MCP server (by endpoint URL) for malware / prompt-injection / tool-poisoning: returns our stored verdict if indexed, else probes + scans + indexes it."},
            {"name": "register", "description": "Submit an x402 / MCP service; it is verified automatically and listed when verification passes."},
            {"name": "create_api_key", "description": "Create a free account API key (no sign-up); returned once. Unlocks complete directory_stats lists, paid verification and claims."},
            {"name": "prepare_paid_verification", "description": "Quote a sponsored paid verification of an x402 service: returns EIP-712 typed data for your own wallet to sign (non-custodial, Base USDC, <= $1)."},
            {"name": "execute_paid_verification", "description": "Relay your signed authorization to the service exactly once and score the paid call."},
            {"name": "get_paid_verification", "description": "Read one of your own paid verifications."},
            {"name": "claim_service_url", "description": "Issue a fresh token for one exact public HTTPS URL. Requires an account API key in the MCP Authorization: Bearer header.", "transports": ["streamable_http"]},
            {"name": "verify_service_url", "description": "Verify your published ownership token; grants rights only for the exact URL and returns its listings.", "transports": ["streamable_http"]},
            {"name": "get_owned_listing", "description": "Read editable metadata for a listing covered by your verified claim.", "transports": ["streamable_http"]},
            {"name": "edit_owned_listing", "description": "Edit metadata for your own listing using the same authorization and audit trail as REST.", "transports": ["streamable_http"]},
        ],
    }


@app.get("/.well-known/agent-card.json", tags=["discovery"])
@app.get("/.well-known/agent.json", tags=["discovery"])
def well_known_agent_card(request: Request) -> dict[str, Any]:
    """A2A Agent Card for agent-tools.cloud itself."""
    from directory import a2a as directory_a2a

    from directory import card_signing

    host = (request.headers.get("host") or "agent-tools.cloud").split(":", 1)[0].lower()
    scheme = "https" if request.url.scheme == "https" else "http"
    base = f"{scheme}://{host}"
    return card_signing.sign_card(directory_a2a.own_agent_card(base), base)


@app.get("/.well-known/jwks.json", tags=["discovery"])
def well_known_jwks() -> JSONResponse:
    """Public key(s) that verify the Agent Card signatures (no private material)."""
    from directory import card_signing

    return JSONResponse(card_signing.public_jwks(),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.post("/a2a", tags=["a2a"])
async def a2a_jsonrpc(request: Request):
    """A2A JSON-RPC endpoint: 1.0 `SendMessage` and 0.3 `message/send` (A2A-Version header)."""
    from fastapi.concurrency import run_in_threadpool
    from directory import a2a as directory_a2a

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )
    # A2A service parameter: header, or request parameter as the spec allows.
    a2a_version = (request.headers.get("a2a-version")
                   or request.query_params.get("A2A-Version"))
    # handle_jsonrpc is synchronous and may do blocking work (a safety scan can
    # probe a remote endpoint + call the LLM), so run it off the event loop.
    client_ip = (request.headers.get("cf-connecting-ip")
                 or (request.client.host if request.client else None))
    result = await run_in_threadpool(directory_a2a.handle_jsonrpc, payload, a2a_version, client_ip)
    return JSONResponse(result)




# --- paid endpoints retired 2026-05-25 ------------------------------------
#
# All previously-gated paid routes (POST /v1/chat/completions, /v1/signal/*,
# /v1/onchain/*, /v1/defi/*, /v1/portfolio/*, paid MCP at /mcp) have been
# removed after 30 days with 0 successful x402 settlements. They now 404.
# History preserved in git; bring back individually with `git show` if
# we revisit the paid stack. Free MCP discovery at /mcp-discovery and the
# directory API under /api/v1/ are unaffected.


# --- OpenAPI customization for directory discovery -----------------------

from fastapi.openapi.utils import get_openapi  # noqa: E402


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _build_openapi() -> dict[str, Any]:
    cache_key = "full"
    if cache_key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[cache_key]

    title = "agent-tools.cloud — x402 service directory + MCP discovery"
    description = (
        "Directory of x402 services, MCP servers and A2A agents. "
        "Search public service cards or verify ownership to edit your listings."
    )
    guidance = (
        "Search with GET /api/v1/resources/search; read a service card before calling it. "
        "For recommendations use POST /api/v1/ask. "
        "Claim an exact HTTPS URL with POST /api/v1/listing-claims, publish its token, "
        "then POST /api/v1/listing-claims/{claim_id}/verify. Read or edit through "
        "GET/PATCH /api/v1/listings/{kind}/{slug}. "
        "HTTP MCP at /mcp-discovery/ provides claim_service_url, verify_service_url, "
        "get_owned_listing and edit_owned_listing. Use Authorization: Bearer <API_KEY>, "
        "not key arguments. Verification covers only the claimed path and query, not other services. "
        "Whole-host claims use /api/v1/claims and are unavailable on shared hosts. "
        "Paid verification requires the buyer's signed wallet authorization and is never retried. "
        "MCP provides prepare_paid_verification, execute_paid_verification and get_paid_verification. "
        "MCP-only clients can mint a key with the create_api_key tool. "
        "Proof methods: https://agent-tools.cloud/docs/claim."
    )

    schema = get_openapi(
        title=title,
        version="0.3.0",
        description=description,
        routes=app.routes,
    )

    schema["info"]["x-guidance"] = guidance
    _SCHEMA_CACHE[cache_key] = schema
    return schema


if ARTICLE_STORE is not None:
    from directory.article_routes import create_article_router
    app.include_router(create_article_router(ARTICLE_STORE, TEMPLATES))
app.include_router(auth_router)
app.include_router(directory_router)
app.include_router(paid_verify_router)
app.include_router(claim_invite_router)
app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False, methods=["GET"])


@app.get("/openapi.json", include_in_schema=False)
def openapi_endpoint(request: Request):
    return JSONResponse(_build_openapi())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
