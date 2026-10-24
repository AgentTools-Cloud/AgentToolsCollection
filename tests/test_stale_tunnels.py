#!/usr/bin/env python3
"""Tunnel hosts are accepted on submit and retired after 7 days without a successful check."""
import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

source_db = os.environ.get("AGENT_TOOLS_SOURCE_DB",
                           "/opt/mcpserver/data/agent-tools.db")
copy_db = "/tmp/test-stale-tunnels-%d.db" % os.getpid()
sqlite3.connect(source_db).backup(sqlite3.connect(copy_db))
os.environ["AGENT_TOOLS_DB_PATH"] = copy_db

from fastapi import HTTPException

from directory import crawlers, db, jobs, routes

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-58s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-58s %s" % (label, detail))


for url, expected in (
    ("https://adqe7gavge4j.hostc.app/whale-flows", True),
    ("https://HOSTC.APP./x", True),
    ("https://abc.trycloudflare.com/", True),
    ("https://myhostc.app/", False),
    ("https://api.example.com/v1", False),
    (None, False),
):
    check("is_ephemeral_host(%r)" % url, crawlers.is_ephemeral_host(url) is expected)


async def rest_error(url):
    try:
        await routes._require_public_url(url)
    except HTTPException as exc:
        return (exc.detail or {}).get("error") if isinstance(exc.detail, dict) else exc.detail
    return None

check("REST submit no longer rejects tunnel host by name",
      asyncio.run(rest_error("https://abc123.trycloudflare.com/x")) != "ephemeral_host")

now = int(time.time())
day = 86400
fixtures = [
    # kind, slug, url, created, health, last_ok, expect_retired
    ("x402", "zz-tun-dead", "https://dead-a-b-c.trycloudflare.com/x", now - 30 * day, "down", now - 10 * day, True),
    ("x402", "zz-tun-recent-ok", "https://live-a-b-c.trycloudflare.com/x", now - 30 * day, "down", now - 2 * day, False),
    ("x402", "zz-tun-healthy", "https://ok-a-b-c.trycloudflare.com/x", now - 30 * day, "ok", now, False),
    ("x402", "zz-tun-young", "https://young-a-b.trycloudflare.com/x", now - 3 * day, "down", None, False),
    ("x402", "zz-stable-dead", "https://dead.example.org/x", now - 30 * day, "down", None, False),
    ("mcp", "zz-mcp-tun-dead", "https://m-a-b-c.trycloudflare.com/mcp", now - 30 * day, "down", now - 9 * day, True),
    ("mcp", "zz-mcp-tun-never", "https://n-a-b-c.loca.lt/mcp", now - 30 * day, "degraded", None, True),
    ("a2a", "zz-a2a-tun-ok", "https://a-a-b-c.trycloudflare.com/a2a", now - 30 * day, "degraded", now - 1 * day, False),
    ("a2a", "zz-a2a-tun-dead", "https://d-a-b-c.trycloudflare.com/a2a", now - 30 * day, "down", now - 8 * day, True),
]
with db.writer() as conn:
    for kind, slug, url, created, health, last_ok, _ in fixtures:
        if kind == "x402":
            sid = conn.execute(
                "INSERT INTO services(slug,name,url,source,created_at,updated_at,health,health_checked) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (slug, slug, url, "submission", created, created, health, now)).lastrowid
            if last_ok:
                conn.execute("INSERT INTO health_history(service_id,checked_at,status) VALUES(?,?,'ok')",
                             (sid, last_ok))
        else:
            table, col = ("mcp_servers", "endpoint_url") if kind == "mcp" else ("a2a_agents", "endpoint_url")
            conn.execute(
                "INSERT INTO %s(slug,name,%s,source,created_at,updated_at,health,health_checked,last_success_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)" % (table, col),
                (slug, slug, url, "submission", created, created, health, now, last_ok))

with db.writer() as conn:
    stale = set(jobs.stale_tunnel_listings(conn, 7))
for kind, slug, _, _, _, _, expect in fixtures:
    check("stale(%s/%s)=%s" % (kind, slug, expect), ((kind, slug) in stale) is expect)

jobs.cmd_retire_stale_tunnels(days=7)
with db.writer() as conn:
    for kind, slug, _, _, _, _, expect in fixtures:
        retired = db.get_retired_listing(conn, kind, slug) is not None
        check("retired(%s/%s)=%s" % (kind, slug, expect), retired is expect)

for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(copy_db + suffix)
    except FileNotFoundError:
        pass
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
