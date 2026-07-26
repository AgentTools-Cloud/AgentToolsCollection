import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import crawlers, db, jobs


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
            result = jobs._approve(sub_id, payment=payment)
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

    def test_v2_payment_required_header_extracts_usdc_price(self):
        encoded = base64.b64encode(json.dumps(_payment_required()).encode()).decode()
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
        self.assertEqual(verdict["payment"]["max_amount_usdc"], 1.0)
        self.assertEqual(verdict["payment"]["network"], "eip155:8453")

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