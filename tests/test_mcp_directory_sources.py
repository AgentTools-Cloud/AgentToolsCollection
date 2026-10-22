import unittest
from unittest.mock import patch

from directory import agenstry, crawlers, directories, jobs


FEED = [
    {"kind": "mcp", "id": "x/a", "name": "a", "url": "https://glama.ai/mcp/servers/x/a", "transport": "http"},
    {"kind": "mcp", "id": "x/b", "name": "b", "url": "https://mcp.b.example/mcp", "transport": "http"},
    {"kind": "mcp", "id": "x/e", "name": "e", "url": "https://homepage.example/", "transport": "http"},
    {"kind": "mcp", "id": "x/c", "name": "c", "url": "", "transport": None},
    {"kind": "mcp", "id": "x/d", "name": "d", "url": "https://mcp.b.example/mcp/", "transport": "sse"},
    {"kind": "a2a_agent", "id": "ag", "url": "https://agent.example/.well-known/agent-card.json"},
    {"kind": "a2a_agent", "id": "ag2", "url": "https://agent.example/other"},
    {"kind": "paid_api", "id": "p", "url": "https://pay.example/.well-known/agent-card.json",
     "provider_url": "https://pay.example/", "documentation_url": "https://pay.example/api/v1/chat",
     "payment_protocol": "x402", "payment_network": "solana", "min_price_usd": 0.001},
    {"kind": "paid_api", "id": "q", "url": "https://nopay.example/", "payment_protocol": None},
    {"kind": "skill", "id": "s", "url": "https://github.com/x/y/blob/main/SKILL.md"},
]


class AgenstryFeedTests(unittest.TestCase):
    def test_mcp_rows_skip_catalog_and_empty(self):
        rows = agenstry.feed_mcp_rows(FEED)
        self.assertEqual([r["endpoint_url"] for r in rows], ["https://mcp.b.example/mcp"])
        self.assertEqual(rows[0]["transport"], "streamable-http")

    def test_per_host_cap(self):
        recs = [{"kind": "mcp", "id": str(i), "url": f"https://spam.example/m{i}", "transport": "http"}
                for i in range(50)]
        recs = [dict(r, url=f"https://spam.example/mcp/{r['id']}") for r in recs]
        self.assertEqual(len(agenstry.feed_mcp_rows(recs)), agenstry._FEED_PER_HOST)

    def test_a2a_domains(self):
        self.assertEqual(agenstry.feed_a2a_domains(FEED), ["agent.example"])

    def test_paid_api_rows(self):
        with patch.object(agenstry, "fetch_feed", return_value=FEED), \
             patch.object(directories, "_url_acceptable", return_value=True):
            rows = directories.fetch_agenstry_paid()
        self.assertEqual([r["url"] for r in rows], ["https://pay.example"])
        self.assertEqual(rows[0]["chains"], ["solana"])


class FeedDownloadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(agenstry.db, "DEFAULT_DB_PATH", tmp.name + "/x.db"))
        self.enterContext(patch.object(agenstry, "_feed_cache", None))
        self.enterContext(patch.object(agenstry.time, "sleep"))

    def _resp(self, status, content=b"", headers=None):
        class R:
            status_code = status
            def raise_for_status(self): pass
        r = R(); r.content = content; r.headers = headers or {}
        return r

    def test_429_is_final_and_not_retried(self):
        get = patch.object(agenstry.httpx.Client, "get",
                           return_value=self._resp(429, headers={"retry-after": "3600"}))
        with get as g, self.assertRaises(RuntimeError):
            agenstry.fetch_feed()
        self.assertEqual(g.call_count, 1)

    def test_download_is_cached_on_disk(self):
        import gzip, json
        blob = gzip.compress(json.dumps({"kind": "mcp", "id": "a"}).encode())
        with patch.object(agenstry.httpx.Client, "get", return_value=self._resp(200, blob)) as g:
            self.assertEqual(len(agenstry.fetch_feed()), 1)
            agenstry._feed_cache = None
            self.assertEqual(len(agenstry.fetch_feed()), 1)
        self.assertEqual(g.call_count, 1)


class A2ARegistryGlobalTests(unittest.TestCase):
    def test_live_card_preferred_snapshot_fallback_and_filters(self):
        from directory import a2a
        import json as _json
        agents = [
            {"id": "1", "status": "ACTIVE", "visibility": "public", "protocolStd": "a2a",
             "manifestUrl": "https://live.example/.well-known/agent-card.json",
             "source_agent_card": _json.dumps({"name": "snap1", "url": "https://live.example/a2a"})},
            {"id": "2", "status": "ACTIVE", "visibility": "public",
             "manifestUrl": "https://dead.example/.well-known/agent-card.json",
             "source_agent_card": _json.dumps({"name": "snap2", "url": "https://dead.example/a2a"})},
            {"id": "3", "status": "INACTIVE", "manifestUrl": "https://x.example/"},
            {"id": "4", "status": "ACTIVE", "protocolStd": "mcp", "manifestUrl": "https://y.example/"},
        ]

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"total": 4, "agents": agents}

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return R()

        def card(base, client=None):
            if "live" in base:
                return {"name": "live1", "url": "https://live.example/a2a"}, base
            raise RuntimeError("down")
        captured = {}
        with patch.object(a2a.public_http, "client", return_value=C()), \
             patch.object(a2a, "fetch_agent_card", side_effect=card), \
             patch.object(a2a, "card_to_row", side_effect=lambda c, u, **k: {"name": c["name"], "card_url": u, **k}), \
             patch.object(a2a.db, "with_retry", side_effect=lambda fn: captured.setdefault("n", 0) or (2, 0)):
            out = a2a.crawl_a2a_registry_global()
        self.assertEqual(out["resolved"], 2)
        self.assertEqual(out["inserted"], 2)


class RegistrationTests(unittest.TestCase):
    def test_registered(self):
        self.assertIs(crawlers.MCP_CRAWLERS["agenstry-feed"], agenstry.fetch_agenstry_feed_mcp)
        self.assertIs(crawlers.ALL_CRAWLERS["agenstry-paid"], directories.fetch_agenstry_paid)


if __name__ == "__main__":
    unittest.main()
