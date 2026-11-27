"""Public /stats snapshot: counts, CSV rows, headlines and page rendering."""
import calendar
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
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


class FunnelNestingTests(unittest.TestCase):
    def test_each_step_keeps_only_the_one_before_and_ready_counts_every_a_or_b(self):
        # MCP: B behind OAuth (401) and B with initialize only are ready, not verified.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            db.init_db(path)
            c = sqlite3.connect(path)
            for i, (health, conf, code, grade) in enumerate([
                    ("ok", "pass", 200, "A"), ("ok", "fail", 401, "B"), ("ok", "partial", 200, "B"),
                    ("degraded", "fail", 404, "D")], start=1):
                c.execute("INSERT INTO mcp_servers(id, slug, name, description, endpoint_url, health, conformance, "
                          "http_status, readiness_grade, created_at, updated_at, source) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,'t')",
                          (i, f"m{i}", f"m{i}", "d", f"https://m{i}.example/mcp", health, conf, code, grade, NOW, NOW))
            for i, (ok, grade) in enumerate([(1, "A"), (0, "B")], start=1):
                c.execute("INSERT INTO services(id, slug, name, url, description, health, x402_ok, readiness_grade, "
                          "created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,'t')",
                          (i, f"s{i}", f"s{i}", f"https://s{i}.example", "d", "ok", ok, grade, NOW, NOW))
            c.commit()
            f = stats.funnel(c)
            kpis = stats.headline_kpis(f, stats.readiness(c))
            c.close()
        self.assertEqual([s["count"] for s in f["mcp"]], [4, 4, 3, 1, 1])
        self.assertEqual([s["count"] for s in f["x402"]], [2, 2, 2, 1, 1])
        self.assertTrue(all(s["pct_prev"] <= 100 for kind in stats.KINDS for s in f[kind]))
        self.assertEqual((kpis["mcp"]["verified"], kpis["mcp"]["ready"]), (1, 3))
        self.assertEqual((kpis["x402"]["ready"], kpis["all"]["ready"]), (2, 5))


