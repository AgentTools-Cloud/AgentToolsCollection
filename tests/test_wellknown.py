import unittest
from unittest.mock import Mock, patch

from directory import crawlers, jobs, wellknown


class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _record(record_id, updated_at, **overrides):
    record = {
        "id": record_id,
        "handle": record_id,
        "name": record_id,
        "kind": "mcp_server",
        "protocols": ["mcp"],
        "status": "live",
        "endpoint": f"https://{record_id}.example/mcp",
        "updatedAt": updated_at,
        "record": f"https://wellknown.network/agents/{record_id}/record.json",
    }
    record.update(overrides)
    return record


class WellknownCrawlerTests(unittest.TestCase):
    def setUp(self):
        wellknown._RECORD_CACHE = None

    def test_since_pagination_advances_without_cursor(self):
        first = [_record("one", "2026-01-01T00:00:01Z"),
                 _record("two", "2026-01-01T00:00:02Z")]
        second = [_record("three", "2026-01-01T00:00:03Z")]
        client = _Client([
            _Response(200, {"agents": first}),
            _Response(200, {"agents": second}),
        ])
        rows = wellknown._fetch_pages(client, page_limit=2, page_delay=0)
        self.assertEqual([row["id"] for row in rows], ["one", "two", "three"])
        self.assertNotIn("cursor", client.requests[1][1])
        self.assertEqual(client.requests[1][1]["since"], "2026-01-01T00:00:01.999Z")
        self.assertEqual(client.requests[1][1]["remote"], "true")

    def test_mcp_source_is_scheduled(self):
        self.assertIs(
            crawlers.MCP_CRAWLERS["wellknown"],
            wellknown.fetch_wellknown_mcp,
        )

    def test_existing_endpoint_only_gains_attribution(self):
        row = {
            "source": "wellknown",
            "source_id": "ag_1",
            "source_url": "https://wellknown.network/agents/example",
            "endpoint_url": "https://api.example/mcp/",
        }
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"id": 42}
        with (
            patch.object(jobs.db, "_record_source") as record_source,
            patch.object(jobs.db, "upsert_mcp_server") as upsert,
        ):
            result = jobs._upsert_wellknown_listing(conn, "mcp", row)
        self.assertEqual(result, (False, 42))
        record_source.assert_called_once_with(
            conn,
            "mcp",
            42,
            ("wellknown", "ag_1", "https://wellknown.network/agents/example"),
        )
        upsert.assert_not_called()

    def test_a2a_source_is_scheduled(self):
        row = {
            "slug": "agent",
            "source": "wellknown",
            "source_id": "ag_1",
            "endpoint_url": "https://agent.example/a2a",
        }
        empty = {"inserted": 0, "updated": 0, "failed": 0,
                 "candidates": 0, "resolved": 0}
        missing_seed = Mock()
        missing_seed.exists.return_value = False
        with (
            patch.object(jobs, "A2A_SEED_FILE", missing_seed),
            patch.object(jobs.a2a_mod, "crawl_directories", return_value=empty),
            patch.object(jobs.agenstry_mod, "crawl_agenstry_a2a", return_value=empty),
            patch.object(jobs.a2a_mod, "crawl_a2aregistry", return_value=empty),
            patch.object(jobs.a2a_mod, "crawl_github_topic", return_value=empty),
            patch.object(jobs.wellknown_mod, "fetch_wellknown_a2a", return_value=[row]),
            patch.object(jobs, "_upsert_wellknown_a2a_rows", return_value=(1, 0, [])) as upsert,
            patch.object(jobs, "_start_run", return_value=1),
            patch.object(jobs, "_finish_run") as finish,
            patch.object(jobs, "cmd_health_a2a") as health,
        ):
            self.assertEqual(jobs.cmd_crawl_a2a(), 0)
        upsert.assert_called_once_with([row])
        finish.assert_called_once_with(1, 1, 0, [], status="ok")
        health.assert_called_once_with(only_unknown=True)

    def test_second_page_failure_surfaces_partial_results(self):
        client = _Client([
            _Response(200, {"agents": [
                _record("one", "2026-01-01T00:00:01Z"),
                _record("two", "2026-01-01T00:00:02Z"),
            ]}),
            _Response(500), _Response(500), _Response(500),
        ])
        with patch.object(wellknown.time, "sleep"):
            with self.assertRaises(crawlers.PartialCrawl) as caught:
                wellknown._fetch_pages(client, page_limit=2, page_delay=0)
        self.assertEqual([row["id"] for row in caught.exception.items], ["one", "two"])
        self.assertIn("page 2", caught.exception.reason)

    def test_429_honors_retry_after(self):
        client = _Client([
            _Response(429, headers={"retry-after": "7"}),
            _Response(200, {"agents": []}),
        ])
        with patch.object(wellknown.time, "sleep") as sleep:
            self.assertEqual(wellknown._fetch_pages(client, page_delay=0), [])
        sleep.assert_called_once_with(7.0)

    def test_non_advancing_overlap_is_partial(self):
        first = [_record("one", "2026-01-01T00:00:01Z"),
                 _record("two", "2026-01-01T00:00:02Z")]
        repeated = [_record("two", "2026-01-01T00:00:02Z"),
                    _record("other", "2026-01-01T00:00:02Z")]
        with self.assertRaisesRegex(crawlers.PartialCrawl, "did not advance"):
            wellknown._fetch_pages(
                _Client([
                    _Response(200, {"agents": first}),
                    _Response(200, {"agents": repeated}),
                ]),
                page_limit=2,
                page_delay=0,
            )

    def test_only_public_concrete_http_endpoints_are_accepted(self):
        accepted = [
            "https://api.example.com/mcp",
            "http://8.8.8.8/mcp",
        ]
        rejected = [
            "npm:@scope/server",
            "pypi:example",
            "https://api.example.com/{env_id}/mcp",
            "https://api.example.com/${TENANT}/mcp",
            "http://localhost:8000/mcp",
            "http://127.0.0.1/mcp",
            "http://10.0.0.2/mcp",
            "http://service.local/mcp",
            "https://github.com/acme/server",
            "https://www.npmjs.com/package/example",
        ]
        for endpoint in accepted:
            self.assertTrue(wellknown._is_online_endpoint(endpoint), endpoint)
        for endpoint in rejected:
            self.assertFalse(wellknown._is_online_endpoint(endpoint), endpoint)

    def test_local_protocol_split_and_attribution(self):
        records = [
            _record("mcp", "2026-01-01T00:00:01Z"),
            _record("both", "2026-01-01T00:00:02Z",
                    kind="agent", protocols=["mcp", "a2a"]),
            _record("a2a", "2026-01-01T00:00:03Z",
                    kind="agent", protocols=["a2a"]),
            _record("package", "2026-01-01T00:00:04Z",
                    endpoint="npm:package"),
            _record("dead", "2026-01-01T00:00:05Z", status="down"),
                _record("duplicate", "2026-01-01T00:00:06Z",
                    endpoint="https://mcp.example/mcp/"),
        ]
        wellknown._RECORD_CACHE = (records, None)
        mcp = wellknown.fetch_wellknown_mcp()
        a2a = wellknown.fetch_wellknown_a2a()
        self.assertEqual({row["source_id"] for row in mcp}, {"mcp", "both"})
        self.assertEqual({row["source_id"] for row in a2a}, {"both", "a2a"})
        self.assertTrue(all(row["source"] == "wellknown" for row in mcp + a2a))
        self.assertTrue(all(row["source_url"].startswith(
            "https://wellknown.network/agents/") for row in mcp + a2a))
        self.assertTrue(all(row["confidence"] is None for row in mcp + a2a))
        self.assertTrue(all(row["card_url"].endswith(
            "/.well-known/agent-card.json") for row in a2a))

    def test_partial_cache_is_filtered_and_re_raised(self):
        records = [_record("mcp", "2026-01-01T00:00:01Z")]
        wellknown._RECORD_CACHE = (records, "upstream stopped")
        with self.assertRaises(crawlers.PartialCrawl) as caught:
            wellknown.fetch_wellknown_mcp()
        self.assertEqual(len(caught.exception.items), 1)
        self.assertEqual(caught.exception.reason, "upstream stopped")


if __name__ == "__main__":
    unittest.main()