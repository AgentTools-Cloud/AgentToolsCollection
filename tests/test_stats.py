"""Public /stats snapshot: counts, CSV rows, headlines and page rendering."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jinja2 import Environment, FileSystemLoader  # noqa: E402

from directory import db, stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = int(time.time())
DAY = 86400


def _a2a_readiness(rpc_points, sig_points, security_detail):
    return json.dumps({"criteria": [
        {"key": "live_jsonrpc", "label": "Live JSON-RPC", "points": rpc_points, "max_points": 25, "applicable": True},
        {"key": "signature", "label": "JWS signature", "points": sig_points, "max_points": 10, "applicable": True},
        {"key": "security", "label": "Security", "points": 5, "max_points": 10, "applicable": True,
         "detail": security_detail},
    ]})


class StatsSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "t.db")
        db.init_db(cls.path)
        c = sqlite3.connect(cls.path)
        ins = c.execute
        # x402: 3 indexed, 2 with url, 2 reachable, 1 with a valid challenge, graded A.
        pay = json.dumps({"network": "eip155:8453", "max_amount_usdc": 0.005})
        samples = json.dumps([{"url": "https://a.example/r", "x402_version": 2}])
        for i, (url, health, ok, grade, p) in enumerate([
                ("https://a.example", "ok", 1, "A", pay),
                ("https://b.example", "ok", 0, "C", None),
                ("", "unknown", 0, None, None)], start=1):
            ins("INSERT INTO services(id, slug, name, url, description, health, x402_ok, readiness_grade, payment, "
                "resource_samples, latency_ms, created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'t')",
                (i, f"s{i}", f"weather api {i}", url or f"https://none{i}.example", "weather data",
                 health, ok, grade, p, samples if ok else None, 200 * i, NOW, NOW))
        c.execute("UPDATE services SET url='' WHERE id=3")
        # MCP: 2 indexed, both reachable, 1 conformance pass.
        for i, (conf, grade, auth) in enumerate([("pass", "B", "oauth"), ("fail", "D", None)], start=1):
            ins("INSERT INTO mcp_servers(id, slug, name, description, endpoint_url, health, conformance, "
                "readiness_grade, protocol_version, auth_method, latency_ms, safety_verdict, created_at, updated_at, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'t')",
                (i, f"m{i}", f"github tool {i}", "code search", f"https://m{i}.example/mcp", "ok",
                 conf, grade, "2025-06-18" if i == 1 else None, auth, 100 * i, "clean", NOW, NOW))
        # A2A: 2 indexed, both fetched, 1 answers JSON-RPC with a verified signature.
        for i, (rpc, sig, sec, grade) in enumerate([
                (25, 10, "Declares mutual TLS", "A"), (0, 0, "No authentication declared", "F")], start=1):
            ins("INSERT INTO a2a_agents(id, slug, name, description, card_url, health, readiness_json, "
                "readiness_grade, protocol_version, latency_ms, created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'t')",
                (i, f"a{i}", f"agent {i}", "travel agent", f"https://a{i}.example/.well-known/agent-card.json",
                 "ok", _a2a_readiness(rpc, sig, sec), grade, "1.0" if i == 1 else "0.3.0", 300, NOW, NOW))
        for kind, lid, src in [("x402", 1, "x402scan"), ("x402", 1, "bazaar"), ("x402", 2, "x402scan"),
                               ("mcp", 1, "registry"), ("mcp", 2, "smithery"), ("a2a", 1, "awesome-a2a")]:
            ins("INSERT INTO listing_sources(kind, listing_id, source, first_seen, last_seen) VALUES (?,?,?,?,?)",
                (kind, lid, src, NOW - 2 * DAY, NOW))
        c.commit()
        c.row_factory = sqlite3.Row
        cls.snap = stats.compute(c, NOW)
        cls.conn = c

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_funnel_counts(self):
        f = self.snap["funnel"]
        self.assertEqual([s["count"] for s in f["x402"]], [3, 2, 2, 1, 1])
        self.assertEqual([s["count"] for s in f["mcp"]], [2, 2, 2, 1, 1])
        self.assertEqual([s["count"] for s in f["a2a"]], [2, 2, 2, 1, 1])
        self.assertEqual(self.snap["kpis"]["all"], {"indexed": 7, "reachable": 6, "verified": 3, "ready": 3})

    def test_protocols_and_signatures(self):
        p = self.snap["protocols"]
        self.assertEqual({r["label"]: r["count"] for r in p["mcp"]}["2025-06-18"], 1)
        self.assertEqual({r["label"]: r["count"] for r in p["mcp"]}["Not reported"], 1)
        self.assertEqual({r["label"]: r["count"] for r in p["a2a"]}["1.x"], 1)
        self.assertEqual({r["label"]: r["count"] for r in p["a2a"]}["0.3"], 1)
        self.assertEqual(p["a2a_signatures"], {"verified": 1, "unverified": 0, "unsigned": 1})
        self.assertEqual({r["label"]: r["count"] for r in p["x402"]}["v2"], 1)

    def test_sources_overlap_and_growth(self):
        s = self.snap["sources"]["x402"]
        self.assertEqual(s["multi_source_pct"], 50.0)
        self.assertEqual(self.snap["growth"]["x402"]["new_7d"], 2)

    def test_x402_prices_and_access(self):
        x = self.snap["x402"]
        self.assertEqual(x["priced"], 1)
        self.assertEqual(x["price_quantiles"]["p50"], 0.005)
        self.assertEqual(x["chains"][0]["label"], "Base")
        mcp_access = {r["label"]: r["count"] for r in self.snap["access"]["mcp"]}
        self.assertEqual((mcp_access["OAuth"], mcp_access["Not declared"]), (1, 1))
        a2a_access = {r["label"]: r["count"] for r in self.snap["access"]["a2a"]}
        self.assertEqual((a2a_access["mTLS"], a2a_access["Declared public"]), (1, 1))

    def test_snapshot_round_trip_csv_and_headlines(self):
        with db.writer(self.path) as w:
            db.save_stats_snapshot(w, self.snap)
            db.save_stats_snapshot(w, self.snap)  # same day: replaced, not duplicated
        self.assertEqual(db.latest_stats_snapshot(self.conn)["kpis"], self.snap["kpis"])
        hist = db.stats_history(self.conn, 90)
        self.assertEqual(len(hist), 1)
        rows = stats.flatten(self.snap)
        self.assertIn(("all", "indexed", 7), rows)
        h = stats.headlines(self.snap)
        self.assertIn("50.0% of indexed MCP servers", h["funnel"])
        hx = stats.headlines(self.snap, "x402")
        self.assertIn("33.3% return a valid 402 challenge", hx["funnel"])
        self.assertIn("x402 v2", hx["protocols"])
        self.assertIn("declare no security scheme", stats.headlines(self.snap, "a2a")["access"])
        json.dumps(self.snap)

    def test_page_renders_all_views(self):
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        env.globals.update(build_version="t", current_user=lambda r: None,
                           may_edit_listing=lambda *a: False, url_host=lambda u: u)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v
        tpl = env.get_template("stats.html")

        class Req:
            class url:
                path = "/stats"
        for view in ("all", "x402", "mcp", "a2a"):
            kinds = [k for k in stats.KINDS if view in ("all", k)]
            kpis = {m: {"value": self.snap["kpis"][view][m], "series": [], "delta": None}
                    for m in ("indexed", "reachable", "verified", "ready")}
            html = tpl.render(request=Req, snap=self.snap, view=view, period=30, kpis=kpis,
                              headlines=stats.headlines(self.snap, view), history_days=1, first_day=None,
                              kinds=kinds, kind_label=stats.KIND_LABEL)
            self.assertIn("The state of agent tools", html)
            self.assertIn('"@type": "Dataset"', html)
            self.assertEqual("id=\"x402\"" in html, view in ("all", "x402"))
        html = tpl.render(request=Req, snap=None)
        self.assertIn("has not been computed yet", html)


class AgentAccessTests(unittest.TestCase):
    """The digest agents get through MCP, A2A and /api/v1/stats."""

    @classmethod
    def setUpClass(cls):
        StatsSnapshotTests.setUpClass()
        cls.snap = StatsSnapshotTests.snap

    @classmethod
    def tearDownClass(cls):
        StatsSnapshotTests.tearDownClass()

    def test_summary_scopes_and_is_small(self):
        s = stats.summary(self.snap)
        self.assertTrue(s["available"])
        self.assertEqual(s["kpis"]["all"]["indexed"], 7)
        self.assertEqual(set(s["facts"]), {"x402", "mcp", "a2a"})
        self.assertLess(len(json.dumps(s)), 8000)
        m = stats.summary(self.snap, "mcp")
        self.assertEqual(list(m["kpis"]), ["mcp"])
        self.assertEqual(set(m["facts"]), {"mcp"})
        self.assertEqual(stats.summary(self.snap, "bogus")["view"], "all")
        self.assertFalse(stats.summary(None)["available"])

    def test_a2a_stats_intent(self):
        from directory import a2a
        self.assertEqual(a2a._stats_request({}, "How many MCP servers are reachable?"), "mcp")
        self.assertEqual(a2a._stats_request({}, "what is the state of agent tools today"), "all")
        self.assertEqual(a2a._stats_request(
            {"parts": [{"kind": "data", "data": {"skill": "directory_stats", "view": "x402"}}]}, ""), "x402")
        self.assertIsNone(a2a._stats_request({}, "find a statistics API for sports data"))
        stats._latest_cache.update(at=time.time(), snap=self.snap)
        try:
            out = a2a._answer({"parts": [{"kind": "text", "text": "directory stats for x402"}]})
        finally:
            stats._latest_cache.update(at=0.0, snap=None)
        self.assertIn("3 indexed", out["text"])
        self.assertEqual(out["data"]["view"], "x402")
        skills = [s["id"] for s in a2a.own_agent_card()["skills"]]
        self.assertIn("directory_stats", skills)


if __name__ == "__main__":
    unittest.main()
