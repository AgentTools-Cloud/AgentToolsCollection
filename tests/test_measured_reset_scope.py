#!/usr/bin/env python3
"""An endpoint edit only invalidates the measurements taken against it.

Run: PYTHONPATH=/opt/mcpserver .venv/bin/python tests/test_measured_reset_scope.py

An x402 listing is probed at `url`. `mcp_url` is a second address on the same
service, so moving it used to wipe health, x402_ok, the on-chain counts and the
score -- measurements that came from the first address and were still true.

Also covers the third state in the score breakdown: a payability signal that
has never been looked for reads `unknown`, not `no`.
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

CP = "/tmp/test-measured-reset-%d.db" % os.getpid()
sqlite3.connect(SRC).backup(sqlite3.connect(CP))
os.environ["AGENT_TOOLS_DB_PATH"] = CP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import db  # noqa: E402

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-56s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-56s %s" % (label, detail))


HOST = "measured-reset.example"
MEASURED = ("health", "x402_ok", "tx_30d", "payto_payers_30d", "quality_score")


def fixture(slug):
    with db.writer() as c:
        c.execute(
            "INSERT INTO services(slug, name, url, mcp_url, source, source_id, "
            "health, health_checked, x402_ok, tx_30d, payto_payers_30d, "
            "payto_checked, quality_score, created_at, updated_at) "
            "VALUES (?,?,?,?,'submission',?, 'ok',1700000000,1,120,7,"
            "1700000000,88.0,1700000000,1700000000)",
            (slug, slug, "https://%s/v1/pay" % HOST, "https://%s/mcp" % HOST,
             "mr:" + slug))
        return int(c.execute("SELECT id FROM services WHERE slug=?",
                             (slug,)).fetchone()[0])


def measured(sid):
    with db.writer() as c:
        r = c.execute("SELECT %s FROM services WHERE id=?"
                      % ",".join(MEASURED), (sid,)).fetchone()
        return {k: r[k] for k in MEASURED}


def edit(sid, changes):
    with db.writer() as c:
        return db.apply_listing_edits(c, "x402", sid, 1, 1, changes,
                                      verified_hosts={HOST}, via="api")


print("1. 改 mcp_url 不动属于 url 的测量值")
a = fixture("mr-mcp")
applied, rejected = edit(a, {"mcp_url": "https://%s/mcp/v2" % HOST})
check("edit applied", applied == ["mcp_url"], "%s %s" % (applied, rejected))
after = measured(a)
check("health 保住", after["health"] == "ok", str(after["health"]))
check("x402_ok 保住", after["x402_ok"] == 1, str(after["x402_ok"]))
check("链上计数保住", after["tx_30d"] == 120 and after["payto_payers_30d"] == 7,
      str(after))
check("分数保住", after["quality_score"] == 88.0, str(after["quality_score"]))

print("\n2. 改 url 仍然作废测量值（信誉不跟着地址走）")
b = fixture("mr-url")
applied, _ = edit(b, {"url": "https://%s/v2/pay" % HOST})
check("edit applied", applied == ["url"], str(applied))
after = measured(b)
check("测量值全部清空", all(after[k] is None for k in MEASURED), str(after))

print("\n3. 评分明细的第三态")
row = {"health": "ok", "x402_ok": None, "payment": None, "payto_checked": None}
parts = db.score_breakdown(row)["components"][1]["parts"]
check("没测过时标 unknown",
      all(p["state"] == "unknown" for p in parts),
      str([p["state"] for p in parts]))

row = {"health": "ok", "x402_ok": 0,
       "payment": json.dumps({"accepts": [{}]}), "payto_checked": None}
parts = db.score_breakdown(row)["components"][1]["parts"]
check("测过但没有时标 no",
      all(p["state"] == "no" for p in parts),
      str([p["state"] for p in parts]))

row = {"health": "ok", "x402_ok": 1, "well_known_url": "https://x/.well-known/x402",
       "payment": json.dumps({"pay_to": "0x" + "a" * 40, "network": "base",
                              "max_amount_usdc": 0.01}),
       "payto_checked": 1700000000}
comps = db.score_breakdown(row)["components"]
parts = comps[1]["parts"]
check("有信号时标 yes", all(p["state"] == "yes" for p in parts),
      str([(p["label"][:22], p["state"]) for p in parts]))
check("ok 字段保留给既有调用方", all("ok" in p for p in parts))
check("Demand 标出是否测过", comps[2]["unmeasured"] is False,
      str(comps[2].get("unmeasured")))

os.unlink(CP)
print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(bool(failed))
