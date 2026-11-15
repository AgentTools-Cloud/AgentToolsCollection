"""Code-only MCP sub-directory (/mcp/code): crawler merge, scope split, pages,
statistics. Offline: temporary database, no network."""
import time
from contextlib import closing
from datetime import date
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_resources_search_and_payto_limit import _TempDB
from directory import code_mcp, db, jobs, routes, stats


def gh_item(full, stars=50, desc="An MCP server", topics=("mcp-server",), lic="MIT",
            pushed="2026-11-01T00:00:00Z", homepage=None):
    return {"full_name": full, "html_url": f"https://github.com/{full}", "description": desc,
            "stargazers_count": stars, "topics": list(topics), "license": {"spdx_id": lic},
            "pushed_at": pushed, "homepage": homepage}


def npm_obj(name, repo=None, desc="npm MCP server"):
    links = {"npm": f"https://www.npmjs.com/package/{name}"}
    if repo:
        links["repository"] = f"git+https://github.com/{repo}.git"
    return {"package": {"name": name, "description": desc, "links": links,
                        "keywords": ["mcp", "mcp-server"], "license": "MIT",
                        "date": "2026-10-01T00:00:00Z"}}


class MergeTests(_TempDB):
    def test_github_row_wins_and_takes_its_npm_package(self):
        rows, skipped = code_mcp.merge(
            [gh_item("Acme/Fs-MCP"), gh_item("acme/other")],
            [npm_obj("@acme/fs-mcp", repo="acme/fs-mcp"),
             npm_obj("solo-mcp"),
             npm_obj("@acme/fs-mcp-2", repo="acme/fs-mcp")],
            (set(), set()))
        by_id = {r["source_id"]: r for r in rows}
        self.assertEqual(set(by_id), {"acme/fs-mcp", "acme/other", "solo-mcp"})
        fs = by_id["acme/fs-mcp"]
        self.assertEqual((fs["package_registry"], fs["package_name"]), ("npm", "@acme/fs-mcp"))
        self.assertEqual(fs["kind"], "catalog")
        self.assertIsNone(fs["endpoint_url"])
        self.assertEqual(fs["license"], "MIT")
        self.assertEqual(by_id["solo-mcp"]["source"], "npm")
        self.assertEqual(skipped["duplicate_package"], 1)

    def test_repositories_owned_by_other_listings_are_skipped(self):
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "remote", "name": "Remote",
                                     "endpoint_url": "https://r.example/mcp",
                                     "source_code_url": "https://github.com/acme/remote",
                                     "source": "mcp-registry", "source_id": "r"})
            db.upsert_mcp_server(c, {"slug": "own", "name": "Own", "endpoint_url": None,
                                     "kind": "catalog",
                                     "source_code_url": "https://github.com/acme/own",
                                     "source": "github", "source_id": "acme/own"})
        with closing(db.connect(read_only=True)) as c:
            known = code_mcp.known_repos(c)
        self.assertIn("acme/remote", known[0])
        self.assertNotIn("acme/own", known[0])   # our own rows are refreshed, not skipped
        rows, skipped = code_mcp.merge([gh_item("acme/remote"), gh_item("acme/own")],
                                       [npm_obj("x", repo="acme/remote")], known)
        self.assertEqual([r["source_id"] for r in rows], ["acme/own"])
        self.assertEqual(skipped["linked_elsewhere"], 2)

    def test_date_ranges_split_until_under_the_search_cap(self):
        counts = {}

        def count(a, b):
            n = (b - a).days * 300 + 300
            counts[(a, b)] = n
            return n
        ranges = list(code_mcp._date_ranges(date(2026, 1, 1), date(2026, 1, 8), count))
        self.assertTrue(all(n <= code_mcp.SEARCH_CAP or a == b for a, b, n in ranges))
        days = sum((b - a).days + 1 for a, b, _n in ranges)
        self.assertEqual(days, 8)


