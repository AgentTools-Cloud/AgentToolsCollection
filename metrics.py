"""Prometheus instrumentation for mcpserver.

Exposes /metrics and counts every HTTP request (incl. /mcp + /mcp-discovery
streamable-http POSTs and x402-gated 402 challenges) with bounded-cardinality
route labels.
"""
from __future__ import annotations

import re
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

REQUESTS = Counter(
    "mcpserver_http_requests_total",
    "HTTP requests handled by mcpserver, labelled by method/route/status.",
    ["method", "route", "status"],
)
DURATION = Histogram(
    "mcpserver_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

_SLUG = re.compile(r"^/services/[^/]+/?$")
_CAT = re.compile(r"^/categories/[^/]+/?$")
_WELL = re.compile(r"^/\.well-known/.+")
_STATIC = re.compile(r"^/(static|assets)/.+")
_KNOWN_ROOT_PREFIXES = (
    "/mcp", "/mcp-discovery", "/api/v1", "/v1",
    "/services", "/categories", "/healthz", "/health",
    "/llms.txt", "/openapi.json", "/robots.txt", "/favicon.ico",
    "/submit", "/about", "/metrics", "/", "/.well-known",
)


def normalize_route(path: str) -> str:
    p = path or "/"
    if p != "/" and p.endswith("/"):
        p = p.rstrip("/")
    if _SLUG.match(p + "/"):
        return "/services/:slug"
    if _CAT.match(p + "/"):
        return "/categories/:cat"
    if _WELL.match(p):
        return "/.well-known/*"
    if _STATIC.match(p):
        return "/static/*"
    if not any(p == r or p.startswith(r + "/") or p == r for r in _KNOWN_ROOT_PREFIXES):
        # Unknown path — likely vuln scanner; collapse to avoid label explosion.
        return "/_other"
    if len(p) > 64:
        return "/_long"
    return p


class PrometheusMiddleware:
    """ASGI middleware: count + time every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "?")
        route = normalize_route(scope.get("path", "/"))
        # /metrics itself must not be timed (avoid scraper self-counting).
        if route == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def _send(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration = time.perf_counter() - start
            REQUESTS.labels(method, route, str(status_holder["code"])).inc()
            DURATION.labels(method, route).observe(duration)


async def metrics_endpoint(request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
