import io
import tarfile
import unittest
from unittest.mock import patch

from directory import claim_invites, crawlers

_retired = patch.object(crawlers, "url_retired", return_value=False)


def setUpModule():
    # The privacy/retirement lookup needs a DB and key; these tests are
    # about parsing only.
    _retired.start()


def tearDownModule():
    _retired.stop()


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class _MSClient:
    """Fake modelscope list API: Hosted x Category partitions with a 300 window."""

    def __init__(self, rows_by_part, cats, fail_values=()):
        self.rows_by_part = rows_by_part
        self.cats = cats
        self.fail_values = set(fail_values)
        self.bodies = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def put(self, url, json=None):
        self.bodies.append(json)
        crit = json["Criterion"]
        hosted = crit[0]["BoolValues"][0]
        if len(crit) == 1:
            agg = [{"Value": v, "Count": n} for v, n in self.cats.get(hosted, [])]
            return _Resp(200, {"Data": {"McpServer": {"TotalCount": 0, "McpServers": []},
                                        "FiledAgg": {"Category": agg}}})
        value = crit[1]["StringValues"][0]
        if value in self.fail_values:
            return _Resp(500, {})
        rows = [r for r in self.rows_by_part.get((hosted, value), [])
                if json["Query"] in r.get("Name", "")]
        size, page = json["PageSize"], json["PageNumber"]
        rows = rows[:300]
        chunk = rows[(page - 1) * size: page * size]
        return _Resp(200, {"Data": {"McpServer": {"TotalCount": len(rows), "McpServers": chunk}}})


def _ms(id_, name, cfg=None, sse=None, hosted=True, path=None):
    return {"Id": id_, "Name": name, "ChineseName": "", "Abstract": f"{name} desc",
            "Path": path or f"owner/{name}", "Hosted": hosted, "Category": ["search"],
            "StreamableHTTPServerConfig": cfg or "", "SSEServerConfig": sse or "",
            "FromSiteUrl": "https://github.com/owner/" + name}


class RemoteEndpointTests(unittest.TestCase):
    def test_query_placeholder_is_dropped_and_marked_keyed(self):
        self.assertEqual(crawlers._remote_endpoint("https://mcp.amap.com/mcp?key=<YOUR_KEY>"),
                         ("https://mcp.amap.com/mcp", True))

    def test_path_or_host_placeholder_is_rejected(self):
        self.assertIsNone(crawlers._remote_endpoint("https://{tenant}.example.com/mcp"))
        self.assertIsNone(crawlers._remote_endpoint("https://x.example.com/mcp/<ID>/sse"))
        self.assertIsNone(crawlers._remote_endpoint("https://x.example.com/${PATH}"))

    def test_bare_ip_and_non_http_rejected(self):
        self.assertIsNone(crawlers._remote_endpoint("http://39.99.229.0:80/sse"))
        self.assertIsNone(crawlers._remote_endpoint("stdio://x"))
        self.assertIsNone(crawlers._remote_endpoint(None))

    def test_local_and_intranet_hosts_rejected(self):
        for u in ("http://localhost:8000/mcp", "http://LOCALHOST/sse",
                  "http://api.localhost/mcp", "http://my-box.local:3000/mcp",
                  "http://mcp-server:8080/mcp", "https://svc.internal/mcp",
                  "https://example.com/mcp"):
            self.assertIsNone(crawlers._remote_endpoint(u), u)
        self.assertEqual(crawlers._remote_endpoint("https://mcp.amap.com/sse"),
                         ("https://mcp.amap.com/sse", False))

    def test_real_credentials_in_query_are_dropped(self):
        self.assertEqual(
            crawlers._remote_endpoint("https://mcp.didichuxing.com/mcp-servers?key=g2owxHfMsc"),
            ("https://mcp.didichuxing.com/mcp-servers", True))
        self.assertEqual(
            crawlers._remote_endpoint("https://a.example.cn/mcp?source=modelscope&access_token=abc"),
            ("https://a.example.cn/mcp", True))
        self.assertEqual(
            crawlers._remote_endpoint("https://bizveris.kuaicha365.com/mcp?source=modelscope"),
            ("https://bizveris.kuaicha365.com/mcp?source=modelscope", False))
        self.assertIsNone(crawlers._remote_endpoint("https://u:p@mcp.example.cn/mcp"))

    def test_plain_url_kept(self):
        self.assertEqual(crawlers._remote_endpoint("https://xsct.ai/mcp#frag"),
                         ("https://xsct.ai/mcp", False))

    def test_config_parses_python_repr_and_json(self):
        repr_cfg = "[{'mcpServers': {'a': {'type': 'streamable_http', 'url': 'https://a.example.com/mcp'}}}]"
        json_cfg = '{"mcpServers": {"b": {"type": "sse", "url": "https://b.example.com/sse"}}}'
        self.assertEqual(crawlers._config_remotes(repr_cfg),
                         [("https://a.example.com/mcp", "streamable_http")])
        self.assertEqual(crawlers._config_remotes(json_cfg),
                         [("https://b.example.com/sse", "sse")])
        self.assertEqual(crawlers._config_remotes("not json https://c.example.com/mcp"),
                         [("https://c.example.com/mcp", None)])
        self.assertEqual(crawlers._config_remotes(""), [])


