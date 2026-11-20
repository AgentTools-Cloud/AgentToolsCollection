"""External adoption signals: identity parsing, fetch jobs (fake HTTP), read side."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import db, popularity as P  # noqa: E402

NOW = int(time.time())


class IdentityTests(unittest.TestCase):
    def test_github_repo(self):
        self.assertEqual(P.github_repo("https://github.com/Owner/Repo.git"), "owner/repo")
        self.assertEqual(P.github_repo("https://github.com/o/r/tree/main/packages/x"), "o/r")
        self.assertIsNone(P.github_repo("https://github.com/orgs/acme"))
        self.assertIsNone(P.github_repo("https://gitlab.com/o/r"))
        self.assertIsNone(P.github_repo(None))

    def test_npm_package(self):
        self.assertEqual(P.npm_package("npm", "@scope/pkg"), "@scope/pkg")
        self.assertIsNone(P.npm_package("", "sentry"))          # Smithery names are not npm
        self.assertIsNone(P.npm_package("npm", "Bad Name"))

    def test_site(self):
        self.assertEqual(P.site("https://api.example.co.uk/x"), "example.co.uk")
        self.assertEqual(P.site("https://a.vercel.app/mcp"), "a.vercel.app")   # private suffix
        self.assertIsNone(P.site("http://127.0.0.1:8000/"))

    def test_parse_tranco(self):
        rows = P.parse_tranco(["1,google.com", "2,example.com", "x,bad"], {"example.com": 3})
        self.assertEqual(rows, [("example.com", 2, 3)])


class JobAndReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        c = sqlite3.connect(self.path)
        rows = [
            (1, "gh-tool", "GitHub Tool", "https://mcp.example.com/mcp", "https://github.com/acme/tool", "npm", "@acme/tool"),
            (2, "fork-tool", "Fork Tool", "https://other.example.org/mcp", "https://github.com/acme/tool", None, None),
            (3, "missing", "Missing", "https://gone.example.net/mcp", "https://github.com/acme/gone", None, None),
        ]
        for i, slug, name, ep, src, reg, pkg in rows:
            c.execute("INSERT INTO mcp_servers(id, slug, name, endpoint_url, source_code_url, package_registry, "
                      "package_name, health, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'ok','t',?,?)",
                      (i, slug, name, ep, src, reg, pkg, NOW, NOW))
        for i in range(25):   # a platform domain behind many listings
            c.execute("INSERT INTO mcp_servers(id, slug, name, endpoint_url, health, source, created_at, updated_at) "
                      "VALUES (?,?,?,?,'ok','t',?,?)", (100 + i, f"p{i}", f"P{i}", f"https://host.example.io/{i}", NOW, NOW))
        c.commit()
        c.close()
        self.read = lambda: db.connect(self.path, read_only=True)

        @contextmanager
        def writer():
            with db.writer(self.path) as w:
                yield w
        self.writer = writer

    def tearDown(self):
        self.tmp.cleanup()

    def fake_http(self, url, headers=None, timeout=30.0):
        self.calls.append(url)
        if "api.github.com/repos/acme/tool" in url:
            return 200, {"X-RateLimit-Remaining": "4000"}, json.dumps({
                "full_name": "acme/tool", "stargazers_count": 1234, "forks_count": 56,
                "pushed_at": "2026-11-01T00:00:00Z", "archived": False}).encode()
        if "api.github.com/repos/" in url:
            return 404, {}, b""
        if "api.npmjs.org" in url:
            return 200, {}, json.dumps({"downloads": 789, "package": "@acme/tool"}).encode()
        if url == P.TRANCO_LATEST:
            return 200, {}, json.dumps({"list_id": "L1", "created_on": "2026-11-10T22:00:00",
                                        "download": "https://tranco.test/L1"}).encode()
        if url == "https://tranco.test/L1":
            body = "1,google.com\n500,example.com\n900,example.io\n" + "\n".join(
                f"{i},filler{i}.com" for i in range(1000, 80000))
            return 200, {}, body.encode()
        return 500, {}, b""

    def test_jobs_then_read(self):
        self.calls = []
        env = {"GITHUB_OAUTH_CLIENT_ID": "id", "GITHUB_OAUTH_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env):
            gh = P.refresh_github(self.read, self.writer, http=self.fake_http, sleep=lambda s: None)
        self.assertEqual((gh["ok"], gh["not_found"], gh["wanted"]), (1, 1, 2))
        auth = [c for c in self.calls if "api.github.com" in c]
        self.assertEqual(len(auth), 2)          # one request per distinct repo
        npm = P.refresh_npm(self.read, self.writer, http=self.fake_http, sleep=lambda s: None)
        self.assertEqual(npm["ok"], 1)
        tr = P.refresh_tranco(self.read, self.writer, http=self.fake_http)
        self.assertEqual(tr["list_id"], "L1")
        self.assertEqual(P.refresh_tranco(self.read, self.writer, http=self.fake_http), {"unchanged": "L1"})
        # second GitHub run: nothing stale
        with patch.dict(os.environ, env):
            again = P.refresh_github(self.read, self.writer, http=self.fake_http, sleep=lambda s: None)
        self.assertEqual(again["queued"], 0)

        c = self.read()
        one = db.get_mcp_by_slug(c, "gh-tool")["popularity"]
        self.assertEqual(one["github"]["stars"], 1234)
        self.assertEqual(one["npm"]["downloads_30d"], 789)
        self.assertEqual(one["tranco"]["rank"], 500)
        self.assertIsNone(db.get_mcp_by_slug(c, "missing")["popularity"])
        # a platform domain shared by 25 listings is never attributed
        self.assertIsNone((db.get_mcp_by_slug(c, "p0").get("popularity") or {}).get("tranco"))
        stats = P.adoption_stats(c)
        self.assertEqual(stats["mcp_with_github"], 2)
        self.assertEqual(stats["top_repos"][0]["stars"], 1234)
        self.assertEqual(stats["tranco"]["mcp"]["ranked"], 1)
        c.close()

    def test_npm_caps_scoped_lookups_and_saves_as_it_goes(self):
        c = sqlite3.connect(self.path)
        for i in range(5):
            c.execute("INSERT INTO mcp_servers(id, slug, name, package_registry, package_name, health, "
                      "source, created_at, updated_at) VALUES (?,?,?,?,?,'ok','t',?,?)",
                      (200 + i, f"s{i}", f"S{i}", "npm", f"@scope/pkg{i}", NOW, NOW))
        c.commit()
        c.close()
        self.calls = []
        first = P.refresh_npm(self.read, self.writer, http=self.fake_http,
                              sleep=lambda s: None, scoped_limit=2)
        self.assertEqual((first["ok"], first["scoped_deferred"]), (2, 4))
        second = P.refresh_npm(self.read, self.writer, http=self.fake_http,
                               sleep=lambda s: None, scoped_limit=2)
        self.assertEqual((second["ok"], second["queued"]), (2, 4))   # the first two are fresh now
        c = self.read()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ext_npm_packages").fetchone()[0], 4)
        c.close()

    def test_github_skips_without_credentials(self):
        with patch.dict(os.environ, {"GITHUB_OAUTH_CLIENT_ID": "", "GITHUB_OAUTH_CLIENT_SECRET": ""}):
            self.assertIn("skipped", P.refresh_github(self.read, self.writer, http=self.fake_http))

    def test_github_fetches_remote_repos_first_then_code_listings_without_stars(self):
        c = sqlite3.connect(self.path)
        for i, slug, source, url, reg, pkg, stars in [
                (300, "gh-row", "github", "https://github.com/acme/starred", None, None, 40),
                (301, "own-pkg", "npm", "https://github.com/acme/widget", "npm", "widget-mcp", None),
                (302, "copy-pkg", "npm", "https://github.com/acme/famous", "npm", "@mirror/famous-copy", None)]:
            c.execute("INSERT INTO mcp_servers(id, slug, name, source_code_url, package_registry, package_name, "
                      "github_stars, kind, source, source_id, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,'catalog',?,?,?,?)", (i, slug, slug, url, reg, pkg, stars, source, slug,
                                                                 NOW, NOW))
        c.commit()
        c.close()
        c = self.read()
        # the GitHub row keeps its own stars; the copy only links someone else's repository
        self.assertEqual(P.wanted_repos(c), {"acme/tool": 0, "acme/gone": 0, "acme/widget": 1,
                                             "acme/starred": 2, "acme/famous": 2})
        c.close()
        self.calls = []
        with patch.dict(os.environ, {"GITHUB_OAUTH_CLIENT_ID": "id", "GITHUB_OAUTH_CLIENT_SECRET": "secret"}):
            gh = P.refresh_github(self.read, self.writer, budget=3, http=self.fake_http, sleep=lambda s: None)
        fetched = [u.rsplit("/repos/", 1)[1] for u in self.calls if "api.github.com" in u]
        self.assertEqual((sorted(fetched[:2]), fetched[2:]), (["acme/gone", "acme/tool"], ["acme/widget"]))
        self.assertEqual(gh["queued_by_tier"], [2, 1, 0])

    def test_read_side_without_tables(self):
        c = sqlite3.connect(":memory:")
        self.assertIsNone(P.for_listing(c, "x402", {"url": "https://example.com"}))
        self.assertEqual(P.adoption_stats(c), {"available": False})


if __name__ == "__main__":
    unittest.main()
