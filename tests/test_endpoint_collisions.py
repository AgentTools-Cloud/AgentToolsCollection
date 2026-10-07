#!/usr/bin/env python3
"""Owner edits that land on an endpoint another listing already holds.

Run: PYTHONPATH=/opt/mcpserver .venv/bin/python tests/test_endpoint_collisions.py

Everything happens on a throwaway copy of the live database. The copy and the
env override come first: directory.db reads AGENT_TOOLS_DB_PATH once, at import
time, so an override placed after the import does nothing.
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

CP = "/tmp/test-endpoint-collisions-%d.db" % os.getpid()
sqlite3.connect(SRC).backup(sqlite3.connect(CP))
os.environ["AGENT_TOOLS_DB_PATH"] = CP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import collisions, db  # noqa: E402

db.init_db()

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-56s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-56s %s" % (label, detail))


HOST = "collision-fixture.example"
NEXT = [900001]


def mkservice(conn, slug, path, **extra):
    NEXT[0] += 1
    row = {"slug": slug, "name": slug, "url": "https://%s%s" % (HOST, path),
           "source": "submission", "source_id": "fixture:%d" % NEXT[0],
           "category": "general", "health": "ok", "created_at": 1700000000,
           "updated_at": 1700000000, "last_seen": 1700000000}
    row.update(extra)
    cols = ",".join(row)
    conn.execute("INSERT INTO services (%s) VALUES (%s)"
                 % (cols, ",".join("?" * len(row))), list(row.values()))
    return int(conn.execute("SELECT id FROM services WHERE slug=?",
                            (slug,)).fetchone()[0])


def mkowner(conn, uid, host, status="verified"):
    conn.execute("INSERT OR REPLACE INTO users(id, provider, provider_uid, "
                 "created_at) VALUES (?,'domain',?,1700000000)",
                 (uid, "fixture-%d" % uid))
    conn.execute("INSERT INTO domain_ownership(user_id, host, method, "
                 "token_hash, status, created_at, verified_at) "
                 "VALUES (?,?,'wellknown_file','x',?,1700000000,1700000000)",
                 (uid, host, status))
    return int(conn.execute("SELECT id FROM domain_ownership WHERE user_id=? "
                            "AND host=? ORDER BY id DESC LIMIT 1",
                            (uid, host)).fetchone()[0])


def ledger(conn, listing_id):
    r = conn.execute("SELECT * FROM endpoint_collisions WHERE listing_id=? "
                     "ORDER BY id DESC LIMIT 1", (listing_id,)).fetchone()
    return dict(r) if r else None


def move(conn, listing_id, uid, own_id, to_path):
    return db.apply_listing_edits(
        conn, "x402", listing_id, uid, own_id,
        {"url": "https://%s%s" % (HOST, to_path)},
        via="api")


# --- 1. occupied endpoint is rejected without deleting either row -----------
print("\n1. 已占用端点在写入前拒绝")
with db.writer() as c:
    uid = 900001
    own = mkowner(c, uid, HOST)
    a = mkservice(c, "fixture-root", "/", quality_score=10.0)
    b = mkservice(c, "fixture-real", "/api/v1/extract", quality_score=80.0,
                  x402_ok=1, resource_count=12)
    applied, rejected = move(c, a, uid, own, "/api/v1/extract")
    check("edit rejected", not applied and rejected and
          "already used" in rejected[0], "%s %s" % (applied, rejected))
    rows = [dict(r) for r in c.execute(
        "SELECT id,url,quality_score,x402_ok,resource_count FROM services "
        "WHERE id IN (?,?) ORDER BY id", (a, b))]
    check("both rows remain", len(rows) == 2, str(rows))
    check("source endpoint stays unchanged",
          rows[0]["url"] == "https://%s/" % HOST, str(rows[0]))
    check("occupant measurements stay unchanged",
          rows[1]["quality_score"] == 80.0 and rows[1]["x402_ok"] == 1
          and rows[1]["resource_count"] == 12, str(rows[1]))
    check("no collision ledger row", ledger(c, a) is None, str(ledger(c, a)))

# --- 2. shared gateway follows the same no-merge rule -----------------------
print("\n2. 多租户网关同样拒绝占用端点")
with db.writer() as c:
    c.execute("INSERT OR REPLACE INTO shared_hosts(host, path_count, "
              "listing_count, verdict, updated_at) "
              "VALUES (?,50,50,'shared',1700000000)", (HOST,))
    uid = 900001
    x = mkservice(c, "fixture-tenant-a", "/tenant-a")
    y = mkservice(c, "fixture-tenant-b", "/tenant-b")
    applied, rejected = move(c, x, uid, own, "/tenant-b")
    check("edit rejected", not applied and rejected and
          "already used" in rejected[0], "%s %s" % (applied, rejected))
    both = c.execute("SELECT COUNT(*) FROM services WHERE id IN (?,?)",
                     (x, y)).fetchone()[0]
    check("nothing was deleted", both == 2, "rows=%s" % both)
    check("no collision ledger row", ledger(c, x) is None, str(ledger(c, x)))
    c.execute("DELETE FROM shared_hosts WHERE host=?", (HOST,))

# --- 3. payout metadata cannot override the no-merge rule -------------------
print("\n3. 收款地址不同也在写入前拒绝")
with db.writer() as c:
    uid = 900001
    p = mkservice(c, "fixture-pay-a", "/pay-a")
    q = mkservice(c, "fixture-pay-b", "/pay-b")
    for sid, addr in ((p, "0xaaa"), (q, "0xbbb")):
        c.execute("INSERT INTO service_paytos(service_id, chain, address, "
                  "first_seen, last_seen) VALUES (?,'base',?,1,1)", (sid, addr))
    applied, rejected = move(c, p, uid, own, "/pay-b")
    check("edit rejected", not applied and rejected and
          "already used" in rejected[0], "%s %s" % (applied, rejected))
    both = c.execute("SELECT COUNT(*) FROM services WHERE id IN (?,?)",
                     (p, q)).fetchone()[0]
    check("nothing was deleted", both == 2, "rows=%s" % both)
    check("no collision ledger row", ledger(c, p) is None, str(ledger(c, p)))

# --- 4. an edit that collides with nothing ----------------------------------
print("\n4. 没撞上任何条目时不留痕")
with db.writer() as c:
    uid = 900001
    s = mkservice(c, "fixture-alone", "/alone")
    before = c.execute("SELECT COUNT(*) FROM endpoint_collisions").fetchone()[0]
    applied, _ = move(c, s, uid, own, "/still-alone")
    after = c.execute("SELECT COUNT(*) FROM endpoint_collisions").fetchone()[0]
    check("edit applied", applied == ["url"], str(applied))
    check("no ledger row", before == after, "%s -> %s" % (before, after))

# --- 5. the merge did not leave orphans -------------------------------------
print("\n5. 合并没有留下孤儿")
with db.writer() as c:
    for table, col in (("listing_sources", "listing_id"),
                       ("service_paytos", "service_id")):
        extra = " AND kind='x402'" if table == "listing_sources" else ""
        n = c.execute("SELECT COUNT(*) FROM %s t LEFT JOIN services s "
                      "ON t.%s=s.id WHERE s.id IS NULL%s"
                      % (table, col, extra)).fetchone()[0]
        check("%s has no orphan" % table, n == 0, "orphans=%s" % n)
    fts = c.execute("SELECT COUNT(*) FROM services_fts").fetchone()[0]
    svc = c.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    check("FTS still matches services", fts == svc, "%s vs %s" % (fts, svc))
    check("integrity_check", c.execute("PRAGMA integrity_check").fetchone()[0]
          == "ok")

os.unlink(CP)
print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(bool(failed))
