"""On-chain demand data sources: Etherscan V2 per-address, throttled bulk log sweep."""

import os
import unittest
from unittest.mock import patch

import httpx

from contextlib import closing

from directory import crawlers, db, jobs
from test_ingestion_pipeline import TemporaryDatabaseTestCase

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


class IncrementalWriteTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        import json as _json
        self.ids = {}
        with db.writer() as conn:
            for slug, net, pt in [("on-polygon", "eip155:137", "0x" + "1" * 40),
                                  ("on-base", "eip155:8453", "0x" + "2" * 40)]:
                _, sid = db.upsert_service(conn, dict(slug=slug, name=slug, url="https://%s.example/x" % slug,
                                                      source="fixture", source_id=slug, health="ok"))
                conn.execute("UPDATE services SET payment=? WHERE id=?",
                             (_json.dumps({"accepts": [{"network": net, "pay_to": pt}]}), sid))
                self.ids[slug] = sid

    def checked(self, slug):
        with closing(db.connect(read_only=True)) as conn:
            return conn.execute("SELECT payto_checked, payto_tx_30d FROM services WHERE id=?",
                                (self.ids[slug],)).fetchone()

    def test_per_address_chain_is_written_before_slow_bulk_sweep_runs(self):
        seen_during_sweep = {}

        def bulk(addrs, chain="base", **kw):
            seen_during_sweep["polygon"] = tuple(self.checked("on-polygon"))
            seen_during_sweep["base"] = tuple(self.checked("on-base"))
            return {a.lower(): {"tx": 3, "payers": 2, "payer_ids": ["a", "b"]} for a in addrs}, True

        with patch.object(jobs.crawlers, "BULK_LOG_CHAINS", {"base": None}), \
                patch.object(jobs.crawlers, "fetch_chain_activity_bulk", side_effect=bulk), \
                patch.object(jobs.crawlers, "fetch_payto_activity",
                             return_value={"tx": 5, "payers": 1, "ok": True, "capped": False, "payer_ids": ["x"]}), \
                patch.object(jobs.time, "sleep"), patch.object(jobs, "cmd_rescore"):
            jobs.cmd_onchain()
        self.assertIsNotNone(seen_during_sweep["polygon"][0])
        self.assertEqual(seen_during_sweep["polygon"][1], 5)
        self.assertIsNone(seen_during_sweep["base"][0])
        self.assertEqual(self.checked("on-base")[1], 3)



    def test_failed_hyperevm_rpc_preserves_previous_statistics_without_etherscan(self):
        import json
        now = int(crawlers.time.time())
        service_id = self.ids["on-base"]
        with db.writer() as conn:
            conn.execute("UPDATE services SET payto_checked=? WHERE id=?",
                         (now, self.ids["on-polygon"]))
            conn.execute(
                "UPDATE services SET payment=?,payto_checked=1,"
                "payto_tx_30d=17,payto_payers_30d=3,tx_30d=17 WHERE id=?",
                (json.dumps({"accepts": [{"network": "eip155:999",
                                         "pay_to": "0x" + "2" * 40}]}), service_id))
        def statistics():
            with closing(db.connect(read_only=True)) as conn:
                return tuple(conn.execute(
                    "SELECT payto_checked,payto_tx_30d,payto_payers_30d,tx_30d "
                    "FROM services WHERE id=?", (service_id,)).fetchone())
        before = statistics()
        with patch.dict(os.environ, {"ETHERSCAN_API_KEY": "test-key"}), \
                patch.object(crawlers, "fetch_chain_activity_bulk", return_value=({}, False)), \
                patch.object(crawlers, "_etherscan_payto_activity") as etherscan, \
                patch.object(jobs.time, "sleep"), patch.object(jobs, "cmd_rescore"):
            jobs.cmd_onchain()
        etherscan.assert_not_called()
        self.assertEqual(statistics(), before)

    def _stale_without_readable_payto(self, payment):
        import json
        sid = self.ids["on-base"]
        with db.writer() as conn:
            conn.execute("UPDATE services SET payto_checked=? WHERE id=?",
                         (int(crawlers.time.time()), self.ids["on-polygon"]))
            conn.execute(
                "UPDATE services SET payment=?, payto_checked=1, payto_tx_30d=40, "
                "payto_payers_30d=8, tx_30d=40 WHERE id=?",
                (json.dumps(payment) if payment is not None else None, sid))
        return sid

    def _demand(self, sid):
        with closing(db.connect(read_only=True)) as conn:
            return tuple(conn.execute(
                "SELECT payto_checked, payto_tx_30d, payto_payers_30d, tx_30d "
                "FROM services WHERE id=?", (sid,)).fetchone())

    def test_demand_cleared_when_no_readable_payto_is_left(self):
        for payment in ({"accepts": [{"network": "eip155:84532", "pay_to": "0x" + "2" * 40}]},
                        None):
            sid = self._stale_without_readable_payto(payment)
            with patch.object(jobs.time, "sleep"), patch.object(jobs, "cmd_rescore"):
                jobs.cmd_onchain()
            self.assertEqual(self._demand(sid), (None, None, None, None), payment)

    def test_recent_wallet_history_keeps_being_measured(self):
        sid = self._stale_without_readable_payto(
            {"accepts": [{"network": "eip155:84532", "pay_to": "0x" + "2" * 40}]})
        with db.writer() as conn:
            db.record_service_paytos(conn, {sid: [("base", "0x" + "3" * 40)]},
                                     int(crawlers.time.time()) - 5 * 86400)
        with patch.object(jobs.crawlers, "BULK_LOG_CHAINS", {"base": None}), \
                patch.object(jobs.crawlers, "fetch_chain_activity_bulk",
                             return_value=({"0x" + "3" * 40: {"tx": 2, "payers": 1,
                                                              "payer_ids": ["p"]}}, True)), \
                patch.object(jobs.time, "sleep"), patch.object(jobs, "cmd_rescore"):
            jobs.cmd_onchain()
        self.assertEqual(self._demand(sid)[1:], (2, 1, 2))