class CodeOnlyTests(unittest.TestCase):
    """Code-only MCP (no remote endpoint): profile figures and the /stats section."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "t.db")
        db.init_db(path)
        c = sqlite3.connect(path)

        def pushed(days):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days * DAY))
        rows = [
            ("big", "Big Server", "github code search", "github", None, 1500, pushed(3), "MIT",
             NOW - 2 * DAY, "https://github.com/acme/big"),
            ("mid", "Mid Server", "weather data", "npm", "npm", 150, pushed(60), "Apache-2.0",
             NOW - 20 * DAY, "https://github.com/acme/mid"),
            ("small", "Small", "pdf tools", "pypi", "pypi", None, pushed(400), "", NOW - 40 * DAY, None),
            ("old", "Old repo", "email calendar", "github", None, 5, pushed(200), "NOASSERTION",
             calendar.timegm((2023, 5, 1, 0, 0, 0)), "https://github.com/acme/old"),
            # Names more than five topics: a catalogue, left out of the topic shares.
            ("cat", "Catalogue", "search web data database finance payment", "cargo", "cargo", None, None,
             "MIT", NOW - 100 * DAY, "https://github.com/acme/cat"),
            # No date from the source: created_at is the moment it was stored (first_seen below).
            ("ingest", "Ingest Stamp", "for Claude Code and AI agents", "smithery", None, None, None,
             "MIT License", NOW - 3 * DAY, None),
            # NuGet placeholders (1900); stars from the adoption crawl of its repository.
            ("nuget", "NuGet One", "data tools", "nuget", "nuget", None, "1900-01-01T00:00:00Z", "mit",
             calendar.timegm((1900, 1, 1, 0, 0, 0)), "https://github.com/Acme/NuGet-One"),
        ]
        packages = {"mid": "@acme/mid", "small": "small", "cat": "copycat-tools", "nuget": "Acme.NuGet-One"}
        for i, (slug, name, desc, source, reg, stars, pushed_at, lic, created, url) in enumerate(rows, start=1):
            c.execute("INSERT INTO mcp_servers(id, slug, name, description, source, source_id, package_registry, "
                      "package_name, github_stars, repo_pushed_at, license, created_at, updated_at, source_code_url, "
                      "kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'catalog')",
                      (i, slug, name, desc, source, slug, reg, packages.get(slug), stars, pushed_at, lic, created,
                       NOW, url))
        c.execute("INSERT INTO listing_sources(kind, listing_id, source, first_seen, last_seen) "
                  "VALUES ('mcp', 6, 'smithery', ?, ?)", (NOW - 3 * DAY, NOW))
        c.execute("INSERT INTO listing_sources(kind, listing_id, source, first_seen, last_seen) "
                  "VALUES ('mcp', 1, 'github', ?, ?)", (NOW - DAY, NOW))
        # The NuGet package is its repository's own and takes its stars; the Cargo
        # package only copies acme/cat's URL and does not.
        c.execute("INSERT INTO ext_github_repos(repo, full_name, stars, status, fetched_at) "
                  "VALUES ('acme/nuget-one', 'Acme/NuGet-One', 42, 'ok', ?), ('acme/cat', 'acme/cat', 7, 'ok', ?)",
                  (NOW, NOW))
        # A hosted listing of the same server as "big": both are listed.
        c.execute("INSERT INTO mcp_servers(id, slug, name, description, endpoint_url, health, conformance, github_stars, "
                  "created_at, updated_at, source, source_code_url) VALUES (9, 'remote', 'Remote', 'search', "
                  "'https://r.example/mcp', 'ok', 'pass', 99999, ?, ?, 't', 'https://github.com/acme/big')",
                  (NOW, NOW))
        c.commit()
        c.row_factory = sqlite3.Row
        cls.co = stats.code_only(c, NOW)
        cls.snap = stats.compute(c, NOW)
        c.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_profile(self):
        co = self.co
        as_map = lambda rows: {r["label"]: r["count"] for r in rows}  # noqa: E731
        self.assertEqual(co["count"], 7)
        self.assertEqual(as_map(co["by_source"]),
                         {"GitHub": 2, "npm": 1, "PyPI": 1, "Cargo": 1, "Smithery": 1, "NuGet": 1})
        self.assertEqual((co["with_repo"], co["with_repo_pct"], co["also_hosted"]), (5, 71.4, 1))
        self.assertEqual(as_map(co["packages"]), {"npm": 1, "PyPI": 1, "Cargo": 1, "NuGet": 1})
        self.assertEqual(as_map(co["star_bands"]),
                         {"0": 0, "1–9": 1, "10–99": 1, "100–999": 1, "1k–9.9k": 1, "10k+": 0})
        self.assertEqual((co["with_stars"], co["no_github_data"], co["stars_100"]), (4, 3, 2))
        # 1900 push dates are placeholders: unknown, not "over a year".
        self.assertEqual(as_map(co["activity"]),
                         {"Last 30 days": 1, "31–90 days": 1, "91–365 days": 1, "Over a year": 1, "Unknown": 3})
        self.assertEqual((co["active_90d"], co["active_90d_pct"]), (2, 28.6))
        self.assertEqual(as_map(co["licenses"]), {"MIT": 4, "Apache-2.0": 1, "Not stated": 1, "Unrecognised": 1})
        # The stored-moment date (3 days ago) and the 1900 placeholder are neither new nor old.
        self.assertEqual((co["new_7d"], co["new_30d"], co["created_earlier"], co["created_unknown"]),
                         (1, 2, 1, 2))
        months = co["created_monthly"]
        self.assertEqual((months[0][0], months[-1][0]), ("2024-11", time.strftime("%Y-%m", time.gmtime(NOW))))
        self.assertEqual(sum(n for _m, n in months), 4)
        t = co["topics"]
        self.assertEqual((t["counted"], t["skipped_catalogue"]), (6, 1))
        self.assertEqual((as_map(t["rows"])["search"], as_map(t["rows"])["code"]), (1, 1))
        self.assertNotIn("database", as_map(t["rows"]))
        self.assertNotIn("ai", as_map(t["rows"]))          # "Claude Code and AI agents" names no topic
        self.assertEqual([r["slug"] for r in co["top_repos"]], ["big", "mid", "nuget", "old"])
        self.assertEqual(co["top_repos"][0], {"slug": "big", "name": "Big Server", "repo": "acme/big", "stars": 1500})
        self.assertEqual(co["top_repos"][2]["repo"], "Acme/NuGet-One")

    def test_licence_labels_and_topic_noise(self):
        text = ("MIT License\n\nCopyright (c) 2025 X\n\nPermission is hereby granted, free of charge, "
                "to any person obtaining a copy")
        for raw, want in [("MIT License", "MIT"), ("mit", "MIT"), (text, "MIT"), ("apache-2.0", "Apache-2.0"),
                          ("Apache Software License", "Apache-2.0"), ("AGPL-3.0-or-later", "AGPL-3.0"),
                          ("GPL-3.0-only", "GPL-3.0"), ("Apache-2.0 OR MIT", "MIT OR Apache-2.0"),
                          ("(MIT OR Apache-2.0)", "MIT OR Apache-2.0"), ("PROPRIETARY", "Proprietary"),
                          ("UNLICENSED", "UNLICENSED"), ("unlicense", "Unlicense"), ("NOASSERTION", "Unrecognised"),
                          ("SEE LICENSE IN LICENSE.md", "See license file"), ("", "Not stated"), (None, "Not stated"),
                          ("CC-BY-NC-4.0", "CC-BY-NC-4.0")]:
            self.assertEqual(stats._licence(raw), want, raw)
        self.assertEqual(stats._topic_hits("Notes", "An MCP server for Claude Code and VS Code"), [])
        self.assertEqual(stats._topic_hits("Repo tools", "Lets AI agents search GitHub issues"), ["search", "github"])
        self.assertEqual(stats._topic_hits("Helper", "Works with GitHub Copilot"), [])
        self.assertEqual(stats._topic_hits("Code review", "AI-powered code review"), ["code", "ai"])

    def test_snapshot_csv_and_headline(self):
        self.assertEqual(self.snap["kpis"]["mcp"]["code_only"], 7)
        self.assertEqual(self.snap["code_only"]["mcp"]["with_package"], 4)
        self.assertIn(("mcp_code", "with_package", 4), stats.flatten(self.snap))
        self.assertEqual(stats.headlines(self.snap)["code_only"],
                         "7 MCP servers are published as code you install and run yourself; "
                         "28.6% had a push or release in the last 90 days.")
        json.dumps(self.snap)

    def test_section_renders_for_all_and_mcp(self):
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        env.globals.update(build_version="t", current_user=lambda r: None,
                           may_edit_listing=lambda *a: False, url_host=lambda u: u)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v
        tpl = env.get_template("stats.html")

        class Req:
            class url:
                path = "/stats"

        def render(snap, view):
            kpis = {m: {"value": snap["kpis"][view][m], "series": [], "delta": None}
                    for m in ("indexed", "reachable", "verified", "ready")}
            return tpl.render(request=Req, snap=snap, view=view, period=30, kpis=kpis,
                              headlines=stats.headlines(snap, view), history_days=1, first_day=None,
                              kinds=[k for k in stats.KINDS if view in ("all", k)], kind_label=stats.KIND_LABEL)
        for view in ("all", "mcp", "x402", "a2a"):
            html = render(self.snap, view)
            self.assertEqual('id="code-only"' in html, view in ("all", "mcp"), view)
        html = render(self.snap, "all")
        self.assertIn("7 MCP servers are published as code you install and run yourself", html)
        self.assertIn("1 of them also have a hosted endpoint", html)
        self.assertIn("GitHub stars (4 of 5 with a GitHub link)", html)
        self.assertIn("2 have no reliable date and are left out", html)
        self.assertIn(time.strftime("%Y-%m", time.gmtime(NOW)) + " (to date)", html)
        self.assertIn('href="/mcp/code/big"', html)
        self.assertIn('href="#code-only">12 Code-only', html)
        self.assertIn('href="#searches">13 Searches', html)
        self.assertIn('id="method-code-only"', html)
        empty = dict(self.snap, code_only={"mcp": {"count": 0}})
        self.assertNotIn('id="code-only"', render(empty, "all"))
        self.assertNotIn("12 Code-only", render(empty, "all"))


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
            self.assertNotIn("grade A or B without", html)
        kpis = {m: {"value": self.snap["kpis"]["all"][m], "series": [], "delta": None}
                for m in ("indexed", "reachable", "verified", "ready")}
        snap = dict(self.snap, ready_beyond_funnel={"x402": 0, "mcp": 2, "a2a": 0})
        html = tpl.render(request=Req, snap=snap, view="all", period=30, kpis=kpis,
                          headlines=stats.headlines(snap, "all"), history_days=1, first_day=None,
                          kinds=list(stats.KINDS), kind_label=stats.KIND_LABEL)
        self.assertIn("Another 2 grade A or B without", html)
        self.assertIn("a full handshake: servers behind OAuth", html)
        self.assertEqual(html.count("grade A or B without"), 1)
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

    def test_peer_gap_for_auth_gated_item(self):
        snap = {"as_of": "2026-11-11 06:00 UTC", "distributions": {"mcp": {
            "readiness_n": 21, "readiness": list(range(0, 101, 5)), "latency_n": 0, "latency_ms": []}}}
        r = {"score": 40, "criteria": [
            {"key": "skills", "label": "Tool declaration", "points": 0, "max_points": 10,
             "detail": "Requires authentication (HTTP 401): …", "auth_gated": True},
            {"key": "protocol_version", "label": "Protocol revision", "points": 0, "max_points": 10}]}
        gaps = {g["key"]: g for g in stats.peer_position(snap, "mcp", r)["biggest_gaps"]}
        self.assertEqual(gaps["skills"]["fix"], stats.GAP_FIXES["auth_gated"])
        self.assertEqual(gaps["protocol_version"]["fix"], stats.GAP_FIXES["protocol_version"])
        r["criteria"][0]["fix"] = "Publish the metadata."
        gaps = {g["key"]: g for g in stats.peer_position(snap, "mcp", r)["biggest_gaps"]}
        self.assertEqual(gaps["skills"]["fix"], "Publish the metadata.")


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
            search_terms._pending_tags.clear()
        search_terms._salt_cache = search_terms._script_key_cache = None
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

    BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

    def test_record_flush_and_threshold(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for i in range(5):  # five people
            self.st.record("mcp", "Weather", 3, "web", client=(f"10.0.0.{i}", self.BROWSER))
        for i in range(6):  # three agents, twice each
            self.st.record("x402", "flight booking", 0, "api", client=(f"10.1.0.{i}", f"agent-{i % 3}/1.0"))
        for _ in range(40):  # one script hammering a term from many addresses
            self.st.record("mcp", "council of ai", 1, "api", client=(f"10.2.0.{_}", "csoai-presence/1.0"))
        self.st.record("mcp", "rare term", 0, "mcp", client=("10.3.0.1", "claude-ai"))
        self.st.record("mcp", "me@x.com", 0, "mcp", client=("10.3.0.1", "claude-ai"))
        for _ in range(9):  # crawlers following our links are not demand
            self.st.record("mcp", "weather", 3, "web", client=("10.4.0.1", "Mozilla/5.0 (compatible; GPTBot/1.2)"))
        self.st.flush()
        s = self.st.summary(today, today)
        self.assertEqual(s["searches"], 52)
        self.assertEqual(s["searchers"], 5 + 3 + 1 + 1)
        self.assertEqual([(i["term"], i["searchers"], i["searches"]) for i in s["top"]],
                         [("weather", 5, 5), ("flight booking", 3, 6)])
        self.assertEqual([i["term"] for i in s["unmet"]], ["flight booking"])
        self.assertEqual(s["by_channel"], {"api": 46, "mcp": 1, "web": 5})
        self.assertNotIn("rare term", json.dumps(s))
        self.assertNotIn("council of ai", json.dumps(s))
        self.assertEqual(self.st.prune(time.time() + 500 * 86400), 4)
        c = sqlite3.connect(self.st.db_path())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM search_searchers").fetchone()[0], 0)
        c.close()

    def test_no_client_details_are_stored(self):
        self.st.record("mcp", "weather", 3, "web", client=("203.0.113.9", self.BROWSER))
        self.st.flush()
        with open(self.st.db_path(), "rb") as f:
            raw = f.read()
        self.assertNotIn(b"203.0.113.9", raw)
        self.assertNotIn(b"Chrome", raw)
        c = sqlite3.connect(self.st.db_path())
        cols = [r[1] for r in c.execute("PRAGMA table_info(search_searchers)")]
        tag = c.execute("SELECT tag FROM search_searchers").fetchone()[0]
        c.close()
        self.assertEqual(cols, ["day", "term", "tag"])
        self.assertRegex(tag, r"^d[0-9a-f]{16}$")

    def test_daily_key_is_shared_and_replaced(self):
        self.st._salt_cache = None
        k1 = self.st._salt("2026-11-26")
        self.st._salt_cache = None  # another process reads the same key
        self.assertEqual(self.st._salt("2026-11-26"), k1)
        k2 = self.st._salt("2026-11-27")
        self.assertNotEqual(k1, k2)
        with open(self.st.db_path() + ".key") as f:
            content = f.read()
        self.assertNotIn(k1.hex(), content)  # yesterday's key is gone
        self.assertEqual(oct(os.stat(self.st.db_path() + ".key").st_mode & 0o777), "0o600")
        key = self.st.client_key("web", "10.0.0.1", self.BROWSER)
        self.assertNotEqual(self.st.tag_for("2026-11-26", key, k1), self.st.tag_for("2026-11-27", key, k2))
        script = self.st.client_key("api", "10.0.0.1", "stock-trends-agent/2.0")
        self.assertEqual(self.st.tag_for("2026-11-26", script, k1), self.st.tag_for("2026-11-27", script, k2))
        self.assertTrue(self.st.tag_for("2026-11-26", script).startswith("s"))
        self.st._salt_cache = None

    def test_daily_script_counts_once_per_window(self):
        rows = []
        for day in ("2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"):
            rows.append((day, "sector breadth", self.st.tag_for(day, self.st.client_key("api", "1.1.1.1", "trends-agent/1"))))
        for i, day in enumerate(("2026-11-20", "2026-11-21", "2026-11-22")):
            rows.append((day, "weather", self.st.tag_for(day, self.st.client_key("web", f"2.2.2.{i}", self.BROWSER))))
        self.st._write({("2026-11-20", "mcp", "api", "sector breadth", "n"): 40,
                        ("2026-11-20", "mcp", "web", "weather", "n"): 6}, rows)
        s = self.st.summary("2026-11-01", "2026-11-30", min_searchers=1, min_n=1)
        self.assertEqual([(i["term"], i["searchers"]) for i in s["top"]], [("weather", 3), ("sector breadth", 1)])
        self.assertEqual(s["searchers"], 4)

    def test_client_key_rules(self):
        ck = self.st.client_key
        self.assertIsNone(ck("web", "1.1.1.1", "Mozilla/5.0 (compatible; MJ12bot/v1.4.8; http://mj12bot.com/)"))
        self.assertIsNone(ck("api", "1.1.1.1", "Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)"))
        self.assertIsNone(ck("web", "1.1.1.1", "Epochal-Search/1.0"))
        self.assertEqual(ck("api", "1.1.1.1", "node"), ck("api", "2.2.2.2", "node"))  # script: by UA
        self.assertNotEqual(ck("web", "1.1.1.1", self.BROWSER), ck("web", "2.2.2.2", self.BROWSER))
        self.assertNotEqual(ck("mcp", "1.1.1.1", "claude-ai"), ck("mcp", "2.2.2.2", "claude-ai"))
        self.assertEqual(ck("api", "1.1.1.1", "Mozilla/5.0 (X11; Linux) HeadlessChrome/120"),
                         ck("api", "9.9.9.9", "Mozilla/5.0 (X11; Linux) HeadlessChrome/120"))

    def test_backfill_from_access_log(self):
        log_lines = [
            f'10.0.0.{i} - 10.0.0.{i} [20/Nov/2026:10:00:00 +0000] "GET /mcp?q=Weather HTTP/1.1" 200 5 cc=- rt=0.1 '
            f'ua="{self.BROWSER}"\n' for i in range(4)
        ] + [
            '10.0.0.1 - x [20/Nov/2026:10:01:00 +0000] "GET /mcp?q=weather HTTP/1.1" 200 5 ua="' + self.BROWSER + '"\n',
            '10.9.0.1 - x [20/Nov/2026:10:02:00 +0000] "GET /mcp?q=weather HTTP/1.1" 200 5 ua="Mozilla/5.0 (compatible; GPTBot/1.2)"\n',
            '10.9.0.2 - x [20/Nov/2026:10:02:00 +0000] "GET /mcp?q=weather&ref=topic HTTP/1.1" 200 5 ua="' + self.BROWSER + '"\n',
            '10.9.0.3 - x [20/Nov/2026:10:02:00 +0000] "GET /api/v1/search?q=weather&offset=20 HTTP/1.1" 200 5 ua="node"\n',
            '10.9.0.4 - x [20/Nov/2026:10:02:00 +0000] "GET /api/v1/search?q=weather HTTP/1.1" 200 5 ua="node"\n',
            '10.9.0.5 - x [20/Nov/2026:10:02:00 +0000] "GET /api/v1/search?q=weather HTTP/1.1" 200 5 ua="node"\n',
            '10.9.0.6 - x [21/Nov/2026:10:02:00 +0000] "GET /api/v1/search?q=weather HTTP/1.1" 500 5 ua="node"\n',
            '10.9.0.7 - x [25/Oct/2026:10:02:00 +0000] "GET /api/v1/search?q=weather HTTP/1.1" 200 5 ua="node"\n',
        ]
        path = os.path.join(self.tmp.name, "access.log")
        with open(path, "w") as f:
            f.writelines(log_lines)
        self.assertEqual(self.st.backfill_from_logs([path], "2026-11-01", "2026-11-26"), 5)
        self.assertEqual(self.st.backfill_from_logs([path], "2026-11-01", "2026-11-26"), 5)  # rerun replaces
        c = sqlite3.connect(self.st.db_path())
        rows = c.execute("SELECT day, term, COUNT(*) FROM search_searchers GROUP BY day, term").fetchall()
        c.close()
        self.assertEqual(rows, [("2026-11-20", "weather", 5)])


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
            # Template path: never call the real model from a unit test.
            with patch.dict(os.environ, {"AGENT_TOOLS_A2A_LLM": "0"}):
                out = a2a._answer({"parts": [{"kind": "text", "text": "directory stats for x402"}]})
        finally:
            stats._latest_cache.update(at=0.0, snap=None)
        self.assertIn("3 indexed", out["text"])
        self.assertEqual(out["data"]["view"], "x402")
        self.assertEqual(out["data"]["license"], "CC BY 4.0")
        skills = [s["id"] for s in a2a.own_agent_card()["skills"]]
        self.assertIn("ask_analyst", skills)
        self.assertNotIn("search_x402_services", skills)

if __name__ == "__main__":
    unittest.main()
