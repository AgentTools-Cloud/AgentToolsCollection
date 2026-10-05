"""Prometheus instrumentation for mcpserver.

Exposes /metrics and counts every HTTP request (incl. /mcp + /mcp-discovery
streamable-http POSTs and x402-gated 402 challenges) with bounded-cardinality
route labels.
"""
from __future__ import annotations

import ipaddress
import os
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

_SLUG_ROUTES = (
    (re.compile(r"^/services/[^/]+/?$"), "/services/:slug"),
    (re.compile(r"^/mcp/servers/[^/]+/?$"), "/mcp/servers/:slug"),
    (re.compile(r"^/a2a/agents/[^/]+/?$"), "/a2a/agents/:slug"),
    (re.compile(r"^/listings/(x402|mcp|a2a)/[^/]+/edit/?$"),
     "/listings/:kind/:slug/edit"),
    (re.compile(r"^/api/v1/services/[^/]+/?$"), "/api/v1/services/:slug"),
    (re.compile(r"^/api/v1/mcp/servers/[^/]+/?$"),
     "/api/v1/mcp/servers/:slug"),
    (re.compile(r"^/api/v1/a2a/agents/[^/]+/?$"),
     "/api/v1/a2a/agents/:slug"),
    (re.compile(r"^/api/v1/listings/(x402|mcp|a2a)/[^/]+/?$"),
     "/api/v1/listings/:kind/:slug"),
)
_CAT = re.compile(r"^/categories/[^/]+/?$")
_WELL = re.compile(r"^/\.well-known/.+")
_STATIC = re.compile(r"^/(static|assets)/.+")
_EXACT_ROUTES = {
    "/", "/mcp", "/mcp-discovery", "/api/v1/search", "/api/v1/ask",
    "/api/v1/categories", "/api/v1/stats", "/api/v1/resources/search",
    "/api/v1/mcp/search", "/api/v1/mcp/stats", "/api/v1/a2a/search",
    "/api/v1/a2a/stats", "/api/v1/submit", "/api/v1/mcp/submit",
    "/api/v1/a2a/submit", "/v1/models", "/x402", "/a2a", "/categories",
    "/healthz", "/health", "/llms.txt", "/openapi.json", "/robots.txt",
    "/favicon.ico", "/submit", "/about", "/metrics",
}
_COLLAPSED_PREFIXES = (
    "/api/v1", "/mcp-discovery", "/mcp", "/v1", "/services",
    "/categories", "/listings",
)
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


def normalize_route(path: str) -> str:
    p = path or "/"
    if p != "/" and p.endswith("/"):
        p = p.rstrip("/")
    for pattern, label in _SLUG_ROUTES:
        if pattern.match(p):
            return label
    if _CAT.match(p + "/"):
        return "/categories/:cat"
    if _WELL.match(p):
        return "/.well-known/*"
    if _STATIC.match(p):
        return "/static/*"
    if p in _EXACT_ROUTES:
        return p
    for prefix in _COLLAPSED_PREFIXES:
        if p.startswith(prefix + "/"):
            return prefix + "/*"
    return "/_other"


class PrometheusMiddleware:
    """ASGI middleware: count + time every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_method = str(scope.get("method", "?")).upper()
        method = raw_method if raw_method in _HTTP_METHODS else "_other"
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


def _peer_ip(request: Request) -> str | None:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _is_direct_loopback(request: Request) -> bool:
    if any(request.headers.get(name) for name in (
            "cf-connecting-ip", "x-forwarded-for", "x-real-ip")):
        return False
    return _is_loopback(request.client.host if request.client else None)


def _is_loopback(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


async def metrics_endpoint(request: Request) -> Response:
    token = os.getenv("METRICS_BEARER_TOKEN") or os.getenv("METRICS_TOKEN")
    if token:
        auth = request.headers.get("authorization") or ""
        if auth != f"Bearer {token}":
            return Response("not found\n", status_code=404, media_type="text/plain")
    elif not _is_direct_loopback(request):
        return Response("not found\n", status_code=404, media_type="text/plain")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