class IncrementalTests(_TempDB):
    def _run(self, gh, npm=(), created=None, **kw):
        created = created or {}
        with patch.object(code_mcp, "fetch_github", return_value=list(gh)) as fg, \
                patch.object(code_mcp, "fetch_npm", return_value=list(npm)), \
                patch.object(code_mcp, "npm_created",
                             side_effect=lambda names: {n: created[n] for n in names if n in created}):
            jobs.cmd_crawl_code(**kw)
        return fg.call_args.kwargs

    def test_first_run_is_full_then_incremental_since_last_run(self):
        args = self._run([gh_item("acme/a")])
        self.assertIsNone(args["pushed_since"])
        args = self._run([gh_item("acme/b")])
        self.assertIsNotNone(args["pushed_since"])
        self.assertLessEqual(args["pushed_since"], date.today())
        args = self._run([], full=True)
        self.assertIsNone(args["pushed_since"])
        with closing(db.connect(read_only=True)) as c:
            runs = [r[0] for r in c.execute("SELECT source FROM crawl_runs ORDER BY id")]
        self.assertEqual(runs, [jobs.CODE_FULL_RUN, jobs.CODE_INCR_RUN, jobs.CODE_FULL_RUN])

    def test_a_bounded_run_is_not_a_baseline(self):
        args = self._run([gh_item("acme/a")], min_stars=100)
        self.assertIsNone(args["pushed_since"])
        args = self._run([gh_item("acme/b")])
        self.assertIsNone(args["pushed_since"])   # still no full baseline

    def test_rows_are_dated_by_creation_not_by_import(self):
        old = dict(gh_item("acme/old"), created_at="2025-03-01T00:00:00Z")
        self._run([old], [npm_obj("solo-mcp")], created={"solo-mcp": 1735689600})
        with closing(db.connect(read_only=True)) as c:
            got = dict(c.execute("SELECT source_id, created_at FROM mcp_servers "
                                 "WHERE source IN ('github','npm')").fetchall())
        self.assertEqual(got["acme/old"], 1740787200)
        self.assertEqual(got["solo-mcp"], 1735689600)

    def test_rows_imported_today_are_backdated_on_the_next_run(self):
        self._run([gh_item("acme/old")])            # no creation date: stamped now
        old = dict(gh_item("acme/old"), created_at="2025-03-01T00:00:00Z")
        self._run([old], full=True)
        with closing(db.connect(read_only=True)) as c:
            ts = c.execute("SELECT created_at FROM mcp_servers WHERE source_id='acme/old'").fetchone()[0]
            snap = stats.compute(c, now=int(time.time()))
        self.assertEqual(ts, 1740787200)
        self.assertEqual(snap["code_only"]["mcp"]["new_7d"], 0)


class InstallHintTests(_TempDB):
    def test_smithery_local_server_gets_the_smithery_install_command(self):
        from directory import resources
        hint = resources.code_install_hint({"source": "smithery", "package_name": "acme/tool",
                                            "endpoint_url": None})
        self.assertEqual((hint["package_registry"], hint["command"]),
                         ("smithery", "npx -y @smithery/cli install acme/tool"))


class ScopeTests(_TempDB):
    def setUp(self):
        super().setUp()
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "weather-remote", "name": "Weather remote",
                                     "description": "weather forecasts",
                                     "endpoint_url": "https://w.example/mcp", "health": "ok",
                                     "source": "fixture", "source_id": "w"})
            rows, _ = code_mcp.merge(
                [gh_item("acme/weather-code", stars=900, desc="weather forecasts locally"),
                 gh_item("acme/tiny", stars=1, desc=None)], [], (set(), set()))
            for r in rows:
                db.upsert_mcp_server(c, dict(r))
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def test_search_and_api_default_to_remote(self):
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual([r["slug"] for r in db.search_mcp(c, q="weather")], ["weather-remote"])
            code = [r["slug"] for r in db.search_mcp(c, q="weather", scope="code")]
            self.assertEqual(code, ["weather-code-acme"])
            self.assertEqual(len(db.search_mcp(c, q="weather", scope="all")), 2)
            s = db.mcp_stats(c)
        self.assertEqual((s["total"], s["code_only"]), (1, 2))
        api = self.client.get("/api/v1/mcp/search", params={"q": "weather"}).json()
        self.assertEqual([x["slug"] for x in api["servers"]], ["weather-remote"])
        api = self.client.get("/api/v1/mcp/search", params={"q": "weather", "scope": "code"}).json()
        item = api["servers"][0]
        self.assertEqual((api["total_matched"], item["scope"]), (1, "code"))
        self.assertEqual(item["code"]["repository"], "https://github.com/acme/weather-code")
        self.assertEqual(item["call_hint"], {})

    def test_pages_and_cross_redirects(self):
        r = self.client.get("/mcp/code")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/mcp/code/weather-code-acme", r.text)
        self.assertNotIn("/mcp/servers/weather-remote", r.text)
        r = self.client.get("/mcp/code/weather-code-acme")
        self.assertEqual(r.status_code, 200)
        self.assertIn("index,follow", r.text)
        self.assertIn("github.com/acme/weather-code", r.text)
        self.assertIn("noindex", self.client.get("/mcp/code/tiny-acme").text)
        r = self.client.get("/mcp/servers/weather-code-acme", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (301, "/mcp/code/weather-code-acme"))
        r = self.client.get("/mcp/code/weather-remote", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (301, "/mcp/servers/weather-remote"))
        self.assertNotIn("weather-code-acme", self.client.get("/mcp").text)

    def test_statistics_and_readiness_leave_code_only_out(self):
        with db.writer() as c:
            c.execute("UPDATE mcp_servers SET readiness_grade='A', readiness_score=90 "
                      "WHERE slug='weather-code-acme'")
        jobs.cmd_readiness(kinds=("mcp",))
        with closing(db.connect(read_only=True)) as c:
            grade = c.execute("SELECT readiness_grade FROM mcp_servers WHERE slug='weather-code-acme'").fetchone()[0]
            snap = stats.compute(c, now=int(time.time()))
        self.assertIsNone(grade)
        self.assertEqual(snap["funnel"]["mcp"][0]["count"], 1)
        self.assertEqual(snap["kpis"]["mcp"]["indexed"], 1)
        self.assertEqual(snap["kpis"]["mcp"]["code_only"], 2)
        self.assertEqual(snap["code_only"]["mcp"]["stars_100"], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
