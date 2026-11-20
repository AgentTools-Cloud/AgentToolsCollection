"""Code-only MCP sub-directory (/mcp/code): crawler merge, scope split, pages,
statistics. Offline: temporary database, no network."""
import time
from contextlib import closing
from datetime import date
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_resources_search_and_payto_limit import _TempDB
from directory import code_mcp, code_sources, db, jobs, routes, stats

_extra = patch.object(code_sources, "fetch_extra", return_value=({}, [], {}))
_meta = patch("directory.code_meta.apply_official",                  # tested in test_code_sources
               side_effect=lambda rows, **k: (rows, {}))


def setUpModule():
    _extra.start()      # the directories in code_sources are tested offline separately
    _meta.start()


def tearDownModule():
    _extra.stop()
    _meta.stop()


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

    def test_only_code_listings_of_other_directories_are_skipped(self):
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "remote", "name": "Remote",
                                     "endpoint_url": "https://r.example/mcp",
                                     "source_code_url": "https://github.com/acme/remote",
                                     "package_registry": "npm", "package_name": "remote-mcp",
                                     "source": "mcp-registry", "source_id": "r"})
            db.upsert_mcp_server(c, {"slug": "listed", "name": "Listed", "endpoint_url": None,
                                     "kind": "catalog",
                                     "source_code_url": "https://github.com/acme/listed",
                                     "source": "cline", "source_id": "listed"})
            db.upsert_mcp_server(c, {"slug": "own", "name": "Own", "endpoint_url": None,
                                     "kind": "catalog",
                                     "source_code_url": "https://github.com/acme/own",
                                     "source": "github", "source_id": "acme/own"})
        with closing(db.connect(read_only=True)) as c:
            known = code_mcp.known_repos(c)
            hosted = code_mcp.hosted_repos(c)
        # our own rows are refreshed, not skipped; a hosted listing does not own its code
        self.assertEqual(known, ({"acme/listed"}, set()))
        self.assertEqual(hosted, {"acme/remote"})
        rows, skipped = code_mcp.merge([gh_item("acme/remote"), gh_item("acme/own"), gh_item("acme/listed")],
                                       [npm_obj("remote-mcp", repo="acme/remote"),
                                        npm_obj("x", repo="acme/listed")], known)
        by_id = {r["source_id"]: r for r in rows}
        self.assertEqual(sorted(by_id), ["acme/own", "acme/remote"])
        self.assertEqual(by_id["acme/remote"]["package_name"], "remote-mcp")
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


