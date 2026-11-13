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
        self.assertEqual(rows["2026-08-04"]["mcp"], {"reachable": 1})
        self.assertNotIn("mcp", rows["2026-08-03"])  # its own warm-up day
        self.assertTrue(all("all" not in r for r in rows.values()))

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
