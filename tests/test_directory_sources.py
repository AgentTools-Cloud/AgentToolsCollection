import unittest
from unittest.mock import patch

from directory import crawlers, directories as d, jobs


def page(items_key, items, **top):
    return {items_key: items, **top}


class AggregateTests(unittest.TestCase):
    def test_groups_by_origin_and_drops_tunnels(self):
        rows = d.aggregate("nohumans", [
            {"url": "https://API.example.com/a", "price_usd": 0.01, "chains": ["base"],
             "protocol": "x402", "name": "A", "description": "first", "quality": {"q": 1}},
            {"url": "https://api.example.com/b", "price_usd": 0.5, "chains": ["solana"]},
            {"url": "https://api.example.com/a", "price_usd": 0.02},
            {"url": "https://abc-def.trycloudflare.com/x"},
            {"url": "ftp://bad.example/x"},
        ])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["url"], "https://api.example.com")
        self.assertEqual(r["source"], "nohumans")
        self.assertEqual(r["source_id"], "origin:https://api.example.com")
        self.assertEqual(r["resource_count"], 2)
        self.assertEqual((r["price_min"], r["price_max"]), (0.01, 0.5))
        self.assertEqual(r["chains"], ["base", "solana"])
        self.assertEqual(r["quality"], {"nohumans": {"q": 1}})
        self.assertEqual(r["name"], "api.example.com")


class FetcherTests(unittest.TestCase):
    def run_fetch(self, fn, responses):
        calls = iter(responses)
        with patch.object(d, "_get_json", side_effect=lambda *a, **k: next(calls)), \
             patch.object(d.time, "sleep"):
            return fn()

    def test_nohumans_skips_failing_and_paginates(self):
        rows = self.run_fetch(d.fetch_nohumans, [
            page("results", [{"endpoint_url": "https://a.example/x", "status": "verified",
                              "price_amount": 0.05, "price_currency": "USDC", "chains": ["base"],
                              "paid_verified": True}], next_offset=1),
            page("results", [{"endpoint_url": "https://b.example/x", "status": "failing"}],
                 next_offset=None),
        ])
        self.assertEqual([r["url"] for r in rows], ["https://a.example"])
        self.assertTrue(rows[0]["quality"]["nohumans"]["paid_verified"])

    def test_agentgates_skips_cdp_and_reads_atomic_price(self):
        rows = self.run_fetch(d.fetch_agentgates, [page("items", [
            {"resource": "https://cdp.example/x", "source": "cdp"},
            {"resource": "https://payai.example/x", "source": "payai",
             "legs": [{"chain": "base", "amountAtomic": "20000", "decimals": 6}],
             "traffic": {"calls30d": 9}},
        ], pagination={"total": 2})])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_min"], 0.02)
        self.assertEqual(rows[0]["quality"]["agentgates"]["calls_30d"], 9)

    def test_the402_keeps_payable_submissions_only(self):
        rows = self.run_fetch(d.fetch_the402, [page("items", [
            {"resource": "https://bz.example/x", "source": "bazaar", "verdict": "payable"},
            {"resource": "https://ok.example/x", "source": "submission", "verdict": "payable"},
            {"resource": "https://no.example/x", "source": "submission", "verdict": "not-x402"},
        ], total=3)])
        self.assertEqual([r["url"] for r in rows], ["https://ok.example"])

    def test_aegis_drops_flagged(self):
        rows = self.run_fetch(d.fetch_aegis, [page("results", [
            {"url": "https://good.example/a", "tier": "verified", "network": "eip155:8453"},
            {"url": "https://bad.example/a", "tier": "flagged"},
        ], count=2)])
        self.assertEqual([r["url"] for r in rows], ["https://good.example"])

    def test_mppdev_expands_endpoints(self):
        rows = self.run_fetch(d.fetch_mppdev, [{"services": [{
            "name": "AIsa", "serviceUrl": "https://api.aisa.one/", "status": "active",
            "methods": {"tempo": {}},
            "endpoints": [{"path": "/a", "payment": {"amount": "1000", "decimals": 6}},
                          {"path": "/b", "payment": {"amount": "5000", "decimals": 6}}]}]}])
        self.assertEqual(rows[0]["resource_count"], 2)
        self.assertEqual((rows[0]["price_min"], rows[0]["price_max"]), (0.001, 0.005))
        self.assertIn("mpp", rows[0]["tags"])

    def test_verantis_skips_bazaar_only(self):
        rows = self.run_fetch(d.fetch_verantis, [{"entities": [
            {"host": "bz.example", "sources": ["cdp-bazaar"], "rails": []},
            {"host": "own.example", "sources": ["self"], "buyers": 4,
             "rails": [{"chain": "base", "sample_url": "https://own.example/x"}]},
        ]}])
        self.assertEqual([r["url"] for r in rows], ["https://own.example"])

    def test_partial_page_failure_raises_partial(self):
        def boom(*a, **k):
            raise RuntimeError("503")
        with patch.object(d, "_get_json", side_effect=boom), patch.object(d.time, "sleep"):
            with self.assertRaises(crawlers.PartialCrawl):
                d.fetch_x402_list()


class RegistrationTests(unittest.TestCase):
    def test_all_sources_registered(self):
        for name in d.FETCHERS:
            self.assertIs(crawlers.ALL_CRAWLERS[name], d.FETCHERS[name])


if __name__ == "__main__":
    unittest.main()
