#!/usr/bin/env python3
"""SSRF regression coverage for public submission probes."""
import os
import socket
import sqlite3
import sys
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# directory.db freezes AGENT_TOOLS_DB_PATH into a module constant at import
# time, so the copy has to happen before the first `from directory import ...`.
source_db = os.environ.get("AGENT_TOOLS_SOURCE_DB",
                           "/opt/mcpserver/data/agent-tools.db")
copy_db = "/tmp/test-submission-ssrf-%d.db" % os.getpid()
sqlite3.connect(source_db).backup(sqlite3.connect(copy_db))
os.environ["AGENT_TOOLS_DB_PATH"] = copy_db

from directory import a2a, crawlers, public_http

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-58s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-58s %s" % (label, detail))


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
           ("93.184.216.34", 443))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("127.0.0.1", 443))]
MIXED = PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                   ("10.0.0.1", 443))]

for label, records in (("loopback", PRIVATE), ("mixed public/private", MIXED)):
    with patch("directory.public_http.socket.getaddrinfo", return_value=records):
        try:
            public_http.validate_url("https://public.example/path")
            blocked = False
        except public_http.UnsafeURL:
            blocked = True
    check("reject %s DNS answer" % label, blocked)

seen = []


def redirector(request):
    seen.append(str(request.url))
    return httpx.Response(
        302, headers={"location": "http://127.0.0.1/secret"}, request=request)


def client_factory(**kwargs):
    kwargs["transport"] = public_http.PublicTransport(
        resolver=lambda host, port: ["93.184.216.34"],
        inner=httpx.MockTransport(redirector),
    )
    return httpx.Client(**kwargs)


with patch("directory.crawlers._host_safety", return_value="public"), \
     patch("directory.crawlers.public_http.client", side_effect=client_factory):
    x402 = crawlers.verify_x402("https://public.example/paid")
    mcp = crawlers.probe_mcp_health("https://public.example/mcp")
with patch("directory.a2a.public_http.client", side_effect=client_factory):
    try:
        a2a.fetch_agent_card("https://public.example", reject_unsafe=True)
        a2a_blocked = False
    except public_http.UnsafeURL:
        a2a_blocked = True
check("x402 rejects redirect to loopback",
      x402.get("status") == "rejected" and bool(x402.get("unsafe_url")))
check("MCP preserves unsafe redirect signal",
    mcp.get("status") == "down" and bool(mcp.get("unsafe_url")))
check("A2A strict fetch rejects redirect to loopback", a2a_blocked)
check("no request reaches redirected loopback",
      all("127.0.0.1" not in url for url in seen), str(seen))

connected = []


def pin_handler(request):
    connected.append((str(request.url), request.headers["host"],
                      request.extensions.get("sni_hostname")))
    return httpx.Response(200, request=request)


transport = public_http.PublicTransport(
    resolver=lambda host, port: ["93.184.216.34"],
    inner=httpx.MockTransport(pin_handler),
)
with httpx.Client(transport=transport) as client:
    response = client.get("https://public.example:8443/path")
check("transport connects to the validated IP", response.status_code == 200 and
      connected[0][0] == "https://93.184.216.34:8443/path")
check("transport preserves Host and TLS SNI", connected[0][1:] ==
      ("public.example:8443", "public.example"), str(connected))

resolutions = []
rebind_answers = iter((["93.184.216.34"], ["127.0.0.1"]))


def rebind_resolver(host, port):
    resolutions.append((host, port))
    return next(rebind_answers)


rebind_requests = []


def relative_redirect(request):
    rebind_requests.append(str(request.url))
    return httpx.Response(
        302, headers={"location": "/after-rebind"}, request=request)


transport = public_http.PublicTransport(
    resolver=rebind_resolver,
    inner=httpx.MockTransport(relative_redirect),
)
try:
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        client.get("https://rebind.example/start")
    rebind_blocked = False
except public_http.UnsafeURL:
    rebind_blocked = True
check("same-host redirect is re-resolved", len(resolutions) == 2,
      str(resolutions))
check("DNS rebind to loopback is blocked before reconnect", rebind_blocked and
      rebind_requests == ["https://93.184.216.34/start"], str(rebind_requests))

# Public endpoints must reject before probes, rate-limit mutation, or DB writes.
from fastapi import FastAPI
from fastapi.testclient import TestClient
from directory import routes

app = FastAPI()
app.include_router(routes.router)
client = TestClient(app)
probe_calls = []


def should_not_probe(*args, **kwargs):
    probe_calls.append((args, kwargs))
    raise AssertionError("unsafe URL reached a network probe")


with patch("directory.public_http.socket.getaddrinfo", return_value=PRIVATE), \
     patch.object(routes.directory_crawlers, "url_retired", return_value=False), \
     patch.object(routes.directory_jobs, "review_submission", side_effect=should_not_probe), \
     patch.object(routes.directory_crawlers, "probe_mcp_health", side_effect=should_not_probe), \
     patch.object(routes.directory_a2a, "fetch_agent_card", side_effect=should_not_probe):
    cases = (
        ("x402", "/api/v1/submit", {
            "name": "Private", "url": "https://private.example/x402",
            "contact": "test@example.com"}),
        ("MCP", "/api/v1/mcp/submit", {
            "url": "https://private.example/mcp", "contact": "test@example.com"}),
        ("A2A", "/api/v1/a2a/submit", {
            "url": "https://private.example/a2a", "contact": "test@example.com"}),
    )
    for label, path, payload in cases:
        result = client.post(path, json=payload)
        detail = result.json().get("detail", {})
        check("%s submit returns 422 unsafe_url" % label,
              result.status_code == 422 and detail.get("error") == "unsafe_url",
              "status=%s body=%s" % (result.status_code, result.text[:200]))
check("unsafe submissions never reach a probe", not probe_calls, str(probe_calls))
os.unlink(copy_db)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(bool(failed))
