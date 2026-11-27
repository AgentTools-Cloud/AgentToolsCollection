"""Live 402 quotes outrank directory copies.

A seller pricing per task (Hermes, 2026-11-27) states its prices only in the
402s its POST endpoints answer, and those name a sibling resource
(/task?task=sentiment). x402scan kept a three-week-old copy and overwrote our
listing with it every night. Now:
- verify_x402 asks every advertised resource (POST with the descriptor's
  probe_body) and accepts a same-origin resource in the challenge;
- reverify prices the listing from those quotes and stamps live_quote_at;
- upsert_service keeps the quoted fields unless the incoming copy is newer.
"""

import base64
import json
import unittest
from contextlib import closing
from unittest.mock import patch

import httpx

from directory import crawlers, db, public_http
from test_ingestion_pipeline import TemporaryDatabaseTestCase

ORIGIN = "https://seller.example"
PAY_TO = "0x37239DF5f8ffE8541657624726e0ffc517B38C57"
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PRICES = {"summarize": "5000", "code_review": "30000", "csv_to_json": "10000"}

DESCRIPTOR = {
    "x402Version": 2,
    "resources": [ORIGIN + "/task"],
    "endpoints": [
        {"path": "/task", "method": "POST",
         "probe_body": {"task": "summarize", "prompt": "test"}},
        {"path": "/code_review", "method": "POST", "probe_body": {"prompt": "test"}},
        {"path": "/csv_to_json", "method": "POST", "probe_body": {"prompt": "test"}},
        {"path": "/no_body", "method": "POST"},
    ],
}


def _challenge(task, resource_host=ORIGIN):
    obj = {"x402Version": 2,
           "resource": {"url": f"{resource_host}/task?task={task}"},
           "accepts": [{"scheme": "exact", "network": "eip155:8453",
                        "amount": PRICES[task], "asset": ASSET, "payTo": PAY_TO,
                        "maxTimeoutSeconds": 60,
                        "extra": {"name": "USD Coin", "version": "2"}}]}
    return {"PAYMENT-REQUIRED": base64.b64encode(json.dumps(obj).encode()).decode()}


def _handler(resource_host=ORIGIN, seen=None):
    def handle(request):
        path = request.url.path
        if seen is not None:
            seen.append((request.method, path, request.content))
        if path in ("/.well-known/x402", "/.well-known/x402.json"):
            return httpx.Response(200, json=DESCRIPTOR)
        if request.method == "GET":
            return httpx.Response(405)
        task = path.strip("/")
        if task == "task":
            task = json.loads(request.content).get("task")
        if task not in PRICES:
            return httpx.Response(404)
        return httpx.Response(402, headers=_challenge(task, resource_host), json={})
    return handle


def _verify(handler):
    def client_factory(**kwargs):
        kwargs["transport"] = public_http.PublicTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            inner=httpx.MockTransport(handler))
        return httpx.Client(**kwargs)
    crawlers._QUOTE_CACHE.clear()
    crawlers._WK_CACHE.clear()
    with patch("directory.crawlers.public_http.client", side_effect=client_factory), \
         patch("directory.crawlers._host_safety", return_value="public"):
        return crawlers.verify_x402(ORIGIN + "/task")


class VerifyLiveQuotesTests(unittest.TestCase):
    def test_every_advertised_post_is_quoted(self):
        seen = []
        out = _verify(_handler(seen=seen))
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["verified_url"], ORIGIN + "/task")
        amounts = sorted(q["amount_usdc"] for q in out["live_quotes"])
        self.assertEqual(amounts, [0.005, 0.01, 0.03])
        posts = [(m, p) for m, p, _ in seen if m == "POST"]
        self.assertIn(("POST", "/code_review"), posts)
        self.assertNotIn(("POST", "/no_body"), posts)   # no probe_body, no POST
        self.assertFalse(out["live_quotes_complete"])   # so the span only widens

    def test_foreign_resource_in_challenge_is_refused(self):
        out = _verify(_handler(resource_host="https://relay.example"))
        self.assertNotEqual(out["status"], "verified")
        self.assertFalse(out.get("live_quotes"))

    def test_strict_matching_unchanged_by_default(self):
        obj = {"resource": {"url": ORIGIN + "/task?task=x"}}
        self.assertFalse(crawlers._challenge_matches_url(obj, ORIGIN + "/sentiment"))
        self.assertTrue(crawlers._challenge_matches_url(obj, ORIGIN + "/sentiment", True))
        rel = {"resource": {"url": "/task?task=x"}}
        self.assertTrue(crawlers._challenge_matches_url(rel, ORIGIN + "/sentiment", True))

    def test_quotes_are_memoised_only_within_a_pass(self):
        seen = []
        with crawlers.memoised_quotes():
            _verify(_handler(seen=seen))
            before = len(seen)
            crawlers._WK_CACHE.clear()
            with patch("directory.crawlers.public_http.client",
                       side_effect=lambda **k: httpx.Client(
                           transport=httpx.MockTransport(_handler(seen=seen)), **k)), \
                 patch("directory.crawlers._host_safety", return_value="public"):
                crawlers.verify_x402(ORIGIN + "/task")
            self.assertEqual([x for x in seen[before:] if x[0] == "POST"], [])
        self.assertEqual(crawlers._QUOTE_CACHE, {})
        before = len(seen)
        _verify(_handler(seen=seen))
        self.assertTrue([x for x in seen[before:] if x[0] == "POST"])


