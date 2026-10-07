import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import crawlers, db, jobs, reverify_x402


USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


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
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
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

    def test_non_usdc_amount_is_not_reported_as_usd(self):
        payment_required = _payment_required()
        payment_required["accepts"][0]["asset"] = "0xTokenWith18Decimals"
        payment_required["accepts"][0]["extra"] = {"name": "OTHER"}
        payment = crawlers._extract_payment(payment_required)
        self.assertIsNotNone(payment)
        self.assertIsNone(payment["max_amount_usdc"])
        self.assertIsNone(payment["currency"])

    def test_payment_required_header_lookup_is_case_insensitive(self):
        encoded = base64.b64encode(json.dumps(_payment_required()).encode()).decode()
        for name in ("PAYMENT-REQUIRED", "payment-required"):
            decoded = crawlers._decode_payment_required_header({name: encoded})
            self.assertEqual(decoded, _payment_required())


if __name__ == "__main__":
    unittest.main()