"""Extra code-only MCP sources (official Registry, Cline, ModelScope, Docker,
Baidu MCP World, extra GitHub topics / package ecosystems). Offline."""
import urllib.error
from contextlib import closing
from unittest.mock import patch

from test_resources_search_and_payto_limit import _TempDB
from directory import code_mcp, code_sources, db, jobs
from directory.resources import code_install_hint


def gh_item(full, desc="An MCP server", created="2025-03-01T00:00:00Z"):
    return {"full_name": full, "name": full.split("/")[1], "html_url": f"https://github.com/{full}",
            "description": desc, "stargazers_count": 5, "topics": ["mcp"],
            "license": {"spdx_id": "MIT"}, "pushed_at": "2026-11-01T00:00:00Z",
            "created_at": created}


def reg_entry(name, version, latest, published, remotes=None, packages=None, repo=None,
              subfolder=None):
    s = {"name": name, "description": f"{name} server", "version": version}
    if remotes:
        s["remotes"] = remotes
    if packages:
        s["packages"] = packages
    if repo:
        s["repository"] = {"url": f"https://github.com/{repo}", "source": "github"}
        if subfolder:
            s["repository"]["subfolder"] = subfolder
    return {"server": s, "_meta": {"io.modelcontextprotocol.registry/official": {
        "status": "active", "publishedAt": published, "isLatest": latest}}}


class RegistryTests(_TempDB):
    def test_package_only_servers_dated_by_first_publish(self):
        entries = [
            reg_entry("io.github.acme/tool", "1.0.0", False, "2025-06-01T00:00:00Z",
                      packages=[{"registryType": "npm", "identifier": "@acme/tool"}], repo="acme/tool"),
            reg_entry("io.github.acme/tool", "1.1.0", True, "2025-09-01T00:00:00Z",
                      packages=[{"registryType": "npm", "identifier": "@acme/tool"}], repo="acme/tool"),
            reg_entry("io.github.acme/img", "2.0.0", True, "2025-07-01T00:00:00Z",
                      packages=[{"registryType": "oci", "identifier": "docker.io/acme/img:2.0.0"}]),
            reg_entry("io.github.acme/remote", "1.0.0", True, "2025-07-01T00:00:00Z",
                      remotes=[{"type": "streamable-http", "url": "https://r.example/mcp"}],
                      packages=[{"registryType": "npm", "identifier": "remote-pkg"}], repo="acme/remote"),
            reg_entry("io.github.acme/mono", "1.0.0", True, "2025-07-01T00:00:00Z",
                      packages=[{"registryType": "pypi", "identifier": "mono-a"}],
                      repo="acme/monorepo", subfolder="servers/a"),
        ]
        hints, rows = code_sources.registry_rows(entries)
        self.assertEqual(hints, {"acme/tool": "mcp-registry"})   # monorepo not hinted
        by = {(r["package_registry"], r["package_name"]): r for r in rows}
        self.assertEqual(set(by), {("npm", "@acme/tool"), ("oci", "docker.io/acme/img"),
                                   ("pypi", "mono-a")})
        tool = by[("npm", "@acme/tool")]
        self.assertEqual(tool["created_at"], code_mcp._epoch("2025-06-01T00:00:00Z"))
        self.assertEqual(tool["source"], "npm")
        self.assertIsNone(tool["endpoint_url"])
        self.assertEqual(code_install_hint(by[("oci", "docker.io/acme/img")])["command"],
                         "docker run -i --rm docker.io/acme/img")


