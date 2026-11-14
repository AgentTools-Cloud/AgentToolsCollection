"""KPI trend backfill from raw probe history (days before the first snapshot)."""
import calendar
import json
import os
import sqlite3
import tempfile
import time
import unittest

from directory import db, stats


def ts(day, hour=12):
    return calendar.timegm(time.strptime(day, "%Y-%m-%d")) + hour * 3600


class StatsBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        c = sqlite3.connect(self.path)
        probes = [  # (service, day, hour, status)
            (1, "2026-08-01", 1, "ok"), (2, "2026-08-01", 2, "down"),
            (1, "2026-08-02", 1, "ok"), (2, "2026-08-02", 2, "ok"), (3, "2026-08-02", 3, "ok"),
            (1, "2026-08-03", 1, "down"), (1, "2026-08-03", 9, "ok"), (2, "2026-08-03", 2, "down"),
            (1, "2026-08-04", 1, "down"),
            (1, "2026-08-05", 1, "ok"),  # the first snapshot's day: not backfilled
        ]
        c.executemany("INSERT INTO health_history(service_id, checked_at, status) VALUES (?,?,?)",
                      [(s, ts(d, h), st) for s, d, h, st in probes])
        c.executemany("INSERT INTO mcp_health_history(server_id, checked_at, status) VALUES (?,?,?)",
                      [(1, ts("2026-08-03"), "ok"), (1, ts("2026-08-04"), "ok")])
        c.commit()
        c.close()

    def test_end_of_day_state_and_warmup(self):
        with db.connect(self.path, read_only=True) as c:
            rows = stats.backfill_kpis(c, "2026-08-05", warmup_days=1)
        # 08-01 is warm-up; end of 08-02: 1,2,3 ok; 08-03: 1 ok (last probe), 2 down, 3 ok;
        # 08-04: 1 down, 3 still ok.
        self.assertEqual({d: r["x402"]["reachable"] for d, r in rows.items() if "x402" in r},
                         {"2026-08-02": 3, "2026-08-03": 2, "2026-08-04": 1})
        self.assertEqual(rows["2026-08-04"]["mcp"].get("reachable"), 1)
        self.assertNotIn("reachable", rows["2026-08-03"].get("mcp", {}))  # its own warm-up day
        # No all-protocol reachable: the raw histories start on different days.
        self.assertTrue(all("reachable" not in r.get("all", {}) for r in rows.values()))

    def test_indexed_from_first_seen(self):
        c = sqlite3.connect(self.path)
        for table, extra in (("services", "url"), ("mcp_servers", "endpoint_url"), ("a2a_agents", "card_url")):
            info = list(c.execute(f"PRAGMA table_info({table})"))
            cols = [r[1] for r in info]
            required = [r for r in info if r[3] and r[4] is None and not r[5]]  # NOT NULL, no default, not PK
            for i, day in enumerate(["2026-08-01", "2026-08-03", "2026-08-03"]):
                vals = {r[1]: (0 if "INT" in (r[2] or "").upper() or "REAL" in (r[2] or "").upper()
                               else f"{r[1]}-{i}") for r in required}
                vals.update({"slug": f"{table}-{i}", "name": f"n{i}", extra: f"https://h{i}.example/{table}",
                             "created_at": ts(day)})
                vals = {k: v for k, v in vals.items() if k in cols}
                c.execute(f"INSERT INTO {table} ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                          list(vals.values()))
        c.commit()
        c.close()
        with db.connect(self.path, read_only=True) as c:
            rows = stats.backfill_kpis(c, "2026-08-05", warmup_days=1)
        self.assertEqual(rows["2026-08-02"]["x402"]["indexed"], 1)
        self.assertEqual(rows["2026-08-04"]["mcp"]["indexed"], 3)
        self.assertEqual(rows["2026-08-04"]["all"], {"indexed": 9})
        self.assertEqual(rows["2026-08-02"]["x402"]["reachable"], 3)  # merged with probes
        self.assertNotIn("2026-08-05", rows)  # first snapshot day itself

    def test_later_run_keeps_older_metrics(self):
        with db.writer(self.path) as w:
            db.save_kpi_backfill(w, {"2026-08-03": {"x402": {"reachable": 2}}}, "test")
            db.save_kpi_backfill(w, {"2026-08-03": {"x402": {"indexed": 5}, "all": {"indexed": 7}}}, "test")
            row = w.execute("SELECT kpis FROM stats_kpi_backfill WHERE day='2026-08-03'").fetchone()
        self.assertEqual(json.loads(row[0]), {"x402": {"reachable": 2, "indexed": 5}, "all": {"indexed": 7}})

    def test_history_merges_backfill_before_first_snapshot_only(self):
        snap = {"generated_at": ts("2026-08-05"), "kpis": {"all": {"indexed": 9}}}
        with db.writer(self.path) as w:
            db.save_stats_snapshot(w, snap)
            n = db.save_kpi_backfill(w, {
                "2026-08-03": {"x402": {"reachable": 2}},
                "2026-08-05": {"x402": {"reachable": 99}},  # has a real snapshot: skipped
            }, "test")
            self.assertEqual(n, 1)
            # A later run with fewer days keeps the older ones.
            db.save_kpi_backfill(w, {"2026-08-04": {"x402": {"reachable": 1}}}, "test")
        with db.connect(self.path, read_only=True) as c:
            hist = db.stats_history(c, 90)
            self.assertEqual([d for d, _ in hist], ["2026-08-03", "2026-08-04", "2026-08-05"])
            self.assertTrue(hist[0][1]["backfill"])
            self.assertNotIn("backfill", hist[-1][1])
            self.assertEqual(db.stats_history(c, 2)[0][0], "2026-08-04")
            # Monthly reports read only real snapshots.
            self.assertEqual(stats.months(c), ["2026-08"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
