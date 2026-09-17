#!/usr/bin/env python3
"""SSRF regression coverage for ownership verification."""
import os
import sqlite3
import socket
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import ownership

passed = failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-54s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-54s %s" % (label, detail))

for host in (
    "127.0.0.1", "[::1]", "localhost", "metadata.google.internal",
    "example.com:8443", "user@example.com", "https://example.com",
    "example.com/path", "example.com?x=1", "-bad.example.com",
):
    try:
        ownership.normalize_host(host)
        rejected = False
    except ownership.UnsafeTarget:
        rejected = True
    check("reject host %s" % host, rejected)

check("normalise case and trailing dot",
      ownership.normalize_host("Example.COM.") == "example.com")
check("allow IDNA DNS names",
      ownership.normalize_host("bücher.example") == "xn--bcher-kva.example")

public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
           ("93.184.216.34", 443))]
private_sets = {
    "loopback": [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))],
    "private": [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))],
    "metadata": [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", 443))],
    "mixed": public + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.1", 443))],
    "ipv6": [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 443, 0, 0))],
}
with patch("directory.ownership.socket.getaddrinfo", return_value=public):
    check("allow all-public DNS resolution",
          ownership._public_addresses("example.com") == ["93.184.216.34"])
for label, infos in private_sets.items():
    with patch("directory.ownership.socket.getaddrinfo", return_value=infos):
        try:
            ownership._public_addresses("example.com")
            rejected = False
        except ownership.UnsafeTarget:
            rejected = True
    check("reject %s DNS resolution" % label, rejected)

response = ownership._FetchResponse
with patch("directory.ownership._read_https",
           return_value=response(302, b"", {"location": "https://127.0.0.1/secret"})):
    try:
        ownership._fetch("example.com", "/start", "text/plain")
        rejected = False
    except ownership.UnsafeTarget:
        rejected = True
check("reject redirect to IP literal", rejected)

with patch("directory.ownership._read_https",
           return_value=response(302, b"", {"location": "http://example.com/insecure"})):
    try:
        ownership._fetch("example.com", "/start", "text/plain")
        rejected = False
    except ownership.UnsafeTarget:
        rejected = True
check("reject redirect to HTTP", rejected)

with patch("directory.ownership._read_https",
           return_value=response(302, b"", {"location": "https://example.com:444/x"})):
    try:
        ownership._fetch("example.com", "/start", "text/plain")
        rejected = False
    except ownership.UnsafeTarget:
        rejected = True
check("reject redirect to non-443 port", rejected)

calls=[]
def redirects(host, path, accept):
    calls.append((host, path))
    if len(calls) == 1:
        return response(302, b"", {"location": "https://next.example/verify?x=1"})
    return response(200, b"token", {})
with patch("directory.ownership._read_https", side_effect=redirects):
    got=ownership._fetch("example.com", "/start", "text/plain")
check("follow validated HTTPS redirect", got.status_code == 200,
      str(calls))
check("redirect target was revalidated", calls == [("example.com", "/start"), ("next.example", "/verify?x=1")])

class FakeSocket:
    def settimeout(self, value): pass
    def close(self): pass
class FakeConn:
    def __init__(self, host, address): pass
    def request(self, *args, **kwargs): pass
    def getresponse(self):
        class R:
            status=200
            def read(self, n): return b"x" * n
            def getheaders(self): return []
        return R()
    def close(self): pass
with patch("directory.ownership._public_addresses", return_value=["93.184.216.34"]), \
     patch("directory.ownership._PinnedHTTPSConnection", FakeConn):
    try:
        ownership._read_https("example.com", "/", "text/plain")
        rejected=False
    except ownership.UnsafeTarget:
        rejected=True
check("reject response above 64 KiB before buffering more", rejected)

# Unsafe targets are refused before a pending claim is persisted.
source_db = "/opt/mcpserver/data/agent-tools.db"
copy_db = "/tmp/test-ownership-ssrf-%d.db" % os.getpid()
sqlite3.connect(source_db).backup(sqlite3.connect(copy_db))
os.environ["AGENT_TOOLS_DB_PATH"] = copy_db
os.environ["AGENT_TOOLS_KEY_MINT_PER_DAY"] = "200"
from fastapi import FastAPI
from fastapi.testclient import TestClient
from directory.routes import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)
key = client.post("/api/v1/keys", json={"name": "ssrf-test"}).json()["api_key"]
headers = {"Authorization": "Bearer " + key}
for host in ("127.0.0.1", "localhost", "example.com:8443", "user@example.com"):
    result = client.post(
        "/api/v1/claims",
        headers=headers,
        json={"host": host, "method": "wellknown_file"},
    )
    check("claim API rejects %s" % host, result.status_code == 422,
          "status=%s" % result.status_code)
os.unlink(copy_db)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(bool(failed))
