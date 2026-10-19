import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import agenstry, crawlers, db, jobs


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
    def setUp(self):
        crawlers._WK_CACHE.clear()

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

    def test_x402scan_uses_only_paid_resource_as_primary_url(self):
        origin = "https://agents.example.test"
        paid = origin + "/v1/property"
        client = _Client([
            _Response(200, {"result": {"data": {"json": [
                {"id": "origin-1"},
            ]}}}),
            _Response(200, {"result": {"data": {"json": [{
                "id": "origin-1",
                "origin": origin,
                "title": "Property Check",
                "resources": [
                    {"resource": origin + "/a2a", "accepts": []},
                    {"resource": paid, "accepts": [{
                        "scheme": "exact",
                        "network": "eip155:8453",
                        "maxAmountRequired": "1000000",
                        "asset": "0x" + "1" * 40,
                        "payTo": "0x" + "2" * 40,
                    }]},
                    {"resource": origin + "/v1/reports", "accepts": []},
                ],
            }]}}}),
        ])
        with patch.object(crawlers.httpx, "Client", return_value=client):
            rows = crawlers.fetch_x402scan()

        self.assertEqual(rows[0]["url"], paid)
        self.assertEqual(rows[0]["well_known_url"], origin + "/.well-known/x402")
        self.assertEqual(rows[0]["resource_count"], 1)
        self.assertEqual(
            [sample["url"] for sample in rows[0]["resource_samples"]],
            [paid],
        )

    def test_x402scan_multiple_paid_resources_never_uses_origin(self):
        origin = "https://agents.example.test"
        paid_urls = [origin + "/v1/a", origin + "/v1/b"]
        resources = [{
            "resource": url,
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:8453",
                "maxAmountRequired": "1000000",
                "asset": "0x" + "1" * 40,
                "payTo": "0x" + "2" * 40,
            }],
        } for url in reversed(paid_urls)]
        client = _Client([
            _Response(200, {"result": {"data": {"json": [
                {"id": "origin-1"},
            ]}}}),
            _Response(200, {"result": {"data": {"json": [{
                "id": "origin-1", "origin": origin,
                "resources": resources,
            }]}}}),
        ])
        with patch.object(crawlers.httpx, "Client", return_value=client):
            rows = crawlers.fetch_x402scan()

        self.assertEqual(rows[0]["url"], paid_urls[0])
        self.assertNotEqual(rows[0]["url"], origin)

    def test_registry_required_bearer_header_maps_to_api_key(self):
        remote = {
            "headers": [{
                "name": "Authorization",
                "value": "Bearer {api_key}",
                "description": "Bearer followed by your API key",
                "isRequired": True,
                "isSecret": True,
            }],
        }
        self.assertEqual(crawlers._registry_auth_method(remote),
                         "bearer_api_key")

    def test_registry_optional_headers_do_not_gate_access(self):
        remote = {
            "headers": [{
                "name": "X-Trace-Id",
                "isRequired": False,
            }],
        }
        self.assertIsNone(crawlers._registry_auth_method(remote))

    def test_registry_other_required_headers_are_not_open(self):
        remote = {
            "headers": [{
                "name": "X-API-Key",
                "isRequired": True,
                "isSecret": True,
            }],
        }
        self.assertEqual(crawlers._registry_auth_method(remote),
                         "required_headers")

    def test_removed_mcp_catalog_and_pulsemcp_are_not_sources(self):
        for name, fetcher in (("mcp-catalog", "fetch_mcp_catalog"), ("pulsemcp", "fetch_pulsemcp")):
            self.assertNotIn(name, crawlers.MCP_CRAWLERS)
            self.assertFalse(hasattr(crawlers, fetcher))

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

    def test_probe_health_gets_manifest_advertised_get_after_head_200(self):
        endpoint = "https://head200-get402.example.test/x402/agent-check"
        descriptor = {
            "x402Version": 2,
            "resources": [{
                "endpoint": endpoint,
                "method": "GET",
                "accepts": [{
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": "0x" + "1" * 40,
                    "amount": "1000",
                    "payTo": "0x" + "2" * 40,
                }],
            }],
        }

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, url):
                return _Response(200 if url == endpoint else 404)

            def get(self, url):
                if url == endpoint:
                    return _Response(402)
                if url.endswith("/.well-known/x402"):
                    return _Response(200, descriptor)
                return _Response(404)

            def post(self, _url, json):
                raise AssertionError(f"unexpected POST with {json!r}")

        crawlers._WK_CACHE.clear()
        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_status"], 402)
        self.assertTrue(result["x402"])

    def test_probe_health_does_not_post_unadvertised_endpoint(self):
        endpoint = "https://api.example.test/v1/x402/services/check"

        class Client:
            posted = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(404)

            def get(self, url):
                # The descriptor is served but advertises no resource, so the
                # endpoint itself stays unreachable and must not be POSTed to.
                if url == endpoint:
                    return _Response(404)
                return _Response(200, {"x402Version": 2, "endpoints": []})

            def post(self, _url, json):
                self.posted = True
                raise AssertionError(f"unexpected POST with {json!r}")

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "down")
        self.assertFalse(client.posted)

    def test_probe_health_requires_explicit_post_probe_body(self):
        endpoint = "https://api.example.test/v1/x402/services/check"
        descriptor = {
            "x402Version": 2,
            "endpoints": [{"resource": endpoint, "method": "POST"}],
        }

        class Client:
            posted = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(405)

            def get(self, url):
                if url == endpoint:
                    return _Response(405)
                return _Response(200, descriptor)

            def post(self, _url, json):
                self.posted = True
                raise AssertionError(f"unexpected POST with {json!r}")

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "down")
        self.assertFalse(client.posted)

    def test_probe_health_rejects_cross_origin_descriptor_for_post(self):
        endpoint = "https://victim.example.test/pay"
        descriptor = "https://attacker.example.test/.well-known/x402"

        class Client:
            posted = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(405)

            def get(self, url):
                if url == endpoint:
                    return _Response(405)
                if url == descriptor:
                    return _Response(200, {"endpoints": [{
                        "resource": endpoint, "method": "POST",
                        "probe_body": {"action": "mutate"},
                    }]})
                return _Response(404)

            def post(self, _url, json):
                self.posted = True
                raise AssertionError(f"unexpected POST with {json!r}")

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint, descriptor)
        self.assertEqual(result["status"], "down")
        self.assertFalse(client.posted)

    def test_probe_health_rejects_cross_origin_descriptor_redirect(self):
        endpoint = "https://victim.example.test/pay"
        descriptor = "https://victim.example.test/.well-known/x402"

        class Client:
            posted = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(405)

            def get(self, url):
                if url == endpoint:
                    return _Response(405)
                if url == descriptor:
                    response = _Response(200, {"endpoints": [{
                        "resource": endpoint, "method": "POST",
                        "probe_body": {"action": "mutate"},
                    }]})
                    response.url = "https://attacker.example.test/x402.json"
                    return response
                return _Response(404)

            def post(self, _url, json):
                self.posted = True
                raise AssertionError(f"unexpected POST with {json!r}")

        client = Client()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            result = crawlers.probe_health(endpoint, descriptor)
        self.assertEqual(result["status"], "down")
        self.assertFalse(client.posted)

    def test_probe_health_preserves_network_scoped_probe_body(self):
        endpoint = "https://api.example.test/paid"
        descriptor = {
            "base": {
                "network": "eip155:8453",
                "resources": [{
                    "url": endpoint, "method": "POST",
                    "probe_body": {"query": "health"},
                }],
            },
        }

        class Client:
            posted = None

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
        self.assertTrue(result["x402"])
        self.assertEqual(client.posted, (endpoint, {"query": "health"}))

    def test_probe_health_canonical_get_500_overrides_descriptor_200(self):
        endpoint = "https://api.example.test/paid"
        descriptor = {
            "resources": [{"endpoint": endpoint, "method": "GET"}],
        }

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, url):
                return _Response(200 if url == endpoint else 404)

            def get(self, url):
                if url == endpoint:
                    return _Response(500)
                if url.endswith("/.well-known/x402"):
                    return _Response(200, descriptor)
                return _Response(404)

            def post(self, _url, json):
                raise AssertionError(f"unexpected POST with {json!r}")

        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(endpoint)
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["http_status"], 500)

    def test_probe_health_endpoint_500_overrides_unrelated_descriptor_200(self):
        endpoint = "https://api.example.test/paid"

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, url):
                return _Response(200 if url.endswith("/.well-known/x402") else 500)

            def get(self, url):
                if url.endswith("/.well-known/x402"):
                    return _Response(200, {"resources": []})
                return _Response(404)

            def post(self, _url, json):
                raise AssertionError(f"unexpected POST with {json!r}")

        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(
                endpoint, "https://api.example.test/.well-known/x402",
            )
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["http_status"], 500)

    def test_probe_health_skips_manifest_inspection_when_x402_is_known(self):
        endpoint = "https://known-x402.example.test/pay"

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, url):
                if url != endpoint:
                    raise AssertionError(f"unexpected HEAD {url}")
                return _Response(200)

            def get(self, url):
                raise AssertionError(f"unexpected GET {url}")

            def post(self, url, json):
                raise AssertionError(f"unexpected POST {url} {json!r}")

        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(
                endpoint, inspect_payment=False,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_status"], 200)
        self.assertFalse(result["x402"])

    def test_known_post_without_probe_body_is_degraded_not_down(self):
        endpoint = "https://known-x402.example.test/pay"

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def head(self, _url):
                return _Response(405)

            def get(self, _url):
                return _Response(405)

            def post(self, _url, json):
                raise AssertionError(f"unexpected POST with {json!r}")

        with patch.object(crawlers.httpx, "Client", return_value=Client()):
            result = crawlers.probe_health(
                endpoint, inspect_payment=False, method_hint="POST",
            )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["http_status"], 405)

    def test_health_job_uses_stored_method_for_known_x402(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "directory.db")
            db.init_db(db_path)
            with db.writer(db_path) as conn:
                for slug, x402_ok in (("unknown-x402", 0), ("known-x402", 1)):
                    _, service_id = db.upsert_service(conn, {
                        "slug": slug, "name": slug,
                        "url": f"https://{slug}.example.test/pay",
                        "source": "test", "source_id": slug,
                        "health": "unknown",
                    })
                    conn.execute(
                        "UPDATE services SET x402_ok=?, resource_samples=? WHERE id=?",
                        (x402_ok, json.dumps([{
                            "url": f"https://{slug}.example.test/pay",
                            "method": "GET",
                        }]), service_id),
                    )

            real_connect, real_writer = db.connect, db.writer
            inspected = {}

            def probe(url, _well_known, inspect_payment=True, method_hint=None):
                inspected[url] = (inspect_payment, method_hint)
                return {
                    "status": "ok", "latency_ms": 1,
                    "http_status": 200, "x402": False,
                }

            with (
                patch.object(
                    jobs.db, "connect",
                    side_effect=lambda *_args, **kwargs: real_connect(
                        db_path, read_only=kwargs.get("read_only", False)
                    ),
                ),
                patch.object(
                    jobs.db, "writer",
                    side_effect=lambda *_args, **kwargs: real_writer(
                        db_path, immediate=kwargs.get("immediate", False)
                    ),
                ),
                patch.object(jobs.crawlers, "probe_health", side_effect=probe),
                patch.object(jobs, "cmd_ownership_recheck"),
                patch.object(jobs, "cmd_rescore"),
                patch.object(jobs, "_maintain_down_since"),
            ):
                self.assertEqual(jobs.cmd_health(only_unknown=True), 0)

            self.assertEqual(inspected, {
                "https://unknown-x402.example.test/pay": (True, None),
                "https://known-x402.example.test/pay": (False, "GET"),
            })
            with real_connect(db_path, read_only=True) as conn:
                known = conn.execute(
                    "SELECT x402_ok FROM services WHERE slug='known-x402'"
                ).fetchone()
                self.assertEqual(known["x402_ok"], 1)


if __name__ == "__main__":
    unittest.main()