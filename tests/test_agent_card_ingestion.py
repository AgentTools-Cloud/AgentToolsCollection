"""Real fetch-to-ingestion paths, using temporary DBs and mocked HTTP only."""

from unittest.mock import patch

import httpx
from directory import a2a, agenstry
from test_ingestion_pipeline import TemporaryDatabaseTestCase, _row


class AgentCardIngestionTests(TemporaryDatabaseTestCase):
    def clients(self, handler):
        return patch.object(a2a.public_http, "client", side_effect=lambda **kwargs:
                            httpx.Client(transport=httpx.MockTransport(handler), **kwargs))

    def test_invalid_live_registry_card_cannot_clear_existing_valid_endpoint(self):
        existing = _row("a2a", "registry-existing", source="a2aregistry", source_id="agent-1",
                        endpoint_url="https://agent.example/a2a",
                        card_url="https://agent.example/.well-known/agent-card.json")
        self.seed("a2a", existing)
        before = self.listings("a2a")[0]
        snapshot = {"id": "agent-1", "name": "Old registry snapshot",
                    "url": "https://agent.example/a2a",
                    "wellKnownURI": existing["card_url"], "skills": [{"id": "old"}]}
        def handler(request):
            if str(request.url).split("?")[0] == a2a.A2AREGISTRY_API:
                return httpx.Response(200, json={"agents": [snapshot], "total": 1})
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Invalid live card", "url": None})
            return httpx.Response(404)
        with self.clients(handler):
            result = a2a.crawl_a2aregistry(max_pages=1)
        self.assertEqual(result["resolved"], 1)
        after = self.listings("a2a")[0]
        for key in ("endpoint_url", "card_url", "name", "skills", "source_id"):
            self.assertEqual(after[key], before[key], key)

    def test_agenstry_opt_in_invalid_genuine_card_reaches_rejection_audit(self):
        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Invalid agent", "url": None})
            return httpx.Response(404)
        with self.clients(handler), patch.object(agenstry, "_fetch_sitemap_paths", return_value=[
                "https://agenstry.com/agents/invalid.example"]):
            result = agenstry.crawl_agenstry_a2a(workers=1)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.issues()[0]["reason"], "missing_endpoint")
        self.assertEqual(self.listings("a2a"), [])

    def test_agenstry_homepage_metadata_does_not_create_missing_endpoint_issue(self):
        def handler(request):
            if request.url.path == "/.well-known/agent.json":
                return httpx.Response(200, json={"name": "Real Agent", "url": "/a2a"})
            if request.url.path == "/":
                return httpx.Response(200, json={"name": "Site metadata"})
            return httpx.Response(404)
        with self.clients(handler), patch.object(agenstry, "_fetch_sitemap_paths", return_value=[
                "https://agenstry.com/agents/agent.example"]):
            result = agenstry.crawl_agenstry_a2a(workers=1)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.listings("a2a")[0]["endpoint_url"], "https://agent.example/a2a")
