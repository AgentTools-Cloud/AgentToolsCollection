"""Detail-page text from our own probes (directory/measured.py)."""
import sqlite3
import unittest

from directory import measured

NOW = 1794500000


class MeasuredTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute("CREATE TABLE mcp_health_history(server_id INT, checked_at INT, "
                       "status TEXT, latency_ms INT, http_status INT)")
        rows = [(1, NOW - d * 86400, "ok" if d % 5 else "down", 100 + d * 10, 200)
                for d in range(1, 11)]
        rows.append((1, NOW - 40 * 86400, "ok", 5, 200))   # outside the window
        self.c.executemany("INSERT INTO mcp_health_history VALUES (?,?,?,?,?)", rows)

    def test_probe_record_counts_the_window_only(self):
        r = measured.probe_record(self.c, "mcp", 1, now=NOW)
        self.assertEqual((r["probes"], r["answered"], r["answered_pct"], r["days"]), (10, 8, 80, 10))
        self.assertEqual(r["median_ms"], 160)
        self.assertIsNone(measured.probe_record(self.c, "mcp", 2, now=NOW))

    def test_sentences_and_meta(self):
        row = {"id": 1, "name": "Demo", "description": "Does demo things\nmore",
               "tool_count": 2, "protocol_version": "2025-11-25",
               "tools": [{"name": "alpha", "description": "a"}, {"name": "beta"}, {"bad": 1}]}
        peer = {"peers": 53000, "peer_group": "reachable MCP servers", "as_of": "2026-11-13",
                "latency": {"faster_than_pct": 61.4}, "readiness": {"better_than_pct": 40}}
        rec = measured.probe_record(self.c, "mcp", 1, now=NOW)
        out = measured.sentences("mcp", row, rec, peer)
        self.assertEqual(len(out), 3)
        self.assertIn("answered 8 (80%)", out[0])
        self.assertIn("returned 2 tools, including alpha, beta, over MCP protocol revision 2025-11-25.", out[1])
        self.assertIn("faster than 61% and higher on readiness than 40%", out[2])
        row["measured"] = {"record": rec, "tools": row["tools"][:2], "tools_total": 2}
        meta = measured.meta_description("mcp", row)
        self.assertTrue(meta.startswith("MCP server with 2 tools (alpha, beta). Does demo things."))
        self.assertLessEqual(len(measured.meta_description("mcp", {**row, "description": "x " * 200})), 160)

    def test_x402_on_chain_sentence(self):
        out = measured.sentences("x402", {"payto_checked": 1, "payto_payers_30d": 1,
                                          "payto_tx_30d": 3}, None, None)
        self.assertEqual(out, ["On-chain, 1 distinct wallet paid its payment address 3 times in the last 30 days."])
        self.assertEqual(measured.sentences("a2a", {}, None, None), [])


if __name__ == "__main__":
    unittest.main()
