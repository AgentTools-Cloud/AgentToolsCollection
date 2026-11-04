import unittest
from unittest.mock import patch

from directory import crawlers, jobs, prowl


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return next(self.responses)


class ProwlCrawlerTests(unittest.TestCase):
    def test_public_list_sources_are_scheduled(self):
        self.assertIs(crawlers.MCP_CRAWLERS["prowl"], prowl.fetch_prowl_mcp)
        self.assertIs(
            crawlers.ALL_CRAWLERS["flows-litprotocol"],
            jobs.flows_mod.fetch_flows_litprotocol,
        )
        self.assertIs(crawlers.ALL_CRAWLERS["x402-fuchss"], crawlers.fetch_x402_fuchss)

    def test_resource_list_dedup_preserves_new_same_host_resource(self):
        items = [
            {
                "source_id": "known",
                "url": "https://example.com",
                "resource_samples": [{"url": "https://example.com/a"}],
            },
            {
                "source_id": "duplicate",
                "url": "https://example.com",
                "resource_samples": [{"url": "https://example.com/a/"}],
            },
            {
                "source_id": "new-resource",
                "url": "https://example.com",
                "resource_samples": [{"url": "https://example.com/b"}],
            },
        ]
        with patch.object(jobs.db, "connect") as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
                {
                    "source": "test-source",
                    "source_id": "known",
                    "url": "https://example.com",
                    "resource_samples": '[{"url":"https://example.com/a"}]',
                }
            ]
            kept = jobs._filter_claimed_resource_items(items, "test-source")
        self.assertEqual([item["source_id"] for item in kept], ["known", "new-resource"])

    def test_manifest_endpoints_are_concrete_and_deduplicated(self):
        endpoints = prowl._manifest_endpoints({
            "endpoint": "https://example.com/mcp",
            "transport": "streamable-http",
            "transports": [
                {"type": "http", "url": "https://example.com/mcp/"},
                {"type": "http", "url_template": "https://example.com/mcp/{id}"},
                {"type": "stdio", "url": "https://www.npmjs.com/package/example"},
            ],
        })
        self.assertEqual(endpoints, [("https://example.com/mcp", "streamable-http")])

    def test_fetch_maps_public_manifest_endpoint(self):
        discovery = {
            "results": [{
                "id": "service-1",
                "slug": "example",
                "name": "Example",
                "description": "Example service",
                "website_url": "https://example.com",
                "mcp_manifest_url": "https://example.com/.well-known/mcp.json",
                "auth_type": "bearer",
                "category": ["test"],
                "score": {"overall": 82},
                "supports_x402": False,
            }],
            "has_more": False,
        }
        manifest = {
            "transport": "streamable-http",
            "endpoint": "https://api.example.com/mcp",
        }
        client = _Client([_Response(discovery), _Response(manifest)])
        with patch.object(prowl.httpx, "Client", return_value=client):
            rows = prowl.fetch_prowl_mcp()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint_url"], "https://api.example.com/mcp")
        self.assertEqual(rows[0]["source"], "prowl")
        self.assertEqual(rows[0]["auth_method"], "bearer")
        self.assertEqual(rows[0]["confidence"], 0.82)


class FuchssCrawlerTests(unittest.TestCase):
    def test_host_leaderboard_does_not_invent_resource_paths(self):
        payload = {"schemaVersion": "2.0.0", "top": [
            {"host": "api.vendor.example", "score": 88.6, "grade": "A", "endpoints": 8},
            {"host": "api.vendor.example", "score": 86, "grade": "A", "endpoints": 8},
        ]}
        with patch.object(crawlers.httpx, "Client", return_value=_Client([_Response(payload)])):
            rows = crawlers.fetch_x402_fuchss()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://api.vendor.example")
        self.assertEqual(rows[0]["source_id"], "api.vendor.example")
        self.assertEqual(rows[0]["resource_samples"], [])
        self.assertIsNone(rows[0]["confidence"])

    def test_legacy_resource_leaderboard_remains_supported(self):
        payload = {"top": [{"resource": "https://api.vendor.example/v1/check",
                            "score": 87, "grade": "A", "confidence": 0.9}]}
        with patch.object(crawlers.httpx, "Client", return_value=_Client([_Response(payload)])):
            rows = crawlers.fetch_x402_fuchss()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://api.vendor.example")
        self.assertEqual(rows[0]["resource_samples"][0]["url"], "https://api.vendor.example/v1/check")
        self.assertEqual(rows[0]["confidence"], 0.9)

    def test_invalid_host_records_are_not_promoted(self):
        payload = {"top": [None, "api.vendor.example", {"host": 123},
                            {"host": "https://api.vendor.example"},
                            {"host": "api.vendor.example/path"},
                            {"host": "user:password@api.vendor.example"},
                            {"host": "api.vendor.example?token=secret"},
                            {"host": "api.vendor.example#fragment"},
                            {"host": "bad host.example"}, {"host": "127.0.0.1"},
                            {"resource": {}}, {"host": "api.vendor.example"}]}
        with patch.object(crawlers.httpx, "Client", return_value=_Client([_Response(payload)])):
            rows = crawlers.fetch_x402_fuchss()
        self.assertEqual([row["url"] for row in rows], ["https://api.vendor.example"])


if __name__ == "__main__":
    unittest.main()