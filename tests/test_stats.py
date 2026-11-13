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


    def test_failures_and_usable_page(self):
        x = {r["label"]: r["count"] for r in self.snap["failures"]["x402"]}
        self.assertEqual(x[stats.FAILURE_LABELS["answered"]], 2)
        self.assertEqual(x[stats.FAILURE_LABELS["not_probed"]], 1)
        self.assertEqual(self.snap["failures"]["a2a"][0]["count"], 2)
        u = stats.usable(self.snap)
        self.assertTrue(u["available"])
        self.assertIn("of the 7 agent tools we index", u["answer"])
        self.assertEqual([r["kind"] for r in u["by_protocol"]], list(stats.KINDS))
        self.assertFalse(stats.usable(None)["available"])
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        env.globals.update(build_version="t", current_user=lambda r: None,
                           may_edit_listing=lambda *a: False, url_host=lambda u: u)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v

        class Req:
            class url:
                path = "/stats/usable"
        tpl = env.get_template("stats_usable.html")
        html = tpl.render(request=Req, u=u, snap=self.snap)
        self.assertIn("How many agent tools actually work?", html)
        self.assertIn('"FAQPage"', html)
        self.assertIn("Not probed yet", html)
        self.assertIn("has not been computed yet",
                      tpl.render(request=Req, u=stats.usable(None), snap=None))

    def test_peer_position(self):
        snap = {"as_of": "2026-11-11 06:00 UTC", "distributions": {"mcp": {
            "readiness_n": 21, "readiness": list(range(0, 101, 5)),
            "latency_n": 21, "latency_ms": [i * 100 for i in range(21)]}}}
        r = {"score": 72.5, "criteria": [
            {"key": "a", "label": "Auth", "points": 0, "max_points": 10, "detail": "Declare auth"},
            {"key": "b", "label": "Schema", "points": 8, "max_points": 10},
            {"key": "c", "label": "Card", "points": 0, "max_points": 5, "applicable": False},
            {"key": "d", "label": "Docs", "points": 5, "max_points": 5}]}
        p = stats.peer_position(snap, "mcp", r, 500)
        self.assertEqual(p["readiness"]["better_than_pct"], 72.5)
        self.assertEqual(p["readiness"]["peer_median"], 50)
        self.assertEqual(p["latency"]["faster_than_pct"], 75.0)
        self.assertEqual([g["key"] for g in p["biggest_gaps"]], ["a", "b"])
        self.assertIsNone(stats.peer_position(None, "mcp", r, 1))
        self.assertIsNone(stats.peer_position(snap, "x402", r, 1))
        self.assertEqual(stats._share_below([5] * 21, 5), 0.0)
        self.assertEqual(stats._share_below([5] * 21, 9), 100.0)
        d = self.snap["distributions"]["mcp"]
        self.assertEqual((d["latency_n"], len(d["latency_ms"])), (2, 21))
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v
        html = env.from_string('{% from "_macros.html" import peer_section %}{{ peer_section(p) }}').render(p=p)
        self.assertIn("Higher than 72%", html)
        self.assertIn("Faster than 75%", html)
        self.assertIn("Declare auth", html)


    def test_topic_page_and_monthly_report(self):
        t = stats.topic_page(self.snap, "weather")
        self.assertEqual(t["reachable"], 2)
        self.assertIn("mention \u201cweather\u201d", t["answer"])
        self.assertEqual({r["kind"] for r in t["by_kind"]}, set(stats.KINDS))
        self.assertIsNone(stats.topic_page(self.snap, "nope"))
        c = self.conn
        later = json.loads(json.dumps(self.snap))
        later["kpis"]["all"]["indexed"] += 3
        c.execute("INSERT OR REPLACE INTO stats_snapshots(day, generated_at, json) VALUES (?,?,?)",
                  ("2026-10-01", NOW, json.dumps(self.snap)))
        c.execute("INSERT OR REPLACE INTO stats_snapshots(day, generated_at, json) VALUES (?,?,?)",
                  ("2026-10-31", NOW, json.dumps(later)))
        c.commit()
        r = stats.monthly_report(c, "2026-10", now=time.mktime((2026, 11, 2, 0, 0, 0, 0, 0, 0)))
        self.assertTrue(r["complete"])
        self.assertEqual(r["days_covered"], 2)
        self.assertEqual(r["kpis"][0]["change"], 3)
        self.assertIn("October 2026", r["answer"])
        self.assertIn("2026-10", stats.months(c))
        self.assertIsNone(stats.monthly_report(c, "2025-01"))
        self.assertIsNone(stats.monthly_report(c, "bad"))
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        env.globals.update(build_version="t", current_user=lambda r: None,
                           may_edit_listing=lambda *a: False, url_host=lambda u: u)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v

        class Req:
            class url:
                path = "/stats/x"
        html = env.get_template("stats_report.html").render(request=Req, r=r, kind_label=stats.KIND_LABEL)
        self.assertIn("Agent tools in October 2026", html)
        html = env.get_template("stats_topic.html").render(request=Req, t=t, topics=list(stats.TOPICS))
        self.assertIn("Weather agent tools", html)
        self.assertIn("ref=topic", html)
        html = env.get_template("stats_reports.html").render(request=Req, months=["2026-10"], current="2026-11")
        self.assertIn("final", html)