class HyperEvmBulkTests(unittest.TestCase):
    def test_response_too_big_splits_without_retry_backoff(self):
        import json
        queries = []

        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "eth_blockNumber":
                return httpx.Response(200, json={"result": hex(999)})
            if body["method"] == "eth_getBlockByNumber":
                return httpx.Response(200, json={"result": {"timestamp": "0x0"}})
            query = body["params"][0]
            queries.append(query)
            lower = int(query["fromBlock"], 16)
            upper = int(query["toBlock"], 16)
            if upper - lower + 1 == 1000:
                return httpx.Response(200, json={
                    "error": {"code": -32008, "message": "Response is too big"}})
            return httpx.Response(200, json={"result": [
                {"topics": [crawlers._TRANSFER_TOPIC, crawlers._topic_addr(PAYER),
                            crawlers._topic_addr(PAYTO)]}
            ] if lower == 500 else []})

        with patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)), \
                patch.object(crawlers.time, "sleep") as sleeper:
            results, ok = crawlers.fetch_chain_activity_bulk(
                [PAYTO], chain="hyperevm", days=1000 / 86400)
        self.assertTrue(ok)
        self.assertEqual(results[PAYTO.lower()]["tx"], 1)
        self.assertEqual(len(queries), 3)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1.0, 1.0])

    def test_hyperevm_uses_keyless_rpc_and_never_falls_back_to_etherscan(self):
        self.assertNotIn("hyperevm", crawlers.ETHERSCAN_FREE_CHAINS)
        url, contract, _seconds, window, _batch = crawlers.BULK_LOG_CHAINS["hyperevm"]
        self.assertEqual(url, "https://rpc.hypurrscan.io")
        self.assertEqual(contract, "0xb88339cb7199b77e23db6e890353e22632ba630f")
        self.assertEqual(window, 1000)
        self.assertGreaterEqual(crawlers.BULK_LOG_MIN_INTERVAL["hyperevm"], 1.0)
        with patch.dict(os.environ, {"ETHERSCAN_API_KEY": "test-key"}), \
                patch.object(crawlers, "_etherscan_payto_activity") as etherscan:
            self.assertFalse(crawlers.fetch_payto_activity(PAYTO, chain="hyperevm")["ok"])
        etherscan.assert_not_called()

    def test_bulk_queries_only_usdc_transfers_to_requested_addresses(self):
        import json
        seen = []

        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(request.url.host, "rpc.hypurrscan.io")
            self.assertNotIn("apikey", request.url.params)
            if body["method"] == "eth_blockNumber":
                return httpx.Response(200, json={"result": hex(9999)})
            if body["method"] == "eth_getBlockByNumber":
                return httpx.Response(200, json={"result": {"timestamp": "0x0"}})
            self.assertEqual(body["method"], "eth_getLogs")
            seen.append(body["params"][0])
            return httpx.Response(200, json={"result": [
                {"topics": [crawlers._TRANSFER_TOPIC, crawlers._topic_addr(PAYER),
                            crawlers._topic_addr(PAYTO)]},
                {"topics": [crawlers._TRANSFER_TOPIC, crawlers._topic_addr(PAYTO),
                            crawlers._topic_addr(PAYER)]},
            ] if len(seen) == 1 else []})

        with patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)), \
                patch.object(crawlers.time, "sleep"):
            results, ok = crawlers.fetch_chain_activity_bulk([PAYTO], chain="hyperevm", days=0.1)
        self.assertTrue(ok)
        self.assertEqual((results[PAYTO.lower()]["tx"], results[PAYTO.lower()]["payers"]), (1, 1))
        self.assertEqual(len(seen), 9)
        for query in seen:
            self.assertLessEqual(int(query["toBlock"], 16) - int(query["fromBlock"], 16) + 1, 1000)
            self.assertEqual(query["address"], crawlers.EVM_USDC_INDEXERS["hyperevm"][1].lower())
            self.assertEqual(query["topics"], [crawlers._TRANSFER_TOPIC, None,
                                             [crawlers._topic_addr(PAYTO)]])