class ModelScopeTests(unittest.TestCase):
    def test_partitions_split_large_categories_and_keep_only_public_remotes(self):
        big = [_ms(i, f"{'abc'[i % 3]}srv{i}") for i in range(1, 451)]
        big[0]["StreamableHTTPServerConfig"] = (
            "[{'mcpServers': {'x': {'type': 'streamable_http', 'url': 'https://x.example.com/mcp'}}}]")
        # Row past the 300 window: only reachable through the name split.
        big[400]["SSEServerConfig"] = "[{'mcpServers': {'y': {'url': 'https://y.example.com/sse'}}}]"
        small = [
            _ms(1001, "proxy", cfg="[{'mcpServers': {'p': {'url': 'https://mcp.api-inference.modelscope.net/u/streamable_http'}}}]"),
            _ms(1002, "keyed", cfg="[{'mcpServers': {'k': {'url': 'https://k.example.com/mcp?key=<KEY>'}}}]", hosted=False),
            _ms(1003, "local", hosted=False),
            _ms(1004, "dupe", cfg="[{'mcpServers': {'d': {'url': 'https://x.example.com/mcp/'}}}]"),
        ]
        client = _MSClient(
            {(True, "search"): big, (True, "finance"): [small[0], small[3]],
             (False, "finance"): small[1:3]},
            {True: [("search", len(big)), ("finance", 2), ("empty", 0)],
             False: [("finance", 2), ("broken", 5)]},
            fail_values={"broken"})
        with patch.object(crawlers.httpx, "Client", return_value=client), \
                patch.object(crawlers.time, "sleep"):
            rows = crawlers.fetch_modelscope_mcp()
        by_ep = {r["endpoint_url"]: r for r in rows}
        self.assertEqual(set(by_ep), {"https://x.example.com/mcp", "https://y.example.com/sse",
                                      "https://k.example.com/mcp"})
        self.assertEqual(by_ep["https://y.example.com/sse"]["transport"], "sse")
        self.assertEqual(by_ep["https://k.example.com/mcp"]["auth_method"], "required")
        self.assertEqual(by_ep["https://k.example.com/mcp"]["confidence"], 0.5)
        x = by_ep["https://x.example.com/mcp"]
        self.assertEqual(x["source"], "modelscope")
        self.assertEqual(x["source_url"], "https://modelscope.cn/mcp/servers/owner/bsrv1")
        self.assertEqual(x["transport"], "streamable-http")
        self.assertEqual(x["confidence"], 0.7)
        self.assertTrue(x["slug"].endswith("-modelscope"))
        queries = {b["Query"] for b in client.bodies}
        self.assertIn("a", queries)  # large partition was split
        self.assertTrue(all(b["PageNumber"] * b["PageSize"] <= 300 for b in client.bodies))

    def test_transient_5xx_is_retried(self):
        rows = [_ms(1, "srv", cfg="[{'mcpServers': {'x': {'url': 'https://x.example.com/mcp'}}}]")]
        client = _MSClient({(True, "search"): rows}, {True: [("search", 1)], False: []})
        real_put, state = client.put, {"n": 0}

        def flaky(url, json=None):
            state["n"] += 1
            if state["n"] == 2:  # first partition page fails once
                return _Resp(503, {})
            return real_put(url, json=json)

        client.put = flaky
        with patch.object(crawlers.httpx, "Client", return_value=client), \
                patch.object(crawlers.time, "sleep"):
            out = crawlers.fetch_modelscope_mcp()
        self.assertEqual([r["endpoint_url"] for r in out], ["https://x.example.com/mcp"])

    def test_request_budget_stops_cleanly(self):
        client = _MSClient({(True, "search"): [_ms(i, f"s{i}") for i in range(1, 50)]},
                           {True: [("search", 49)], False: [("search", 1)]})
        with patch.object(crawlers.httpx, "Client", return_value=client), \
                patch.object(crawlers.time, "sleep"):
            rows = crawlers.fetch_modelscope_mcp(max_requests=1)
        self.assertEqual(rows, [])
        self.assertEqual(len(client.bodies), 1)


