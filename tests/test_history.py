"""Daily long-term history roll-up (data/history.db)."""
import calendar
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import db, history  # noqa: E402


class HistoryRollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("AGENT_TOOLS_HISTORY_DB")
        os.environ["AGENT_TOOLS_HISTORY_DB"] = os.path.join(self.tmp.name, "h.db")
        self.path = os.path.join(self.tmp.name, "m.db")
        db.init_db(self.path)
        c = sqlite3.connect(self.path)
        self.now = calendar.timegm((2026, 11, 12, 12, 0, 0))
        y = self.now - 86400
        c.execute("INSERT INTO services(id, slug, name, url, health, readiness_score, readiness_grade, "
                  "tx_30d, created_at, updated_at, source) VALUES (1,'s','s','https://s.example','ok',80,'B',7,?,?,'t')",
                  (self.now, self.now))
        c.execute("INSERT INTO mcp_servers(id, slug, name, endpoint_url, health, readiness_score, "
                  "readiness_grade, created_at, updated_at, source) "
                  "VALUES (1,'m','m','https://m.example/mcp','down',40,'D',?,?,'t')", (self.now, self.now))
        for ts, st, lat in ((y + 100, "ok", 100), (self.now - 100, "ok", 300), (self.now - 50, "down", None)):
            c.execute("INSERT INTO health_history(service_id, checked_at, status, latency_ms) VALUES (1,?,?,?)",
                      (ts, st, lat))
        c.execute("INSERT INTO mcp_health_history(server_id, checked_at, status, latency_ms) VALUES (1,?,?,?)",
                  (self.now - 10, "down", None))
        c.commit()
        c.close()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AGENT_TOOLS_HISTORY_DB", None)
        else:
            os.environ["AGENT_TOOLS_HISTORY_DB"] = self._old
        self.tmp.cleanup()

    def test_rollup_is_idempotent_and_keeps_state(self):
        src = sqlite3.connect(self.path)
        for _ in range(2):
            out = history.rollup(src, now=self.now)
        src.close()
        self.assertEqual(out["day"], "2026-11-12")
        h = sqlite3.connect(os.environ["AGENT_TOOLS_HISTORY_DB"])
        rows = {(k, d): r for k, d, *r in h.execute(
            "SELECT kind, day, probes, ok, down, latency_avg_ms, readiness, grade, tx_30d, health "
            "FROM listing_daily ORDER BY 1, 2")}
        self.assertEqual(rows[("x402", "2026-11-12")], [2, 1, 1, 300, 80.0, "B", 7, "ok"])
        self.assertEqual(rows[("x402", "2026-11-11")][:3], [1, 1, 0])   # yesterday re-rolled
        self.assertEqual(rows[("mcp", "2026-11-12")][:2], [1, 0])
        self.assertEqual(h.execute("SELECT COUNT(*) FROM listing_daily").fetchone()[0], 3)
        h.close()
        s = history.summary()
        self.assertEqual(s["listings"]["x402"]["days"], 2)

    def test_backfill_covers_every_raw_day(self):
        src = sqlite3.connect(self.path)
        out = history.backfill(src)
        src.close()
        self.assertEqual(out, {"x402": 2, "mcp": 1, "a2a": 0})


if __name__ == "__main__":
    unittest.main()
