"""On-chain demand data sources: Etherscan V2 per-address, throttled bulk log sweep."""

import os
import unittest
from unittest.mock import patch

import httpx

from directory import crawlers

PAYTO = "0xb8491F953e6860D3b5840b73c60883Ff099ceAfA"
PAYER = "0x1111111111111111111111111111111111111111"


def _client_with(handler):
    real = httpx.Client
    return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)


class EtherscanPerAddressTests(unittest.TestCase):
    def setUp(self):
        crawlers._etherscan_last_call = 0.0
        patcher = patch.dict(os.environ, {"ETHERSCAN_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(crawlers.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def run_fetch(self, handler, chain="polygon"):
        with patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            return crawlers.fetch_payto_activity(PAYTO, chain=chain)

    def test_counts_incoming_transfers_via_v2_with_chainid_and_key(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            now = int(crawlers.time.time())
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": [
                {"timeStamp": str(now - 60), "to": PAYTO.lower(), "from": PAYER},
                {"timeStamp": str(now - 120), "to": PAYTO.lower(), "from": PAYER},
                {"timeStamp": str(now - 180), "to": PAYER, "from": PAYTO.lower()},
            ]})

        result = self.run_fetch(handler)
        self.assertEqual((result["ok"], result["tx"], result["payers"]), (True, 2, 1))
        self.assertEqual(seen["chainid"], "137")
        self.assertEqual(seen["apikey"], "test-key")
        self.assertEqual(seen["action"], "tokentx")

    def test_no_transactions_found_is_a_real_zero(self):
        result = self.run_fetch(lambda r: httpx.Response(200, json={
            "status": "0", "message": "No transactions found", "result": []}))
        self.assertEqual((result["ok"], result["tx"]), (True, 0))

    def test_free_tier_or_error_messages_are_not_a_zero(self):
        result = self.run_fetch(lambda r: httpx.Response(200, json={
            "status": "0", "message": "NOTOK",
            "result": "Free API access is not supported for this chain."}))
        self.assertFalse(result["ok"])

    def test_rate_limit_message_is_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, json={"status": "0", "message": "NOTOK",
                                                 "result": "Max rate limit reached"})
            return httpx.Response(200, json={"status": "0", "message": "No transactions found",
                                             "result": []})

        result = self.run_fetch(handler)
        self.assertEqual((result["ok"], len(calls)), (True, 2))

    def test_base_never_goes_to_etherscan_free_tier(self):
        self.assertNotIn("base", crawlers.ETHERSCAN_FREE_CHAINS)
        self.assertIn("base", crawlers.BULK_LOG_CHAINS)


class BulkSweepThrottleTests(unittest.TestCase):
    def test_429_is_retried_on_the_same_window_not_fatal(self):
        state = {"logs": 0}

        def handler(request):
            body = request.read().decode()
            if "eth_blockNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(5000)})
            state["logs"] += 1
            if state["logs"] == 1:
                return httpx.Response(429, json={})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                {"topics": [crawlers._TRANSFER_TOPIC, "0x" + "0" * 24 + PAYER[2:].lower(),
                            crawlers._topic_addr(PAYTO)]}] if state["logs"] == 2 else []})

        cfg = {"base": ("https://rpc.invalid", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        2.0, 1000, 2000)}
        with patch.object(crawlers, "BULK_LOG_CHAINS", cfg), \
                patch.object(crawlers.time, "sleep"), \
                patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            results, ok = crawlers.fetch_chain_activity_bulk([PAYTO], chain="base", days=1)
        self.assertTrue(ok)
        self.assertEqual(results[PAYTO.lower()]["tx"], 1)


if __name__ == "__main__":
    unittest.main()