class NetworkRetryTests(_TempDB):
    def test_dns_failure_is_retried_then_succeeds(self):
        import io, urllib.error

        class Resp(io.BytesIO):
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))
            return Resp(b'{"ok": 1}')
        with patch("urllib.request.urlopen", side_effect=fake), patch.object(code_mcp.time, "sleep"):
            data, _ = code_mcp._get("https://api.github.com/x")
        self.assertEqual((data, len(calls)), ({"ok": 1}, 3))

    def test_client_errors_are_not_retried(self):
        import urllib.error
        err = urllib.error.HTTPError("https://x", 404, "nf", {}, None)
        with patch("urllib.request.urlopen", side_effect=err) as m, patch.object(code_mcp.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                code_mcp._get("https://x")
        self.assertEqual(m.call_count, 1)


def eco_pkg(name, ecosystem="npm", repo=None, first="2025-05-01T00:00:00Z"):
    return {"name": name, "ecosystem": ecosystem, "description": "MCP server",
            "repository_url": f"https://github.com/{repo}" if repo else "",
            "homepage": None, "licenses": "MIT", "keywords_array": ["mcp-server"],
            "downloads": 42, "first_release_published_at": first,
            "latest_release_published_at": "2026-10-01T00:00:00Z"}


class EcosystemsTests(_TempDB):
    def test_npm_and_pypi_rows_carry_registry_and_first_release(self):
        npm = code_mcp.eco_row(eco_pkg("@acme/x-mcp"))
        py = code_mcp.eco_row(eco_pkg("x-mcp", "pypi"))
        self.assertEqual((npm["source"], npm["package_registry"], npm["created_at"]),
                         ("npm", "npm", 1746057600))
        self.assertEqual((py["source"], py["package_registry"], py["source_url"]),
                         ("pypi", "pypi", "https://pypi.org/project/x-mcp/"))

    def test_merge_attaches_pypi_package_to_its_repository_and_keeps_registries_apart(self):
        rows, _ = code_mcp.merge(
            [gh_item("acme/tool")],
            [code_mcp.eco_row(eco_pkg("tool-mcp", "pypi", repo="acme/tool")),
             code_mcp.eco_row(eco_pkg("same", "pypi")),
             code_mcp.eco_row(eco_pkg("same", "npm"))],
            (set(), {("npm", "taken")}))
        by = {(r["source"], r["source_id"]): r for r in rows}
        self.assertEqual(by[("github", "acme/tool")]["package_registry"], "pypi")
        self.assertIn(("pypi", "same"), by)
        self.assertIn(("npm", "same"), by)

    def test_crawl_uses_ecosystems_and_adds_npm_search_only_packages(self):
        IncrementalTests._run(self, [], npm=[npm_obj("search-only")],
                              eco=[eco_pkg("from-eco"), eco_pkg("py-mcp", "pypi")],
                              created={"search-only": 1735689600})
        with closing(db.connect(read_only=True)) as c:
            got = {(r[0], r[1]): r[2] for r in c.execute(
                "SELECT source, source_id, created_at FROM mcp_servers WHERE source IN ('npm','pypi')")}
        self.assertEqual(got, {("npm", "from-eco"): 1746057600, ("pypi", "py-mcp"): 1746057600,
                               ("npm", "search-only"): 1735689600})


class IncrementalTests(_TempDB):
    def _run(self, gh, npm=(), created=None, eco=(), classes=None, **kw):
        created = created or {}
        from directory import code_classify

        def ask(rows):
            return {i: classes.get(r["name"], "server") for i, r in enumerate(rows)}
        with patch.object(code_classify, "configured", return_value=classes is not None), \
                patch.object(code_classify, "_ask", side_effect=ask) as asked, \
                patch.object(code_mcp, "fetch_github", return_value=list(gh)) as fg, \
                patch.object(code_mcp, "fetch_ecosystems", return_value=list(eco)), \
                patch.object(code_mcp, "fetch_npm", return_value=list(npm)), \
                patch.object(code_mcp, "npm_created",
                             side_effect=lambda names: {n: created[n] for n in names if n in created}):
            jobs.cmd_crawl_code(**kw)
        self.asked = asked.call_count
        return fg.call_args.kwargs

    def _ids(self):
        with closing(db.connect(read_only=True)) as c:
            return {r[0] for r in c.execute(
                "SELECT source_id FROM mcp_servers WHERE source IN ('github','npm','pypi')")}

    def test_non_servers_are_not_imported_and_old_ones_are_removed(self):
        # Imported before classification existed.
        self._run([gh_item("acme/srv"), gh_item("acme/awesome"), gh_item("acme/claimed")])
        with db.writer() as c:
            c.execute("UPDATE mcp_servers SET owner_verified=1 WHERE source_id='acme/claimed'")
        self.assertEqual(self._ids(), {"acme/srv", "acme/awesome", "acme/claimed"})
        cls = {"awesome": "list", "sdk": "sdk", "claimed": "client"}
        self._run([gh_item("acme/sdk"), gh_item("acme/new")], classes=cls)
        self.assertEqual(self._ids(), {"acme/srv", "acme/new", "acme/claimed"})
        with closing(db.connect(read_only=True)) as c:
            orphans = c.execute("SELECT COUNT(*) FROM listing_sources WHERE kind='mcp' AND "
                                "listing_id NOT IN (SELECT id FROM mcp_servers)").fetchone()[0]
        self.assertEqual(orphans, 0)
        # Verdicts are cached: an unchanged re-crawl asks the model nothing.
        self._run([gh_item("acme/sdk"), gh_item("acme/new")], classes=cls)
        self.assertEqual(self.asked, 0)
        self.assertEqual(self._ids(), {"acme/srv", "acme/new", "acme/claimed"})

    def test_unanswered_rows_are_not_imported_but_kept_if_stored(self):
        self._run([gh_item("acme/old")])
        from directory import code_classify
        with patch.object(code_classify, "configured", return_value=True), \
                patch.object(code_classify, "_ask", return_value={}), \
                patch.object(code_mcp, "fetch_github", return_value=[gh_item("acme/fresh")]), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=[]), \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_mcp, "npm_created", return_value={}):
            jobs.cmd_crawl_code()
        self.assertEqual(self._ids(), {"acme/old"})

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

    def test_api_never_grades_code_only_rows(self):
        with db.writer() as c:
            c.execute("UPDATE mcp_servers SET readiness_grade='F', readiness_score=10, "
                      "quality_score=4 WHERE slug='weather-code-acme'")
        d = self.client.get("/api/v1/mcp/servers/weather-code-acme").json()
        d = d.get("server", d)
        for k in ("readiness_grade", "readiness_score", "quality_score"):
            self.assertIsNone(d.get(k), k)
        self.assertNotIn("readiness", d)
        with closing(db.connect(read_only=True)) as c:
            rows = db.attach_ratings("mcp", db.search_mcp(c, q="weather", scope="all"))
        by = {r["slug"]: r for r in rows}
        self.assertIsNone(by["weather-code-acme"]["grade"])
        self.assertIsNotNone(by["weather-remote"]["grade"])


if __name__ == "__main__":
    import unittest
    unittest.main()