class DirectoryRowTests(_TempDB):
    def test_modelscope_stdio_listings(self):
        items = [
            {"Id": 1, "FromSiteUrl": "https://github.com/Acme/Ms-Tool", "Name": "ms-tool"},
            {"Id": 2, "FromSiteUrl": "https://example.com", "Name": "npx-only", "Path": "@x",
             "ServerConfig": "[{'mcpServers': {'a': {'command': 'npx', 'args': ['-y', '@x/npx-only@latest']}}}]"},
            {"Id": 3, "Name": "uvx-only",
             "ServerConfig": [{"mcpServers": {"b": {"command": "uvx", "args": ["uvx-only==1.2"]}}}]},
            {"Id": 4, "Name": "nothing"},
            {"Id": 5, "Name": "remote", "FromSiteUrl": "https://github.com/acme/remote",
             "StreamableHTTPServerConfig": "[{'mcpServers': {'r': {'url': 'https://api.acme-tools.com/mcp'}}}]"},
        ]
        hints, rows = code_sources.modelscope_rows(items)
        self.assertEqual(hints, {"acme/ms-tool": "modelscope"})
        self.assertEqual({(r["package_registry"], r["package_name"]) for r in rows},
                         {("npm", "@x/npx-only"), ("pypi", "uvx-only")})

    def test_docker_local_images(self):
        docs = [{"type": "server", "name": "fetch", "image": "mcp/fetch",
                 "about": {"title": "Fetch", "description": "Fetch pages"},
                 "source": {"project": "https://github.com/acme/fetch"}},
                {"type": "remote", "name": "r", "remote": {"url": "https://r.example/mcp"}}]
        hints, rows = code_sources.docker_rows(docs)
        self.assertEqual(hints, {"acme/fetch": "docker-mcp"})
        self.assertEqual([(r["source"], r["package_name"]) for r in rows], [("oci", "mcp/fetch")])
        self.assertEqual(rows[0]["source_code_url"], "https://github.com/acme/fetch")

    def test_baidu_walk_stops_short_pages_and_budget(self):
        calls = []

        def fake_get(url, headers=None, timeout=40):
            calls.append(url)
            servers = [{"serverUrl": f"https://github.com/acme/s{i}"} for i in range(25)]
            if "wd=b" in url:
                servers = servers[:3]
            return {"data": {"mcpList": [{"servers": servers}]}}, {}
        with patch.object(code_mcp, "_get", side_effect=fake_get), \
                patch.object(code_sources.time, "sleep"):
            hints = code_sources.fetch_baidu(["a", "b"], max_requests=10)
        self.assertEqual(len(calls), 5)     # 4 pages of "a", 1 short page of "b"
        self.assertEqual(len(hints), 25)
        with patch.object(code_mcp, "_get", side_effect=fake_get), \
                patch.object(code_sources.time, "sleep"):
            code_sources.fetch_baidu(["a", "b"], max_requests=2)

    def test_mentions_mcp(self):
        self.assertTrue(code_sources.mentions_mcp("weather-mcp"))
        self.assertTrue(code_sources.mentions_mcp(None, "A Model Context Protocol server"))
        self.assertFalse(code_sources.mentions_mcp("minecraft-plugin", "Multiplayer chat"))

    def test_new_ecosystem_rows_and_install_hints(self):
        rows = {r["package_registry"]: r for r in (
            code_mcp.eco_row({"ecosystem": e, "name": n, "description": "MCP server",
                              "repository_url": "https://github.com/acme/x"})
            for e, n in [("cargo", "x-mcp"), ("go", "github.com/acme/x"),
                         ("maven", "io.acme:x-mcp"), ("nuget", "Acme.Mcp"), ("rubygems", "x_mcp")])}
        self.assertEqual(rows["maven"]["source_url"],
                         "https://central.sonatype.com/artifact/io.acme/x-mcp")
        self.assertEqual(rows["go"]["slug"], "x-acme")
        self.assertEqual(code_install_hint(rows["cargo"])["command"], "cargo install x-mcp")
        self.assertEqual(code_install_hint(rows["go"])["command"], "go run github.com/acme/x@latest")
        self.assertEqual(code_install_hint(rows["nuget"])["command"], "dnx Acme.Mcp --yes")
        self.assertIsNone(code_install_hint(rows["maven"])["command"])


