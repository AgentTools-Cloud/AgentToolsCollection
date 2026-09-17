#!/usr/bin/env python3
"""x402 同端点归一。默认 dry-run，--apply 才真改。

分组键用数据库自己的 rtrim(lower(url),'/')，与 upsert_service 一致。

保留哪条（按优先级，第一个分出胜负就停）：
  1 被所有者编辑过   —— 人工整理过的元数据最贵
  2 所有者已认证
  3 health=ok
  4 x402_ok
  5 quality_score 高
  6 resource_count 多
  7 id 小（最早收录的，slug 对外最久）

幸存者吸收其余行的最优值；listing_sources / listing_edits / service_paytos
改指向幸存者（这正是 listing_sources 的用途）；被合并行的 health_history
直接删除——那是重复行自己的探测史，幸存者有自己的。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict

GENERIC_CATS = {"", "general", "other", "uncategorized", None}


def jlist(v):
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def rank(r):
    edited = 1 if jlist(r["owner_edited"]) else 0
    return (edited,
            int(r["owner_verified"] or 0),
            1 if r["health"] == "ok" else 0,
            int(r["x402_ok"] or 0),
            float(r["quality_score"] or 0),
            int(r["resource_count"] or 0),
            -int(r["id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/mcpserver/data/agent-tools.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()

    print("=== 预检 ===")
    tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("health_history", "listing_sources", "listing_edits", "service_paytos"):
        print("  %-18s %s" % (t, "有" if t in tabs else "无"))
    fts = [t for t in tabs if "fts" in t and ("svc" in t or "service" in t)]
    print("  services 的 FTS 表: %s" % (fts or "无"))
    for idx in c.execute("SELECT name, sql FROM sqlite_master WHERE type='index' "
                         "AND tbl_name IN ('listing_sources','service_paytos')"):
        if idx[1]:
            print("  索引 %s: %s" % (idx[0], idx[1][:96]))

    keys = [r[0] for r in c.execute(
        "SELECT rtrim(lower(url),'/') k FROM services "
        "WHERE url IS NOT NULL AND url != '' GROUP BY k HAVING COUNT(*) > 1")]
    if a.limit:
        keys = keys[:a.limit]
    print()
    print("=== 重复组 %d ===" % len(keys))

    plan = []
    for k in keys:
        rows = c.execute(
            "SELECT * FROM services WHERE rtrim(lower(url),'/')=? ORDER BY id",
            (k,)).fetchall()
        if len(rows) < 2:
            continue
        keep = max(rows, key=rank)
        drop = [r for r in rows if r["id"] != keep["id"]]
        plan.append((k, keep, drop))

    print("  将保留 %d 条，合并掉 %d 条"
          % (len(plan), sum(len(d) for _, _, d in plan)))

    # 保留者的来源分布，确认没有系统性偏向某个源
    from collections import Counter
    ks = Counter(keep["source"] for _, keep, _ in plan)
    print()
    print("  保留者来自哪个源:")
    for s, n in ks.most_common(10):
        print("    %-22s %d" % (s, n))

    print()
    print("  --- 抽查 8 组 ---")
    for k, keep, drop in plan[:8]:
        print("  %s" % k[:64])
        print("    保留 %-30s %-16s score=%-6s ver=%s edited=%s"
              % (keep["slug"][:30], keep["source"], keep["quality_score"],
                 keep["owner_verified"], bool(jlist(keep["owner_edited"]))))
        for d in drop[:3]:
            print("    合并 %-30s %-16s score=%-6s ver=%s"
                  % (d["slug"][:30], d["source"], d["quality_score"],
                     d["owner_verified"]))

    print()
    print("  --- scvd.store 这组的判定 ---")
    for k, keep, drop in plan:
        if "scvd.store" in k:
            print("    key=%s" % k)
            print("    保留 %s (%s)" % (keep["slug"], keep["source"]))
            for d in drop:
                print("    合并 %s (%s)" % (d["slug"], d["source"]))

    if not a.apply:
        print()
        print("=== dry-run 结束，未改动任何数据 ===")
        return 0

    print()
    print("=== 开始执行 ===")
    t0 = time.time()
    merged = deleted_hist = repointed = 0
    conn.execute("BEGIN")
    for k, keep, drop in plan:
        kid = keep["id"]
        dids = [d["id"] for d in drop]
        ph = ",".join("?" * len(dids))
        allrows = [keep] + drop

        # 吸收最优值
        upd = {}
        upd["owner_verified"] = max(int(r["owner_verified"] or 0) for r in allrows)
        ed = []
        for r in allrows:
            for f in jlist(r["owner_edited"]):
                if f not in ed:
                    ed.append(f)
        upd["owner_edited"] = json.dumps(ed) if ed else None
        for col in ("x402_ok", "resource_count", "tx_30d", "payto_tx_30d",
                    "payto_payers_30d", "payto_checked", "confidence"):
            vals = [r[col] for r in allrows if r[col] is not None]
            if vals:
                upd[col] = max(vals)
        rich = max(allrows, key=lambda r: int(r["resource_count"] or 0))
        if rich["resource_samples"]:
            upd["resource_samples"] = rich["resource_samples"]
        best_chain = max(allrows, key=lambda r: len(jlist(r["chains"])))
        if jlist(best_chain["chains"]):
            upd["chains"] = best_chain["chains"]
        mins = [r["price_min"] for r in allrows if r["price_min"] is not None]
        maxs = [r["price_max"] for r in allrows if r["price_max"] is not None]
        if mins:
            upd["price_min"] = min(mins)
        if maxs:
            upd["price_max"] = max(maxs)
        if (keep["category"] or "").lower() in GENERIC_CATS:
            for r in allrows:
                if (r["category"] or "").lower() not in GENERIC_CATS:
                    upd["category"] = r["category"]
                    break
        if not keep["payment"]:
            for r in allrows:
                if r["payment"]:
                    upd["payment"] = r["payment"]
                    break
        upd["created_at"] = min(int(r["created_at"] or 0) for r in allrows if r["created_at"])
        upd["updated_at"] = int(time.time())
        sets = ", ".join("%s=?" % c2 for c2 in upd)
        conn.execute("UPDATE services SET %s WHERE id=?" % sets,
                     list(upd.values()) + [kid])

        # 子表改指向
        for tbl, col, kindcol in (("listing_sources", "listing_id", "kind"),
                                  ("listing_edits", "listing_id", "kind"),
                                  ("service_paytos", "service_id", None)):
            if tbl not in tabs:
                continue
            if kindcol:
                r = conn.execute(
                    "UPDATE OR IGNORE %s SET %s=? WHERE %s IN (%s) AND kind='x402'"
                    % (tbl, col, col, ph), [kid] + dids)
            else:
                r = conn.execute(
                    "UPDATE OR IGNORE %s SET %s=? WHERE %s IN (%s)"
                    % (tbl, col, col, ph), [kid] + dids)
            repointed += r.rowcount
            # UPDATE OR IGNORE 撞唯一索引会跳过，残留的直接删
            if kindcol:
                conn.execute("DELETE FROM %s WHERE %s IN (%s) AND kind='x402'"
                             % (tbl, col, ph), dids)
            else:
                conn.execute("DELETE FROM %s WHERE %s IN (%s)" % (tbl, col, ph), dids)

        if "health_history" in tabs:
            r = conn.execute("DELETE FROM health_history WHERE service_id IN (%s)"
                             % ph, dids)
            deleted_hist += r.rowcount
        conn.execute("DELETE FROM services WHERE id IN (%s)" % ph, dids)
        merged += len(dids)

    conn.commit()
    print("  合并掉 %d 条，改指向 %d 行，删除 health_history %d 行，耗时 %.1fs"
          % (merged, repointed, deleted_hist, time.time() - t0))
    return 0


sys.exit(main())