class ArcBulkConfigTests(unittest.TestCase):
    def test_arc_uses_official_node_with_wide_windows_and_pacing(self):
        url, contract, _secs, window, _batch = crawlers.BULK_LOG_CHAINS["arc"]
        self.assertEqual(url, "https://rpc.mainnet.arc.io")
        self.assertEqual(contract, "0x3600000000000000000000000000000000000000")
        self.assertGreaterEqual(window, 10000)
        self.assertGreaterEqual(crawlers.BULK_LOG_MIN_INTERVAL["arc"], 2.0)

    def test_pacing_applies_between_log_calls(self):
        def handler(request):
            body = request.read().decode()
            if "eth_blockNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(25000)})
            if "eth_getBlockByNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(0)}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

        cfg = {"arc": ("https://rpc.invalid", "0x3600000000000000000000000000000000000000",
                       0.5, 10000, 400)}
        with patch.object(crawlers, "BULK_LOG_CHAINS", cfg), \
                patch.object(crawlers.time, "sleep") as slept, \
                patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            results, ok = crawlers.fetch_chain_activity_bulk([PAYTO], chain="arc", days=0.1)
        self.assertTrue(ok)
        self.assertEqual(results[PAYTO.lower()]["tx"], 0)
        self.assertTrue(slept.call_args_list)
        self.assertTrue(all(c.args[0] >= 2.0 for c in slept.call_args_list))

    def test_jsonrpc_result_cap_splits_without_backoff(self):
        state = {"calls": 0}

        def handler(request):
            body = request.read().decode()
            if "eth_blockNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(9999)})
            if "eth_getBlockByNumber" in body:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(0)}})
            state["calls"] += 1
            if state["calls"] == 1:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32602, "message": "query exceeds max results 20000"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

        cfg = {"base": ("https://rpc.invalid", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        2.0, 10000, 400)}
        with patch.object(crawlers, "BULK_LOG_CHAINS", cfg), \
                patch.object(crawlers.time, "sleep"), \
                patch.object(crawlers.httpx, "Client", side_effect=_client_with(handler)):
            _results, ok = crawlers.fetch_chain_activity_bulk([PAYTO], chain="base", days=0.1)
        self.assertTrue(ok)
        # one refused call, then the two halves -- no six-attempt retry first
        self.assertEqual(state["calls"], 3)


if __name__ == "__main__":
    unittest.main()
