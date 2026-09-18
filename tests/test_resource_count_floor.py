#!/usr/bin/env python3
"""A crawl must not lower resource_count on a shared row.

Run: PYTHONPATH=/opt/mcpserver .venv/bin/python tests/test_resource_count_floor.py

Endpoint dedup put every source that lists a URL onto one row. The sources do
not agree on what a resource is -- for scvd.store on 2026-09-18 pay-skills-pr
said 1 (its parse failed and it fell back to a synthetic sample), our own
descriptor read said 43 and x402scan said 190 -- so without a floor the last
crawler of the night decides. 554 rows had already been lowered this way.

The copy and the env override come first: directory.db reads the path once, at
import time.
"""
import json
import os
import sqlite3
import sys

SRC = os.environ.get("AGENT_TOOLS_SOURCE_DB",
                     "/opt/mcpserver/data/agent-tools.db")
if not os.path.exists(SRC):
    print("SKIP: no database at %s" % SRC)
    raise SystemExit(0)

CP = "/tmp/test-resource-floor-%d.db" % os.getpid()
sqlite3.connect(SRC).backup(sqlite3.connect(CP))
os.environ["AGENT_TOOLS_DB_PATH"] = CP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import db  # noqa: E402

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-52s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-52s %s" % (label, detail))


URL = "https://resource-floor.example/"
RICH = [{"url": "https://resource-floor.example/a"},
        {"url": "https://resource-floor.example/b"}]

with db.writer() as c:
    c.execute("INSERT INTO services(slug, name, url, source, source_id, "
              "resource_count, resource_samples, created_at, updated_at) "
              "VALUES ('rf-fixture','rf',?,'cdp-bazaar','rf:1',43,?,1,1)",
              (URL, json.dumps(RICH)))
    SID = int(c.execute("SELECT id FROM services WHERE slug='rf-fixture'"
                        ).fetchone()[0])


def crawl(source, source_id, count, samples):
    with db.writer() as c:
        db.upsert_service(c, {"slug": "rf-" + source_id, "name": "rf",
                              "url": URL, "source": source,
                              "source_id": source_id,
                              "resource_count": count,
                              "resource_samples": samples})


def row():
    with db.writer() as c:
        r = c.execute("SELECT resource_count, resource_samples FROM services "
                      "WHERE id=?", (SID,)).fetchone()
        return r["resource_count"], r["resource_samples"] or ""


crawl("pay-skills-pr", "rf:2", 1, [{"url": "https://resource-floor.example"}])
count, samples = row()
check("a poorer crawl cannot lower the count", count == 43, "got %s" % count)
check("and cannot replace the richer samples", "/a" in samples, samples[:44])

crawl("x402scan", "rf:3", 77, [{"url": "https://resource-floor.example/c"}])
count, samples = row()
check("a richer crawl still raises it", count == 77, "got %s" % count)
check("and brings its samples", "/c" in samples, samples[:44])

crawl("awesome-x402", "rf:4", None, None)
check("a crawl with nothing to say changes nothing", row()[0] == 77,
      "got %s" % row()[0])

# reverify_x402 writes this column directly, so it stays free to correct a
# descriptor that really did shrink.
with db.writer() as c:
    c.execute("UPDATE services SET resource_count=? WHERE id=?", (43, SID))
check("a direct descriptor write may still lower it", row()[0] == 43,
      "got %s" % row()[0])

crawl("cdp-bazaar", "rf:1", 0, [])
check("zero from the owning source is still a lowering", row()[0] == 43,
      "got %s" % row()[0])

os.unlink(CP)
print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(bool(failed))
