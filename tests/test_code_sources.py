"""Extra code-only MCP sources (official Registry, Cline, ModelScope, Docker,
Baidu MCP World, extra GitHub topics / package ecosystems). Offline."""
import os
import urllib.error
from contextlib import closing
from unittest.mock import patch

from test_resources_search_and_payto_limit import _TempDB
from directory import code_mcp, code_sources, db, jobs
from directory.resources import code_install_hint

_meta = patch("directory.code_meta.fetch", return_value=(0, None))   # registries stay offline


def setUpModule():
    _meta.start()


def tearDownModule():
    _meta.stop()


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
        # Found but never classified (e.g. the run died) -> asked again next time.
        self.assertEqual(code_sources.recently_checked(), {"acme/gone"})
        from directory import code_classify
        with db.writer() as c:
            code_classify.ensure_table(c)
            c.execute("INSERT INTO code_mcp_class VALUES ('github','acme/r1','d','list',1,0)")
        self.assertEqual(code_sources.recently_checked(), {"acme/gone", "acme/r1"})


class CrawlWithSourcesTests(_TempDB):
    def _run(self, gh, eco=(), hints=None, extra_rows=(), looked=(), **kw):
        with patch.object(code_mcp, "fetch_github", return_value=list(gh)), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=list(eco)), \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_mcp, "npm_created", return_value={}), \
                patch.object(code_sources, "fetch_extra",
                             return_value=(dict(hints or {}), list(extra_rows), {"cline_repos": 1})), \
                patch.object(code_sources, "lookup_repos", return_value=list(looked)) as lk, \
                patch("directory.code_classify.configured", return_value=False):
            jobs.cmd_crawl_code(**kw)     # never the real model, even with .env loaded
        return lk

    def _rows(self):
        with closing(db.connect(read_only=True)) as c:
            return {(s, sid): (pkg, ts) for s, sid, pkg, ts in c.execute(
                "SELECT source, source_id, package_name, created_at FROM mcp_servers")}

    def test_unchanged_rows_are_not_rewritten(self):
        self._run([gh_item("acme/a"), gh_item("acme/b")])
        real = db.upsert_mcp_server
        with patch.object(db, "upsert_mcp_server", side_effect=real) as up:
            self._run([gh_item("acme/a"), gh_item("acme/b", desc="Changed")])
        self.assertEqual([c.args[1]["source_id"] for c in up.call_args_list], ["acme/b"])
        # A stored row that disappeared (e.g. pruned) is written again.
        with db.writer() as c:
            c.execute("DELETE FROM mcp_servers WHERE source='github' AND source_id='acme/a'")
        with patch.object(db, "upsert_mcp_server", side_effect=real) as up:
            self._run([gh_item("acme/a"), gh_item("acme/b", desc="Changed")])
        self.assertEqual([c.args[1]["source_id"] for c in up.call_args_list], ["acme/a"])

    def test_daily_runs_skip_the_generic_mcp_keyword(self):
        with patch.object(code_mcp, "fetch_github", return_value=[]), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=[]) as fe, \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_sources, "fetch_extra", return_value=({}, [], {})):
            jobs.cmd_crawl_code(full=True)
            self.assertIn("mcp", [c.args[0] for c in fe.call_args_list])
            fe.reset_mock()
            jobs.cmd_crawl_code(baidu=False)
            self.assertNotIn("mcp", [c.args[0] for c in fe.call_args_list])
            self.assertIn("mcp-server", [c.args[0] for c in fe.call_args_list])

    def test_repositories_of_hosted_listings_are_looked_up_a_capped_number_a_run(self):
        with db.writer() as c:
            for i in (1, 2, 3):
                db.upsert_mcp_server(c, {"slug": f"h{i}", "name": f"H{i}",
                                         "endpoint_url": f"https://h{i}.example/mcp",
                                         "source_code_url": f"https://github.com/acme/h{i}",
                                         "source": "mcp-registry", "source_id": f"h{i}"})
        with patch.object(code_mcp, "HOSTED_LOOKUPS", 2):
            lk = self._run([gh_item("acme/h3")], looked=[gh_item("acme/h1")])
        # h3 came from the topic crawl; h1 and h2 fill this run's cap
        self.assertEqual(lk.call_args.args[0], {"acme/h1": "hosted-listing", "acme/h2": "hosted-listing"})
        rows = self._rows()
        self.assertIn(("github", "acme/h1"), rows)    # listed as code beside its hosted listing
        self.assertIn(("github", "acme/h3"), rows)
        self.assertEqual(len([k for k in rows if k[0] == "mcp-registry"]), 3)

    def test_hinted_repos_are_looked_up_dated_and_packages_deduplicated(self):
        eco = [{"ecosystem": "cargo", "name": "hinted-mcp", "description": "MCP",
                "repository_url": "https://github.com/acme/hinted",
                "first_release_published_at": "2025-05-01T00:00:00Z"}]
        lk = self._run([gh_item("acme/topic")], eco=eco,
                       hints={"acme/hinted": "baidu-mcpworld", "acme/topic": "cline"},
                       looked=[gh_item("acme/hinted", created="2025-04-01T00:00:00Z")])
        self.assertEqual(list(lk.call_args.args[0]), ["acme/hinted"])   # topic repo not looked up
        rows = self._rows()
        self.assertEqual(rows[("github", "acme/hinted")],
                         ("hinted-mcp", code_mcp._epoch("2025-04-01T00:00:00Z")))
        self.assertNotIn(("cargo", "hinted-mcp"), rows)

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

    def test_fetch_is_saved_and_a_run_can_resume_from_it(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        with patch.object(code_mcp, "fetch_dir", return_value=d):
            self._run([gh_item("acme/saved")], full=True)
            files = sorted(os.listdir(d))
            self.assertEqual(len(files), 1)
            rows, info, meta = code_mcp.load_fetch(os.path.join(d, files[0]))
        self.assertEqual([r["source_id"] for r in rows], ["acme/saved"])
        self.assertTrue(meta["is_full"])
        with db.writer() as c:
            c.execute("DELETE FROM mcp_servers")
        with patch.object(code_mcp, "fetch_all", side_effect=AssertionError("no fetch")):
            jobs.cmd_crawl_code(from_file=os.path.join(d, files[0]))
        self.assertIn(("github", "acme/saved"), self._rows())

    def test_baidu_only_on_full_runs_or_when_asked(self):
        with patch.object(code_mcp, "fetch_github", return_value=[]), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=[]), \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_sources, "fetch_extra", return_value=({}, [], {})) as fx:
            jobs.cmd_crawl_code(full=True)
            self.assertTrue(fx.call_args.kwargs["baidu"])
            jobs.cmd_crawl_code(baidu=False)
            self.assertFalse(fx.call_args.kwargs["baidu"])


