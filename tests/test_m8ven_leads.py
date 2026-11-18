"""M8ven leads: only listing URLs are kept; new listings are re-read from GitHub or the endpoint.
Offline: temporary database, fake m8ven.ai, GitHub search, classifier and MCP endpoints."""
import argparse
import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import code_classify, code_mcp, db, m8ven_leads as L, public_http  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402

BASE = "https://m8ven.ai/mcp/"
REAL_CLIENT = httpx.Client


def record(registry="hosted_endpoint", remote="https://mcp.example.com/mcp", github=None):
    return {"id": "0f8e", "name": "M8VEN NAME", "description": "M8VEN DESCRIPTION {beta}",
            "host_tools": [{"name": "lookup", "input": {"q": "string"}}],
            "latest_score": 91, "latest_grade": "A", "source_registry": registry,
            "remote_url": remote, "github_url": github}


def flight_page(rec):
    """A listing page as Next.js streams it: React flight rows split over two script chunks.
    A text row carries raw newlines and braces and runs straight into the next row."""
    text = 'Docs {see "below"}\nline two'
    flight = ('0:["$","html",null,{"lang":"en"}]\n'
              + "6:T%x,%s" % (len(text.encode()), text)
              + "5:" + json.dumps(["$", "$L7", None, {
                  "listing": rec, "related": [dict(rec, remote_url="https://other.example/mcp")]}])
              + "\n")
    cut = len(flight) // 2
    return "<html><body>" + "".join(
        "<script>self.__next_f.push([1,%s])</script>" % json.dumps(part)
        for part in (flight[:cut], flight[cut:])) + "</body></html>"


def gh_item(full, desc="An MCP server", topics=(), fork=False, archived=False):
    return {"full_name": full, "name": full.split("/")[1], "html_url": "https://github.com/" + full,
            "description": desc, "stargazers_count": 3, "topics": list(topics),
            "license": {"spdx_id": "MIT"}, "pushed_at": "2026-11-01T00:00:00Z",
            "created_at": "2026-10-01T00:00:00Z", "homepage": None, "fork": fork,
            "archived": archived}


class FakeM8ven:
    def __init__(self, listings, pages=None):
        self.listings, self.pages, self.seen = list(listings), dict(pages or {}), []

    def handle(self, request):
        url = str(request.url)
        self.seen.append(url)
        if url == L.SITEMAP_INDEX:
            return httpx.Response(200, text=(
                "<sitemapindex><sitemap><loc>https://m8ven.ai/sitemap-publishers/0</loc></sitemap>"
                "<sitemap><loc>https://m8ven.ai/mcp-sitemap/0</loc></sitemap>"
                "<sitemap><loc>https://elsewhere.example/mcp-sitemap/0</loc></sitemap></sitemapindex>"))
        if url == "https://m8ven.ai/mcp-sitemap/0":
            return httpx.Response(200, text="<urlset>" + "".join(
                "<url><loc>%s</loc><lastmod>2026-11-18</lastmod></url>" % u
                for u in self.listings) + "</urlset>")
        page = self.pages.get(url)
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, text=page) if page is not None else httpx.Response(404)

    def client(self, **kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(self.handle), **kwargs)

    def get(self):
        return L.paced(self.client(), sleep=lambda s: None)

    def pages_read(self):
        return [u for u in self.seen if u.startswith(BASE)]


class Parsing(unittest.TestCase):
    def test_the_page_record_gives_only_where_the_listing_lives(self):
        lead = L.page_lead(flight_page(record(github="https://github.com/Acme/Weather-MCP")))
        self.assertEqual(lead, {"registry": "hosted_endpoint",
                                "remote_url": "https://mcp.example.com/mcp",
                                "github_url": "https://github.com/Acme/Weather-MCP"})
        self.assertIsNone(L.page_lead("<html>no flight data</html>"))

    def test_endpoint_leads_exclude_registries_we_crawl_and_unusable_urls(self):
        ok = {"registry": "hosted_endpoint", "remote_url": "https://mcp.example.com/mcp"}
        self.assertEqual(L._endpoint_lead(ok), "https://mcp.example.com/mcp")
        for bad in (dict(ok, registry="smithery"), dict(ok, registry="official"),
                    dict(ok, remote_url="https://srv--acme.run.tools"),
                    dict(ok, remote_url="https://mcp.example.com/mcp?api_key=x"),
                    dict(ok, remote_url="https://{tenant}.example.com/mcp"),
                    dict(ok, remote_url=None), dict(ok, remote_url="ftp://mcp.example.com")):
            self.assertIsNone(L._endpoint_lead(bad), bad)