def _tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _GetClient:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, *_a, **_k):
        return self.resp


class DockerCatalogTests(unittest.TestCase):
    def test_only_remote_servers_with_fixed_urls(self):
        blob = _tarball({
            "mcp-registry-main/servers/ais/server.yaml": (
                "name: ais\ntype: remote\nmeta:\n  category: boating\n  tags: [boating, remote]\n"
                "about:\n  title: AIS Fleet\n  description: Vessel data\n"
                "remote:\n  transport_type: sse\n  url: https://mcp.aisfleet.com/sse\n"),
            "mcp-registry-main/servers/apify/server.yaml": (
                "name: apify\ntype: remote\nabout:\n  title: Apify Remote\n"
                "source:\n  project: https://github.com/apify/apify-mcp-server\n"
                "remote:\n  transport_type: streamable-http\n  url: https://mcp.apify.com\n"
                "  headers:\n    Authorization: 'Bearer ${APIFY_API_KEY}'\n"),
            "mcp-registry-main/servers/gh/server.yaml": (
                "name: gh\ntype: remote\noauth:\n  - provider: github\n"
                "remote:\n  transport_type: streamable-http\n  url: https://api.githubcopilot.com/mcp/\n"),
            "mcp-registry-main/servers/tmpl/server.yaml": (
                "name: tmpl\ntype: remote\nremote:\n  url: https://{{tenant}}.example.com/mcp\n"),
            "mcp-registry-main/servers/local/server.yaml": "name: local\ntype: server\nimage: mcp/local\n",
            "mcp-registry-main/servers/bad/server.yaml": "name: [unclosed\n",
            "mcp-registry-main/README.md": "x",
        })
        with patch.object(crawlers.httpx, "Client", return_value=_GetClient(_Resp(200, content=blob))):
            rows = crawlers.fetch_docker_mcp()
        by = {r["source_id"]: r for r in rows}
        self.assertEqual(set(by), {"ais", "apify", "gh"})
        self.assertEqual(by["ais"]["transport"], "sse")
        self.assertEqual(by["ais"]["tags"], ["boating"])
        self.assertIsNone(by["ais"]["auth_method"])
        self.assertEqual(by["apify"]["auth_method"], "required")
        self.assertEqual(by["apify"]["source_code_url"], "https://github.com/apify/apify-mcp-server")
        self.assertEqual(by["gh"]["auth_method"], "oauth")
        self.assertEqual(by["gh"]["source_url"], "https://hub.docker.com/mcp/server/gh/overview")
        self.assertEqual(by["gh"]["source"], "docker-mcp")

    def test_download_failure_returns_empty(self):
        with patch.object(crawlers.httpx, "Client", return_value=_GetClient(_Resp(503))):
            self.assertEqual(crawlers.fetch_docker_mcp(), [])


class RegistrationTests(unittest.TestCase):
    def test_sources_registered_and_labelled(self):
        self.assertIs(crawlers.MCP_CRAWLERS["modelscope"], crawlers.fetch_modelscope_mcp)
        self.assertIs(crawlers.MCP_CRAWLERS["docker-mcp"], crawlers.fetch_docker_mcp)
        self.assertIn("modelscope", claim_invites.SOURCE_LABELS)
        self.assertIn("docker-mcp", claim_invites.SOURCE_LABELS)


if __name__ == "__main__":
    unittest.main()
