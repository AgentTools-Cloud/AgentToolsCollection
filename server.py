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
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from directory import db as directory_db
from directory.routes import router as directory_router
from directory.mcp_app import discover_mcp, wrap_with_client_capture
from metrics import PrometheusMiddleware, metrics_endpoint

load_dotenv()

# --- config ---------------------------------------------------------------

ALLOWED_MODELS = {
    m.strip()
    for m in os.getenv("ALLOWED_MODELS", "Qwen/Qwen3-8B").split(",")
    if m.strip()
}

# --- FastAPI app ----------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the directory site DB exists. Safe to run repeatedly.
    try:
        directory_db.init_db()
    except Exception:
        import logging
        logging.getLogger("mcpserver").exception("directory db init failed")
    # FastMCP streamable-http needs its own session-manager lifespan.
    # Only the free directory-discovery MCP at /mcp-discovery is mounted now
    # (the paid /mcp mount was retired 2026-05-25).
    async with discover_mcp.session_manager.run():
        yield


app = FastAPI(
    title="mcpserver", version="0.2.0", lifespan=lifespan, openapi_url=None
)

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
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        path = request.url.path
        if resp.status_code == 200 and path.startswith(_CACHEABLE_DETAIL):
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
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
async def well_known_x402(request: Request) -> dict[str, Any]:
    host = (request.headers.get("host") or "").split(":", 1)[0].lower() or "agent-tools.cloud"
    description = (
        "Free MCP discovery server for x402 paid services across the ecosystem. "
        "Search 2000+ x402 endpoints, ask for recommendations, get service cards, browse categories, "
        "and scan any MCP server for malware / prompt-injection before connecting. "
        "The paid relay previously hosted here has been retired; this host is "
        "now directory + discovery only."
    )
    return {
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
            {"path": "/.well-known/agent-tools.json", "method": "GET", "kind": "agents.json", "gated": False},
            {"path": "/healthz", "method": "GET", "kind": "info", "gated": False},
        ],
        "models": sorted(ALLOWED_MODELS),
        "source": "https://github.com/JoursBleu/mcpserver",
    }


@app.get("/.well-known/mcp.json", tags=["discovery"])
async def well_known_mcp(request: Request) -> dict[str, Any]:
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
            {"name": "stats", "description": "Directory size, healthy count, source breakdown."},
            {"name": "search_mcp_servers", "description": "Search the standalone MCP server directory (registry / Smithery / PulseMCP)."},
            {"name": "get_mcp_server", "description": "Get the full card for one MCP server by slug."},
            {"name": "search_a2a_agents", "description": "Find A2A agents you can delegate a task to."},
            {"name": "search_resources", "description": "Unified search across x402 services, MCP servers and A2A agents."},
            {"name": "scan_mcp_safety", "description": "Check an MCP server (by endpoint URL) for malware / prompt-injection / tool-poisoning: returns our stored verdict if indexed, else probes + scans + indexes it."},
            {"name": "register", "description": "Self-register an x402 / MCP service for human review."},
        ],
    }


@app.get("/.well-known/agent-card.json", tags=["discovery"])
@app.get("/.well-known/agent.json", tags=["discovery"])
async def well_known_agent_card(request: Request) -> dict[str, Any]:
    """A2A Agent Card for agent-tools.cloud itself."""
    from directory import a2a as directory_a2a

    host = (request.headers.get("host") or "agent-tools.cloud").split(":", 1)[0].lower()
    scheme = "https" if request.url.scheme == "https" else "http"
    return directory_a2a.own_agent_card(f"{scheme}://{host}")


@app.post("/a2a", tags=["a2a"])
async def a2a_jsonrpc(request: Request):
    """Minimal A2A JSON-RPC endpoint (message/send)."""
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
    # handle_jsonrpc is synchronous and may do blocking work (a safety scan can
    # probe a remote endpoint + call the LLM), so run it off the event loop.
    result = await run_in_threadpool(directory_a2a.handle_jsonrpc, payload)
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
        "Free directory and discovery layer for x402 paid APIs and MCP services. "
        "Agents can search the indexed ecosystem, ask for intent-level recommendations, "
        "retrieve service cards with payment/call/quality metadata, browse categories, "
        "scan any MCP server for malware / prompt-injection before connecting, "
        "submit services for review, or connect via the ungated MCP discovery server at "
        "/mcp-discovery. The previously hosted paid Qwen relay and crypto verticals were "
        "retired on 2026-05-25; this host no longer serves paid endpoints."
    )
    guidance = (
        "Use POST /api/v1/ask for intent-level recommendations grounded "
        "in indexed directory candidates. Use GET /api/v1/search?q=&category=&chain= "
        "for faceted retrieval, then GET /api/v1/services/{slug} for the "
        "agent-readable service card before paying or calling an external "
        "service. GET /api/v1/categories and /api/v1/stats expose facets and "
        "health. POST /mcp-discovery/ is an ungated MCP streamable-http server "
        "with search, ask_services, get, list_categories, stats, scan_mcp_safety "
        "(malware / prompt-injection check for any MCP endpoint) and register. "
        "agent-tools.cloud itself is discovery-only; paid relay routes were retired. "
        "Every indexed resource (x402 service, MCP server, A2A agent) carries a "
        "liveness `health_status`: `ok` = answered a live probe and is callable; "
        "`degraded` = reachable but needs caller auth/credentials or paywalled "
        "(401/402/403); `down` = unreachable/5xx; `unknown` = not yet probed. "
        "Filter any search with health= and read health_status/http_status/latency_ms "
        "per result; results rank ok > degraded > unknown > down."
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


app.include_router(directory_router)
app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False, methods=["GET"])


@app.get("/openapi.json", include_in_schema=False)
async def openapi_endpoint(request: Request):
    return JSONResponse(_build_openapi())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