class Runs(_TempDB):
    def setUp(self):
        super().setUp()
        self.repos, self.searches, self.verdicts, self.answers, self.probed = {}, [], {}, {}, []

        def search(q, page, auth):
            names = [t[len("repo:"):] for t in q.split() if t.startswith("repo:")]
            self.searches += names
            return {"items": [self.repos[n] for n in names if n in self.repos]}, {}

        def endpoint(request):
            url = str(request.url)
            self.probed.append(url)
            answer = self.answers.get(url)
            if isinstance(answer, int) or answer is None:
                return httpx.Response(answer or 404, request=request)
            return httpx.Response(200, request=request, json={
                "jsonrpc": "2.0", "id": 1,
                "result": {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": answer}})

        self.enterContext(patch.object(code_mcp, "_github_search", side_effect=search))
        self.enterContext(patch.object(code_classify, "configured", return_value=True))
        self.enterContext(patch.object(code_classify, "_ask", side_effect=lambda rows: {
            i: self.verdicts.get(r["name"], "server") for i, r in enumerate(rows)}))
        self.enterContext(patch.object(public_http, "client", side_effect=lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(endpoint), **kw)))

    def run_once(self, site, **kw):
        return L.run(site.get(), sleep=lambda s: None, **kw)

    def test_only_new_listings_are_read_and_nothing_of_theirs_is_stored(self):
        old = [BASE + "acme/old-mcp", BASE + "old-endpoint"]
        L.record_baseline(old)
        site = FakeM8ven(old + [BASE + "acme/weather-mcp", BASE + "dant3-net"], {
            BASE + "dant3-net": flight_page(record(remote="https://dant3.net/mcp",
                                                   github="https://github.com/Dant3/Dant3-MCP"))})
        self.repos = {"acme/weather-mcp": gh_item("Acme/Weather-MCP"),
                      "dant3/dant3-mcp": gh_item("Dant3/Dant3-MCP")}
        self.answers = {"https://dant3.net/mcp": {"name": "dant3", "title": "Dant3 Tools",
                                                  "version": "1", "description": "Tools by Dant3",
                                                  "websiteUrl": "https://dant3.net"}}
        out = self.run_once(site)
        self.assertEqual(out["errors"], [])
        self.assertEqual((out["new_listings"], out["added"]), (2, 3))
        # owner/repo listings need no page; the listing on another host is not followed
        self.assertEqual(site.pages_read(), [BASE + "dant3-net"])
        self.assertNotIn("https://elsewhere.example/mcp-sitemap/0", site.seen)
        self.assertEqual(sorted(self.searches), ["acme/weather-mcp", "dant3/dant3-mcp"])
        self.assertEqual({r["source_id"] for r in self.q(
            "SELECT source_id FROM mcp_servers WHERE source='github'")},
            {"acme/weather-mcp", "dant3/dant3-mcp"})
        [own] = self.q("SELECT * FROM mcp_servers WHERE source='manual'")
        self.assertEqual((own["name"], own["description"], own["endpoint_url"], own["homepage_url"],
                          own["source_url"]),
                         ("Dant3 Tools", "Tools by Dant3", "https://dant3.net/mcp", "https://dant3.net",
                          None))
        self.assertNotIn("M8VEN", json.dumps(self.q("SELECT * FROM mcp_servers"), default=str))
        site.seen.clear()
        self.searches.clear()
        out = self.run_once(site)
        self.assertEqual((out["new_listings"], site.pages_read(), self.searches), (0, [], []))

    def test_listed_or_non_server_repositories_and_known_or_silent_endpoints_are_left_out(self):
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "known", "name": "Known",
                                     "endpoint_url": "https://known.example/mcp",
                                     "source_code_url": "https://github.com/acme/listed-mcp",
                                     "source": "mcp-registry", "source_id": "known"})
        L.record_baseline([BASE + "seed"])
        site = FakeM8ven([BASE + "seed", BASE + "acme/listed-mcp", BASE + "acme/weather-app",
                          BASE + "acme/forked-mcp", BASE + "acme/sdk-mcp", BASE + "smithery-one",
                          BASE + "known-host", BASE + "silent"], {
            BASE + "smithery-one": flight_page(record("smithery", "https://smithery.example/mcp")),
            BASE + "known-host": flight_page(record(remote="https://known.example/other")),
            BASE + "silent": flight_page(record(remote="https://silent.example/mcp"))})
        self.repos = {"acme/weather-app": gh_item("acme/weather-app", desc="A weather app"),
                      "acme/forked-mcp": gh_item("acme/forked-mcp", fork=True),
                      "acme/sdk-mcp": gh_item("acme/sdk-mcp")}
        self.verdicts = {"sdk-mcp": "sdk"}
        self.answers = {"https://silent.example/mcp": 401}
        out = self.run_once(site)
        self.assertEqual((out["errors"], out["added"]), ([], 0))
        self.assertNotIn("acme/listed-mcp", self.searches)
        self.assertEqual(self.probed, ["https://silent.example/mcp"])
        self.assertEqual(len(self.q("SELECT 1 FROM mcp_servers")), 1)
        self.assertEqual([L._pending(k) for k in ("listing", "repo", "endpoint")], [[], [], []])

    def test_a_429_stops_reading_and_the_rest_waits_for_the_next_run(self):
        L.record_baseline([BASE + "seed"])
        site = FakeM8ven([BASE + "seed", BASE + "a-one", BASE + "b-two"], {
            BASE + "a-one": flight_page(record(remote="https://a.example/mcp")), BASE + "b-two": 429})
        self.answers = {"https://a.example/mcp": {"name": "Alpha"}, "https://b.example/mcp": {"name": "Beta"}}
        out = self.run_once(site)
        self.assertTrue(any("429" in e for e in out["errors"]))
        self.assertEqual(L._pending("listing"), [BASE + "b-two"])
        site.pages[BASE + "b-two"] = flight_page(record(remote="https://b.example/mcp"))
        out = self.run_once(site)
        self.assertEqual((out["errors"], L._pending("listing")), ([], []))
        self.assertEqual({r["name"] for r in self.q("SELECT name FROM mcp_servers")}, {"Alpha", "Beta"})

    def test_pages_per_run_are_capped_and_unreadable_ones_are_dropped_after_three_tries(self):
        L.record_baseline([BASE + "seed"])
        urls = [BASE + "p%d" % i for i in range(3)]
        site = FakeM8ven([BASE + "seed"] + urls, {u: "<html>new layout</html>" for u in urls})
        out = self.run_once(site, max_pages=2)
        self.assertEqual(out["pages_read"], 2)
        self.assertEqual(out["errors"], ["2 listing pages unread or without a listing record"])
        for _ in range(3):
            self.run_once(site)
        self.assertEqual(L._pending("listing"), [])

    def test_no_baseline_or_a_flood_of_new_listings_stops_the_run(self):
        with self.assertRaises(L.LeadsError):
            self.run_once(FakeM8ven([BASE + "acme/a-mcp"]))
        L.record_baseline([BASE + "seed"])
        with patch.object(L, "MAX_NEW", 1), self.assertRaises(L.LeadsError):
            self.run_once(FakeM8ven([BASE + "seed", BASE + "acme/a-mcp", BASE + "acme/b-mcp"]))
        self.assertEqual(L._pending("listing"), [])

    def test_off_unless_switched_on_and_a_run_is_recorded(self):
        args = argparse.Namespace(baseline=None, dry_run=False, max_pages=L.MAX_PAGES)
        L.record_baseline([BASE + "seed"])
        site = FakeM8ven([BASE + "seed"])
        with patch.object(L.httpx, "Client", side_effect=site.client):
            with patch.dict(os.environ, {"AGENT_TOOLS_M8VEN_LEADS": "0"}):
                self.assertEqual(L.run_cli(args), 0)
            self.assertEqual((site.seen, self.q("SELECT * FROM crawl_runs")), ([], []))
            with patch.dict(os.environ, {"AGENT_TOOLS_M8VEN_LEADS": "1"}), \
                    patch.object(L.time, "sleep"):
                self.assertEqual(L.run_cli(args), 0)
        [run] = self.q("SELECT source, status FROM crawl_runs")
        self.assertEqual((run["source"], run["status"]), (L.RUN, "ok"))


if __name__ == "__main__":
    unittest.main()
