"""Sponsored paid verification: non-custodial flow, quote binding, no retries."""

import base64
import json
import time
from contextlib import closing
from unittest.mock import patch

import httpx

from directory import db, paid_verify as pv, public_http
from test_ingestion_pipeline import TemporaryDatabaseTestCase

from eth_account import Account
from eth_account.messages import encode_typed_data

URL = "https://api.vendor.example/data"
PAY_TO = "0x1111111111111111111111111111111111111111"
ACCOUNT = Account.create()  # throwaway key, never funded


def challenge(**over):
    req = {"scheme": "exact", "network": pv.BASE_NETWORK,
           "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
           "amount": "10000", "payTo": PAY_TO, "maxTimeoutSeconds": 60,
           "extra": {"name": "USD Coin", "version": "2"}}
    req.update(over.pop("req", {}))
    obj = {"x402Version": 2, "resource": {"url": URL}, "accepts": [req]}
    obj.update(over)
    return obj


def header(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def sign(typed):
    msg = dict(typed["message"])
    for key in ("value", "validAfter", "validBefore"):
        msg[key] = int(msg[key])
    signable = encode_typed_data(full_message={**typed, "message": msg})
    return "0x" + Account.sign_message(signable, ACCOUNT.key).signature.hex().removeprefix("0x")


class FakeEndpoint:
    """Stands in for pv._fetch so no test touches the network."""

    def __init__(self, quotes, paid=None, error=None):
        self.quotes = list(quotes)
        self.paid = paid or (200, {"content-type": "application/json"}, b'{"ok":true}')
        self.error = error
        self.calls = []
        self.requests = []

    def __call__(self, url, headers=None, request=None):
        self.calls.append((url, dict(headers or {})))
        self.requests.append(json.loads(json.dumps(request or {})))
        if headers and "PAYMENT-SIGNATURE" in headers:
            if self.error:
                raise self.error
            status, hdrs, body = self.paid
            return httpx.Response(status, headers=hdrs, content=body), body, 120
        obj = self.quotes.pop(0) if len(self.quotes) > 1 else self.quotes[0]
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": header(obj)}), b"", 50


class PaidVerifyTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(public_http, "validate_url", return_value=None))
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','pv','pv','active',1)").lastrowid
            self.other = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','pv2','pv2','active',1)").lastrowid
            self.sid = conn.execute(
                "INSERT INTO services(slug,name,url,payment,source,source_id,created_at,updated_at) "
                "VALUES('vendor','Vendor',?,?,'manual','vendor',1,1)",
                (URL, json.dumps({"accepts": challenge()["accepts"]}))).lastrowid
            self.svc = {"id": self.sid, "url": URL,
                    "payment": {"accepts": challenge()["accepts"]}}

    def set_request(self, method, body=None, primary_url=URL):
        info = {"primary_url": primary_url, "method": method}
        if body is not None:
            info["probe_body"] = body
        encoded = json.dumps(info)
        with db.writer() as conn:
            conn.execute("UPDATE services SET call_info=? WHERE id=?", (encoded, self.sid))
        self.svc["call_info"] = encoded

    def run_flow(self, endpoint, settlement=(True, "verified"), **sign_over):
        with patch.object(pv, "_fetch", endpoint), patch.object(
                pv, "verify_onchain_settlement", return_value=settlement):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            sig = sign(quote["typed_data"])
            return quote, pv.execute(quote["id"], self.uid, sig)

    def row(self, vid):
        with closing(db.connect(read_only=True)) as conn:
            return dict(conn.execute("SELECT * FROM paid_verifications WHERE id=?", (vid,)).fetchone())

    def test_happy_path_pays_target_once_and_scores(self):
        receipt = header({"success": True, "transaction": "0x" + "ab" * 32, "network": pv.BASE_NETWORK})
        ep = FakeEndpoint([challenge()], paid=(200, {"content-type": "application/json",
                                                     "PAYMENT-RESPONSE": receipt}, b'{"ok":true}'))
        quote, res = self.run_flow(ep)
        self.assertEqual(quote["typed_data"]["message"]["to"], PAY_TO)
        self.assertEqual(quote["typed_data"]["domain"]["chainId"], 8453)
        self.assertEqual(res["status"], "succeeded")
        self.assertTrue(res["settled"])
        self.assertEqual(res["score"], 100)
        paid = [h for _, h in ep.calls if "PAYMENT-SIGNATURE" in h]
        self.assertEqual(len(paid), 1)
        payload = json.loads(base64.b64decode(paid[0]["PAYMENT-SIGNATURE"]))
        self.assertEqual(payload["payload"]["authorization"]["to"], PAY_TO)
        self.assertEqual(payload["accepted"]["amount"], "10000")

    def test_declared_post_template_is_used_for_quote_recheck_and_paid_call(self):
        body = {"model": "Public-Model", "messages": [{"role": "user", "content": "probe"}],
                "max_tokens": 1, "stream": False}
        self.set_request("POST", body)
        ep = FakeEndpoint([challenge()])
        quote, res = self.run_flow(ep)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(quote["request_method"], "POST")
        self.assertEqual(len(quote["request_body_sha256"]), 64)
        self.assertEqual([r["method"] for r in ep.requests], ["POST", "POST", "POST"])
        self.assertTrue(all(r["body"] == body for r in ep.requests))
        self.assertEqual(sum("PAYMENT-SIGNATURE" in h for _, h in ep.calls), 1)

    def test_owner_style_post_resource_sample_is_supported(self):
        info = {"primary_url": URL, "method": "POST"}
        samples = [{"url": URL, "method": "POST", "probe_body": {}}]
        with db.writer() as conn:
            conn.execute("UPDATE services SET call_info=?,resource_samples=? WHERE id=?",
                         (json.dumps(info), json.dumps(samples), self.sid))
        self.svc.update(call_info=info, resource_samples=samples)
        ep = FakeEndpoint([challenge()])
        quote, res = self.run_flow(ep)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(quote["request_method"], "POST")
        self.assertEqual([r["body"] for r in ep.requests], [{}, {}, {}])

    def test_changed_post_template_aborts_without_paying(self):
        self.set_request("POST", {"messages": [{"role": "user", "content": "one"}]})
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            self.set_request("POST", {"messages": [{"role": "user", "content": "two"}]})
            with self.assertRaises(pv.VerifyError) as caught:
                pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(caught.exception.code, "request_changed")
        self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))
        self.assertEqual(self.row(quote["id"])["status"], "failed_before_dispatch")

    def test_changed_endpoint_aborts_without_paying(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            with db.writer() as conn:
                conn.execute("UPDATE services SET url=? WHERE id=?",
                             ("https://new.vendor.example/data", self.sid))
            with self.assertRaises(pv.VerifyError) as caught:
                pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(caught.exception.code, "request_changed")
        self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))
        self.assertEqual(self.row(quote["id"])["status"], "failed_before_dispatch")

    def test_unsafe_request_templates_are_rejected_before_network(self):
        cases = [
            ({"method": "POST"}, "missing_probe_body"),
            ({"method": "POST", "probe_body": []}, "missing_probe_body"),
            ({"method": "DELETE"}, "unsupported_method"),
            ({"method": "GET", "primary_url": "https://other.example/x"}, "request_mismatch"),
            ({"method": "POST", "probe_body": {"text": "x" * pv.MAX_REQUEST_BODY_BYTES}},
             "request_too_large"),
        ]
        for info, code in cases:
            with self.subTest(code=code):
                svc = {**self.svc, "call_info": json.dumps(info)}
                ep = FakeEndpoint([challenge()])
                with patch.object(pv, "_fetch", ep):
                    with self.assertRaises(pv.VerifyError) as caught:
                        pv.prepare(self.uid, svc, ACCOUNT.address)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(ep.calls, [])

    def test_legacy_get_record_remains_executable(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            with db.writer() as conn:
                row = conn.execute("SELECT requirements FROM paid_verifications WHERE id=?",
                                   (quote["id"],)).fetchone()
                payment = json.loads(row["requirements"])["payment"]
                conn.execute("UPDATE paid_verifications SET requirements=? WHERE id=?",
                             (json.dumps(payment), quote["id"]))
            res = pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual([r["method"] for r in ep.requests], ["GET", "GET", "GET"])

    def test_signature_and_body_are_not_stored(self):
        quote, _ = self.run_flow(FakeEndpoint([challenge()]))
        row = self.row(quote["id"])
        self.assertNotIn("signature", json.dumps(row).lower())
        self.assertNotIn('{"ok":true}', json.dumps(row))
        self.assertEqual(len(row["response_sha256"]), 64)

    def test_rejected_challenges(self):
        bad = [
            ({"req": {"network": "eip155:1"}}, "unsupported_payment"),
            ({"req": {"asset": "0x" + "22" * 20}}, "unsupported_payment"),
            ({"req": {"amount": "1000001"}}, "amount_too_high"),
            ({"req": {"scheme": "upto"}}, "unsupported_payment"),
            ({"req": {"extra": {"name": "USDC", "version": "2"}}}, "unsupported_payment"),
            ({"resource": {"url": "https://other.example/x"}}, "resource_mismatch"),
            ({"x402Version": 1}, "unsupported_challenge"),
        ]
        for over, code in bad:
            with self.subTest(code=code, over=over):
                with patch.object(pv, "_fetch", FakeEndpoint([challenge(**over)])):
                    with self.assertRaises(pv.VerifyError) as caught:
                        pv.prepare(self.uid, self.svc, ACCOUNT.address)
                self.assertEqual(caught.exception.code, code)

    def test_non_https_and_bad_payer(self):
        with self.assertRaises(pv.VerifyError) as c1:
            pv.prepare(self.uid, {"id": self.sid, "url": "http://api.vendor.example/x"}, ACCOUNT.address)
        self.assertEqual(c1.exception.code, "unsafe_url")
        with self.assertRaises(pv.VerifyError) as c2:
            pv.prepare(self.uid, self.svc, "0xnot-an-address")
        self.assertEqual(c2.exception.code, "bad_payer")

    def test_signature_from_another_wallet_is_refused_before_dispatch(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, "0x" + "33" * 20)
            with self.assertRaises(pv.VerifyError) as caught:
                pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(caught.exception.code, "bad_signature")
        self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))
        self.assertEqual(self.row(quote["id"])["status"], "prepared")

    def test_replay_is_refused(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            sig = sign(quote["typed_data"])
            pv.execute(quote["id"], self.uid, sig)
            with self.assertRaises(pv.VerifyError) as caught:
                pv.execute(quote["id"], self.uid, sig)
        self.assertEqual(caught.exception.code, "not_prepared")
        self.assertEqual(sum("PAYMENT-SIGNATURE" in h for _, h in ep.calls), 1)

    def test_changed_quote_aborts_without_paying(self):
        for over in ({"req": {"amount": "20000"}}, {"req": {"payTo": "0x" + "44" * 20}}):
            with self.subTest(over=over):
                ep = FakeEndpoint([challenge(), challenge(**over)])
                with patch.object(pv, "_fetch", ep):
                    quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
                    with self.assertRaises(pv.VerifyError) as caught:
                        pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
                self.assertEqual(caught.exception.code, "quote_changed")
                self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))
                self.assertEqual(self.row(quote["id"])["status"], "failed_before_dispatch")

    def test_non_ascii_live_amount_aborts_without_paying(self):
        ep = FakeEndpoint([challenge(), challenge(req={"amount": "²"})])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            with self.assertRaises(pv.VerifyError) as caught:
                pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(caught.exception.code, "unsupported_payment")
        self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))
        self.assertEqual(self.row(quote["id"])["status"], "failed_before_dispatch")

    def test_transport_failure_is_indeterminate_and_not_retried(self):
        ep = FakeEndpoint([challenge()], error=httpx.ReadTimeout("slow"))
        _, res = self.run_flow(ep)
        self.assertEqual(res["status"], "indeterminate")
        self.assertEqual(sum("PAYMENT-SIGNATURE" in h for _, h in ep.calls), 1)

    def test_oversized_response_is_indeterminate(self):
        ep = FakeEndpoint([challenge()], error=pv.VerifyError("response_too_large", "big", 502))
        _, res = self.run_flow(ep)
        self.assertEqual(res["status"], "indeterminate")
        self.assertEqual(res["failure_code"], "response_too_large")

    def test_streaming_response_has_total_wall_clock_limit(self):
        class Response:
            status_code = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def iter_bytes(self):
                yield b"a"

        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def stream(self, *args, **kwargs): return Response()

        with patch.object(public_http, "client", return_value=Client()), \
                patch.object(pv.time, "monotonic", side_effect=[0.0, pv.MAX_REQUEST_SECONDS + 1]):
            with self.assertRaises(pv.VerifyError) as caught:
                pv._fetch(URL)
        self.assertEqual(caught.exception.code, "response_timeout")

    def test_deep_json_after_dispatch_reaches_terminal_state(self):
        deep = ("[" * 1100 + "0" + "]" * 1100).encode()
        ep = FakeEndpoint([challenge()], paid=(200, {"content-type": "application/json"}, deep))
        _, res = self.run_flow(ep)
        self.assertEqual(res["status"], "succeeded")
        self.assertNotEqual(self.row(res["id"])["status"], "dispatching")

    def test_post_dispatch_parser_exception_becomes_indeterminate(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep), patch.object(
                pv, "score", side_effect=RecursionError("deep")):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            res = pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertEqual(res["status"], "indeterminate")
        self.assertEqual(res["failure_code"], "invalid_response")

    def test_delivery_failure_and_rejection_scores(self):
        _, res = self.run_flow(FakeEndpoint([challenge()], paid=(503, {}, b"down")))
        self.assertEqual(res["status"], "delivery_failed")
        self.assertLessEqual(res["score"], 30)
        _, res = self.run_flow(FakeEndpoint([challenge()], paid=(402, {}, b"")))
        self.assertEqual(res["status"], "payment_rejected")

    def test_malformed_settlement_receipt_does_not_score_as_settled(self):
        receipts = [
            {"success": "false", "transaction": "0x" + "ab" * 32},
            {"success": True, "transaction": "0x1"},
            {"success": True, "transaction": "0x" + "zz" * 32},
        ]
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                ep = FakeEndpoint([challenge()], paid=(200, {
                    "content-type": "application/json", "PAYMENT-RESPONSE": header(receipt)}, b'{}'))
                _, res = self.run_flow(ep)
                self.assertFalse(res["settled"])
                self.assertFalse(res["checks"]["settled"])

    def test_unverified_onchain_receipt_does_not_score_as_settled(self):
        receipt = header({"success": True, "transaction": "0x" + "ab" * 32})
        ep = FakeEndpoint([challenge()], paid=(200, {
            "content-type": "application/json", "PAYMENT-RESPONSE": receipt}, b'{}'))
        _, res = self.run_flow(ep, settlement=(False, "transfer_event_mismatch"))
        self.assertFalse(res["settled"])
        self.assertFalse(res["checks"]["settled"])
        self.assertEqual(res["checks"]["settlement_check"], "transfer_event_mismatch")

    def test_onchain_verification_binds_payer_nonce_recipient_and_amount(self):
        req = challenge()["accepts"][0]
        payer = ACCOUNT.address.lower()
        nonce = "0x" + "cd" * 32
        tx_hash = "0x" + "ab" * 32
        payer_topic = "0x" + "0" * 24 + payer[2:]
        pay_to_topic = "0x" + "0" * 24 + PAY_TO[2:].lower()
        receipt = {
            "transactionHash": tx_hash,
            "status": "0x1",
            "blockNumber": "0x123",
            "logs": [
                {"address": pv.BASE_USDC, "topics": [pv.AUTHORIZATION_USED_TOPIC,
                 payer_topic, nonce], "data": "0x"},
                {"address": pv.BASE_USDC, "topics": [pv.TRANSFER_TOPIC,
                 payer_topic, pay_to_topic], "data": hex(10000)},
            ],
        }
        def rpc(method, params):
            return hex(pv.BASE_CHAIN_ID) if method == "eth_chainId" else receipt
        with patch.object(pv, "_rpc", side_effect=rpc):
            self.assertEqual(pv.verify_onchain_settlement(req, payer, nonce, tx_hash),
                             (True, "verified"))
            receipt["logs"][1]["data"] = hex(9999)
            self.assertEqual(pv.verify_onchain_settlement(req, payer, nonce, tx_hash),
                             (False, "transfer_event_mismatch"))

    def test_onchain_verification_rejects_wrong_chain(self):
        req = challenge()["accepts"][0]
        with patch.object(pv, "_rpc", return_value="0x1"):
            self.assertEqual(pv.verify_onchain_settlement(
                req, ACCOUNT.address, "0x" + "11" * 32, "0x" + "22" * 32),
                (False, "wrong_chain"))

    def test_pending_settlement_can_be_refreshed_without_replaying_payment(self):
        receipt = header({"success": True, "transaction": "0x" + "ab" * 32})
        ep = FakeEndpoint([challenge()], paid=(200, {
            "content-type": "application/json", "PAYMENT-RESPONSE": receipt}, b'{}'))
        with patch.object(pv, "_fetch", ep), patch.object(
                pv, "verify_onchain_settlement", return_value=(False, "receipt_pending")):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            res = pv.execute(quote["id"], self.uid, sign(quote["typed_data"]))
        self.assertFalse(res["settled"])
        checked_at = res["checks"]["settlement_checked_at"]
        with patch.object(pv, "verify_onchain_settlement", return_value=(True, "verified")), \
                patch.object(pv.time, "time", return_value=checked_at + pv.SETTLEMENT_RETRY_SECONDS + 1):
            refreshed = pv.result(quote["id"], self.uid)
        self.assertTrue(refreshed["settled"])
        self.assertEqual(refreshed["checks"]["settlement_check"], "verified")
        self.assertEqual(sum("PAYMENT-SIGNATURE" in h for _, h in ep.calls), 1)

    def test_expired_authorization_is_not_dispatched(self):
        ep = FakeEndpoint([challenge()])
        with patch.object(pv, "_fetch", ep):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            sig = sign(quote["typed_data"])
            with patch.object(pv.time, "time", return_value=time.time() + 3600):
                with self.assertRaises(pv.VerifyError) as caught:
                    pv.execute(quote["id"], self.uid, sig)
        self.assertEqual(caught.exception.code, "expired")
        self.assertFalse(any("PAYMENT-SIGNATURE" in h for _, h in ep.calls))

    def test_other_user_cannot_execute_or_read(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
            for fn in (lambda: pv.execute(quote["id"], self.other, sign(quote["typed_data"])),
                       lambda: pv.result(quote["id"], self.other)):
                with self.assertRaises(pv.VerifyError) as caught:
                    fn()
                self.assertEqual(caught.exception.code, "not_found")

    def test_open_verification_cap(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            for _ in range(3):
                pv.prepare(self.uid, self.svc, ACCOUNT.address)
            with self.assertRaises(pv.VerifyError) as caught:
                pv.prepare(self.uid, self.svc, ACCOUNT.address)
        self.assertEqual(caught.exception.code, "too_many_open")

    def test_prepare_expires_stale_open_records(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            quotes = [pv.prepare(self.uid, self.svc, ACCOUNT.address) for _ in range(3)]
            with db.writer() as conn:
                conn.execute("UPDATE paid_verifications SET valid_before=1 WHERE user_id=?",
                             (self.uid,))
            fresh = pv.prepare(self.uid, self.svc, ACCOUNT.address)
        self.assertNotIn(fresh["id"], {q["id"] for q in quotes})
        with closing(db.connect(read_only=True)) as conn:
            expired = conn.execute(
                "SELECT COUNT(*) FROM paid_verifications WHERE user_id=? AND status='expired'",
                (self.uid,)).fetchone()[0]
        self.assertEqual(expired, 3)

    def test_stuck_dispatch_becomes_indeterminate(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
        with db.writer() as conn:
            conn.execute("UPDATE paid_verifications SET status='dispatching', dispatched_at=1 WHERE id=?",
                         (quote["id"],))
            pv.expire_stale(conn)
        self.assertEqual(self.row(quote["id"])["status"], "indeterminate")

    def test_terminal_records_are_pruned_after_retention_window(self):
        self.run_flow(FakeEndpoint([challenge()]))
        cutoff = int(time.time()) + (pv.RETENTION_DAYS + 1) * 86400
        with db.writer() as conn:
            pv.expire_stale(conn, cutoff)
        with closing(db.connect(read_only=True)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM paid_verifications").fetchone()[0]
        self.assertEqual(count, 0)

    def test_late_finish_cannot_overwrite_indeterminate(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            quote = pv.prepare(self.uid, self.svc, ACCOUNT.address)
        with db.writer() as conn:
            conn.execute("UPDATE paid_verifications SET status='dispatching', dispatched_at=1 WHERE id=?",
                         (quote["id"],))
            pv.expire_stale(conn)
        pv._finish(quote["id"], status="succeeded", http_status=200)
        row = self.row(quote["id"])
        self.assertEqual(row["status"], "indeterminate")
        self.assertIsNone(row["http_status"])

    def test_public_summary_counts_failures(self):
        self.run_flow(FakeEndpoint([challenge()]))
        self.run_flow(FakeEndpoint([challenge()], paid=(500, {}, b"x")))
        with closing(db.connect(read_only=True)) as conn:
            summary = pv.public_summary(conn, self.sid)
        self.assertEqual((summary["runs"], summary["succeeded"]), (2, 1))

    def test_public_summary_is_bound_to_current_listing_version(self):
        self.run_flow(FakeEndpoint([challenge()]))
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNotNone(pv.public_summary(conn, self.sid))
        with db.writer() as conn:
            conn.execute("UPDATE services SET url=? WHERE id=?",
                         ("https://new.vendor.example/data", self.sid))
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNone(pv.public_summary(conn, self.sid))

    def test_public_summary_is_bound_to_complete_payment_terms(self):
        self.run_flow(FakeEndpoint([challenge()]))
        changed = challenge()["accepts"][0]
        changed["scheme"] = "upto"
        with db.writer() as conn:
            conn.execute("UPDATE services SET payment=? WHERE id=?",
                         (json.dumps({"accepts": [changed]}), self.sid))
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNone(pv.public_summary(conn, self.sid))

    def test_legacy_self_reported_settlement_is_not_public(self):
        quote, _ = self.run_flow(FakeEndpoint([challenge()]))
        row = self.row(quote["id"])
        checks = json.loads(row["checks"])
        checks.pop("settlement_check", None)
        with db.writer() as conn:
            conn.execute("UPDATE paid_verifications SET settled=1,checks=? WHERE id=?",
                         (json.dumps(checks), quote["id"]))
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNone(pv.public_summary(conn, self.sid))

    def test_real_fetch_refuses_private_targets(self):
        with self.assertRaises(public_http.UnsafeURL):
            public_http._parse_url("https://127.0.0.1/x")
        with self.assertRaises(pv.VerifyError) as caught:
            with patch.object(public_http, "validate_url", side_effect=public_http.UnsafeURL("private")):
                pv.fetch_challenge("https://internal.example/x")
        self.assertEqual(caught.exception.code, "unsafe_url")



class PaidVerifyRouteTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from directory import auth, paid_verify_routes
        app = FastAPI()
        app.include_router(paid_verify_routes.router)
        self.client = TestClient(app, base_url="https://agent-tools.cloud")
        self.auth = auth
        import os
        self.enterContext(patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret",
                                                  "PUBLIC_BASE_URL": "https://agent-tools.cloud"}))
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','pvr','pvr','active',1)").lastrowid
            conn.execute("INSERT INTO services(slug,name,url,source,source_id,created_at,updated_at) "
                         "VALUES('vendor','Vendor',?,'manual','vendor',1,1)", (URL,))
        self.enterContext(patch.object(public_http, "validate_url", return_value=None))

    def cookie(self):
        return {self.auth.SESSION_COOKIE: self.auth.sign({"uid": self.uid}, 600)}

    def test_requires_sign_in(self):
        r = self.client.post("/api/v1/services/vendor/paid-verifications", json={"payer": ACCOUNT.address})
        self.assertEqual(r.status_code, 401)

    def test_cookie_write_requires_same_origin(self):
        r = self.client.post("/api/v1/services/vendor/paid-verifications", json={"payer": ACCOUNT.address},
                             cookies=self.cookie(), headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_full_flow_over_http(self):
        hdr = {"Origin": "https://agent-tools.cloud"}
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            r = self.client.post("/api/v1/services/vendor/paid-verifications",
                                 json={"payer": ACCOUNT.address}, cookies=self.cookie(), headers=hdr)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.headers["cache-control"], "private, no-store")
            q = r.json()
            r = self.client.post(f"/api/v1/paid-verifications/{q['id']}/execute",
                                 json={"signature": sign(q["typed_data"])}, cookies=self.cookie(), headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "succeeded")
        r = self.client.get(f"/api/v1/paid-verifications/{q['id']}", cookies=self.cookie())
        self.assertEqual(r.json()["status"], "succeeded")

    def test_unknown_service_404(self):
        r = self.client.post("/api/v1/services/nope/paid-verifications", json={"payer": ACCOUNT.address},
                             cookies=self.cookie(), headers={"Origin": "https://agent-tools.cloud"})
        self.assertEqual(r.status_code, 404)

    def test_prepare_rate_limited(self):
        hdr = {"Origin": "https://agent-tools.cloud"}
        codes = []
        with patch.object(pv, "_fetch", FakeEndpoint([challenge(req={"network": "eip155:1"})])):
            for _ in range(8):
                codes.append(self.client.post("/api/v1/services/vendor/paid-verifications",
                                              json={"payer": ACCOUNT.address}, cookies=self.cookie(),
                                              headers=hdr).status_code)
        self.assertIn(429, codes)



class PaidVerifyDocsTests(TemporaryDatabaseTestCase):
    def test_public_docs_describe_the_flow(self):
        import os
        import importlib.util
        from pathlib import Path
        # Load this checkout's server.py; other tests put /opt/mcpserver on sys.path.
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        from fastapi.testclient import TestClient
        client = TestClient(server.app, base_url="https://agent-tools.cloud")
        llms = client.get("/llms.txt").text
        for needle in ("/api/v1/services/{slug}/paid-verifications",
                       "/api/v1/paid-verifications/{id}/execute", "POST /api/v1/keys",
                       "public JSON probe_body", "AuthorizationUsed", "Evidence is hidden"):
            self.assertIn(needle, llms)
        manifest = client.get("/.well-known/agent-tools.json").json()
        self.assertIn("sponsored_paid_verification", manifest["capabilities"])
        self.assertEqual(manifest["endpoints"]["paid_verification"]["limits"]["network"], pv.BASE_NETWORK)
        self.assertEqual(manifest["endpoints"]["paid_verification"]["limits"]["methods"],
                         ["GET", "POST"])
        self.assertIn("AuthorizationUsed",
                  manifest["endpoints"]["paid_verification"]["notes"])
        spec = client.get("/openapi.json").json()
        self.assertIn("/api/v1/services/{slug}/paid-verifications", spec["paths"])
        self.assertIn("/api/v1/paid-verifications/{vid}/execute", spec["paths"])
        self.assertIn("paid-verifications", spec["info"]["x-guidance"])
        key_docs = spec["paths"]["/api/v1/keys"]["post"]["description"]
        normalized_key_docs = " ".join(key_docs.split())
        self.assertIn("sponsored paid verification", normalized_key_docs)
        self.assertIn("verified claim", normalized_key_docs)
        self.assertIn("account-scoped actions", client.get("/privacy").text)
        self.assertIn("no listing edit rights", client.get("/docs/claim").text)
        self.assertIn("retained for at most 90 days", client.get("/privacy").text)
        self.assertIn("not independently verified",
                  (root / "directory/templates/service.html").read_text())

    def test_service_card_carries_public_summary(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from directory import routes
        with db.writer() as conn:
            sid = conn.execute(
                "INSERT INTO services(slug,name,url,source,source_id,created_at,updated_at) "
                "VALUES('card','Card',?,'manual','card',1,1)", (URL,)).lastrowid
        app = FastAPI(); app.include_router(routes.router)
        card = TestClient(app).get("/api/v1/services/card").json()
        self.assertIn("paid_verification", card)
        self.assertIsNone(card["paid_verification"])


if __name__ == "__main__":
    import unittest
    unittest.main()
