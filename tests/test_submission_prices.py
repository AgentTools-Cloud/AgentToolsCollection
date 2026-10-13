import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import crawlers, db, jobs, reverify_x402


USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDT0_XLAYER = "0x779ded0c9e1022225f8e0630b35a9b54be713736"


def _payment_required() -> dict:
    return {
        "x402Version": 2,
        "resource": {"url": "https://audit-tools.ai/api/evaluations"},
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "1000000",
            "asset": USDC_BASE,
            "payTo": "0x95de884E9eb3F90E496200693d94a636942a130D",
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        }],
    }


class _Response:
    def __init__(self, status: int, payload=None, headers=None, url=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.url = url
        self.content = json.dumps(payload).encode() if payload is not None else b"{}"


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return next(self.responses)

    def post(self, *_args, **_kwargs):
        return next(self.responses)


class _RoutingClient:
    def __init__(self, responses):
        self.responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **_kwargs):
        return self.responses.get(("GET", url), _Response(404))

    def post(self, url, **_kwargs):
        return self.responses.get(("POST", url), _Response(404))


class SubmissionPriceTests(unittest.TestCase):
    def _approve(self, payload: dict, payment=None) -> dict:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        with db.writer(db_path) as conn:
            sub_id = db.create_submission(conn, payload)
        real_connect = db.connect
        real_writer = db.writer
        with (
            patch.object(
                jobs.db,
                "connect",
                side_effect=lambda *_args, **kwargs: real_connect(
                    db_path, read_only=kwargs.get("read_only", False)
                ),
            ),
            patch.object(
                jobs.db,
                "writer",
                side_effect=lambda *_args, **_kwargs: real_writer(db_path),
            ),
            patch.object(jobs.crawlers, "check_health", return_value="ok"),
            patch.object(jobs.mailer, "send_approval_email"),
        ):
            verdict = ({
                "status": "verified",
                "verified_url": payload["url"],
                "payment": payment,
            } if payment else None)
            result = jobs._approve(sub_id, verdict=verdict)
        self.assertIsNotNone(result)
        with db.connect(db_path, read_only=True) as conn:
            return dict(conn.execute("SELECT * FROM services").fetchone())

    def test_rest_fixed_price_maps_to_min_and_max(self):
        row = self._approve({
            "name": "Audit Tools",
            "url": "https://audit-tools.ai/api/evaluations",
            "price_usdc": 1.0,
        })
        self.assertEqual(row["price_min"], 1.0)
        self.assertEqual(row["price_max"], 1.0)

    def test_explicit_range_wins_over_fixed_and_detected_price(self):
        row = self._approve(
            {
                "name": "Tiered API",
                "url": "https://example.com/paid",
                "price_usdc": 1.0,
                "price_min_usdc": 0.25,
                "price_max_usdc": 2.5,
            },
            payment={"max_amount_usdc": 9.0},
        )
        self.assertEqual(row["price_min"], 0.25)
        self.assertEqual(row["price_max"], 2.5)
        self.assertEqual(row["currency"], "USDC")

    def test_explicit_usdc_price_wins_over_detected_currency(self):
        row = self._approve(
            {
                "name": "Owner priced API",
                "url": "https://example.com/paid",
                "price_usdc": 1.0,
            },
            payment={"max_amount_usdc": 0.02, "currency": "USDT0"},
        )
        self.assertEqual(row["price_min"], 1.0)
        self.assertEqual(row["price_max"], 1.0)
        self.assertEqual(row["currency"], "USDC")

    def test_one_sided_usdc_range_does_not_mix_detected_currency(self):
        row = self._approve(
            {
                "name": "Owner priced API",
                "url": "https://example.com/paid",
                "price_min_usdc": 1.0,
            },
            payment={"max_amount_usdc": 0.02, "currency": "USDT0"},
        )
        self.assertEqual(row["price_min"], 1.0)
        self.assertIsNone(row["price_max"])
        self.assertEqual(row["currency"], "USDC")

    def test_inverted_explicit_usdc_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "price_min_usdc"):
            self._approve({
                "name": "Invalid range",
                "url": "https://example.com/paid",
                "price_min_usdc": 2.0,
                "price_max_usdc": 1.0,
            })

    def test_approval_uses_verified_paid_resource_url(self):
        submitted = "https://agents.example.test/a2a"
        paid = "https://agents.example.test/v1/property"
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        with db.writer(db_path) as conn:
            sub_id = db.create_submission(conn, {
                "name": "Property Check", "url": submitted,
            })
        real_connect, real_writer = db.connect, db.writer
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
            patch.object(jobs.crawlers, "check_health", return_value="ok"),
            patch.object(jobs.crawlers, "fetch_wellknown_resources", return_value={}),
            patch.object(jobs.mailer, "send_approval_email"),
        ):
            result = jobs._approve(sub_id, verdict={
                "status": "verified", "verified_url": paid,
                "payment": {
                    "scheme": "exact", "network": "eip155:8453",
                    "amount_raw": "1000000", "max_amount_usdc": 1.0,
                    "pay_to": "0x123",
                },
            })
        self.assertEqual(result["url"], paid)
        with real_connect(db_path, read_only=True) as conn:
            row = conn.execute("SELECT url,mcp_url FROM services").fetchone()
            self.assertEqual(row["url"], paid)
            self.assertIsNone(row["mcp_url"])

    def test_v2_payment_required_header_extracts_usdc_price(self):
        encoded = base64.b64encode(json.dumps(_payment_required()).encode()).decode()
        crawlers._WK_CACHE.clear()
        client = _Client([
            _Response(404),
            _Response(404),
            _Response(402, {}, {"payment-required": encoded}),
        ])
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402("https://audit-tools.ai/api/evaluations")
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(
            verdict["verified_url"],
            "https://audit-tools.ai/api/evaluations",
        )
        self.assertEqual(verdict["payment"]["max_amount_usdc"], 1.0)
        self.assertEqual(verdict["payment"]["network"], "eip155:8453")
        self.assertEqual(verdict["payment"]["networks"], ["eip155:8453"])

    def test_v2_bazaar_items_manifest_extracts_usdc_price(self):
        manifest = {
            "x402Version": 2,
            "items": [{
                "resource": "https://audit-tools.ai/api/evaluations",
                "type": "http",
                "x402Version": 2,
                "accepts": _payment_required()["accepts"],
                "lastUpdated": "2026-07-25T00:00:00Z",
            }],
        }
        payment = crawlers._extract_payment(manifest)
        self.assertIsNotNone(payment)
        self.assertEqual(payment["max_amount_usdc"], 1.0)
        self.assertEqual(payment["asset"], USDC_BASE)

    def test_nested_accepts_preserve_all_networks(self):
        payment = crawlers._extract_payment({
            "accepts": [{
                "resource": "https://example.test/pay",
                "accepts": [
                    {"scheme": "exact", "network": "eip155:8453"},
                    {"scheme": "exact", "network": "eip155:137"},
                ],
            }],
        })
        self.assertEqual(
            payment["networks"], ["eip155:8453", "eip155:137"]
        )
        self.assertEqual(len(payment["accepts"]), 2)

    def test_first_usable_mainnet_accept_is_atomic_payment(self):
        payment = crawlers._extract_payment({
            "accepts": [
                {
                    "scheme": "exact", "network": "eip155:84532",
                    "amount": "1", "asset": "0x" + "1" * 40,
                    "payTo": "0x" + "2" * 40,
                },
                {
                    "scheme": "exact", "network": "eip155:8453",
                    "amount": "1000000", "asset": USDC_BASE,
                    "payTo": "0x" + "3" * 40,
                },
            ],
        })
        self.assertTrue(crawlers._valid_payment(payment))
        self.assertEqual(payment["network"], "eip155:8453")
        self.assertEqual(payment["pay_to"], "0x" + "3" * 40)
        self.assertEqual(payment["networks"], ["eip155:8453"])

    def test_numeric_mainnet_chain_id_is_valid(self):
        payment = {
            "scheme": "exact", "network": 8453,
            "asset": USDC_BASE, "amount_raw": "1000000",
            "pay_to": "0x" + "1" * 40,
        }
        self.assertTrue(crawlers._valid_payment(payment))

    def test_complete_descriptor_still_requires_live_402(self):
        origin = "https://agents.example.test"
        paid_url = origin + "/v1/property"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = paid_url
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "x402Version": 2,
                "resources": [{
                    "url": paid_url,
                    "method": "GET",
                    "accepts": payment_required["accepts"],
                }],
                "accepts": payment_required["accepts"],
            }),
            ("GET", paid_url): _Response(200),
            ("POST", paid_url): _Response(200),
            ("GET", origin + "/a2a"): _Response(200),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(origin + "/a2a")

        self.assertEqual(verdict["status"], "uncertain")
        self.assertIsNone(verdict["verified_url"])

    def test_empty_accept_does_not_verify_402(self):
        origin = "https://empty.example.test"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(404),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", origin + "/pay"): _Response(402, {"accepts": [{}]}),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(origin + "/pay")

        self.assertEqual(verdict["status"], "uncertain")
        self.assertIsNone(verdict["verified_url"])

    def test_invalid_payment_requirements_are_rejected(self):
        valid = {
            "scheme": "exact", "network": "eip155:8453",
            "asset": USDC_BASE, "amount_raw": "1000000",
            "pay_to": "0x" + "1" * 40,
        }
        self.assertTrue(crawlers._valid_payment(valid))
        invalid = (
            {**valid, "scheme": "banana"},
            {**valid, "network": "banana"},
            {**valid, "amount_raw": "0"},
            {**valid, "amount_raw": "-1"},
            {**valid, "amount_raw": "wat"},
            {**valid, "pay_to": "0x123"},
            {**valid, "asset": "USDC"},
        )
        for payment in invalid:
            with self.subTest(payment=payment):
                self.assertFalse(crawlers._valid_payment(payment))

    def test_origin_payment_does_not_replace_advertised_resource_payment(self):
        origin = "https://atomic.example.test"
        paid_url = origin + "/paid"

        def payment(url, pay_to):
            return {
                "x402Version": 2,
                "resource": {"url": url},
                "accepts": [{
                    "scheme": "exact", "network": "eip155:8453",
                    "amount": "1000000", "asset": USDC_BASE,
                    "payTo": pay_to,
                }],
            }

        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "x402Version": 2,
                "resources": [{"url": paid_url, "method": "GET"}],
            }),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", paid_url): _Response(
                402, payment(paid_url, "0x" + "1" * 40)
            ),
            ("GET", origin + "/a2a"): _Response(200),
            ("GET", origin): _Response(
                402, payment(origin, "0x" + "2" * 40)
            ),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(origin + "/a2a")

        self.assertEqual(verdict["verified_url"], paid_url)
        self.assertEqual(verdict["payment"]["pay_to"], "0x" + "1" * 40)

    def test_descriptor_probes_paid_resource_after_first_five(self):
        origin = "https://many.example.test"
        paid_url = origin + "/paid"
        resources = [
            {"url": origin + f"/free-{index}", "method": "GET"}
            for index in range(5)
        ] + [{"url": paid_url, "method": "GET"}]
        payment_required = _payment_required()
        payment_required["resource"]["url"] = paid_url
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "x402Version": 2, "resources": resources,
            }),
            **{("GET", item["url"]): _Response(200) for item in resources[:-1]},
            **{("POST", item["url"]): _Response(200) for item in resources[:-1]},
            ("GET", paid_url): _Response(402, payment_required),
            ("GET", origin + "/a2a"): _Response(200),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(origin + "/a2a")

        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], paid_url)

    def test_invalid_endpoint_402_still_tries_valid_origin(self):
        origin = "https://fallback.example.test"
        endpoint = origin + "/a2a"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = origin
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(404),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", endpoint): _Response(402, {"accepts": [{}]}),
            ("GET", origin): _Response(402, payment_required),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)

        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], origin)

    def test_native_service_does_not_absorb_another_paid_resource(self):
        task = {
            "table": "services", "id": 1,
            "slug": "protocol", "name": "Protocol",
            "description": None,
            "endpoint": "https://example.test/a2a",
            "homepage": None, "category": None,
            "source": None, "source_id": None,
            "delivery": "x402", "was": 0,
            "verdict": {
                "status": "verified",
                "verified_url": "https://example.test/paid",
                "payment": {
                    "scheme": "exact", "network": "eip155:8453",
                    "networks": ["eip155:8453"],
                    "amount_raw": "1000000", "max_amount_usdc": 1.0,
                    "pay_to": "0x123",
                },
            },
        }
        self.assertFalse(
            reverify_x402._endpoint_is_x402(task)
        )

    def test_descriptor_resource_is_the_verified_url(self):
        origin = "https://agents.example.test"
        paid_url = origin + "/v1/property"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = paid_url
        encoded = base64.b64encode(
            json.dumps(payment_required).encode()
        ).decode()
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "x402Version": 2,
                "resources": [{
                    "url": paid_url,
                    "method": "GET",
                    "accepts": payment_required["accepts"],
                }],
                "accepts": payment_required["accepts"],
            }),
            ("GET", paid_url): _Response(
                402, {}, {"payment-required": encoded}
            ),
            ("GET", origin + "/a2a"): _Response(405),
            ("POST", origin + "/a2a"): _Response(200),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(origin + "/a2a")

        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], paid_url)

    def test_verifier_does_not_post_without_explicit_probe_body(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/mutating"

        class Client(_RoutingClient):
            posted = False

            def post(self, url, **kwargs):
                self.posted = True
                return super().post(url, **kwargs)

        client = Client({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "resources": [{"url": endpoint, "method": "POST"}],
            }),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", endpoint): _Response(405),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertFalse(client.posted)
        self.assertEqual(verdict["status"], "uncertain")

    def test_post_only_without_descriptor_stays_uncertain(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/post-only"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(404),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", endpoint): _Response(405),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertEqual(verdict["status"], "uncertain")

    def test_empty_first_descriptor_does_not_hide_second_valid_descriptor(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/paid"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = endpoint
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {}),
            ("GET", origin + "/.well-known/x402.json"): _Response(200, {
                "resources": [{"url": endpoint, "method": "GET"}],
            }),
            ("GET", endpoint): _Response(402, payment_required),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], endpoint)

    def test_verifier_ignores_cross_origin_well_known_hint(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/pay"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = endpoint
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(404),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", endpoint): _Response(402, payment_required),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(
                endpoint, "https://attacker.example.test/x402.json",
            )
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], endpoint)

    def test_resource_discovery_ignores_cross_origin_well_known_hint(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/pay"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "resources": [{"url": endpoint, "method": "GET"}],
            }),
        })
        crawlers._WK_CACHE.clear()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            resources = crawlers.fetch_wellknown_resources(
                endpoint, "https://attacker.example.test/x402.json",
            )
        self.assertEqual(resources["resource_count"], 1)
        self.assertEqual(resources["resource_samples"][0]["url"], endpoint)

    def test_resource_discovery_drops_cross_origin_advertised_resource(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/pay"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "baseUrl": "https://attacker.example.test",
                "resources": [
                    {"url": "https://victim.example.test/pay", "method": "GET"},
                    {"path": "/relative", "method": "GET"},
                    {"url": endpoint, "method": "GET"},
                ],
            }),
        })
        crawlers._WK_CACHE.clear()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            resources = crawlers.fetch_wellknown_resources(endpoint)
        self.assertEqual(resources["resource_count"], 2)
        self.assertEqual(
            {item["url"] for item in resources["resource_samples"]},
            {origin + "/relative", endpoint},
        )

    def test_empty_primary_descriptor_falls_back_to_json_descriptor(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/paid"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {}),
            ("GET", origin + "/.well-known/x402.json"): _Response(200, {
                "resources": [{"url": endpoint, "method": "GET"}],
            }),
        })
        crawlers._WK_CACHE.clear()
        with patch.object(crawlers.httpx, "Client", return_value=client):
            resources = crawlers.fetch_wellknown_resources(endpoint)
        self.assertTrue(resources["descriptor_observed"])
        self.assertEqual(resources["resource_count"], 1)

    def test_verifier_uses_explicit_same_origin_probe_body(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/paid"
        payment_required = _payment_required()
        payment_required["resource"]["url"] = endpoint
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "resources": [{
                    "url": endpoint, "method": "POST", "probe_body": {},
                }],
            }),
            ("POST", endpoint): _Response(402, payment_required),
            ("GET", endpoint): _Response(405),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_url"], endpoint)

    def test_verifier_rejects_cross_origin_get_redirect(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/pay"
        client = _RoutingClient({
            ("GET", origin + "/.well-known/x402"): _Response(404),
            ("GET", origin + "/.well-known/x402.json"): _Response(404),
            ("GET", endpoint): _Response(
                302, headers={"location": "https://attacker.example/pay"},
                url=endpoint,
            ),
            ("GET", origin): _Response(200),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertEqual(verdict["status"], "rejected")
        self.assertIn("unsafe URL", verdict["evidence"][0])

    def test_verifier_does_not_forward_post_body_cross_origin(self):
        origin = "https://safe.example.test"
        endpoint = origin + "/paid"

        class Client(_RoutingClient):
            attacker_posts = 0

            def post(self, url, **kwargs):
                if url.startswith("https://attacker.example"):
                    self.attacker_posts += 1
                return super().post(url, **kwargs)

        client = Client({
            ("GET", origin + "/.well-known/x402"): _Response(200, {
                "resources": [{
                    "url": endpoint, "method": "POST", "probe_body": {},
                }],
            }),
            ("POST", endpoint): _Response(
                307, headers={"location": "https://attacker.example/paid"},
                url=endpoint,
            ),
        })
        crawlers._WK_CACHE.clear()
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(endpoint)
        self.assertEqual(verdict["status"], "rejected")
        self.assertEqual(client.attacker_posts, 0)

    def test_non_usdc_amount_is_not_reported_as_usd(self):
        payment_required = _payment_required()
        payment_required["accepts"][0]["asset"] = "0xTokenWith18Decimals"
        payment_required["accepts"][0]["extra"] = {"name": "OTHER"}
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNotNone(payment)
        self.assertIsNone(payment["max_amount_usdc"])
        self.assertIsNone(payment["currency"])

    def test_untrusted_usdc_ticker_does_not_define_value(self):
        payment_required = _payment_required()
        payment_required["accepts"][0].update({
            "asset": "0x" + "9" * 40,
            "extra": {"name": "USDC", "version": "2"},
        })
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNone(payment["max_amount_usdc"])
        self.assertIsNone(payment["currency"])

    def test_unmarked_numeric_price_is_not_treated_as_usd(self):
        payment_required = _payment_required()
        payment_required["accepts"][0].update({
            "asset": "0x" + "9" * 40,
            "amount": None,
            "price": 0.25,
            "extra": {"name": "USDC"},
        })
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNone(payment["max_amount_usdc"])
        self.assertIsNone(payment["currency"])

    def test_usdc_contract_on_wrong_chain_is_not_priced(self):
        payment_required = _payment_required()
        payment_required["accepts"][0]["network"] = "eip155:1"
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNone(payment["max_amount_usdc"])
        self.assertIsNone(payment["currency"])

    def test_xlayer_usdt0_amount_is_priced_without_relabeling_as_usdc(self):
        payment_required = _payment_required()
        payment_required["accepts"][0].update({
            "network": "eip155:196",
            "asset": USDT0_XLAYER,
            "amount": "20000",
            "extra": {"name": "USD₮0", "version": "1"},
        })
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNotNone(payment)
        self.assertEqual(payment["max_amount_usdc"], 0.02)
        self.assertEqual(payment["currency"], "USDT0")
        self.assertEqual(payment["asset"], USDT0_XLAYER)

    def test_xlayer_usdt0_numeric_network_alias_is_priced(self):
        payment_required = _payment_required()
        payment_required["accepts"][0].update({
            "network": 196,
            "asset": USDT0_XLAYER,
            "amount": "20000",
        })
        payment = crawlers._extract_payment(payment_required)
        self.assertEqual(payment["max_amount_usdc"], 0.02)
        self.assertEqual(payment["currency"], "USDT0")

    def test_sui_coin_type_asset_is_valid(self):
        payment = {
            "scheme": "exact", "network": "sui:mainnet",
            "asset": "0x2::sui::SUI", "amount_raw": "1",
            "pay_to": "0x" + "1" * 64,
        }
        self.assertTrue(crawlers._valid_payment(payment))

    def test_approval_persists_detected_usdt0_currency(self):
        row = self._approve(
            {
                "name": "Entropy Signal",
                "url": "https://example.com/signal",
            },
            payment={
                "scheme": "exact", "network": "eip155:196",
                "asset": USDT0_XLAYER, "amount_raw": "20000",
                "max_amount_usdc": 0.02, "currency": "USDT0",
                "pay_to": "0x" + "1" * 40,
            },
        )
        self.assertEqual(row["price_min"], 0.02)
        self.assertEqual(row["price_max"], 0.02)
        self.assertEqual(row["currency"], "USDT0")

    def test_mirror_service_persists_detected_usdt0_currency(self):
        service = reverify_x402._build_service({
            "slug": "entropy", "name": "Entropy",
            "endpoint": "https://example.com/mcp",
            "delivery": "mcp", "source": "registry", "source_id": "one",
        }, {
            "status": "verified",
            "verified_url": "https://example.com/signal",
            "payment": {
                "scheme": "exact", "network": "eip155:196",
                "asset": USDT0_XLAYER, "amount_raw": "20000",
                "max_amount_usdc": 0.02, "currency": "USDT0",
                "pay_to": "0x" + "1" * 40,
            },
        })
        self.assertEqual(service["price_min"], 0.02)
        self.assertEqual(service["price_max"], 0.02)
        self.assertEqual(service["currency"], "USDT0")

    def test_native_reverify_persists_detected_usdt0_currency(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        endpoint = "https://example.com/signal"
        with db.writer(db_path) as conn:
            _, service_id = db.upsert_service(conn, {
                "slug": "entropy", "name": "Entropy", "url": endpoint,
                "source": "submission", "source_id": "entropy",
                "health": "ok",
            })
        real_connect, real_writer = db.connect, db.writer
        task = {
            "table": "services", "id": service_id, "slug": "entropy",
            "name": "Entropy", "description": None, "endpoint": endpoint,
            "homepage": None, "well_known": None, "category": None,
            "source": None, "source_id": None, "delivery": "x402", "was": 0,
        }

        def probe(value):
            value["verdict"] = {
                "status": "verified", "verified_url": endpoint,
                "payment": {
                    "scheme": "exact", "network": "eip155:196",
                    "asset": USDT0_XLAYER, "amount_raw": "20000",
                    "max_amount_usdc": 0.02, "currency": "USDT0",
                    "pay_to": "0x" + "1" * 40,
                },
            }
            value["resources"] = {
                "descriptor_observed": True,
                "resource_count": 1,
                "resource_samples": [{
                    "url": endpoint,
                    "accepts": [value["verdict"]["payment"]],
                }],
                "price_min": 0.02, "price_max": 0.02,
            }
            return value

        with (
            patch.object(
                reverify_x402.db, "connect",
                side_effect=lambda *_args, **kwargs: real_connect(
                    db_path, read_only=kwargs.get("read_only", False)
                ),
            ),
            patch.object(
                reverify_x402.db, "writer",
                side_effect=lambda *_args, **kwargs: real_writer(
                    db_path, immediate=kwargs.get("immediate", False)
                ),
            ),
            patch.object(reverify_x402, "_gather", return_value=[task]),
            patch.object(reverify_x402, "_probe", side_effect=probe),
            patch.object(
                reverify_x402, "_privacy_suppressed_metadata",
                return_value=False,
            ),
        ):
            reverify_x402.reverify(targets=("services",), workers=1)

        with real_connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT price_min,price_max,currency,payment "
                "FROM services WHERE id=?", (service_id,),
            ).fetchone()
            self.assertEqual(row["price_min"], 0.02)
            self.assertEqual(row["price_max"], 0.02)
            self.assertEqual(row["currency"], "USDT0")
            self.assertEqual(json.loads(row["payment"])["currency"], "USDT0")

    def test_native_reverify_clears_stale_unpriced_currency(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        endpoint = "https://example.com/signal"
        with db.writer(db_path) as conn:
            _, service_id = db.upsert_service(conn, {
                "slug": "entropy", "name": "Entropy", "url": endpoint,
                "source": "submission", "source_id": "entropy",
                "health": "ok", "price_min": 0.02, "price_max": 0.02,
                "currency": "USDT0",
            })
        real_connect, real_writer = db.connect, db.writer
        task = {
            "table": "services", "id": service_id, "slug": "entropy",
            "name": "Entropy", "description": None, "endpoint": endpoint,
            "homepage": None, "well_known": None, "category": None,
            "source": None, "source_id": None, "delivery": "x402", "was": 1,
        }
        payment = {
            "scheme": "exact", "network": "eip155:196",
            "asset": "0x" + "3" * 40, "amount_raw": "20000",
            "max_amount_usdc": None, "currency": None,
            "pay_to": "0x" + "1" * 40,
        }

        def probe(value):
            value["verdict"] = {
                "status": "verified", "verified_url": endpoint,
                "payment": payment,
            }
            value["resources"] = {
                "descriptor_observed": True,
                "resource_count": 1,
                "resource_samples": [{"url": endpoint, "accepts": [payment]}],
                "price_min": None, "price_max": None,
            }
            return value

        with (
            patch.object(
                reverify_x402.db, "connect",
                side_effect=lambda *_args, **kwargs: real_connect(
                    db_path, read_only=kwargs.get("read_only", False)
                ),
            ),
            patch.object(
                reverify_x402.db, "writer",
                side_effect=lambda *_args, **kwargs: real_writer(
                    db_path, immediate=kwargs.get("immediate", False)
                ),
            ),
            patch.object(reverify_x402, "_gather", return_value=[task]),
            patch.object(reverify_x402, "_probe", side_effect=probe),
            patch.object(
                reverify_x402, "_privacy_suppressed_metadata",
                return_value=False,
            ),
        ):
            reverify_x402.reverify(targets=("services",), workers=1)

        with real_connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT price_min,price_max,currency,payment "
                "FROM services WHERE id=?", (service_id,),
            ).fetchone()
            self.assertIsNone(row["price_min"])
            self.assertIsNone(row["price_max"])
            self.assertIsNone(row["currency"])
            self.assertIsNone(json.loads(row["payment"])["currency"])

    def test_native_reverify_preserves_submitter_explicit_price(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        endpoint = "https://example.com/owner-priced"
        with db.writer(db_path) as conn:
            submission_id = db.create_submission(conn, {
                "name": "Owner priced", "url": endpoint,
                "price_usdc": 7.0,
            })
            _, service_id = db.upsert_service(conn, {
                "slug": "owner-priced", "name": "Owner priced",
                "url": endpoint, "source": "submission",
                "source_id": f"sub:{submission_id}", "health": "ok",
                "price_min": 7.0, "price_max": 7.0, "currency": "USDC",
            })
        real_connect, real_writer = db.connect, db.writer

        with (
            patch.object(
                reverify_x402.db, "connect",
                side_effect=lambda *_args, **kwargs: real_connect(
                    db_path, read_only=kwargs.get("read_only", False)
                ),
            ),
            patch.object(
                reverify_x402.db, "writer",
                side_effect=lambda *_args, **kwargs: real_writer(
                    db_path, immediate=kwargs.get("immediate", False)
                ),
            ),
            patch.object(reverify_x402, "_probe", side_effect=lambda task: {
                **task,
                "verdict": {
                    "status": "verified", "verified_url": endpoint,
                    "payment": {
                        "scheme": "exact", "network": "eip155:196",
                        "asset": "0x" + "3" * 40, "amount_raw": "20000",
                        "max_amount_usdc": None, "currency": None,
                        "pay_to": "0x" + "1" * 40,
                    },
                },
                "resources": {
                    "descriptor_observed": True,
                    "resource_count": 1,
                    "resource_samples": [{"url": endpoint}],
                    "price_min": None, "price_max": None,
                },
            }),
            patch.object(
                reverify_x402, "_privacy_suppressed_metadata",
                return_value=False,
            ),
        ):
            reverify_x402.reverify(targets=("services",), workers=1)

        with real_connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT price_min,price_max,currency FROM services WHERE id=?",
                (service_id,),
            ).fetchone()
            self.assertEqual(tuple(row), (7.0, 7.0, "USDC"))

    def test_native_reverify_preserves_merged_submission_price(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = str(Path(temp_dir.name) / "directory.db")
        db.init_db(db_path)
        endpoint = "https://example.com/merged"
        with db.writer(db_path) as conn:
            submission_id = db.create_submission(conn, {
                "name": "Merged", "url": endpoint, "price_usdc": 7.0,
            })
            _, service_id = db.upsert_service(conn, {
                "slug": "merged", "name": "Merged", "url": endpoint,
                "source": "x402scan", "source_id": "scan:merged",
                "health": "ok", "price_min": 7.0, "price_max": 7.0,
                "currency": "USDC",
            })
            db._record_source(conn, "x402", service_id, (
                "submission", f"sub:{submission_id}", None,
            ))
        real_connect, real_writer = db.connect, db.writer
        with (
            patch.object(
                reverify_x402.db, "connect",
                side_effect=lambda *_args, **kwargs: real_connect(
                    db_path, read_only=kwargs.get("read_only", False)
                ),
            ),
            patch.object(
                reverify_x402.db, "writer",
                side_effect=lambda *_args, **kwargs: real_writer(
                    db_path, immediate=kwargs.get("immediate", False)
                ),
            ),
            patch.object(reverify_x402, "_probe", side_effect=lambda task: {
                **task,
                "verdict": {"status": "verified", "verified_url": endpoint,
                            "payment": {"scheme": "exact", "network": "base",
                                        "amount_raw": "500000", "max_amount_usdc": 0.5,
                                        "currency": "USDC", "pay_to": "0x" + "1" * 40}},
                "resources": {"descriptor_observed": True, "resource_count": 1,
                              "resource_samples": [{"url": endpoint}],
                              "price_min": 0.5, "price_max": 0.5},
            }),
            patch.object(reverify_x402, "_privacy_suppressed_metadata", return_value=False),
        ):
            reverify_x402.reverify(targets=("services",), workers=1)
        with real_connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT price_min,price_max,currency FROM services WHERE id=?",
                (service_id,),
            ).fetchone()
            self.assertEqual(tuple(row), (7.0, 7.0, "USDC"))

    def test_payment_required_header_lookup_is_case_insensitive(self):
        encoded = base64.b64encode(json.dumps(_payment_required()).encode()).decode()
        for name in ("PAYMENT-REQUIRED", "payment-required"):
            decoded = crawlers._decode_payment_required_header({name: encoded})
            self.assertEqual(decoded, _payment_required())


if __name__ == "__main__":
    unittest.main()