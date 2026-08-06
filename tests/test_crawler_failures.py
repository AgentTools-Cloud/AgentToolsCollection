import unittest
from unittest.mock import patch

from directory import agenstry, crawlers


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    @property
    def content(self):
        import json
        return json.dumps(self._payload).encode()

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
            _Response(429),
            _Response(200, {"results": []}),
        ])
        with (
            patch.object(agenstry.httpx, "Client", return_value=client),
            patch.object(agenstry.time, "sleep"),
        ):
            self.assertEqual(agenstry.fetch_agenstry_mcp(), [])
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.requests[0][1]["params"]["limit"], 50)
        self.assertNotIn("offset", client.requests[0][1]["params"])

    def test_agenstry_failure_surfaces_after_retry(self):
        client = _Client([_Response(429)] * 4)
        with (
            patch.object(agenstry.httpx, "Client", return_value=client),
            patch.object(agenstry.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed after retries"):
                agenstry.fetch_agenstry_mcp()

    def test_agenstry_non_transient_failure_is_immediate(self):
        client = _Client([_Response(401)])
        with (
            patch.object(agenstry.httpx, "Client", return_value=client),
            patch.object(agenstry.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                agenstry.fetch_agenstry_mcp()
        self.assertEqual(client.calls, 1)

    def test_agenstry_full_window_is_not_paginated(self):
        client = _Client([_Response(200, {"results": [{}] * 50})])
        with patch.object(agenstry.httpx, "Client", return_value=client):
            self.assertEqual(agenstry.fetch_agenstry_mcp(), [])
        self.assertEqual(client.calls, 1)

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

    def test_probe_health_uses_descriptor_advertised_post(self):
        endpoint = "https://api.example.test/v1/x402/services/check"
        descriptor = {
            "x402Version": 2,
            "endpoints": [{
                "resource": endpoint,
                "method": "POST",
                "probe_body": {},
            }],
        }

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(405)

            def get(self, url):
                if url == endpoint:
                    return _Response(405)
                if url.endswith("/.well-known/x402"):
                    return _Response(200, descriptor)
                return _Response(404)

            def post(self, url, json):
                self.posted = (url, json)
                return _Response(402)

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_status"], 402)
        self.assertTrue(result["x402"])
        self.assertEqual(client.posted, (endpoint, {}))

    def test_well_known_200_does_not_hide_post_402(self):
        endpoint = "https://api.example.test/v1/x402/services/check"
        well_known = "https://api.example.test/.well-known/x402"
        descriptor = {
            "x402Version": 2,
            "endpoints": [{
                "resource": endpoint,
                "method": "POST",
                "probe_body": {},
            }],
        }

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, url):
                return _Response(200 if url == well_known else 405)

            def get(self, url):
                if url == endpoint:
                    return _Response(405)
                if url == well_known:
                    return _Response(200, descriptor)
                return _Response(404)

            def post(self, url, json):
                self.posted = (url, json)
                return _Response(402)

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint, well_known)
        self.assertEqual(result["http_status"], 402)
        self.assertTrue(result["x402"])
        self.assertEqual(client.posted, (endpoint, {}))

    def test_probe_health_does_not_post_unadvertised_endpoint(self):
        endpoint = "https://api.example.test/v1/x402/services/check"

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(404)

            def get(self, _url):
                return _Response(200, {"x402Version": 2, "endpoints": []})

            def post(self, _url, json):
                raise AssertionError(f"unexpected POST with {json!r}")

        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "down")


if __name__ == "__main__":
    unittest.main()