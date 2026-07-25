import unittest
from unittest.mock import patch

from directory import agenstry, crawlers


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = 0
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


class CrawlerFailureTests(unittest.TestCase):
    def test_agenstry_retries_once(self):
        client = _Client([
            _Response(401),
            _Response(200, {"results": []}),
        ])
        with (
            patch.object(agenstry.httpx, "Client", return_value=client),
            patch.object(agenstry.time, "sleep"),
        ):
            self.assertEqual(agenstry.fetch_agenstry_mcp(), [])
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.requests[0][1]["params"]["limit"], 50)

    def test_agenstry_failure_surfaces_after_retry(self):
        client = _Client([_Response(401), _Response(401)])
        with (
            patch.object(agenstry.httpx, "Client", return_value=client),
            patch.object(agenstry.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed after retry"):
                agenstry.fetch_agenstry_mcp()

    def test_x402scan_failure_surfaces(self):
        client = _Client([OSError("upstream unavailable")])
        with patch.object(crawlers.httpx, "Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "tRPC fetch failed"):
                crawlers.fetch_x402scan()

    def test_mcp_catalog_missing_key_surfaces(self):
        with patch.object(crawlers, "_mcp_catalog_anon_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "anon key"):
                crawlers.fetch_mcp_catalog()

    def test_retired_chiark_is_not_scheduled(self):
        self.assertNotIn("chiark", crawlers.MCP_CRAWLERS)


if __name__ == "__main__":
    unittest.main()