import random
import unittest
from unittest.mock import patch

from directory import crawlers, directories as d, jobs
from test_ingestion_pipeline import TemporaryDatabaseTestCase, _row


def page(items_key, items, **top):
    return {items_key: items, **top}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(d, "_url_acceptable",
                                       side_effect=lambda u: not u.startswith("ftp")))


class AggregateTests(OfflineCase):
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


class FetcherTests(OfflineCase):
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

    def agentgates(self, providers, rows_by_host, claimed=(), paging="stable", providers_total=None):
        # Fake upstream: /providers pages are stable; /resources?q=<host> pages
        # are "shuffled" (fresh random sample per request, as live) or "stuck"
        # (offset ignored, always the first page).
        rng = random.Random(7)
        calls = []

        def get_json(client, url, params, gap):
            calls.append((url.rsplit("/", 1)[1], params.get("q"), params["offset"]))
            if url.endswith("/providers"):
                hosts = providers[params["offset"]:params["offset"] + params["limit"]]
                return page("items", [{"host": h} for h in hosts],
                            pagination={"total": providers_total or len(providers)})
            rows = rows_by_host.get(params["q"], [])
            if paging == "shuffled":
                batch = rng.sample(rows, min(len(rows), params["limit"]))
            elif paging == "stuck":
                batch = rows[:params["limit"]]
            else:
                batch = rows[params["offset"]:params["offset"] + params["limit"]]
            return page("items", batch, pagination={"total": len(rows)})

        with patch.object(d, "_get_json", side_effect=get_json), patch.object(d.time, "sleep"):
            try:
                return d.fetch_agentgates(claimed=set(claimed)), calls
            except crawlers.PartialCrawl as e:
                return e, calls

    def test_agentgates_skips_cdp_delisted_and_other_hosts(self):
        rows, _ = self.agentgates(["pay.example"], {"pay.example": [
            {"id": 1, "resource": "https://pay.example/x", "host": "pay.example", "source": "payai",
             "legs": [{"chain": "base", "amountAtomic": "20000", "decimals": 6}],
             "traffic": {"calls30d": 9}},
            {"id": 2, "resource": "https://pay.example/cdp", "host": "pay.example", "source": "cdp"},
            {"id": 3, "resource": "https://pay.example/old", "host": "pay.example", "source": "payai",
             "delisted": True},
            {"id": 4, "resource": "https://mirror.example/pay.example", "host": "mirror.example",
             "source": "payai"},
        ]})
        self.assertEqual([r["url"] for r in rows], ["https://pay.example"])
        self.assertEqual(rows[0]["resource_count"], 1)
        self.assertEqual(rows[0]["price_min"], 0.02)
        self.assertEqual(rows[0]["quality"]["agentgates"]["calls_30d"], 9)

    def test_agentgates_never_queries_hosts_another_source_lists(self):
        rows_by_host = {h: [{"id": h, "resource": f"https://{h}/x", "host": h, "source": "payai"}]
                        for h in ("ours.example", "theirs.example")}
        rows, calls = self.agentgates(sorted(rows_by_host), rows_by_host, claimed={"https://theirs.example"})
        self.assertEqual([r["url"] for r in rows], ["https://ours.example"])
        self.assertNotIn("theirs.example", {q for _, q, _ in calls})

    def big_host(self, n):
        return {"big.example": [{"id": i, "resource": f"https://big.example/r{i}", "host": "big.example",
                                 "source": "payai"} for i in range(n)]}

    def test_agentgates_repeats_unstable_host_pages_until_every_row_is_seen(self):
        rows, calls = self.agentgates(["big.example"], self.big_host(250), paging="shuffled")
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["resource_count"], 250)
        self.assertLessEqual(sum(1 for path, _, _ in calls if path == "resources"),
                             3 * d.AGENTGATES_PASSES)

    def test_agentgates_host_that_never_completes_is_partial_and_keeps_rows(self):
        error, _ = self.agentgates(["big.example"], self.big_host(250), paging="stuck")
        self.assertIsInstance(error, crawlers.PartialCrawl)
        self.assertIn("1 of 1 hosts incomplete", error.reason)
        self.assertEqual(error.items[0]["resource_count"], 100)

    def test_agentgates_runaway_host_query_is_partial_without_walking(self):
        error, calls = self.agentgates(["big.example"], self.big_host(d.AGENTGATES_MAX_HOST_ROWS + 1))
        self.assertIsInstance(error, crawlers.PartialCrawl)
        self.assertEqual(sum(1 for path, _, _ in calls if path == "resources"), 1)

    def test_agentgates_repeated_provider_rows_are_not_a_gap(self):
        rows, calls = self.agentgates(["a.example", "a.example"], {})
        self.assertEqual(rows, [])
        self.assertEqual(sum(1 for path, _, _ in calls if path == "resources"), 1)

    def test_agentgates_short_providers_listing_is_partial(self):
        error, _ = self.agentgates(["a.example"], {}, providers_total=2)
        self.assertIsInstance(error, crawlers.PartialCrawl)
        self.assertIn("providers: 1/2 rows", error.reason)

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


class AgentgatesClaimedTests(TemporaryDatabaseTestCase):
    def test_claimed_origins_are_other_sources_only(self):
        self.seed("x402", _row("x402", "theirs", "http://theirs.example:8443/api", source="cdp-bazaar"))
        self.seed("x402", _row("x402", "ours", source="agentgates"))
        self.assertEqual(d._agentgates_claimed(), {"http://theirs.example:8443"})


if __name__ == "__main__":
    unittest.main()