class LookupTests(_TempDB):
    def test_batched_lookup_halves_on_422_and_records(self):
        queries = []

        def fake_search(q, page, auth):
            repos = [t[5:] for t in q.split() if t.startswith("repo:")]
            queries.append(len(repos))
            if len(repos) > 8:
                raise urllib.error.HTTPError("u", 422, "too long", {}, None)
            return {"items": [gh_item(r) for r in repos if not r.endswith("gone")]}, {}
        hints = {f"acme/r{i}": "baidu-mcpworld" for i in range(20)}
        hints["acme/gone"] = "cline"
        with patch.object(code_mcp, "_github_search", side_effect=fake_search), \
                patch.object(code_mcp, "_github_auth", return_value=None):
            items = code_sources.lookup_repos(hints)
        self.assertEqual(len(items), 20)
        self.assertEqual(queries[:3], [21, 15, 7])
        self.assertEqual(code_sources.recently_checked(), set(hints))


class CrawlWithSourcesTests(_TempDB):
    def _run(self, gh, eco=(), hints=None, extra_rows=(), looked=(), **kw):
        with patch.object(code_mcp, "fetch_github", return_value=list(gh)), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=list(eco)), \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_mcp, "npm_created", return_value={}), \
                patch.object(code_sources, "fetch_extra",
                             return_value=(dict(hints or {}), list(extra_rows), {"cline_repos": 1})), \
                patch.object(code_sources, "lookup_repos", return_value=list(looked)) as lk:
            jobs.cmd_crawl_code(**kw)
        return lk

    def _rows(self):
        with closing(db.connect(read_only=True)) as c:
            return {(s, sid): (pkg, ts) for s, sid, pkg, ts in c.execute(
                "SELECT source, source_id, package_name, created_at FROM mcp_servers")}

    def test_hinted_repos_are_looked_up_dated_and_packages_deduplicated(self):
        eco = [{"ecosystem": "cargo", "name": "tool-mcp", "description": "MCP",
                "repository_url": "https://github.com/acme/hinted",
                "first_release_published_at": "2025-05-01T00:00:00Z"}]
        lk = self._run([gh_item("acme/topic")], eco=eco,
                       hints={"acme/hinted": "baidu-mcpworld", "acme/topic": "cline"},
                       looked=[gh_item("acme/hinted", created="2025-04-01T00:00:00Z")])
        self.assertEqual(list(lk.call_args.args[0]), ["acme/hinted"])   # topic repo not looked up
        rows = self._rows()
        self.assertEqual(rows[("github", "acme/hinted")],
                         ("tool-mcp", code_mcp._epoch("2025-04-01T00:00:00Z")))
        self.assertNotIn(("cargo", "tool-mcp"), rows)

    def test_incremental_run_does_not_duplicate_stored_repos(self):
        self._run([gh_item("acme/old")])
        # A stale duplicate left by an older run, and a package for the stored repo.
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "dup", "name": "dup", "endpoint_url": None,
                                     "kind": "catalog", "package_registry": "npm",
                                     "package_name": "old-dup",
                                     "source_code_url": "https://github.com/acme/old",
                                     "source": "npm", "source_id": "old-dup"})
        eco = [{"ecosystem": "pypi", "name": "old-mcp", "description": "MCP",
                "repository_url": "https://github.com/acme/old"}]
        self._run([], eco=eco)
        rows = self._rows()
        self.assertNotIn(("pypi", "old-mcp"), rows)
        self.assertNotIn(("npm", "old-dup"), rows)
        # The package offered for the stored repository is attached first.
        self.assertEqual(rows[("github", "acme/old")][0], "old-mcp")

    def test_baidu_only_on_full_runs_or_when_asked(self):
        with patch.object(code_mcp, "fetch_github", return_value=[]), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=[]), \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_sources, "fetch_extra", return_value=({}, [], {})) as fx:
            jobs.cmd_crawl_code(full=True)
            self.assertTrue(fx.call_args.kwargs["baidu"])
            jobs.cmd_crawl_code(baidu=False)
            self.assertFalse(fx.call_args.kwargs["baidu"])