class LiveSpanTests(unittest.TestCase):
    def test_span_only_counts_the_listing_payee(self):
        from directory.reverify_x402 import _live_span
        quotes = [{"amount_usdc": 0.005, "pay_to": PAY_TO.lower()},
                  {"amount_usdc": 0.03, "pay_to": PAY_TO},
                  {"amount_usdc": 9.0, "pay_to": "0xother"},
                  {"amount_usdc": None, "pay_to": PAY_TO}]
        self.assertEqual(_live_span(quotes, PAY_TO), (0.005, 0.03))
        self.assertIsNone(_live_span([], PAY_TO))


class X402scanTimestampTests(unittest.TestCase):
    def test_iso_epoch(self):
        self.assertEqual(crawlers._iso_epoch("2026-11-04T08:12:54.741Z"), 1793779974)
        self.assertIsNone(crawlers._iso_epoch("not a date"))
        self.assertIsNone(crawlers._iso_epoch(None))


class UpsertKeepsLiveQuoteTests(TemporaryDatabaseTestCase):
    LIVE_AT = 1_800_000_000

    def _seed(self):
        with db.writer() as c:
            db.upsert_service(c, {
                "slug": "seller-scan", "name": "Seller", "url": ORIGIN + "/task",
                "source": "x402scan", "source_id": "o1",
                "price_min": 0.005, "price_max": 0.03,
                "payment": {"pay_to": PAY_TO, "max_amount_usdc": 0.005}})
            c.execute("UPDATE services SET live_quote_at=? WHERE slug='seller-scan'",
                      (self.LIVE_AT,))

    def _crawl(self, updated_at):
        with db.writer() as c:
            db.upsert_service(c, {
                "slug": "seller-scan", "name": "Seller (stale title)",
                "url": ORIGIN + "/base64", "source": "x402scan", "source_id": "o1",
                "price_min": 0.02, "price_max": 0.10,
                "payment": {"pay_to": PAY_TO, "price_min_usd": 0.02},
                "source_updated_at": updated_at})
        with closing(db.connect()) as c:
            return dict(c.execute("SELECT * FROM services WHERE slug='seller-scan'"
                                  ).fetchone())

    def test_older_copy_keeps_live_quote(self):
        self._seed()
        row = self._crawl(self.LIVE_AT - 86400 * 20)
        self.assertEqual((row["price_min"], row["price_max"]), (0.005, 0.03))
        self.assertEqual(row["url"], ORIGIN + "/task")
        self.assertEqual(json.loads(row["payment"])["max_amount_usdc"], 0.005)
        self.assertEqual(row["name"], "Seller (stale title)")   # not a quoted field

    def test_undated_copy_keeps_live_quote(self):
        self._seed()
        row = self._crawl(None)
        self.assertEqual((row["price_min"], row["price_max"]), (0.005, 0.03))

    def test_newer_copy_replaces_it(self):
        self._seed()
        row = self._crawl(self.LIVE_AT + 60)
        self.assertEqual((row["price_min"], row["price_max"]), (0.02, 0.10))
        self.assertEqual(row["url"], ORIGIN + "/base64")

    def test_no_live_quote_behaves_as_before(self):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "seller-scan", "name": "Seller",
                                  "url": ORIGIN + "/task", "source": "x402scan",
                                  "source_id": "o1", "price_min": 0.005})
        row = self._crawl(None)
        self.assertEqual(row["price_min"], 0.02)


if __name__ == "__main__":
    unittest.main()