class AccountTierTests(unittest.TestCase):
    """Without an account lists are shortened; with one they are complete."""

    @classmethod
    def setUpClass(cls):
        StatsSnapshotTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        StatsSnapshotTests.tearDownClass()

    def _snap(self):
        top = [{"kind": k, "slug": f"{k}{i}", "name": f"{k} {i}", "grade": "A", "readiness": 90 - i,
                "latency_ms": 100} for k in stats.KINDS for i in range(8)]
        terms = [{"term": f"t{i}", "searches": 50 - i, "no_results_pct": 60.0} for i in range(30)]
        return {"as_of": "2026-11-12 06:00 UTC",
                "topics": {"rows": [{"topic": "weather", "x402": 5, "mcp": 5, "a2a": 5,
                                     "x402_pct": 1.0, "mcp_pct": 1.0, "a2a_pct": 1.0}],
                           "gaps": [], "detail": {"weather": {
                               "by_kind": {k: {"reachable": 5, "ready": 2, "median_readiness": 80,
                                               "median_latency_ms": 100} for k in stats.KINDS},
                               "top": top}}},
                "search_demand": {"days": 30, "searches": 900, "min_searches": 5,
                                  "top": terms, "unmet": terms[:12]}}

    def test_public_and_full(self):
        snap = self._snap()
        pub = stats.public_snapshot(snap)
        self.assertEqual(len(pub["topics"]["detail"]["weather"]["top"]), 9)
        self.assertEqual(len(snap["topics"]["detail"]["weather"]["top"]), 24)  # original untouched
        self.assertEqual(len(pub["search_demand"]["top"]), 10)
        self.assertEqual(pub["search_demand"]["more_with_account"], 20 + 2)
        self.assertIn("sign_in", pub["search_demand"])
        full = stats.searches_view(snap, full=True)
        self.assertEqual((len(full["top"]), full["more_with_account"]), (30, 0))
        t = stats.topic_page(snap, "weather")
        self.assertEqual((len(t["top"]), t["full"]), (9, False))
        self.assertGreater(t["more_with_account"], 0)
        tf = stats.topic_page(snap, "weather", full=True)
        self.assertEqual((len(tf["top"]), tf["more_with_account"]), (24, 0))
        s = stats.summary({**StatsSnapshotTests.snap, "search_demand": snap["search_demand"]})
        self.assertEqual(len(s["searches"]["top"]), 10)
        s = stats.summary({**StatsSnapshotTests.snap, "search_demand": snap["search_demand"]}, full=True)
        self.assertEqual(len(s["searches"]["top"]), 30)


class SearchTermTests(unittest.TestCase):
    def setUp(self):
        from directory import search_terms
        self.st = search_terms
        with search_terms._lock:  # searches made by other tests
            search_terms._pending.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("AGENT_TOOLS_SEARCH_TERMS_DB")
        os.environ["AGENT_TOOLS_SEARCH_TERMS_DB"] = os.path.join(self.tmp.name, "st.db")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AGENT_TOOLS_SEARCH_TERMS_DB", None)
        else:
            os.environ["AGENT_TOOLS_SEARCH_TERMS_DB"] = self._old
        self.tmp.cleanup()

    def test_normalize_drops_personal_terms(self):
        n = self.st.normalize
        self.assertEqual(n("  Weather   API! "), "weather api")
        for ok in ("key-value store", "claude-3-5-sonnet", "github.com", "c++", "x402 payment"):
            self.assertEqual(n(ok), ok)
        for bad in ("a", "me@x.com", "https://x.io", "0xabcdef1234", "order 123456", "x" * 70, None, "",
                    "example.com/path", "10.0.0.1", "138-1234-5678", "415.555.0101", "sk-abc123def",
                    "ghp_ab12cd34", "a1b2c3d4e5f6g7h8i9j0k1l2m3",
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0", "me\uff20x.com", "+86 13812345678"):
            self.assertIsNone(n(bad), bad)

    def test_record_flush_and_threshold(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for _ in range(5):
            self.st.record("mcp", "Weather", 3, "web")
        for _ in range(6):
            self.st.record("x402", "flight booking", 0, "api")
        self.st.record("mcp", "rare term", 0, "mcp")
        self.st.record("mcp", "me@x.com", 0, "mcp")
        self.st.flush()
        s = self.st.summary(today, today)
        self.assertEqual(s["searches"], 12)
        self.assertEqual([i["term"] for i in s["top"]], ["flight booking", "weather"])
        self.assertEqual([i["term"] for i in s["unmet"]], ["flight booking"])
        self.assertEqual(s["by_channel"], {"api": 6, "mcp": 1, "web": 5})
        self.assertNotIn("rare term", json.dumps(s))
        self.assertEqual(self.st.prune(time.time() + 500 * 86400), 3)


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
        from directory import a2a, a2a_research
        parse = a2a_research.parse_request
        self.assertEqual(parse({}, "How many MCP servers are reachable?"),
                         {"skill": "ecosystem_report", "view": "mcp"})
        self.assertEqual(parse({}, "what is the state of agent tools today")["skill"], "ecosystem_report")
        self.assertEqual(parse({"parts": [{"kind": "data", "data": {"skill": "directory_stats", "view": "x402"}}]}, ""),
                         {"skill": "ecosystem_report", "view": "x402"})
        self.assertEqual(parse({}, "find a statistics API for sports data"), {"skill": "tool_request"})
        stats._latest_cache.update(at=time.time(), snap=self.snap)
        try:
            out = a2a._answer({"parts": [{"kind": "text", "text": "directory stats for x402"}]})
        finally:
            stats._latest_cache.update(at=0.0, snap=None)
        self.assertIn("3 indexed", out["text"])
        self.assertEqual(out["data"]["view"], "x402")
        self.assertEqual(out["data"]["license"], "CC BY 4.0")
        skills = [s["id"] for s in a2a.own_agent_card()["skills"]]
        self.assertIn("ecosystem_report", skills)
        self.assertNotIn("search_x402_services", skills)

if __name__ == "__main__":
    unittest.main()
