#!/usr/bin/env python3
"""Quick-tunnel hosts are refused at every submission entry point."""
import asyncio
import json
import os
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

source_db = os.environ.get("AGENT_TOOLS_SOURCE_DB",
                           "/opt/mcpserver/data/agent-tools.db")
copy_db = "/tmp/test-ephemeral-submission-%d.db" % os.getpid()
sqlite3.connect(source_db).backup(sqlite3.connect(copy_db))
os.environ["AGENT_TOOLS_DB_PATH"] = copy_db

from fastapi import HTTPException

from directory import crawlers, db, jobs, mcp_app, routes

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
    ("https://x.ngrok-free.app/api", True),
    ("https://foo.loca.lt", True),
    ("https://myhostc.app/", False),
    ("https://api.example.com/v1", False),
    ("not a url", False),
    (None, False),
):
    check("is_ephemeral_host(%r)" % url, crawlers.is_ephemeral_host(url) is expected)


async def rest_status(url):
    try:
        await routes._require_public_url(url)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return None, None

status, detail = asyncio.run(rest_status("https://abc123.hostc.app/token-safety"))
check("REST submit rejects tunnel host", status == 422
      and detail.get("error") == "ephemeral_host", str(status))

register = getattr(mcp_app.register, "fn", mcp_app.register)
if True:
    try:
        res = asyncio.run(register(name="t", url="https://ok.example.com/x",
                                   mcp_url="https://q1.trycloudflare.com/mcp",
                                   contact="t@example.com"))
    except TypeError as exc:
        res = {"error": "signature: %s" % exc}
check("MCP register rejects tunnel mcp_url",
      isinstance(res, dict) and res.get("error") == "ephemeral_host", str(res)[:80])

with db.writer() as conn:
    sub_id = conn.execute(
        "INSERT INTO submissions (payload, status, created_at) VALUES (?, 'pending', strftime('%s','now'))",
        (json.dumps({"name": "t", "url": "https://zz9.hostc.app/whale-flows",
                     "contact": "t@example.com"}),)).lastrowid
with patch.object(crawlers, "verify_x402",
                  side_effect=AssertionError("must not probe tunnel")) as probe:
    try:
        jobs.review_submission(sub_id)
    except Exception as exc:
        print("  (review raised %s)" % exc)
    check("auto-review does not probe tunnel host", not probe.called)
with db.writer() as conn:
    row = conn.execute("SELECT status, note FROM submissions WHERE id=?", (sub_id,)).fetchone()
check("auto-review rejects tunnel host", row and row[0] == "rejected"
      and "stable domain" in (row[1] or ""), str(tuple(row) if row else None)[:90])

for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(copy_db + suffix)
    except FileNotFoundError:
        pass
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
