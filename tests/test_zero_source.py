import unittest
from unittest.mock import patch

from directory import crawlers, jobs, zero


def cap(url, protocol="x402", amount="0.01", asset="USD", status="healthy",
        act=0, rate="0.00", state="unrated", reviews=0, **extra):
    return {"url": url, "protocol": protocol, "method": "POST",
            "name": url.rsplit("/", 1)[-1],
            "cost": {"amount": amount, "asset": asset},
            "availabilityStatus": status, "activationCount": act,
            "rating": {"successRate": rate, "state": state, "reviews": reviews},
            **extra}


class BuildRowTests(unittest.TestCase):
    def test_aggregates_host(self):
        caps = [
            cap("https://deepai.pay.zeroclick.io/api/text2img?utm_source=zero.xyz&a=1",
                protocol="mpp", act=300, rate="0.90", state="rated", reviews=5,
                whatItDoes="Generates images"),
            cap("https://deepai.pay.zeroclick.io/api/upscale", amount="0.08",
                act=100, rate="0.50", state="rated", reviews=1),
            cap("https://deepai.pay.zeroclick.io/video", amount="1", status="unknown"),
            cap("https://other.example/x", amount="99"),
        ]
        commerce = {"computedAt": "2026-10-21", "headline": {
            "onchainCalls": 358, "onchainVolumeUsdc": "33.906972"}}
        row = zero.build_row("WWW.deepai.pay.zeroclick.io", caps, 11, commerce)
        self.assertEqual(row["url"], "https://deepai.pay.zeroclick.io")
        self.assertEqual(row["source"], "zero")
        self.assertEqual(row["source_id"], "deepai.pay.zeroclick.io")
        self.assertEqual(row["slug"], "deepai-pay-zeroclick-io-zero")
        self.assertEqual((row["price_min"], row["price_max"]), (0.01, 1.0))
        self.assertEqual(row["description"], "Generates images")
        self.assertEqual(row["resource_count"], 11)
        self.assertEqual([s["url"] for s in row["resource_samples"]], [
            "https://deepai.pay.zeroclick.io/api/text2img?a=1",
            "https://deepai.pay.zeroclick.io/api/upscale",
            "https://deepai.pay.zeroclick.io/video"])
        self.assertEqual(row["resource_samples"][0]["kind"], "mpp-resource")
        self.assertEqual(row["tags"], ["zero", "mpp", "x402"])
        q = row["quality"]["zero"]
        self.assertEqual((q["healthy"], q["unknown"], q["down"]), (2, 1, 0))
        self.assertEqual(q["activations"], 400)
        self.assertEqual(q["reviews"], 6)
        self.assertEqual(q["success_rate"], 0.8)
        self.assertEqual(q["onchain_calls_30d"], 358)
        self.assertAlmostEqual(q["onchain_volume_usdc_30d"], 33.906972)

    def test_zero_amount_is_unpriced(self):
        row = zero.build_row("a.example", [cap("https://a.example/x", amount="0")], 1, None)
        self.assertIsNone(row["price_min"])

    def test_non_usd_price_ignored_and_no_commerce(self):
        row = zero.build_row("a.example", [cap("https://a.example/x", amount="5", asset="SATS")],
                             None, None)
        self.assertIsNone(row["price_min"])
        self.assertNotIn("onchain_calls_30d", row["quality"]["zero"])
        self.assertNotIn("success_rate", row["quality"]["zero"])
        self.assertEqual(row["resource_count"], 1)

    def test_rejects_empty_ip_and_foreign_only(self):
        self.assertIsNone(zero.build_row("a.example", [], 0, None))
        self.assertIsNone(zero.build_row("1.2.3.4", [cap("https://1.2.3.4/x")], 1, None))
        self.assertIsNone(zero.build_row("a.example", [cap("https://b.example/x")], 1, None))


class SelectHostsTests(unittest.TestCase):
    def test_changed_first_then_stalest_skipping_claimed(self):
        now = 10 * 86400
        hosts = [
            {"host": "same.example", "count": 2, "contentUpdatedAt": "2026-10-01"},
            {"host": "old.example", "count": 2, "contentUpdatedAt": "2026-09-01"},
            {"host": "new.example", "count": 2, "contentUpdatedAt": "2026-10-20"},
            {"host": "newer.example", "count": 2, "contentUpdatedAt": "2026-10-21"},
            {"host": "claimed.example", "count": 2, "contentUpdatedAt": "2026-10-22"},
            {"host": "empty.example", "count": 0, "contentUpdatedAt": "2026-10-22"},
        ]
        seen = {"same.example": {"u": "2026-10-01", "t": now - 86400},
                "old.example": {"u": "2026-09-01", "t": 0}}
        batch, pending = zero.select_hosts(hosts, seen, {"claimed.example"}, now, 10)
        self.assertEqual([h["host"] for h in batch],
                         ["newer.example", "new.example", "old.example"])
        self.assertEqual(pending, 3)
        batch, _ = zero.select_hosts(hosts, seen, set(), now, 1)
        self.assertEqual([h["host"] for h in batch], ["claimed.example"])


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p, self.status_code, self.headers = payload, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._p


class _Client:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


class ApiTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(zero.time, "sleep", lambda *_: None)
        p.start()
        self.addCleanup(p.stop)

    def test_hosts_paginate_and_retry_429(self):
        page1 = {"total": 501, "hosts": [{"host": f"h{i}"} for i in range(500)]}
        client = _Client([_Resp({}, 429, {"x-ratelimit-reset": "1"}),
                          _Resp(page1), _Resp({"total": 501, "hosts": [{"host": "last"}]})])
        hosts = zero._Api(client).hosts()
        self.assertEqual(len(hosts), 501)
        self.assertEqual(client.calls[-1][1], {"limit": 500, "offset": 500})

    def test_caps_paginate_until_total(self):
        client = _Client([
            _Resp({"total": 101, "capabilities": [{}] * 100}),
            _Resp({"total": 101, "capabilities": [{}]}),
        ])
        caps, total = zero._Api(client).caps("a.example")
        self.assertEqual((len(caps), total), (101, 101))
        self.assertTrue(client.calls[0][0].endswith("/capabilities/by-host"))


class RegistrationTests(unittest.TestCase):
    def test_registered(self):
        self.assertIs(crawlers.ALL_CRAWLERS["zero"], zero.fetch_zero)
        self.assertIn("zero", jobs.crawlers.ALL_CRAWLERS)


if __name__ == "__main__":
    unittest.main()