class ClassifierModelTests(_TempDB):
    def test_classify_model_override(self):
        from directory import code_classify
        env = {"AGENT_TOOLS_A2A_BASE_URL": "https://gw.example", "AGENT_TOOLS_A2A_API_KEY": "k",
               "AGENT_TOOLS_A2A_MODEL": "big-model"}
        with patch.dict("os.environ", env, clear=False):
            os.environ.pop("AGENT_TOOLS_CLASSIFY_MODEL", None)   # set on the server
            self.assertEqual(code_classify._backend()[2], "big-model")
            with patch.dict("os.environ", {"AGENT_TOOLS_CLASSIFY_MODEL": "small-model"}):
                self.assertEqual(code_classify._backend(), ("https://gw.example", "k", "small-model"))


class RemotePartTests(_TempDB):
    def test_main_run_uses_parts_from_another_machine(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        topics_seen = []

        def local_fetch(topic, **kw):
            topics_seen.append(topic)
            return [gh_item("acme/local")]
        with patch.object(code_mcp, "parts_dir", return_value=d), \
                patch.object(code_mcp, "fetch_github", side_effect=local_fetch), \
                patch.object(code_mcp, "fetch_ecosystems", side_effect=AssertionError("fetched here")), \
                patch.object(code_mcp, "fetch_npm", side_effect=AssertionError("fetched here")), \
                patch.object(code_sources, "fetch_extra", side_effect=AssertionError("fetched here")), \
                patch.object(code_sources, "lookup_repos", return_value=[]), \
                patch.object(code_mcp, "npm_created", return_value={}):
            code_mcp.write_part("github:mcp", [code_mcp.slim_github(gh_item("acme/remote"))], d)
            code_mcp.write_part("packages", {"eco": [], "npm": []}, d)
            code_mcp.write_part("extra", {"hints": {}, "rows": [], "info": {"cline_repos": 0}}, d)
            rows, info = code_mcp.fetch_all(remote_parts=("github:mcp", "packages", "extra"))
        self.assertNotIn("mcp", topics_seen)
        self.assertEqual({r["source_id"] for r in rows}, {"acme/local", "acme/remote"})
        self.assertTrue(os.path.exists(os.path.join(d, "github_mcp.json.gz")))

    def test_missing_part_falls_back_to_fetching_here(self):
        import tempfile
        with patch.object(code_mcp, "parts_dir", return_value=tempfile.mkdtemp()), \
                patch.object(code_mcp, "PART_WAIT_SECONDS", 0), \
                patch.object(code_mcp, "fetch_github", return_value=[]), \
                patch.object(code_mcp, "fetch_ecosystems", return_value=[]) as fe, \
                patch.object(code_mcp, "fetch_npm", return_value=[]), \
                patch.object(code_sources, "fetch_extra", return_value=({}, [], {})), \
                patch.object(code_mcp, "npm_created", return_value={}):
            code_mcp.fetch_all(remote_parts=("packages",))
        self.assertTrue(fe.called)


class OfficialRegistryMetaTests(_TempDB):
    def _rows(self):
        rows = [code_mcp.eco_row({"ecosystem": "npm", "name": n, "description": "eco text",
                                  "repository_url": "https://github.com/eco/" + n,
                                  "licenses": "MIT", "downloads": 99, "keywords_array": ["eco"]})
                for n in ("kept", "gone", "unread")]
        rows.append({"source": "npm", "source_id": "other", "package_registry": "npm",
                     "package_name": "other", "description": "from the MCP Registry"})
        rows.append({"source": "npm", "source_id": "gone-reg", "package_registry": "npm",
                     "package_name": "gone", "description": "registry row of a gone package"})
        return rows

    @staticmethod
    def _fake(reg, name):
        return {"kept": (200, {"description": "npm text", "license": "Apache-2.0",
                               "repository": "git+https://github.com/acme/kept.git",
                               "homepage": None, "keywords": ["mcp"]}),
                "gone": (404, None)}.get(name, (0, None))

    def test_registry_metadata_replaces_ecosystems(self):
        from directory import code_meta
        rows = self._rows()
        with patch.object(code_meta, "fetch", side_effect=self._fake):
            out, counts = code_meta.apply_official(rows)
        by = {r["source_id"]: r for r in out}
        self.assertEqual((by["kept"]["description"], by["kept"]["license"], by["kept"]["tags"],
                          by["kept"]["source_code_url"], by["kept"]["package_download_count"]),
                         ("npm text", "Apache-2.0", ["mcp"], "https://github.com/acme/kept", None))
        self.assertEqual(by["unread"]["description"], "eco text")       # kept for classification
        self.assertEqual(by["other"]["description"], "from the MCP Registry")
        # One "not found" is not enough: the package is only held back.
        self.assertIn("gone", by)
        self.assertEqual((counts["official"], counts["gone"]), (1, 0))
        held, counts = code_meta.apply_official(rows, budget=0, keep_unread=False)
        self.assertEqual({r["source_id"] for r in held}, {"kept", "other", "gone-reg"})
        # A failed read (status 0) is not cached; the 404 is.
        cache = code_meta.cached()
        self.assertNotIn(("npm", "unread"), cache)
        self.assertEqual(cache[("npm", "gone")][0], 404)
        self.assertFalse(cache[("npm", "gone")][1]["confirmed"])
        self.assertEqual(code_meta.drop_gone(), 0)

    def test_gone_needs_two_answers_a_day_apart_then_drops_every_source(self):
        from directory import code_meta
        rows = self._rows()
        with patch.object(code_meta, "fetch", side_effect=self._fake):
            code_meta.apply_official(rows)
        # Re-checked too early: nothing read again.
        with patch.object(code_meta, "fetch", side_effect=AssertionError("fetched")):
            code_meta.apply_official(rows, budget=10)
        real = code_meta.time.time
        with patch.object(code_meta.time, "time", side_effect=lambda: real() + 2 * 86400), \
                patch.object(code_meta, "fetch", side_effect=self._fake):
            out, counts = code_meta.apply_official(rows, keep_unread=False)
        self.assertTrue(code_meta.cached()[("npm", "gone")][1]["confirmed"])
        self.assertNotIn("gone", {r["source_id"] for r in out})
        self.assertNotIn("gone-reg", {r["source_id"] for r in out})   # any directory's row
        self.assertEqual(counts["gone"], 2)

    def test_one_odd_answer_does_not_sink_the_batch(self):
        from directory import code_meta

        def fake(reg, name):
            if name == "unread":
                raise TypeError("odd json")
            return self._fake(reg, name)
        with patch.object(code_meta, "fetch", side_effect=fake):
            out, counts = code_meta.apply_official(self._rows(), keep_unread=False)
        self.assertEqual(counts["official"], 1)
        self.assertIn(("npm", "kept"), code_meta.cached())

    def test_fetchers(self):
        from directory import code_meta
        self.assertEqual(code_meta.go_escape("github.com/Kong/MCP-x"), "github.com/!kong/!m!c!p-x")
        self.assertEqual(code_meta._maven("no-colon"), (0, None))       # never "gone"
        self.assertEqual(code_meta._go("github.com/o/r/test/repos/go/x"), (404, None))
        calls = []

        def get(url, timeout=30, raw=False):
            calls.append(url)
            if "azuresearch" in url:
                return 200, {"data": []}
            return 200, '{"versions": ["1.0.0-preview.97"]}'
        with patch.object(code_meta, "_get", side_effect=get):
            st, meta = code_meta._nuget("Some.Package")
        self.assertEqual(st, 200)
        self.assertIn("semVerLevel=2.0.0", calls[0])
        self.assertIn("v3-flatcontainer/some.package/index.json", calls[1])

        def go_get(url, timeout=30, raw=False):
            if url.endswith("/@latest"):
                return 200, {"Version": "v0.0.0-2024"}
            return 200, "module github.com/o/pyproject\n"       # synthesized: no go.mod
        with patch.object(code_meta, "_get", side_effect=go_get):
            self.assertEqual(code_meta._go("github.com/o/pyproject")[0], 404)


class PackageMatchesRepoTests(_TempDB):
    def test_mirrors_and_platform_binaries_are_not_the_repos_package(self):
        m = code_mcp.package_matches_repo
        for reg, pkg, repo, want in [
                ("npm", "@flemist/playwright-mcp", "microsoft/playwright-mcp", False),
                ("npm", "@playwright/mcp", "microsoft/playwright-mcp", True),
                ("npm", "@iflow-mcp/n8n-mcp", "czlonkowski/n8n-mcp", False),
                ("npm", "n8n-mcp", "czlonkowski/n8n-mcp", True),
                ("npm", "@upstash/context7-mcp", "upstash/context7", True),
                ("npm", "firecrawl-mcp", "mendableai/firecrawl-mcp-server", True),
                ("pypi", "webcam-mcp", "modelcontextprotocol/servers", False),
                ("pypi", "dungngo-ida-pro-mcp", "mrexodia/ida-pro-mcp", False),
                ("npm", "@dbx-app/mcp-linux-arm64-gnu", "dbx-app/dbx", False),
                ("go", "github.com/oraios/serena/test/resources/repos/go/test_repo", "oraios/serena", False),
                ("go", "github.com/Kong/mcp-konnect", "Kong/mcp-konnect", True),
                ("maven", "io.modelcontextprotocol.sdk:mcp", "modelcontextprotocol/java-sdk", True)]:
            self.assertEqual(m(reg, pkg, repo), want, (reg, pkg, repo))

    def test_merge_skips_mirror_and_detach_clears_stored_ones(self):
        gh = [{"full_name": "acme/tool-mcp", "html_url": "https://github.com/acme/tool-mcp",
               "description": "MCP server", "stargazers_count": 5, "topics": ["mcp-server"],
               "created_at": "2025-01-01T00:00:00Z", "pushed_at": "2025-02-01T00:00:00Z"}]
        mirror = code_mcp.eco_row({"ecosystem": "npm", "name": "@mseep/tool-mcp",
                                   "repository_url": "https://github.com/acme/tool-mcp"})
        rows, skipped = code_mcp.merge(gh, [mirror], (set(), set()))
        self.assertEqual(skipped["foreign_package"], 1)
        self.assertFalse(rows[0].get("package_name"))
        with closing(db.connect()) as c:
            row = code_mcp.github_row(gh[0])
            row.update(package_registry="npm", package_name="@mseep/tool-mcp")
            db.upsert_mcp_server(c, row)
            c.commit()
        self.assertEqual(code_sources.detach_foreign_packages(), 1)
        with closing(db.connect()) as c:
            self.assertIsNone(c.execute("SELECT package_name FROM mcp_servers WHERE source='github'").fetchone()[0])


class CodeOnlyCleanupTests(_TempDB):
    def test_self_hosted_access_and_install_wording(self):
        from directory.resources import normalize_mcp_server
        row = {"slug": "x", "name": "x", "endpoint_url": None, "source": "cargo",
               "package_registry": "cargo", "package_name": "x-mcp"}
        item = normalize_mcp_server(row)
        self.assertEqual(item["access"]["model"], "self_hosted")
        self.assertFalse(item["access"]["agent_can_self_serve"])
        self.assertEqual(item["code"]["command_kind"], "install")
        row.update(package_registry="npm", source="npm")
        self.assertEqual(normalize_mcp_server(row)["code"]["command_kind"], "run")

    def test_deleting_a_listing_removes_its_source_pointers(self):
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "p", "name": "p", "endpoint_url": None, "kind": "catalog",
                                     "package_registry": "npm", "package_name": "p",
                                     "source": "npm", "source_id": "p"})
        with db.writer() as c:
            c.execute("DELETE FROM mcp_servers")
        with closing(db.connect()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM listing_sources WHERE kind='mcp'").fetchone()[0], 0)

    def test_package_row_duplicating_a_github_rows_package_is_dropped(self):
        with db.writer() as c:
            gh = code_mcp.github_row({"full_name": "acme/tool-mcp", "description": "MCP server",
                                      "stargazers_count": 1, "topics": ["mcp-server"]})
            gh.update(package_registry="npm", package_name="tool-mcp")
            db.upsert_mcp_server(c, gh)
            db.upsert_mcp_server(c, {"slug": "tool-mcp-npm", "name": "tool-mcp", "endpoint_url": None,
                                     "kind": "catalog", "package_registry": "npm", "package_name": "tool-mcp",
                                     "source_code_url": "https://github.com/someone/else",
                                     "source": "npm", "source_id": "tool-mcp"})
        self.assertEqual(code_sources.drop_duplicate_packages(), {"npm": 1})

    def test_attach_fills_only_stored_github_rows_without_a_package(self):
        def gh(name, **extra):
            row = code_mcp.github_row({"full_name": name, "description": "MCP server",
                                       "stargazers_count": 1, "topics": ["mcp-server"]})
            row.update(extra)
            return row
        empty = gh("acme/tool-mcp")
        taken = gh("acme/other-mcp", package_registry="npm", package_name="other-mcp")
        with db.writer() as c:
            for row in (empty, taken):
                db.upsert_mcp_server(c, row)
        attach = [(empty["source_id"], "npm", "@mseep/tool-mcp"),
                  (empty["source_id"], "npm", "tool-mcp"),
                  (taken["source_id"], "npm", "other-mcp"),
                  ("acme/not-stored-mcp", "npm", "not-stored-mcp")]
        self.assertEqual(code_sources.attach_packages(attach), 1)
        with closing(db.connect()) as c:
            got = dict(c.execute("SELECT source_id, package_registry || ':' || package_name "
                                 "FROM mcp_servers WHERE source='github'").fetchall())
        self.assertEqual(got, {empty["source_id"]: "npm:tool-mcp",
                               taken["source_id"]: "npm:other-mcp"})
