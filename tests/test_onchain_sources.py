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


class HeliusSolanaTests(unittest.TestCase):
    SOL = "985iFjbnGQ3dJcwXnfRCMSrH4Jnc3kW1N6msR64B5KX1"

    def setUp(self):
        crawlers._helius_last_call = 0.0
        patcher = patch.dict(os.environ, {"HELIUS_API_KEY": "test-helius"})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(crawlers.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_counts_incoming_usdc_transfers_and_skips_mints(self):
        seen = {}

        def handler(request):
            import json as _json
            seen["body"] = _json.loads(request.content)
            seen["key"] = request.url.params.get("api-key")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"data": [
                {"type": "transfer", "toUserAccount": self.SOL, "fromUserAccount": "PayerA"},
                {"type": "transfer", "toUserAccount": self.SOL, "fromUserAccount": "PayerB"},
                {"type": "transfer", "toUserAccount": self.SOL, "fromUserAccount": "PayerA"},
                {"type": "mint", "toUserAccount": self.SOL, "fromUserAccount": None},
            ], "paginationToken": None}})

        with patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            result = crawlers.fetch_payto_activity(self.SOL, chain="solana")
        self.assertEqual((result["ok"], result["tx"], result["payers"]), (True, 3, 2))
        cfg = seen["body"]["params"][1]
        self.assertEqual(seen["body"]["method"], "getTransfersByAddress")
        self.assertEqual((cfg["mint"], cfg["direction"]), (crawlers.SOLANA_USDC_MINT, "in"))
        self.assertIn("gte", cfg["filters"]["blockTime"])
        self.assertEqual(seen["key"], "test-helius")

    def test_rpc_error_is_not_a_zero(self):
        handler = lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                      "error": {"code": -32601, "message": "plan"}})
        with patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            self.assertFalse(crawlers.fetch_payto_activity(self.SOL, chain="solana")["ok"])

    def test_without_key_solana_is_skipped(self):
        with patch.dict(os.environ, {"HELIUS_API_KEY": ""}):
            self.assertFalse(crawlers.fetch_payto_activity(self.SOL, chain="solana")["ok"])


class ChainMappingTests(unittest.TestCase):
    def test_new_networks_map_to_supported_chains(self):
        for net, chain in [("eip155:480", "worldchain"), ("eip155:143", "monad"), ("eip155:1329", "sei"),
                           ("eip155:130", "unichain"), ("eip155:146", "sonic"), ("eip155:999", "hyperevm"),
                           ("eip155:42220", "celo"), ("eip155:5042", "arc"),
                           ("solana", "solana"),
                           ("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "solana")]:
            self.assertEqual(crawlers.network_to_chain(net), chain, net)

    def test_testnets_stay_unsupported(self):
        # BSC and Robinhood Chain are deliberately not counted (sweep cost far exceeds coverage).
        for net in ("eip155:84532", "base-sepolia", "solana:etwtrabzayq6imfeykouru166vu2xqa1",
                    "eip155:56", "eip155:4663"):
            self.assertIsNone(crawlers.network_to_chain(net), net)

    def test_solana_payto_keeps_case_and_rejects_garbage(self):
        from directory import jobs
        sol = "985iFjbnGQ3dJcwXnfRCMSrH4Jnc3kW1N6msR64B5KX1"
        payment = {"accepts": [{"network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "pay_to": sol},
                               {"network": "solana", "pay_to": "not a base58 address!"},
                               {"network": "eip155:8453", "pay_to": "0xABCDEF0000000000000000000000000000000001"}]}
        keys = jobs._paytos_for_row(payment)
        self.assertIn(("solana", sol), keys)
        self.assertIn(("base", "0xabcdef0000000000000000000000000000000001"), keys)
        self.assertEqual(len(keys), 2)


class BulkSweepThrottleTests(unittest.TestCase):
    def test_429_is_retried_on_the_same_window_not_fatal(self):
        state = {"logs": 0}

        def handler(request):
            body = request.read().decode()
            if "eth_blockNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(5000)})
            if "eth_getBlockByNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(0)}})
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
