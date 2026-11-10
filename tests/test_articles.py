import io
import importlib
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from directory import auth, db
from directory.article_routes import create_article_router
from directory.articles import ArticleError, ArticleStore, MAX_MARKDOWN_BYTES
from directory.article_render import outbound_link_count, private_html, render_markdown
from directory.article_payments import NowPaymentsClient, NowPaymentsConfig, payment_fingerprint, strict_payment_json
from PIL import Image


class ArticleHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))


class NowPaymentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = NowPaymentsConfig(api_key="offline-test-key", ipn_secret="offline-test-secret", enabled=True,
            approved=True, signature_verified=True, signature_format="python-sorted-v1",
            prices={"standard": 9900, "premium": 19900}, currencies={"usdtbsc": "bsc"},
            outcome_currency="usdtbsc", outcome_network="bsc", terms_version="offline-v1",
            terms_text="Offline fixture terms only.", fixed_rate=True, fee_paid_by_user=False)
        self.order = {"id": "blog-" + "a" * 24, "merchant_key_ref": self.config.merchant_ref,
                      "price_usd_cents": 9900, "pay_currency": "usdtbsc", "plan": "standard"}

    def sign(self, body):
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        return hmac.new(self.config.ipn_secret.encode(), canonical, hashlib.sha512).hexdigest()

    def test_ipn_requires_signature_and_rejects_ambiguous_json(self):
        body = {"payment_id": 12345, "order_id": self.order["id"], "payment_status": "finished"}
        raw = json.dumps(body).encode()
        self.assertEqual(self.config.verify_ipn(raw, self.sign(body)), body)
        for signature in (None, "a" * 128, "bad", "非ASCII"):
            with self.subTest(signature=signature), self.assertRaises(ArticleError):
                self.config.verify_ipn(raw, signature)
        for raw in (b'{"payment_id":1,"payment_id":2}', b'{"amount":NaN}', b'{"amount":1e9999}', b'[]', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ArticleError):
                self.config.verify_ipn(raw, "a" * 128)

    def test_ipn_locks_one_serialization_rule_for_arrays_floats_and_unicode(self):
        body = {"amount": 100.0, "payment_extra_ids": [123], "description": "离线签名测试"}
        self.assertEqual(self.config.verify_ipn(json.dumps(body).encode(), self.sign(body)), body)
        other = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        signature = hmac.new(self.config.ipn_secret.encode(), other, hashlib.sha512).hexdigest()
        with self.assertRaisesRegex(ArticleError, "invalid_payment_signature"):
            self.config.verify_ipn(json.dumps(body).encode(), signature)

    def test_rest_amounts_keep_decimal_precision_and_duplicate_keys_fail(self):
        self.assertEqual(str(strict_payment_json(b'{"actually_paid":0.10000000000000001}')["actually_paid"]),
                         "0.10000000000000001")
        self.assertNotEqual(payment_fingerprint({"payment_status": "waiting"}),
                            payment_fingerprint({"payment_status": "finished"}))
        with self.assertRaises(ArticleError):
            strict_payment_json(b'{"order_id":"one","order_id":"two"}')

    def test_incomplete_existing_configuration_stays_closed_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nowpayments.json"
            path.write_text(json.dumps({"api_key": "offline-test-key", "rest_api_base": "https://api.nowpayments.io/v1"}))
            path.chmod(0o600)
            config = NowPaymentsConfig.from_file(path)
            self.assertFalse(config.ready)
            self.assertNotIn("offline-test-key", repr(config))
            path.chmod(0o644)
            with self.assertRaisesRegex(ArticleError, "unsafe_payment_configuration"):
                NowPaymentsConfig.from_file(path)

    def test_merchant_default_settlement_has_no_primary_balance_requirement(self):
        config = replace(self.config, settlement_mode="merchant-default", outcome_currency="", outcome_network="",
                         settlement_currencies={"usdtbsc": "bsc", "usdttrc20": "trx"})
        self.assertTrue(config.ready)
        self.assertIn("existing NOWPayments merchant payout settings", config.publication_terms)
        self.assertNotEqual(config.checkout_hash, self.config.checkout_hash)
        self.assertNotEqual(config.checkout_hash, replace(config, settlement_currencies={"usdtbsc": "bsc"}).checkout_hash)
        self.assertFalse(replace(config, settlement_currencies={}).ready)
        self.assertFalse(replace(config, signature_verified=False).ready)
        self.assertFalse(replace(config, approved=False).ready)
        self.assertFalse(replace(config, outcome_currency="usdtbsc", outcome_network="bsc").ready)
        self.assertFalse(replace(config, settlement_currencies={"ton": "ton"}).ready)
        self.assertFalse(replace(config, settlement_currencies={"ton": None}).ready)
        self.assertFalse(replace(self.config, currencies={"btc": None}).ready)
        self.assertTrue(replace(config, settlement_currencies={"usdtton": "ton"}).ready)
        self.assertFalse(replace(config, ipn_secret="https://example.invalid/notifications").ready)
        self.assertFalse(replace(config, ipn_secret=config.api_key).ready)

    def test_configuration_only_accepts_stablecoin_networks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nowpayments.json"
            base = {"api_key": "offline-test-key", "settlement_mode": "merchant-default",
                    "pay_currencies": {"usdtbsc": "bsc"}, "settlement_currencies": {"usdttrc20": "trx"}}
            path.write_text(json.dumps(base))
            path.chmod(0o600)
            self.assertEqual(dict(NowPaymentsConfig.from_file(path).settlement_currencies), {"usdttrc20": "trx"})
            self.assertFalse(NowPaymentsConfig.from_file(path).ready)
            for invalid in ({"settlement_mode": "custody"}, {"settlement_currencies": []},
                            {"pay_currencies": {"btc": "btc"}}, {"pay_currencies": {"eth": "eth"}},
                            {"pay_currencies": {"btc": None}},
                            {"settlement_currencies": {"ton": "ton"}},
                            {"settlement_currencies": {"ton": None}},
                            {"ipn_secret": "https://example.invalid/notifications"},
                            {"pay_currencies": {"usdtbsc": "eth"}},
                            {"settlement_currencies": {"usdtbsc": "wrong network"}}):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps({**base, **invalid}))
                    with self.assertRaisesRegex(ArticleError, "invalid_payment_configuration"):
                        NowPaymentsConfig.from_file(path)

    def formal_configuration_module(self):
        script = Path(__file__).resolve().parents[2] / "configure-formal-collection.py"
        if not script.exists():
            self.skipTest("operator script is not deployed with the site")
        spec = importlib.util.spec_from_file_location("formal_collection_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_formal_configuration_keeps_credentials_and_qualification_flags(self):
        formal = self.formal_configuration_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nowpayments.json"
            original = {**formal.APPROVED_PRIOR, "api_key": "offline-test-key", "ipn_secret": "offline-test-secret",
                        "prices_usd_cents": {"standard": 9900, "premium": 19900}, "pay_currencies": dict(formal.CURRENCIES),
                        "is_fee_paid_by_user": False, "sponsored_articles_enabled": False,
                        "business_approved": False, "settlement_verified": False, "ipn_signature_verified": False}
            path.write_text(json.dumps(original))
            path.chmod(0o600)
            def respond(request):
                if request.url.path.endswith("merchant/coins"):
                    return httpx.Response(200, json={"selectedCurrencies": list(formal.CURRENCIES)})
                return httpx.Response(200, json={"currencies": [
                    {"code": code.upper(), "network": network, "enable": True, "available_for_payment": True,
                     "is_stable": True, "available_for_to_conversion": True, "available_for_payout": True}
                    for code, network in {**formal.CURRENCIES, **formal.SETTLEMENT_CURRENCIES}.items()]})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                result = formal.configure(path, root, root / "absent-articles.db", client,
                                          expected_merchant_ref=hashlib.sha256(b"offline-test-key").hexdigest())
            actual = json.loads(path.read_text())
            self.assertEqual(actual["pay_currencies"], {"usdtbsc": "bsc", "usdcbsc": "bsc", "usdcbase": "base", "usdttrc20": "trx"})
            self.assertEqual(actual["settlement_currencies"], {"usdtbsc": "bsc", "usdttrc20": "trx", "usdtton": "ton"})
            self.assertEqual(actual["settlement_mode"], "merchant-default")
            self.assertFalse(actual["is_fixed_rate"])
            self.assertIn("accepts USDT or USDC only", actual["terms_text"])
            self.assertEqual({name: value for name, value in actual.items() if name not in formal.UPDATES},
                             {name: value for name, value in original.items() if name not in formal.UPDATES})
            self.assertFalse(result["payments_enabled"])
            self.assertTrue(result["verification_flags_unchanged"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_formal_configuration_does_not_rewrite_unknown_terms_or_wrong_merchant(self):
        formal = self.formal_configuration_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nowpayments.json"
            base = {**formal.APPROVED_PRIOR, "api_key": "offline-test-key", "ipn_secret": "offline-test-secret",
                    "prices_usd_cents": {"standard": 9900, "premium": 19900}}
            for change, expected in (({"terms_text": "Unexpected contract text"}, "existing_formal_setting_differs"),
                                     ({"api_key": "different-offline-key"}, "approved_merchant_changed")):
                original = json.dumps({**base, **change})
                path.write_text(original)
                path.chmod(0o600)
                with self.subTest(change=change), self.assertRaisesRegex(formal.FormalConfigurationError, expected):
                    formal.configure(path, root, root / "absent-articles.db",
                                     expected_merchant_ref=hashlib.sha256(b"offline-test-key").hexdigest())
                self.assertEqual(path.read_text(), original)

    def test_ipn_secret_configuration_repairs_url_only_for_the_bound_merchant(self):
        script = Path(__file__).resolve().parents[2] / "set-merchant-ipn-secret.py"
        if not script.exists():
            self.skipTest("operator script is not deployed with the site")
        spec = importlib.util.spec_from_file_location("ipn_secret_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nowpayments.json"
            original = {"api_key": "offline-test-key", "ipn_secret": "https://example.invalid/notifications",
                        "prices_usd_cents": {"standard": 9900, "premium": 19900},
                        "sponsored_articles_enabled": False, "ipn_signature_verified": False,
                        "settlement_mode": "merchant-default", "other_field": "unchanged"}
            path.write_text(json.dumps(original))
            path.chmod(0o600)
            with self.assertRaisesRegex(module.SecretConfigurationError, "invalid_ipn_secret_format"):
                module.update_ipn_secret("https://example.invalid/another-url", path, root)
            with self.assertRaisesRegex(module.SecretConfigurationError, "approved_merchant_changed"):
                module.update_ipn_secret("offline-valid-ipn-secret", path, root, expected_merchant_ref="a" * 64)
            with self.assertRaisesRegex(module.SecretConfigurationError, "refusing_to_replace_existing_ipn_secret"):
                module.update_ipn_secret("offline-valid-ipn-secret", path, root)
            self.assertEqual(json.loads(path.read_text()), original)
            result = module.update_ipn_secret("offline-valid-ipn-secret", path, root,
                                             expected_merchant_ref=hashlib.sha256(b"offline-test-key").hexdigest())
            actual = json.loads(path.read_text())
            self.assertEqual(actual["ipn_secret"], "offline-valid-ipn-secret")
            self.assertEqual({name: value for name, value in actual.items() if name != "ipn_secret"},
                             {name: value for name, value in original.items() if name != "ipn_secret"})
            self.assertTrue(result["repaired_notification_url"])
            self.assertFalse(actual["ipn_signature_verified"])
            with self.assertRaisesRegex(module.SecretConfigurationError, "refusing_to_replace_existing_ipn_secret"):
                module.update_ipn_secret("different-valid-ipn-secret", path, root,
                                         expected_merchant_ref=hashlib.sha256(b"offline-test-key").hexdigest())

    def test_verification_callback_checks_signature_and_merchant_query_without_publication(self):
        script = Path(__file__).resolve().parents[2] / "verify-nowpayments-ipn.py"
        if not script.exists():
            self.skipTest("operator script is not deployed with the site")
        spec = importlib.util.spec_from_file_location("nowpayments_verification_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            result = module.prepare_verification(self.config, Path(temporary), expected_merchant_ref=self.config.merchant_ref)
            directory = Path(result["private_case"])
            case = module.load_case(directory)
            case["invoice_id"] = "900001"
            module.private_json(directory / "case.json", case)
            calls = []
            async def lookup(configuration, identifier):
                calls.append(identifier)
                return {"payment_id": identifier, "invoice_id": "900001", "order_id": case["order_id"],
                        "price_amount": "99", "price_currency": "usd", "pay_currency": "usdtbsc", "payment_status": "waiting"}
            app = module.verification_app(directory, configuration_loader=lambda: self.config, payment_loader=lookup)
            with TestClient(app) as client:
                body = {"order_id": case["order_id"], "payment_id": "900002", "payment_status": "waiting"}
                self.assertEqual(client.post(case["callback_path"], json=body).status_code, 401)
                self.assertEqual(calls, [])
                self.assertFalse((directory / "receipt.json").exists())
                response = client.post(case["callback_path"], content=json.dumps(body),
                                       headers={"x-nowpayments-sig": self.sign(body)})
                self.assertEqual(response.status_code, 200, response.text)
                receipt = json.loads((directory / "receipt.json").read_text())
                self.assertTrue(receipt["signature_valid"])
                self.assertTrue(receipt["merchant_query_verified"])
                self.assertTrue(receipt["no_article_publication"])
                self.assertFalse(receipt["funds_sent_by_verifier"])
                self.assertNotIn(self.config.ipn_secret, json.dumps(receipt))
                self.assertNotIn("raw_notification", receipt)

    def test_verification_invoice_is_created_once_and_unknown_result_is_not_retried(self):
        script = Path(__file__).resolve().parents[2] / "verify-nowpayments-ipn.py"
        if not script.exists():
            self.skipTest("operator script is not deployed with the site")
        spec = importlib.util.spec_from_file_location("nowpayments_single_invoice_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            for outcome in ("success", "timeout"):
                with self.subTest(outcome=outcome):
                    result = module.prepare_verification(self.config, Path(temporary), expected_merchant_ref=self.config.merchant_ref)
                    directory = Path(result["private_case"])
                    calls = []
                    def respond(request):
                        calls.append(request)
                        if outcome == "timeout":
                            raise httpx.ReadTimeout("offline uncertainty", request=request)
                        payload = json.loads(request.content)
                        self.assertEqual(payload["price_amount"], 99)
                        self.assertEqual(payload["pay_currency"], "usdtbsc")
                        self.assertNotIn(self.config.api_key, request.content.decode())
                        return httpx.Response(201, json={"id": "900001", "order_id": payload["order_id"],
                            "price_amount": "99", "price_currency": "usd", "pay_currency": "usdtbsc",
                            "invoice_url": "https://nowpayments.io/payment/?iid=900001"})
                    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                        if outcome == "timeout":
                            with self.assertRaises(httpx.ReadTimeout):
                                module.create_verification_invoice(directory, self.config, client=client)
                        else:
                            created = module.create_verification_invoice(directory, self.config, client=client)
                            self.assertTrue(created["invoice_created_once"])
                            self.assertFalse(created["funds_sent"])
                        with self.assertRaisesRegex(ValueError, "verification_invoice_already_attempted"):
                            module.create_verification_invoice(directory, self.config, client=client)
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(module.load_case(directory)["invoice_creation_attempted"])

    def test_verification_nginx_patch_preserves_private_directives_and_is_reversible(self):
        script = Path(__file__).resolve().parents[2] / "verify-nowpayments-ipn.py"
        if not script.exists():
            self.skipTest("operator script is not deployed with the site")
        spec = importlib.util.spec_from_file_location("nowpayments_nginx_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "agent-tools.cloud"
            original = 'server {\n    include /etc/nginx/snippets/agent-tools-a2a-cache.conf;\n    proxy_set_header X-API-Key "offline-private-fixture";\n}\n'
            path.write_text(original)
            module.patch_nginx_candidate(directory)
            self.assertIn('proxy_set_header X-API-Key "offline-private-fixture";', path.read_text())
            self.assertEqual(path.read_text().count("include /etc/nginx/snippets/agent-tools-nowpayments-verification.conf;"), 1)
            module.patch_nginx_candidate(directory, reverse=True)
            self.assertEqual(path.read_text(), original)
            path.write_text(original + '    include /etc/nginx/snippets/agent-tools-a2a-cache.conf;\n')
            with self.assertRaisesRegex(ValueError, "nginx_verification_anchor_not_unique"):
                module.patch_nginx_candidate(directory)

    async def test_invoice_refuses_nonstable_or_unconfigured_currencies(self):
        calls = []
        client = NowPaymentsClient(self.config, transport=httpx.MockTransport(lambda request: calls.append(request)))
        for currency in ("btc", "eth", "ton", "usdcbsc", "usdtbscfake", None):
            with self.subTest(currency=currency), self.assertRaisesRegex(ArticleError, "invalid_payment_currency"):
                await client.create_invoice({**self.order, "pay_currency": currency}, "https://agent-tools.cloud")
        self.assertEqual(calls, [])
        self.assertFalse(replace(self.config, currencies={"btc": "btc"}).ready)

    async def test_payment_query_keeps_unaccepted_currency_for_manual_review(self):
        for currency in ("btc", "eth", "ton", "usdcbsc", None):
            client = NowPaymentsClient(self.config, transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"payment_id": "900002", "pay_currency": currency, "payment_status": "finished"})))
            with self.subTest(currency=currency):
                payment = await client.get_payment("900002", self.config.merchant_ref)
                self.assertEqual(payment["pay_currency"], currency)

    async def test_merchant_default_preflight_does_not_request_a_primary_balance(self):
        config = replace(self.config, settlement_mode="merchant-default", outcome_currency="", outcome_network="",
                         settlement_currencies={"usdtbsc": "bsc", "usdttrc20": "trx"}, fixed_rate=False)
        calls = []
        def respond(request):
            calls.append(request)
            if request.url.path.endswith("merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["USDTBSC", "USDTTRC20"]})
            if request.url.path.endswith("full-currencies"):
                return httpx.Response(200, json={"currencies": [
                    {"code": "USDTBSC", "network": "bsc", "enable": True, "available_for_payment": True,
                     "is_stable": True, "available_for_to_conversion": True, "available_for_payout": True},
                    {"code": "USDTTRC20", "network": "trx", "enable": True, "available_for_payment": True,
                     "is_stable": True, "available_for_to_conversion": True, "available_for_payout": True}]})
            if request.url.path.endswith("min-amount"):
                self.assertNotIn("currency_to", request.url.params)
                return httpx.Response(200, json={"currency_from": "usdtbsc", "currency_to": "false", "min_amount": "0.1"})
            return httpx.Response(200, json={"currency_from": "usd", "currency_to": "usdtbsc", "estimated_amount": "99"})
        await NowPaymentsClient(config, transport=httpx.MockTransport(respond)).preflight(9900, "usdtbsc")
        self.assertTrue(all("balance" not in request.url.path for request in calls))

    def test_isolated_config_override_and_invalid_header_values_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unconfigured-nowpayments.json"
            with patch.dict(os.environ, {"NOWPAYMENTS_ARTICLES_CONFIG": str(path)}):
                self.assertFalse(NowPaymentsConfig.from_file().ready)
            with patch.dict(os.environ, {"NOWPAYMENTS_ARTICLES_CONFIG": "relative-config.json"}):
                with self.assertRaisesRegex(ArticleError, "unsafe_payment_configuration"):
                    NowPaymentsConfig.from_file()
            path.write_text(json.dumps({"api_key": "invalid-\u0000-header"}))
            path.chmod(0o600)
            with self.assertRaisesRegex(ArticleError, "invalid_payment_configuration"):
                NowPaymentsConfig.from_file(path)

    async def test_invoice_is_fixed_host_and_uses_frozen_server_price(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(201, json={"id": "900001", "invoice_url": "https://nowpayments.io/payment/?iid=900001"})
        result = await NowPaymentsClient(self.config, transport=httpx.MockTransport(respond)).create_invoice(
            self.order, "https://agent-tools.cloud")
        self.assertEqual(result["id"], "900001")
        self.assertEqual(str(requests[0].url), "https://api.nowpayments.io/v1/invoice")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["price_amount"], 99)
        self.assertEqual(payload["order_id"], self.order["id"])
        self.assertFalse(payload["is_fee_paid_by_user"])
        self.assertNotIn(self.config.api_key, requests[0].content.decode())

    async def test_invoice_timeout_is_not_retried_and_redirect_is_not_followed(self):
        calls = []
        def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout("offline timeout", request=request)
        with self.assertRaisesRegex(ArticleError, "payment_provider_unavailable"):
            await NowPaymentsClient(self.config, transport=httpx.MockTransport(timeout)).create_invoice(
                self.order, "https://agent-tools.cloud")
        self.assertEqual(len(calls), 1)
        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://untrusted.example"})
        with self.assertRaisesRegex(ArticleError, "payment_provider_unavailable"):
            await NowPaymentsClient(self.config, transport=httpx.MockTransport(redirect)).create_invoice(
                self.order, "https://agent-tools.cloud")
        self.assertEqual(len(calls), 2)

    async def test_query_is_bound_to_original_key_and_requested_payment(self):
        def respond(request):
            return httpx.Response(200, json={"payment_id": "900002", "payment_status": "finished"})
        client = NowPaymentsClient(self.config, transport=httpx.MockTransport(respond))
        with self.assertRaisesRegex(ArticleError, "payment_merchant_mismatch"):
            await client.get_payment("900002", "a" * 64)
        with self.assertRaisesRegex(ArticleError, "payment_id_mismatch"):
            await client.get_payment("900003", self.config.merchant_ref)

    async def test_full_currency_metadata_can_exceed_payment_body_limit(self):
        metadata = {"currencies": [{"code": "usdtbsc", "network": "bsc"}], "padding": "x" * 250_000}
        client = NowPaymentsClient(self.config, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=metadata)))
        self.assertEqual((await client._request("GET", "full-currencies"))["currencies"], metadata["currencies"])
        with self.assertRaisesRegex(ArticleError, "invalid_payment_json"):
            await client._request("GET", "payment/900002")
        with self.assertRaisesRegex(ArticleError, "invalid_payment_json"):
            strict_payment_json(json.dumps(metadata).encode())

    async def test_currency_metadata_still_has_a_bounded_limit(self):
        client = NowPaymentsClient(self.config, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"currencies": [], "padding": "x" * (1024 * 1024)})))
        with self.assertRaisesRegex(ArticleError, "invalid_payment_json"):
            await client._request("GET", "full-currencies")

    async def test_preflight_checks_merchant_network_and_minimum(self):
        def respond(request):
            if request.url.path.endswith("merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["usdtbsc"]})
            if request.url.path.endswith("full-currencies"):
                return httpx.Response(200, json={"currencies": [{"code": "usdtbsc", "network": "bsc",
                                                                "enable": True, "available_for_payment": True}]})
            if request.url.path.endswith("min-amount"):
                return httpx.Response(200, json={"min_amount": "10"})
            return httpx.Response(200, json={"currency_from": "usd", "currency_to": "usdtbsc", "estimated_amount": "99"})
        await NowPaymentsClient(self.config, transport=httpx.MockTransport(respond)).preflight(9900, "usdtbsc")
        with self.assertRaisesRegex(ArticleError, "invalid_payment_currency"):
            await NowPaymentsClient(self.config, transport=httpx.MockTransport(respond)).preflight(9900, "usdterc20")

    async def test_preflight_accepts_official_uppercase_currency_codes(self):
        def respond(request):
            if request.url.path.endswith("merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["USDTBSC"]})
            if request.url.path.endswith("full-currencies"):
                return httpx.Response(200, json={"currencies": [{"code": "USDTBSC", "network": "bsc",
                                                                "enable": True, "available_for_payment": True}]})
            if request.url.path.endswith("min-amount"):
                return httpx.Response(200, json={"min_amount": "10"})
            return httpx.Response(200, json={"currency_from": "USD", "currency_to": "USDTBSC", "estimated_amount": "99"})
        await NowPaymentsClient(self.config, transport=httpx.MockTransport(respond)).preflight(9900, "usdtbsc")

    async def test_preflight_rejects_conflicting_currency_network_metadata(self):
        def respond(request):
            if request.url.path.endswith("merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["USDTBSC"]})
            return httpx.Response(200, json={"currencies": [{"code": "USDTBSC", "network": "bsc"},
                                                           {"code": "usdtbsc", "network": "eth"}]})
        with self.assertRaisesRegex(ArticleError, "payment_network_mismatch"):
            await NowPaymentsClient(self.config, transport=httpx.MockTransport(respond)).preflight(9900, "usdtbsc")


class ArticleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ArticleStore(Path(self.temporary.name) / "articles.db")
        self.store.init()
        self.snapshot = {"title": "MCP service checklist", "summary": "Check a service before connecting.",
                         "author": "Agent Tools", "markdown": "## Connection\n\nCheck authentication.\n",
                         "tags": ["MCP"], "canonical_url": None}
        self.article = self.store.create("mcp-service-checklist", self.snapshot, editor_id=7)

    def test_draft_is_private_and_published_snapshot_does_not_follow_edits(self):
        self.assertEqual(self.store.public_state(self.article["slug"]), {"status": "missing"})
        self.assertEqual(self.store.list_published()["total"], 0)
        self.store.publish(self.article["id"], 1, editor_id=7)
        original = self.store.public_state(self.article["slug"])["article"]
        revised = {**self.snapshot, "title": "Private unreleased revision", "tags": ["Private"]}
        self.store.save(self.article["id"], 1, revised, editor_id=7)
        self.assertEqual(self.store.public_state(self.article["slug"])["article"], original)
        self.assertEqual(self.store.list_published(query="unreleased")["total"], 0)
        self.assertEqual(self.store.published_tags(), ["MCP"])
        self.assertNotIn("editor_id", original)
        self.assertNotIn("latest_version", original)

    def test_stale_save_and_stale_publish_do_not_change_state(self):
        self.store.save(self.article["id"], 1, {**self.snapshot, "title": "Updated title"}, editor_id=7)
        for operation in (lambda: self.store.save(self.article["id"], 1, self.snapshot, editor_id=7),
                          lambda: self.store.publish(self.article["id"], 1, editor_id=7)):
            with self.assertRaisesRegex(ArticleError, "version_conflict"):
                operation()
        self.assertEqual(self.store.get_for_edit(self.article["id"])["latest"]["version"], 2)
        self.assertEqual(self.store.list_published()["total"], 0)

    def test_publish_is_idempotent_and_revisions_have_separate_publication(self):
        self.store.publish(self.article["id"], 1, editor_id=7)
        first = self.store.public_state(self.article["slug"])["article"]
        self.store.publish(self.article["id"], 1, editor_id=7)
        self.assertEqual(self.store.public_state(self.article["slug"])["article"], first)
        saved = self.store.save(self.article["id"], 1, {**self.snapshot, "title": "Updated title"}, editor_id=7)
        self.store.publish(self.article["id"], saved["latest"]["version"], editor_id=7)
        current = self.store.public_state(self.article["slug"])["article"]
        self.assertEqual(current["title"], "Updated title")
        self.assertEqual(current["published_at"], first["published_at"])
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM article_audit WHERE action='publish'").fetchone()[0], 2)

    def test_unchanged_save_does_not_create_a_new_revision(self):
        state = self.store.save(self.article["id"], 1, self.snapshot, editor_id=7)
        self.assertEqual(state["latest"]["version"], 1)

    def frozen_order(self):
        self.snapshot = {**self.snapshot, "publication_type": "sponsored", "sponsor_name": "Example sponsor",
                         "sponsored_plan": "standard"}
        self.article = self.store.create("sponsored-example", self.snapshot, editor_id=7)
        self.store.submit(self.article["id"], 1, 7)
        return self.store.freeze_order(self.article["id"], 1, "standard", 9900, "offline-terms-v1", 8,
                           terms_text="Offline fixture terms only.", checkout_config_hash="f" * 64)["order"]

    def test_sponsored_draft_cannot_use_ordinary_publication_before_order(self):
        snapshot = {**self.snapshot, "publication_type": "sponsored", "sponsor_name": "Example sponsor"}
        article = self.store.create_for_author("unpaid-sponsored-example", snapshot, 7)
        self.store.submit(article["id"], 1, 7)
        with self.assertRaisesRegex(ArticleError, "article_payment_required"):
            self.store.publish(article["id"], 1, 8)
        self.assertEqual(self.store.public_state(article["slug"]), {"status": "missing"})

    def test_sponsored_link_quota_is_checked_on_the_frozen_rendered_version(self):
        snapshot = {**self.snapshot, "publication_type": "sponsored", "sponsor_name": "Example sponsor",
                    "sponsored_plan": "standard", "markdown": "[One](https://one.example) [Two](https://two.example)"}
        article = self.store.create_for_author("too-many-sponsored-links", snapshot, 7)
        self.store.submit(article["id"], 1, 7)
        with self.assertRaisesRegex(ArticleError, "sponsored_link_limit"):
            self.store.freeze_order(article["id"], 1, "standard", 9900, "offline-terms-v1", 8,
                                    terms_text="Offline fixture terms only.", checkout_config_hash="f" * 64)
        self.assertIsNone(self.store.get_for_edit(article["id"])["order"])

    def test_sponsored_cancellation_cannot_clear_the_paid_publication_type(self):
        order = self.frozen_order()
        self.store.cancel_order(order["id"], 7)
        with self.assertRaisesRegex(ArticleError, "article_order_type_locked"):
            self.store.save(self.article["id"], 1, {key: value for key, value in self.snapshot.items()
                                                if key not in ("publication_type", "sponsor_name", "sponsored_plan")}, 7)

    def test_sponsored_frozen_order_blocks_edits_and_unpaid_publication(self):
        order = self.frozen_order()
        for editor_id in (7, 8):
            with self.subTest(editor_id=editor_id), self.assertRaisesRegex(ArticleError, "article_order_locked"):
                self.store.save(self.article["id"], 1, {**self.snapshot, "title": "Changed frozen draft"}, editor_id)
        with self.assertRaisesRegex(ArticleError, "article_payment_required"):
            self.store.publish(self.article["id"], 1, 8)
        with self.assertRaisesRegex(ArticleError, "article_order_locked"):
            self.store.submit(self.article["id"], 1, 7)
        self.assertEqual(self.store.public_state(self.article["slug"]), {"status": "missing"})
        self.assertEqual(self.store.get_for_edit(self.article["id"])["latest"]["version"], 1)
        self.assertEqual(order["author_id"], 7)
        self.assertEqual(order["reviewer_id"], 8)

    def test_sponsored_confirmation_is_author_scoped_and_binds_price_and_version(self):
        order = self.frozen_order()
        with self.assertRaisesRegex(ArticleError, "article_order_not_found"):
            self.store.confirm_order(order["id"], 8, order["confirmation_hash"])
        for invalid_hash in ("0" * 64, "错误确认值", None, True):
            with self.subTest(invalid_hash=invalid_hash), self.assertRaisesRegex(ArticleError, "article_order_confirmation_conflict"):
                self.store.confirm_order(order["id"], 7, invalid_hash)
        confirmed = self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
        self.assertEqual(confirmed["order"]["status"], "awaiting_payment")
        self.assertEqual(self.store.confirm_order(order["id"], 7, order["confirmation_hash"]), confirmed)
        with self.assertRaisesRegex(ArticleError, "article_payment_required"):
            self.store.publish(self.article["id"], 1, 8)

    def test_sponsored_order_cancel_does_not_turn_frozen_content_into_free_publication(self):
        order = self.frozen_order()
        self.store.cancel_order(order["id"], 7)
        self.store.submit(self.article["id"], 1, 7)
        with self.assertRaisesRegex(ArticleError, "article_payment_required"):
            self.store.publish(self.article["id"], 1, 8)
        saved = self.store.save(self.article["id"], 1, {**self.snapshot, "title": "Revised sponsored draft"}, 7)
        self.store.submit(self.article["id"], saved["latest"]["version"], 7)
        with self.assertRaisesRegex(ArticleError, "article_order_version_conflict"):
            self.store.publish(self.article["id"], saved["latest"]["version"], 8)

    def invoice_order(self, *, merchant_default=False):
        order = self.frozen_order()
        self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
        pending, claimed = self.store.begin_invoice(order["id"], 7, "a" * 64, "usdtbsc", None if merchant_default else "usdtbsc",
                               checkout_config_hash="f" * 64, pay_network="bsc",
                               outcome_currencies={"usdtbsc": "bsc", "usdttrc20": "trx"} if merchant_default else None)
        self.assertTrue(claimed)
        invoice = {"id": "900001", "order_id": order["id"], "price_amount": "99", "price_currency": "usd",
                   "pay_currency": "usdtbsc", "invoice_url": "https://nowpayments.io/payment/?iid=900001"}
        return self.store.attach_invoice(order["id"], "a" * 64, invoice)

    def verified_payment(self, order):
        return {"payment_id": "900002", "invoice_id": "900001", "order_id": order["id"],
                "payment_status": "finished", "price_amount": "99.00", "price_currency": "usd",
                "pay_currency": "usdtbsc", "pay_amount": "99", "actually_paid": "99",
                "outcome_currency": "usdtbsc", "outcome_amount": "98", "parent_payment_id": None,
                "payment_extra_ids": None}

    def test_merchant_default_order_binds_actual_stablecoin_settlement(self):
        order = self.invoice_order(merchant_default=True)
        self.assertIsNone(order["outcome_currency"])
        payment = {**self.verified_payment(order), "outcome_currency": "usdttrc20"}
        paid = self.store.apply_verified_payment(order["id"], "a" * 64, payment, "b" * 64,
                    checkout_config_hash="f" * 64, outcome_currencies={"usdtbsc": "bsc", "usdttrc20": "trx"})
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(paid["outcome_currency"], "usdttrc20")
        self.store.publish(self.article["id"], 1, 8)
        repeated = self.store.apply_verified_payment(order["id"], "a" * 64, payment, "b" * 64,
                    checkout_config_hash="f" * 64, outcome_currencies={"usdtbsc": "bsc", "usdttrc20": "trx"})
        self.assertEqual(repeated["status"], "published")

    def test_merchant_default_order_rejects_other_coins_and_changed_configuration(self):
        for case_index, change in enumerate(({"pay_currency": "btc"}, {"outcome_currency": "ton"},
                                             {"outcome_currency": "usdcbsc"}, {"outcome_amount": "0"},
                                             {"changed_configuration": True}, {"empty_settlements": True})):
            with self.subTest(change=change):
                if case_index:
                    self.store = ArticleStore(Path(self.temporary.name) / ("merchant-default-" + str(case_index) + ".db"))
                    self.store.init()
                    self.article = self.store.create("mcp-service-checklist", self.snapshot, editor_id=7)
                order = self.invoice_order(merchant_default=True)
                payment = {**self.verified_payment(order), **{name: value for name, value in change.items()
                                                            if name not in ("changed_configuration", "empty_settlements")}}
                result = self.store.apply_verified_payment(order["id"], "a" * 64, payment, "c" * 64,
                    checkout_config_hash="e" * 64 if change.get("changed_configuration") else "f" * 64,
                    outcome_currencies={} if change.get("empty_settlements") else {"usdtbsc": "bsc", "usdttrc20": "trx"})
                self.assertEqual(result["status"], "manual_review")
                self.assertIsNone(result["paid_at"])
                self.assertIsNone(result["outcome_currency"])
                with self.assertRaisesRegex(ArticleError, "article_payment_required"):
                    self.store.publish(self.article["id"], 1, 8)

    def test_begin_invoice_rejects_nonstable_coins_before_any_provider_request(self):
        order = self.frozen_order()
        self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
        for currency in ("btc", "eth", "ton", "usdtbscfake"):
            with self.subTest(currency=currency), self.assertRaisesRegex(ArticleError, "invalid_payment_currency"):
                self.store.begin_invoice(order["id"], 7, "a" * 64, currency, "usdtbsc", checkout_config_hash="f" * 64)
        self.assertEqual(self.store.get_order(order["id"])["status"], "awaiting_payment")

    def test_invoice_creation_is_claimed_once_and_unknown_result_cannot_retry(self):
        order = self.frozen_order()
        self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
        self.store.begin_invoice(order["id"], 7, "a" * 64, "usdtbsc", "usdtbsc", checkout_config_hash="f" * 64)
        with self.assertRaisesRegex(ArticleError, "article_payment_creation_pending"):
            self.store.begin_invoice(order["id"], 7, "a" * 64, "usdtbsc", "usdtbsc", checkout_config_hash="f" * 64)
        with self.assertRaisesRegex(ArticleError, "article_order_not_cancellable"):
            self.store.cancel_order(order["id"], 7)

    def test_verified_paid_order_publishes_once_and_stays_frozen(self):
        order = self.invoice_order()
        payment = self.verified_payment(order)
        paid = self.store.apply_verified_payment(order["id"], "a" * 64, payment, "b" * 64)
        self.assertEqual(paid["status"], "paid")
        self.store.publish(self.article["id"], 1, 8)
        self.assertEqual(self.store.public_state(self.article["slug"])["status"], "published")
        self.store.apply_verified_payment(order["id"], "a" * 64, payment, "b" * 64)
        self.store.publish(self.article["id"], 1, 8)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM article_audit WHERE action='publish' AND article_id=?",
                                                (self.article["id"],)).fetchone()[0], 1)
        with self.assertRaisesRegex(ArticleError, "article_order_locked"):
            self.store.save(self.article["id"], 1, {**self.snapshot, "title": "Altered paid post"}, 7)

    def test_finished_underpaid_wrong_currency_and_redeposit_are_not_published(self):
        for change in ({"actually_paid": "98"}, {"pay_currency": "usdterc20"}, {"parent_payment_id": "900000"},
                       {"payment_extra_ids": [900003]}, {"price_amount": "98.99"}):
            with self.subTest(change=change):
                order = self.invoice_order()
                result = self.store.apply_verified_payment(order["id"], "a" * 64,
                                                           {**self.verified_payment(order), **change}, "c" * 64)
                self.assertEqual(result["status"], "manual_review")
                with self.assertRaisesRegex(ArticleError, "article_payment_required"):
                    self.store.publish(self.article["id"], 1, 8)
                self.store = ArticleStore(Path(self.temporary.name) / ("next-" + str(len(change)) + "-" + str(change.keys()) + ".db"))
                self.store.init()

    def test_payment_wrong_invoice_and_key_do_not_change_order(self):
        order = self.invoice_order()
        with self.assertRaisesRegex(ArticleError, "payment_invoice_mismatch"):
            self.store.apply_verified_payment(order["id"], "a" * 64,
                                               {**self.verified_payment(order), "invoice_id": "900009"}, "d" * 64)
        with self.assertRaisesRegex(ArticleError, "payment_merchant_mismatch"):
            self.store.apply_verified_payment(order["id"], "f" * 64, self.verified_payment(order), "d" * 64)
        self.assertEqual(self.store.get_order_for_author(order["id"], 7)["status"], "payment_pending")

    def test_terms_text_and_checkout_rules_cannot_change_after_confirmation(self):
        order = self.frozen_order()
        with self.assertRaisesRegex(ArticleError, "article_order_locked"):
            self.store.freeze_order(self.article["id"], 1, "standard", 9900, "offline-terms-v1", 8,
                                    terms_text="Different terms.", checkout_config_hash="f" * 64)
        self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
        with self.assertRaisesRegex(ArticleError, "payment_configuration_changed"):
            self.store.begin_invoice(order["id"], 7, "a" * 64, "usdtbsc", "usdtbsc", checkout_config_hash="e" * 64)
        self.assertEqual(self.store.get_order(order["id"])["status"], "awaiting_payment")

    def test_order_confirmation_detects_mutated_price_terms_and_payment_rules(self):
        order = self.frozen_order()
        for field, value in (("price_usd_cents", 1), ("terms_text", "Changed terms."), ("checkout_config_hash", "e" * 64)):
            with self.subTest(field=field):
                with sqlite3.connect(self.store.path) as connection:
                    connection.execute(f"UPDATE article_orders SET {field}=? WHERE id=?", (value, order["id"]))
                with self.assertRaisesRegex(ArticleError, "article_order_confirmation_conflict"):
                    self.store.confirm_order(order["id"], 7, order["confirmation_hash"])
                with self.assertRaisesRegex(ArticleError, "article_order_confirmation_conflict"):
                    self.store.get_order_snapshot(order["id"])
                with sqlite3.connect(self.store.path) as connection:
                    connection.execute(f"UPDATE article_orders SET {field}=? WHERE id=?", (order[field], order["id"]))

    def test_pending_payment_id_is_persisted_and_out_of_order_status_does_not_regress(self):
        order = self.invoice_order()
        payment = {**self.verified_payment(order), "payment_status": "confirmed"}
        confirmed = self.store.apply_verified_payment(order["id"], "a" * 64, payment, "1" * 64)
        self.assertEqual(confirmed["payment_id"], "900002")
        waiting = self.store.apply_verified_payment(order["id"], "a" * 64,
                                                   {**payment, "payment_status": "waiting"}, "2" * 64)
        self.assertEqual(waiting["provider_status"], "confirmed")

    def test_late_old_order_receipt_holds_new_checkout_without_publishing_old_version(self):
        old = self.invoice_order()
        payment = self.verified_payment(old)
        self.store.apply_verified_payment(old["id"], "a" * 64, {**payment, "payment_status": "expired"}, "3" * 64)
        self.store.submit(self.article["id"], 1, 7)
        new = self.store.freeze_order(self.article["id"], 1, "standard", 9900, "offline-terms-v1", 8,
                                      terms_text="Offline fixture terms only.", checkout_config_hash="f" * 64)["order"]
        result = self.store.apply_verified_payment(old["id"], "a" * 64, payment, "4" * 64)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["failure_code"], "payment_order_inactive")
        self.assertEqual(self.store.get_order(new["id"])["status"], "manual_review")
        self.assertEqual(self.store.get_order(new["id"])["failure_code"], "prior_payment_requires_review")
        self.assertEqual(self.store.list_orders(alerts_only=True)["total"], 2)

    def test_paid_order_cannot_be_reopened_for_charge_by_expired_status(self):
        order = self.invoice_order()
        payment = self.verified_payment(order)
        self.store.apply_verified_payment(order["id"], "a" * 64, payment, "5" * 64)
        result = self.store.apply_verified_payment(order["id"], "a" * 64,
                                                   {**payment, "payment_status": "expired"}, "6" * 64)
        self.assertEqual(result["status"], "manual_review")
        self.assertIsNotNone(result["paid_at"])
        with self.assertRaisesRegex(ArticleError, "article_order_locked"):
            self.store.submit(self.article["id"], 1, 7)
        state = self.store.freeze_order(self.article["id"], 1, "standard", 9900, "offline-terms-v1", 8,
                                        terms_text="Offline fixture terms only.", checkout_config_hash="f" * 64)
        self.assertEqual(state["order"]["id"], order["id"])
        self.assertEqual(self.store.list_orders()["total"], 1)

    def test_invoice_link_must_reference_the_bound_invoice(self):
        order = self.invoice_order()
        with self.assertRaisesRegex(ArticleError, "invalid_invoice_url"):
            self.store.attach_invoice(order["id"], "a" * 64, {"id": "900001", "order_id": order["id"],
                "price_amount": "99", "price_currency": "usd", "pay_currency": "usdtbsc",
                "invoice_url": "https://nowpayments.io/payment/?iid=900009"})

    def test_withdrawal_hides_all_public_content_and_does_not_reuse_slug(self):
        self.store.publish(self.article["id"], 1, editor_id=7)
        self.store.withdraw(self.article["id"], 1, editor_id=7)
        self.assertEqual(self.store.public_state(self.article["slug"]), {"status": "withdrawn"})
        self.assertEqual(self.store.list_published()["total"], 0)
        self.assertEqual(self.store.published_tags(), [])
        self.store.withdraw(self.article["id"], 1, editor_id=7)
        with self.assertRaisesRegex(ArticleError, "article_withdrawn"):
            self.store.publish(self.article["id"], 1, editor_id=7)
        with self.assertRaisesRegex(ArticleError, "slug_in_use"):
            self.store.create(self.article["slug"], self.snapshot, editor_id=7)

    def test_search_treats_wildcards_as_literals_and_filters_published_tags(self):
        self.store.publish(self.article["id"], 1, editor_id=7)
        self.assertEqual(self.store.list_published(query="MCP")["total"], 1)
        self.assertEqual(self.store.list_published(query="%_'")["total"], 0)
        self.assertEqual(self.store.list_published(tag="MCP")["total"], 1)
        self.assertEqual(self.store.list_published(tag="Private")["total"], 0)

    def test_reserved_paths_and_invalid_metadata_are_rejected(self):
        for slug in ("contribute", "pricing", "media", "../secret", "feed.xml", "Bad-Slug"):
            with self.subTest(slug=slug), self.assertRaisesRegex(ArticleError, "invalid_slug"):
                self.store.create(slug, self.snapshot, editor_id=7)
        for changes in ({"markdown": "x" * (MAX_MARKDOWN_BYTES + 1)},
                        {"canonical_url": "javascript:alert(1)"},
                        {"canonical_url": "https://user:password@example.test/"},
                        {"title": "Invalid\nheading"}, {"private_email": "private@example.test"}):
            with self.subTest(changes=list(changes)), self.assertRaises(ArticleError):
                self.store.save(self.article["id"], 1, {**self.snapshot, **changes}, editor_id=7)

    def test_hash_mismatch_cannot_be_published(self):
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE article_versions SET snapshot_json='{}' WHERE article_id=?",
                               (self.article["id"],))
        with self.assertRaisesRegex(ArticleError, "snapshot_corrupt"):
            self.store.publish(self.article["id"], 1, editor_id=7)
        self.assertEqual(self.store.list_published()["total"], 0)

    def test_database_is_private_and_independent(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o077, 0)
        with sqlite3.connect(self.store.path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"articles", "article_versions", "article_audit", "article_media", "article_version_media",
                      "article_submissions", "article_orders", "article_payment_events"})

    def test_saved_snapshot_contains_safe_html_and_contents(self):
        state = self.store.get_for_edit(self.article["id"])
        snapshot = state["latest"]["snapshot"]
        self.assertIn('<h2 id="section-1">Connection</h2>', snapshot["html"])
        self.assertEqual(snapshot["toc"], [{"id": "section-1", "title": "Connection", "level": 2}])

    def test_images_are_private_until_current_published_version_references_them(self):
        output = io.BytesIO()
        Image.new("RGB", (12, 8), "white").save(output, format="PNG")
        media = self.store.add_media(output.getvalue(), editor_id=7)
        with self.assertRaisesRegex(ArticleError, "media_not_found"):
            self.store.get_media(media["id"])
        image_snapshot = {**self.snapshot, "markdown": self.snapshot["markdown"]
                          + "\n![Screenshot](/articles/media/" + media["id"] + ")\n"}
        self.store.save(self.article["id"], 1, image_snapshot, editor_id=7)
        with self.assertRaisesRegex(ArticleError, "media_not_found"):
            self.store.get_media(media["id"])
        self.store.publish(self.article["id"], 2, editor_id=7)
        self.assertEqual(self.store.get_media(media["id"])["mime"], "image/png")
        self.store.save(self.article["id"], 2, self.snapshot, editor_id=7)
        self.assertEqual(self.store.get_media(media["id"])["width"], 12)
        self.store.publish(self.article["id"], 3, editor_id=7)
        with self.assertRaisesRegex(ArticleError, "media_not_found"):
            self.store.get_media(media["id"])
        self.assertEqual(self.store.get_media(media["id"], public=False)["height"], 8)

    def test_fake_uploads_and_missing_image_references_are_rejected(self):
        with self.assertRaisesRegex(ArticleError, "invalid_image"):
            self.store.add_media(b'<svg onload="alert(1)"></svg>', editor_id=7)
        missing = {**self.snapshot, "markdown": "![missing](/articles/media/" + "0" * 32 + ")"}
        with self.assertRaisesRegex(ArticleError, "media_not_found"):
            self.store.save(self.article["id"], 1, missing, editor_id=7)
        self.assertEqual(self.store.get_for_edit(self.article["id"])["latest"]["version"], 1)

    def test_consistent_backup_restores_published_draft_and_private_media(self):
        self.store.publish(self.article["id"], 1, editor_id=7)
        published = self.store.public_state(self.article["slug"])
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        media = self.store.add_media(image.getvalue(), editor_id=7)
        revised = {**self.snapshot, "title": "Private backup revision", "markdown": self.snapshot["markdown"]
                   + "\n![private](/articles/media/" + media["id"] + ")"}
        self.store.save(self.article["id"], 1, revised, editor_id=7)
        backup = self.store.backup(Path(self.temporary.name) / "backups" / "articles.db")
        self.assertEqual(backup.stat().st_mode & 0o077, 0)
        with self.assertRaisesRegex(ArticleError, "backup_exists"):
            self.store.backup(backup)
        self.store.withdraw(self.article["id"], 2, editor_id=7)
        restored = ArticleStore(backup)
        restored.init()
        self.assertEqual(restored.public_state(self.article["slug"]), published)
        self.assertEqual(restored.get_for_edit(self.article["id"])["latest"]["snapshot"]["title"], revised["title"])
        self.assertEqual(restored.get_media(media["id"], public=False)["width"], 8)
        with self.assertRaisesRegex(ArticleError, "media_not_found"):
            restored.get_media(media["id"])


class ArticleRenderingTests(unittest.TestCase):
    def test_sponsored_links_are_disclosed_in_public_and_private_rendering(self):
        rendered = render_markdown("[External](https://outside.example) [Internal](/mcp)", sponsored=True)["html"]
        self.assertIn('rel="noopener noreferrer sponsored"', rendered)
        self.assertIn('rel="noopener noreferrer sponsored"', private_html(rendered, sponsored=True))
        self.assertEqual(outbound_link_count(rendered, "https://agent-tools.cloud"), 1)

    def test_raw_html_and_dangerous_links_cannot_execute(self):
        rendered = render_markdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n'
                                   '[bad](javascript:alert(1))\n\n[safe](https://example.test)')
        parser = ArticleHtmlParser()
        parser.feed(rendered["html"])
        self.assertNotIn("<script>", rendered["html"])
        self.assertFalse(any(tag in ("script", "img", "iframe")
                     or any(attribute.startswith("on") for attribute in attributes)
                     for tag, attributes in parser.elements))
        self.assertNotIn('href="javascript:', rendered["html"])
        self.assertIn('rel="noopener noreferrer"', rendered["html"])

    def test_tables_and_stable_heading_anchors(self):
        rendered = render_markdown('# Title\n\n## `Code` heading\n\n| A | B |\n|---|---|\n| 1 | 2 |')
        self.assertIn("<table>", rendered["html"])
        self.assertEqual([item["id"] for item in rendered["toc"]], ["section-1", "section-2"])
        self.assertNotIn("<h1", rendered["html"])

    def test_images_cannot_fetch_external_or_data_urls(self):
        for source in ("https://example.test/image.png", "//example.test/image.png", "data:image/png;base64,AAAA"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "invalid_image_url"):
                render_markdown("![image](" + source + ")")

    def test_private_preview_uses_authenticated_image_routes(self):
        source = "/articles/media/" + "1" * 32
        public = render_markdown("![image](" + source + ")")["html"]
        private = private_html(public)
        self.assertIn('src="/admin' + source + '"', private)
        self.assertNotIn('src="' + source + '"', private)


class ArticleRouteTests(unittest.TestCase):
    def setUp(self):
        disabled_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, disabled_logging)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ArticleStore(Path(self.temporary.name) / "articles.db")
        self.store.init()
        self.environment = patch.dict(os.environ, {"ARTICLE_EDITOR_IDS": "7", "PUBLIC_BASE_URL": "http://testserver",
                                                   "NOWPAYMENTS_ARTICLES_CONFIG": str(Path(self.temporary.name) / "unconfigured-nowpayments.json"),
                                                   "SESSION_SECRET": "isolated-article-test-fixture"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

        def lookup(request):
            session = auth.unsign(request.cookies.get(auth.SESSION_COOKIE))
            user_id = session.get("uid") if session else None
            return {"id": user_id, "status": "active"} if user_id in (7, 8) else None

        self.lookup = patch.object(auth, "current_user", side_effect=lookup)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        app = FastAPI()
        app.include_router(create_article_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.snapshot = {"title": "已审核文章", "summary": "公开摘要", "author": "Agent Tools",
                         "markdown": "## 接入检查\n\n检查认证与连接方式。\n", "tags": ["MCP"]}
        self.identity_path, self.api_keys = self.api_accounts()

    def headers(self, user_id=7, origin="http://testserver"):
        return {"cookie": auth.SESSION_COOKIE + "=" + auth.sign({"uid": user_id}, auth.SESSION_MAX_AGE),
                "origin": origin}

    def create(self):
        response = self.client.post("/admin/articles/api/new", headers=self.headers(),
                                    json={"slug": "connection-checklist", "snapshot": self.snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_shared_article_navigation_is_english(self):
        response = self.client.get("/articles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count(">Blog</a>"), 2)
        self.assertIn("<h1>Blog</h1>", response.text)
        self.assertIn("Blog · Agent Tools", response.text)
        self.assertNotIn(">Articles</a>", response.text)
        self.assertNotIn(">文章</a>", response.text)
        feed = self.client.get("/articles/feed.xml")
        self.assertEqual(feed.status_code, 200)
        self.assertEqual(ElementTree.fromstring(feed.content).findtext("./channel/title"), "Agent Tools Blog")

    def enable_sponsored(self):
        self.enterContext(patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://testserver"}))
        self.payment_config = NowPaymentsConfig(api_key="offline-test-key", ipn_secret="offline-test-secret", enabled=True,
            approved=True, signature_verified=True, signature_format="python-sorted-v1",
            prices={"standard": 9900, "premium": 19900}, currencies={"usdtbsc": "bsc"},
            outcome_currency="usdtbsc", outcome_network="bsc", terms_version="offline-v1",
            terms_text="Offline fixture terms only.", fixed_rate=True, fee_paid_by_user=False)
        self.payment_requests = []
        self.provider_payment = {}
        def respond(request):
            self.payment_requests.append(request)
            if request.url.path.endswith("merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["usdtbsc"]})
            if request.url.path.endswith("full-currencies"):
                return httpx.Response(200, json={"currencies": [{"code": "usdtbsc", "network": "bsc",
                                                                "enable": True, "available_for_payment": True}]})
            if request.url.path.endswith("min-amount"):
                return httpx.Response(200, json={"min_amount": "10"})
            if request.url.path.endswith("estimate"):
                return httpx.Response(200, json={"currency_from": "usd", "currency_to": "usdtbsc", "estimated_amount": "99"})
            if request.method == "POST":
                payload = json.loads(request.content)
                return httpx.Response(201, json={"id": "900001", "order_id": payload["order_id"], "price_amount": "99.00",
                    "price_currency": "usd", "pay_currency": "usdtbsc", "invoice_url": "https://nowpayments.io/payment/?iid=900001"})
            return httpx.Response(200, json=self.provider_payment)
        app = FastAPI()
        app.include_router(create_article_router(self.store, payments=NowPaymentsClient(
            self.payment_config, transport=httpx.MockTransport(respond))))
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.sponsored_headers = {"authorization": "Bearer " + self.api_keys[8]}
        self.sponsored_editor_headers = self.headers(origin="https://testserver")

    def sponsored_submission(self, *, confirmed=False, checkout=False):
        self.enable_sponsored()
        snapshot = {"title": "Sponsored connection checklist", "summary": "A reviewed connection checklist.",
                    "author": "Fixture author", "markdown": "## Connection\n\n[Source](https://source.example)\n",
                    "publication_type": "sponsored", "sponsor_name": "Fixture sponsor", "sponsored_plan": "standard"}
        response = self.client.post("/api/v1/articles", headers=self.sponsored_headers,
                                    json={"slug": "sponsored-fixture", "snapshot": snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article_id = response.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/articles/{article_id}/submit", headers=self.sponsored_headers,
                                         json={"expected_version": 1}).status_code, 200)
        response = self.client.post(f"/admin/articles/api/{article_id}/approve-sponsored", headers=self.sponsored_editor_headers,
                                    json={"expected_version": 1})
        self.assertEqual(response.status_code, 200, response.text)
        order = response.json()["order"]
        if confirmed or checkout:
            response = self.client.post(f"/api/v1/article-payments/orders/{order['id']}/confirm", headers=self.sponsored_headers,
                json={"confirmation_hash": order["confirmation_hash"], "accept_terms": True})
            self.assertEqual(response.status_code, 200, response.text)
            order = response.json()["order"]
        if checkout:
            response = self.client.post(f"/api/v1/article-payments/orders/{order['id']}/checkout", headers=self.sponsored_headers,
                                        json={"pay_currency": "usdtbsc"})
            self.assertEqual(response.status_code, 200, response.text)
            order = response.json()["order"]
        self.provider_payment = {"payment_id": "900002", "invoice_id": "900001", "order_id": order["id"],
            "payment_status": "finished", "price_amount": "99", "price_currency": "usd", "pay_currency": "usdtbsc",
            "network": "bsc", "pay_amount": "99", "actually_paid": "99", "outcome_currency": "usdtbsc", "outcome_amount": "98"}
        return order

    def notify_payment(self, order):
        body = {"payment_id": "900002", "order_id": order["id"], "payment_status": "finished"}
        raw = json.dumps(body).encode()
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.payment_config.ipn_secret.encode(), canonical, hashlib.sha512).hexdigest()
        return self.client.post("/api/v1/article-payments/nowpayments/ipn", content=raw,
                                headers={"content-type": "application/json", "x-nowpayments-sig": signature})

    def test_sponsored_api_keeps_author_isolation_and_strict_bearer(self):
        order = self.sponsored_submission()
        path = "/api/v1/article-payments/orders/" + order["id"]
        self.assertEqual(self.client.get(path, headers=self.headers(8)).status_code, 401)
        self.assertEqual(self.client.get(path, headers={"authorization": "Bearer " + self.api_keys[7]}).status_code, 404)
        self.assertEqual(self.client.get(path, headers={"authorization": "Bearer " + self.api_keys[9]}).status_code, 403)
        response = self.client.get(path, headers=self.sponsored_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("merchant_key_ref", response.json()["order"])
        self.assertIn('rel="noopener noreferrer sponsored"', response.json()["html"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_sponsored_checkout_requires_explicit_confirmation_and_server_price(self):
        order = self.sponsored_submission()
        path = "/api/v1/article-payments/orders/" + order["id"]
        self.assertEqual(self.client.post(path + "/checkout", headers=self.sponsored_headers,
                                         json={"pay_currency": "usdtbsc"}).status_code, 409)
        self.assertEqual(len(self.payment_requests), 0)
        self.assertEqual(self.client.post(path + "/confirm", headers=self.sponsored_headers,
            json={"confirmation_hash": order["confirmation_hash"], "accept_terms": False}).status_code, 422)
        response = self.client.post(f"/admin/articles/api/{order['article_id']}/approve-sponsored", headers=self.sponsored_editor_headers,
                                    json={"expected_version": 1, "price_usd_cents": 1})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.post(f"/admin/articles/api/{order['article_id']}/publish", headers=self.sponsored_editor_headers,
                                         json={"expected_version": 1}).status_code, 409)

    def test_sponsored_checkout_reuses_one_invoice_and_ipn_publishes_once(self):
        order = self.sponsored_submission(checkout=True)
        response = self.client.post(f"/api/v1/article-payments/orders/{order['id']}/checkout", headers=self.sponsored_headers,
                                    json={"pay_currency": "usdtbsc"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sum(request.method == "POST" for request in self.payment_requests), 1)
        for attempt in range(2):
            response = self.notify_payment(order)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get_order(order["id"])["status"], "published")
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM article_audit WHERE action='publish'").fetchone()[0], 1)

    def test_sponsored_ipn_never_trusts_notified_finished_over_api_underpayment(self):
        order = self.sponsored_submission(checkout=True)
        self.provider_payment["actually_paid"] = "98"
        response = self.notify_payment(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get_order(order["id"])["status"], "manual_review")
        self.assertEqual(self.store.public_state("sponsored-fixture"), {"status": "missing"})

    def test_sponsored_same_notification_can_advance_after_provider_confirmation(self):
        order = self.sponsored_submission(checkout=True)
        self.provider_payment["payment_status"] = "waiting"
        self.assertEqual(self.notify_payment(order).status_code, 200)
        self.assertEqual(self.store.get_order(order["id"])["status"], "payment_pending")
        self.provider_payment["payment_status"] = "finished"
        self.assertEqual(self.notify_payment(order).status_code, 200)
        self.assertEqual(self.store.get_order(order["id"])["status"], "published")

    def test_sponsored_paid_order_waits_for_claim_and_refresh_retries_publication_not_charge(self):
        order = self.sponsored_submission(checkout=True)
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("DELETE FROM domain_ownership WHERE user_id=8")
        self.assertEqual(self.notify_payment(order).status_code, 200)
        self.assertEqual(self.store.get_order(order["id"])["status"], "paid")
        self.assertEqual(self.store.get_order(order["id"])["failure_code"], "publication_waiting")
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,status,created_at,verified_at) "
                               "VALUES(8,'api-author-8.example','descriptor',?,'verified',1,1)", ("a" * 64,))
        response = self.client.post(f"/api/v1/article-payments/orders/{order['id']}/refresh", headers=self.sponsored_headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["order"]["status"], "published")
        self.assertEqual(sum(request.method == "POST" for request in self.payment_requests), 1)

    def test_sponsored_invalid_ipn_signature_does_not_query_provider(self):
        self.enable_sponsored()
        response = self.client.post("/api/v1/article-payments/nowpayments/ipn", json={"payment_id": "900002"},
                                    headers={"x-nowpayments-sig": "a" * 128})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.payment_requests, [])

    def test_payment_openapi_and_alerts_do_not_open_article_email_notifications(self):
        self.enable_sponsored()
        self.assertEqual(self.client.get("/admin/article-notifications", headers=self.sponsored_editor_headers).status_code, 404)
        self.assertEqual(self.client.post("/admin/article-notifications/send", headers=self.sponsored_editor_headers, json={}).status_code, 404)
        self.assertEqual(self.client.get("/admin/article-orders/alerts", headers=self.sponsored_editor_headers).status_code, 200)
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/api/v1/article-payments/orders/{order_id}/confirm"]["post"]
        self.assertIn({"ArticleApiKey": []}, operation["security"])
        self.assertEqual(set(operation["requestBody"]["content"]["application/json"]["schema"]["required"]),
                         {"confirmation_hash", "accept_terms"})

    def test_disabled_payments_are_not_advertised_by_openapi(self):
        schema = self.client.get("/openapi.json").json()
        self.assertFalse(any(path.startswith("/api/v1/article-payments") for path in schema["paths"]))

    def test_sponsored_pages_are_english_private_and_show_frozen_terms(self):
        order = self.sponsored_submission()
        for path, headers in (("/articles/pricing", {}), ("/account/article-orders", self.sponsored_headers),
                              ("/account/article-orders/" + order["id"], self.sponsored_headers),
                              ("/account/articles/" + str(order["article_id"]), self.sponsored_headers),
                              ("/admin/article-orders", self.sponsored_editor_headers),
                              ("/admin/article-orders/alerts", self.sponsored_editor_headers),
                              ("/admin/article-orders/" + order["id"], self.sponsored_editor_headers)):
            with self.subTest(path=path):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('lang="en"', response.text)
                self.assertNotRegex(response.text, r"[\u3400-\u9fff]")
                self.assertNotIn(self.payment_config.api_key, response.text)
                self.assertNotIn(self.payment_config.ipn_secret, response.text)
                if path.startswith(("/account", "/admin")):
                    self.assertEqual(response.headers["cache-control"], "private, no-store")
        response = self.client.get("/account/article-orders/" + order["id"], headers=self.sponsored_headers)
        self.assertIn("Offline fixture terms only.", response.text)
        self.assertIn("Payment fees paid by: Agent Tools.", response.text)
        self.assertIn("Confirm publication order", response.text)

    def test_cancelled_order_keeps_original_snapshot_after_author_revision(self):
        order = self.sponsored_submission()
        self.assertEqual(self.client.post(f"/api/v1/article-payments/orders/{order['id']}/cancel",
                                         headers=self.sponsored_headers, json={}).status_code, 200)
        state = self.store.get_for_author(order["article_id"], 8)
        snapshot = {name: value for name, value in state["latest"]["snapshot"].items()
                    if name in ("title", "summary", "author", "markdown", "tags", "canonical_url", "cover_id",
                                "publication_type", "sponsor_name", "sponsored_plan")}
        self.store.save_for_author(order["article_id"], 1, {**snapshot, "title": "New unreleased revision"}, 8)
        response = self.client.get(f"/api/v1/article-payments/orders/{order['id']}", headers=self.sponsored_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["snapshot"]["title"], "Sponsored connection checklist")
        self.assertNotIn("New unreleased revision", response.text)

    def test_sponsored_public_html_markdown_feed_and_metadata_disclose_sponsorship(self):
        order = self.sponsored_submission(checkout=True)
        self.assertEqual(self.notify_payment(order).status_code, 200)
        response = self.client.get("/articles/sponsored-fixture")
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="article-status sponsored">Sponsored</span>', response.text)
        self.assertIn('rel="noopener noreferrer sponsored"', response.text)
        self.assertIn('"sponsor":', response.text)
        markdown = self.client.get("/articles/sponsored-fixture.md").text
        self.assertIn('publication_type: "sponsored"', markdown)
        self.assertIn('sponsor_name: "Fixture sponsor"', markdown)
        self.assertIn("Sponsored publication.", markdown)
        feed = ElementTree.fromstring(self.client.get("/articles/feed.xml").content)
        self.assertTrue(feed.findtext("./channel/item/description").startswith("Sponsored by Fixture sponsor."))
        self.assertIn('class="article-status sponsored">Sponsored</span>', self.client.get("/articles").text)

    def test_public_article_pages_are_english(self):
        self.snapshot = {"title": "MCP connection checklist", "summary": "Check a tool before connecting.",
                         "author": "AgentTools", "markdown": "## Connection\n\nCheck authentication.\n",
                         "tags": ["MCP"]}
        article = self.create()
        self.store.publish(article["id"], 1, editor_id=7)
        for path in ("/articles", "/articles/connection-checklist", "/articles?q=unmatched-query",
                     "/articles?tag=unmatched-topic"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('lang="en"', response.text)
                self.assertNotRegex(response.text, r"[\u3400-\u9fff]")

    def test_article_editing_and_api_labels_are_english(self):
        self.snapshot = {"title": "MCP connection checklist", "summary": "Check a tool before connecting.",
                         "author": "AgentTools", "markdown": "## Connection\n\nCheck authentication.\n",
                         "tags": ["MCP"]}
        article = self.create()
        self.store.submit(article["id"], 1, 7)
        paths = ("/account/articles", "/account/articles/new", "/account/articles/" + str(article["id"]),
                 "/admin/articles", "/admin/articles/new", "/admin/articles/" + str(article["id"]),
                 "/admin/articles?queue=submitted")
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 200)
                self.assertIn('lang="en"', response.text)
                self.assertNotRegex(response.text, r"[\u3400-\u9fff]")
        for path in ("/article-assets/editor.js", "/articles/feed.xml", "/openapi.json"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotRegex(response.text, r"[\u3400-\u9fff]")
        response = self.client.get("/api/v1/articles", headers={"Authorization": "Bearer " + self.api_keys[9]})
        self.assertEqual(response.status_code, 403)
        self.assertNotRegex(response.text, r"[\u3400-\u9fff]")
        response = self.client.get("/articles/missing-article")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Article not found")

    def api_accounts(self):
        if hasattr(self, "api_identity"):
            return self.api_identity
        identity_path = Path(self.temporary.name) / "directory.db"
        db.init_db(str(identity_path))
        keys = {}
        with sqlite3.connect(identity_path) as connection:
            connection.row_factory = sqlite3.Row
            for user_id in (7, 8, 9):
                connection.execute("INSERT INTO users(id,provider,provider_uid,login,status,created_at) "
                                   "VALUES(?,'github',?,?, 'active',1)",
                                   (user_id, "api-fixture-" + str(user_id), "api-author-" + str(user_id)))
                keys[user_id] = db.create_api_key(connection, user_id, "isolated-article-fixture")
            for user_id in (7, 8):
                host = "api-author-" + str(user_id) + ".example"
                connection.execute("INSERT INTO mcp_servers(name,slug,endpoint_url,source,created_at,updated_at) "
                                   "VALUES(?,?,?,'article-fixture',1,1)", ("API fixture", "api-fixture-" + str(user_id),
                                                                          "https://" + host + "/mcp"))
                connection.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,status,created_at,verified_at) "
                                   "VALUES(?,?,'descriptor',?,'verified',1,1)", (user_id, host, "a" * 64))

        def connect_fixture(*args, read_only=False, **kwargs):
            connection = sqlite3.connect(identity_path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                                         uri=True, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            return connection

        self.enterContext(patch.object(db, "connect", side_effect=connect_fixture))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", str(identity_path)))
        self.api_identity = identity_path, keys
        return self.api_identity

    def test_article_api_requires_bearer_identity_not_cookie(self):
        with patch.object(auth, "_uid_from_bearer", return_value=None):
            for headers in ({}, self.headers(8),
                            {**self.headers(7), "authorization": "Bearer invalid-article-test-key"}):
                with self.subTest(cookie="cookie" in headers):
                    response = self.client.post("/api/v1/articles", headers=headers,
                                                json={"slug": "api-draft", "snapshot": self.snapshot})
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers["cache-control"], "private, no-store")
                    self.assertEqual(self.client.get("/api/v1/articles", headers=headers).status_code, 401)
        self.assertEqual(self.store.list_for_edit()["total"], 0)

    def test_claimed_api_author_can_submit_but_not_publish(self):
        _, keys = self.api_accounts()
        headers = {"authorization": "Bearer " + keys[8]}
        other_headers = {"authorization": "Bearer " + keys[7]}
        with patch.dict(os.environ, {"ARTICLE_EDITOR_IDS": ""}):
            response = self.client.post("/api/v1/articles", headers={**self.headers(7), **headers},
                                        json={"slug": "api-author-draft", "snapshot": self.snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article = response.json()
        path = "/api/v1/articles/" + str(article["id"])
        self.assertEqual(article["review_status"], "draft")
        self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
        self.assertEqual(self.client.get(path, headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get("/account/articles/api/" + str(article["id"]),
                                         headers=self.headers(8)).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/articles", headers=headers).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/articles", headers=other_headers).json()["total"], 0)
        changed = {**self.snapshot, "title": "API 作者修订"}
        response = self.client.post(path + "/save", headers=headers,
                                    json={"expected_version": 1, "snapshot": changed})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["latest"]["version"], 2)
        admin_path = "/admin/articles/api/" + str(article["id"])
        self.assertEqual(self.client.post(admin_path + "/publish", headers=self.headers(),
                                         json={"expected_version": 2}).status_code, 409)
        self.assertEqual(self.client.post(path + "/submit", headers=headers,
                                         json={"expected_version": 1}).status_code, 409)
        response = self.client.post(path + "/submit", headers=headers, json={"expected_version": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "submitted")
        self.assertEqual(self.client.get("/articles/api-author-draft.md").status_code, 404)
        self.assertEqual(self.client.post(path + "/publish", headers=headers,
                                         json={"expected_version": 2}).status_code, 404)
        self.assertEqual(self.client.post(admin_path + "/publish", headers=headers,
                                         json={"expected_version": 2}).status_code, 403)
        with patch.dict(os.environ, {"ARTICLE_EDITOR_IDS": ""}):
            self.assertEqual(self.client.post(admin_path + "/publish", headers=other_headers,
                                             json={"expected_version": 2}).status_code, 403)
        response = self.client.post(path + "/cancel-submission", headers=headers, json={"expected_version": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "draft")
        self.assertEqual(self.client.post(path + "/submit", headers=headers,
                                         json={"expected_version": 2}).status_code, 200)
        response = self.client.post(admin_path + "/publish", headers=self.headers(), json={"expected_version": 2})
        self.assertEqual(response.status_code, 200, response.text)
        published = self.client.get("/articles/api-author-draft.md")
        self.assertEqual(published.status_code, 200)
        self.assertIn("API 作者修订", published.text)
        changed["title"] = "未审 API 新版本"
        self.assertEqual(self.client.post(path + "/save", headers=headers,
                                         json={"expected_version": 2, "snapshot": changed}).status_code, 200)
        self.assertEqual(self.client.get("/articles/api-author-draft.md").text, published.text)
        self.assertEqual(self.client.post(admin_path + "/publish", headers=self.headers(),
                                         json={"expected_version": 3}).status_code, 409)

    def test_article_api_checks_current_claim_key_and_account_status(self):
        identity_path, keys = self.api_accounts()
        headers = {"authorization": "Bearer " + keys[8]}
        payload = {"slug": "api-permission-draft", "snapshot": self.snapshot}
        self.assertEqual(self.client.post("/api/v1/articles", headers={**self.headers(),
                                         "authorization": "Bearer " + keys[9]}, json=payload).status_code, 403)
        for status in ("pending", "revoked"):
            with sqlite3.connect(identity_path) as connection:
                connection.execute("UPDATE domain_ownership SET status=? WHERE user_id=8", (status,))
            self.assertEqual(self.client.post("/api/v1/articles", headers=headers, json=payload).status_code, 403)
        with sqlite3.connect(identity_path) as connection:
            connection.execute("UPDATE domain_ownership SET status='verified' WHERE user_id=8")
            connection.execute("UPDATE users SET status='blocked' WHERE id=8")
        self.assertEqual(self.client.post("/api/v1/articles", headers=headers, json=payload).status_code, 401)
        with sqlite3.connect(identity_path) as connection:
            connection.execute("UPDATE users SET status='active' WHERE id=8")
            connection.execute("UPDATE api_keys SET revoked_at=1 WHERE user_id=8")
        self.assertEqual(self.client.post("/api/v1/articles", headers=headers, json=payload).status_code, 401)
        self.assertEqual(self.store.list_for_edit()["total"], 0)

    def test_api_private_media_preview_and_schema_share_author_permissions(self):
        _, keys = self.api_accounts()
        headers = {"authorization": "Bearer " + keys[8]}
        other_headers = {"authorization": "Bearer " + keys[7]}
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        response = self.client.post("/api/v1/articles/media", headers=headers, content=image.getvalue())
        self.assertEqual(response.status_code, 201, response.text)
        media_id = response.json()["id"]
        media_path = "/api/v1/articles/media/" + media_id
        self.assertEqual(self.client.get(media_path, headers=headers).status_code, 200)
        self.assertEqual(self.client.get(media_path, headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get("/articles/media/" + media_id).status_code, 404)
        snapshot = {**self.snapshot, "cover_id": media_id, "markdown": "![图片](/articles/media/" + media_id + ")"}
        response = self.client.post("/api/v1/articles/preview", headers=headers, json={"snapshot": snapshot})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(media_path, response.json()["html"])
        self.assertNotIn("/account/articles/media/", response.json()["html"])
        self.assertEqual(self.client.post("/api/v1/articles/preview", headers=other_headers,
                                         json={"snapshot": snapshot}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/articles", headers=headers,
                                         json={"slug": "invalid-public-state", "snapshot": self.snapshot,
                                               "status": "published", "author_id": 7}).status_code, 422)
        schema = self.client.app.openapi()
        paths = schema["paths"]
        self.assertEqual(paths["/api/v1/articles"]["post"]["security"], [{"ArticleApiKey": []}])
        self.assertEqual(paths["/api/v1/articles"]["post"]["requestBody"]["content"]["application/json"]
                         ["schema"]["required"], ["slug", "snapshot"])
        self.assertIn("/api/v1/articles/{article_id}/submit", paths)
        self.assertEqual(schema["components"]["securitySchemes"]["ArticleApiKey"]["scheme"], "bearer")

    def test_domain_key_can_use_web_editor_with_existing_account(self):
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("UPDATE users SET provider='domain',login='AgentTools',email='admin@agent-tools.cloud' "
                               "WHERE id IN (7,8)")
        headers = {"authorization": "Bearer " + self.api_keys[8]}
        editor_headers = {"authorization": "Bearer " + self.api_keys[7]}
        page = self.client.get("/account/articles/new", headers=headers)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('value="AgentTools"', page.text)
        self.assertEqual(self.client.get("/admin/articles/new", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/new", headers=editor_headers).status_code, 200)
        response = self.client.post("/account/articles/api/new", headers=headers,
                                    json={"slug": "domain-web-author", "snapshot": self.snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article_id = response.json()["id"]
        self.assertEqual(self.store.get_author_id(article_id), 8)
        self.assertEqual(self.client.post("/account/articles/api/" + str(article_id) + "/submit", headers=headers,
                                         json={"expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.post("/admin/articles/api/" + str(article_id) + "/publish", headers=editor_headers,
                                         json={"expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.get("/articles/domain-web-author.md").status_code, 200)

    def test_article_bearer_cannot_fall_back_to_other_cookie_identity(self):
        invalid = {**self.headers(), "authorization": "Bearer invalid-article-test-key"}
        self.assertEqual(self.client.get("/admin/articles/new", headers=invalid).status_code, 401)
        author_key = {**self.headers(), "authorization": "Bearer " + self.api_keys[8]}
        self.assertEqual(self.client.get("/admin/articles/new", headers=author_key).status_code, 403)
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("UPDATE api_keys SET revoked_at=1 WHERE user_id=7")
        revoked = {**self.headers(), "authorization": "Bearer " + self.api_keys[7]}
        self.assertEqual(self.client.get("/admin/articles/new", headers=revoked).status_code, 401)

    def test_claimed_web_author_needs_no_editor_membership(self):
        with patch.dict(os.environ, {"ARTICLE_EDITOR_IDS": ""}):
            response = self.client.post("/account/articles/api/new", headers=self.headers(8),
                                        json={"slug": "author-draft", "snapshot": self.snapshot})
            self.assertEqual(response.status_code, 201, response.text)
            article = response.json()
            path = "/account/articles/api/" + str(article["id"])
            self.assertEqual(self.client.get(path, headers=self.headers(8)).status_code, 200)
            self.assertEqual(self.client.get(path, headers=self.headers(7)).status_code, 404)
            changed = {**self.snapshot, "title": "作者修订"}
            response = self.client.post(path + "/save", headers=self.headers(8),
                                        json={"expected_version": 1, "snapshot": changed})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["latest"]["version"], 2)
            self.assertEqual(self.client.post(path + "/save", headers=self.headers(7),
                                             json={"expected_version": 2, "snapshot": self.snapshot}).status_code, 404)
            self.assertEqual(self.client.get("/admin/articles", headers=self.headers(8)).status_code, 403)
            self.assertEqual(self.client.post("/admin/articles/api/" + str(article["id"]) + "/publish",
                                             headers=self.headers(8), json={"expected_version": 2}).status_code, 403)
            self.assertEqual(self.client.post(path + "/publish", headers=self.headers(8),
                                             json={"expected_version": 2}).status_code, 404)
            self.assertEqual(self.client.get("/articles/author-draft.md").status_code, 404)

    def test_unclaimed_web_and_api_accounts_cannot_write(self):
        article = self.store.create_for_author("previous-private-draft", self.snapshot, 8)
        before = self.store.get_for_edit(article["id"])
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("DELETE FROM domain_ownership WHERE user_id=8")
        author_path = "/account/articles/api/" + str(article["id"])
        api_path = "/api/v1/articles/" + str(article["id"])
        api_headers = {"authorization": "Bearer " + self.api_keys[8]}
        requests = (
            ("/account/articles/api/new", self.headers(8), {"slug": "unclaimed-web", "snapshot": self.snapshot}),
            ("/api/v1/articles", api_headers, {"slug": "unclaimed-api", "snapshot": self.snapshot}),
            (author_path + "/save", self.headers(8), {"expected_version": 1, "snapshot": self.snapshot}),
            (api_path + "/save", api_headers, {"expected_version": 1, "snapshot": self.snapshot}),
            (author_path + "/submit", self.headers(8), {"expected_version": 1}),
            (api_path + "/submit", api_headers, {"expected_version": 1}),
            ("/account/articles/preview", self.headers(8), {"snapshot": self.snapshot}),
            ("/api/v1/articles/preview", api_headers, {"snapshot": self.snapshot}),
        )
        for path, headers, payload in requests:
            with self.subTest(path=path):
                response = self.client.post(path, headers=headers, json=payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.headers["cache-control"], "private, no-store")
        for path, headers in (("/account/articles/media", self.headers(8)), ("/api/v1/articles/media", api_headers)):
            self.assertEqual(self.client.post(path, headers=headers, content=b"unclaimed upload").status_code, 403)
        for path in ("/account/articles", "/account/articles/new", "/account/articles/" + str(article["id"])):
            self.assertEqual(self.client.get(path, headers=self.headers(8)).status_code, 403)
        self.assertEqual(self.store.get_for_edit(article["id"]), before)
        self.assertEqual(self.store.list_for_edit()["total"], 1)
        self.assertEqual(self.client.get("/articles/previous-private-draft.md").status_code, 404)

    def test_unclaimed_editor_cannot_bypass_author_qualification(self):
        article = self.create()
        before = self.store.get_for_edit(article["id"])
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("DELETE FROM domain_ownership WHERE user_id=7")
        path = "/admin/articles/api/" + str(article["id"])
        requests = (
            ("/admin/articles/api/new", {"slug": "unclaimed-editor", "snapshot": self.snapshot}),
            (path + "/save", {"expected_version": 1, "snapshot": {**self.snapshot, "title": "不得保存"}}),
            (path + "/publish", {"expected_version": 1}),
            ("/admin/articles/preview", {"snapshot": self.snapshot}),
        )
        for endpoint, payload in requests:
            with self.subTest(path=endpoint):
                self.assertEqual(self.client.post(endpoint, headers=self.headers(), json=payload).status_code, 403)
        self.assertEqual(self.client.post("/admin/articles/media", headers=self.headers(),
                                         content=b"unclaimed editor upload").status_code, 403)
        self.assertEqual(self.client.get("/admin/articles/new", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get("/admin/articles", headers=self.headers()).status_code, 200)
        self.assertEqual(self.store.get_for_edit(article["id"]), before)
        self.assertEqual(self.store.list_published()["total"], 0)

    def test_publish_checks_original_author_claim_after_editor_revision(self):
        article = self.store.create_for_author("legacy-unclaimed-draft", self.snapshot, 9)
        changed = {**self.snapshot, "title": "编辑修订不转移作者资格"}
        self.store.save(article["id"], 1, changed, editor_id=7)
        self.store.submit(article["id"], 2, author_id=9)
        self.assertEqual(self.store.get_author_id(article["id"]), 9)
        path = "/admin/articles/api/" + str(article["id"]) + "/publish"
        response = self.client.post(path, headers=self.headers(), json={"expected_version": 2})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list_published()["total"], 0)
        self.assertEqual(self.client.get("/articles/legacy-unclaimed-draft.md").status_code, 404)
        article = self.store.create_for_author("revoked-author-draft", self.snapshot, 8)
        self.store.submit(article["id"], 1, author_id=8)
        with sqlite3.connect(self.identity_path) as connection:
            connection.execute("UPDATE domain_ownership SET status='revoked' WHERE user_id=8")
        path = "/admin/articles/api/" + str(article["id"]) + "/publish"
        self.assertEqual(self.client.post(path, headers=self.headers(), json={"expected_version": 1}).status_code, 403)
        self.assertEqual(self.store.list_published()["total"], 0)

    def test_author_submission_is_private_until_review(self):
        response = self.client.post("/account/articles/api/new", headers=self.headers(8),
                                    json={"slug": "reviewed-author-draft", "snapshot": self.snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article = response.json()
        author_path = "/account/articles/api/" + str(article["id"])
        admin_path = "/admin/articles/api/" + str(article["id"])
        self.assertEqual(self.client.post(admin_path + "/publish", headers=self.headers(),
                                         json={"expected_version": 1}).status_code, 409)
        response = self.client.post(author_path + "/submit", headers=self.headers(8), json={"expected_version": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "submitted")
        self.assertEqual(self.store.list_for_review()["total"], 1)
        self.assertEqual(self.client.get("/articles/reviewed-author-draft.md").status_code, 404)
        payload = {"expected_version": 1, "action": "changes_requested", "feedback": "请补充资料来源"}
        self.assertEqual(self.client.post(admin_path + "/review", headers=self.headers(8), json=payload).status_code, 403)
        response = self.client.post(admin_path + "/review", headers=self.headers(), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(author_path, headers=self.headers(8)).json()["submission"]["feedback"],
                         "请补充资料来源")
        changed = {**self.snapshot, "markdown": "## 正文\n\n已补充来源。"}
        self.assertEqual(self.client.post(author_path + "/save", headers=self.headers(8),
                                         json={"expected_version": 1, "snapshot": changed}).status_code, 200)
        self.assertEqual(self.client.post(author_path + "/submit", headers=self.headers(8),
                                         json={"expected_version": 2}).status_code, 200)
        response = self.client.post(admin_path + "/publish", headers=self.headers(), json={"expected_version": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "approved")
        published = self.client.get("/articles/reviewed-author-draft.md")
        self.assertEqual(published.status_code, 200)
        self.assertIn("已补充来源", published.text)
        self.assertEqual(self.store.list_for_review()["total"], 0)
        changed["markdown"] = "私有的下一版"
        self.assertEqual(self.client.post(author_path + "/save", headers=self.headers(8),
                                         json={"expected_version": 2, "snapshot": changed}).status_code, 200)
        self.assertEqual(self.client.get("/articles/reviewed-author-draft.md").text, published.text)
        self.assertNotIn("私有的下一版", self.client.get("/articles/feed.xml").text)

    def test_author_changes_or_cancellation_invalidate_pending_review(self):
        response = self.client.post("/account/articles/api/new", headers=self.headers(8),
                                    json={"slug": "pending-author-draft", "snapshot": self.snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article = response.json()
        author_path = "/account/articles/api/" + str(article["id"])
        admin_path = "/admin/articles/api/" + str(article["id"])
        self.assertEqual(self.client.post(author_path + "/submit", headers=self.headers(7),
                                         json={"expected_version": 1}).status_code, 404)
        for _ in range(2):
            self.assertEqual(self.client.post(author_path + "/submit", headers=self.headers(8),
                                             json={"expected_version": 1}).status_code, 200)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM article_audit WHERE action='submit'").fetchone()[0], 1)
        self.assertEqual(self.client.post(author_path + "/save", headers=self.headers(8),
                                         json={"expected_version": 1, "snapshot": {**self.snapshot, "title": "新稿"}}).status_code, 200)
        self.assertEqual(self.store.list_for_review()["total"], 0)
        self.assertEqual(self.client.post(admin_path + "/publish", headers=self.headers(),
                                         json={"expected_version": 2}).status_code, 409)
        self.assertEqual(self.client.post(admin_path + "/review", headers=self.headers(),
                                         json={"expected_version": 1, "action": "rejected", "feedback": "旧版意见"}).status_code, 409)
        self.assertEqual(self.client.post(author_path + "/submit", headers=self.headers(8),
                                         json={"expected_version": 2}).status_code, 200)
        self.assertEqual(self.client.post(author_path + "/cancel-submission", headers=self.headers(8),
                                         json={"expected_version": 2}).status_code, 200)
        self.assertEqual(self.client.post(admin_path + "/publish", headers=self.headers(),
                                         json={"expected_version": 2}).status_code, 409)
        self.assertEqual(self.client.get("/articles/pending-author-draft.md").status_code, 404)

    def test_author_preview_and_media_are_private_to_the_owner(self):
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        response = self.client.post("/account/articles/media", headers=self.headers(8), content=image.getvalue())
        self.assertEqual(response.status_code, 201, response.text)
        media_id = response.json()["id"]
        private_path = "/account/articles/media/" + media_id
        self.assertEqual(self.client.get(private_path).status_code, 401)
        self.assertEqual(self.client.get(private_path, headers=self.headers(8)).status_code, 200)
        self.assertEqual(self.client.get(private_path, headers=self.headers(7)).status_code, 404)
        self.assertEqual(self.client.get("/articles/media/" + media_id).status_code, 404)
        snapshot = {**self.snapshot, "markdown": "![作者附件](/articles/media/" + media_id + ")", "cover_id": media_id}
        response = self.client.post("/account/articles/preview", headers=self.headers(8), json={"snapshot": snapshot})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('src="' + private_path + '"', response.json()["html"])
        self.assertNotIn("/admin/articles/media", response.json()["html"])
        self.assertEqual(self.client.post("/account/articles/preview", headers=self.headers(7),
                                         json={"snapshot": snapshot}).status_code, 404)
        foreign_media = self.store.add_media(image.getvalue(), editor_id=7)
        foreign = {**self.snapshot, "markdown": "![他人附件](/articles/media/" + foreign_media["id"] + ")"}
        self.assertEqual(self.client.post("/account/articles/api/new", headers=self.headers(8),
                                         json={"slug": "foreign-media", "snapshot": foreign}).status_code, 404)
        self.assertEqual(self.store.list_for_edit()["total"], 0)
        response = self.client.post("/account/articles/api/new", headers=self.headers(8),
                                    json={"slug": "author-media", "snapshot": snapshot})
        self.assertEqual(response.status_code, 201, response.text)
        article = response.json()
        self.assertEqual(self.client.post("/account/articles/api/" + str(article["id"]) + "/submit",
                                         headers=self.headers(8), json={"expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.post("/admin/articles/api/" + str(article["id"]) + "/publish",
                                         headers=self.headers(), json={"expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.get("/articles/media/" + media_id).status_code, 200)

    def test_only_signed_active_editors_can_write_and_preview(self):
        payload = {"slug": "connection-checklist", "snapshot": self.snapshot}
        for headers, status in (({}, 401), (self.headers(8), 403), (self.headers(9), 401),
                                ({"cookie": "atc_session=forged"}, 401),
                                (self.headers(origin="https://untrusted.test"), 403)):
            with self.subTest(status=status):
                response = self.client.post("/admin/articles/api/new", headers=headers, json=payload)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.store.list_for_edit()["total"], 0)
        article = self.create()
        for headers, status in (({}, 401), (self.headers(8), 403), (self.headers(), 200)):
            response = self.client.get("/admin/articles/api/" + str(article["id"]), headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_empty_or_invalid_editor_configuration_fails_closed(self):
        for value in ("", "7,invalid", "0", "-1", "²", "7,²"):
            with self.subTest(value=value), patch.dict(os.environ, {"ARTICLE_EDITOR_IDS": value}):
                self.assertEqual(self.client.get("/admin/articles/api", headers=self.headers()).status_code, 403)

    def test_oauth_state_or_mismatched_identity_cannot_replace_editor_session(self):
        for session in ({"state": "oauth-fixture", "next": "/"}, {"uid": 0}, {"uid": "7"},
                        {"uid": 8}, {"uid": 7, "aud": "hub.agent-tools.cloud"}):
            cookie = auth.sign(session, auth.SESSION_MAX_AGE)
            with self.subTest(fields=list(session)), patch.object(auth, "current_user", return_value={"id": 7, "status": "active"}):
                response = self.client.get("/admin/articles/api", headers={"cookie": auth.SESSION_COOKIE + "=" + cookie})
                self.assertEqual(response.status_code, 401)

    def test_article_feature_is_disabled_without_opt_in(self):
        environment = {**os.environ, "PYTHON_DOTENV_DISABLED": "1", "AGENT_TOOLS_DB_PATH": str(Path(self.temporary.name) / "off-directory.db"),
                       "ARTICLE_DB_PATH": str(Path(self.temporary.name) / "off-articles.db")}
        environment.pop("ARTICLES_ENABLED", None)
        program = ("import json,os; from pathlib import Path; import server; from fastapi.testclient import TestClient; "
                   "client=TestClient(server.app); "
                   "print(json.dumps({'enabled':server.ARTICLES_ENABLED,'store':server.ARTICLE_STORE is None,"
                   "'articles':client.get('/articles').status_code,'admin':client.get('/admin/articles').status_code,"
                   "'database':Path(os.environ['ARTICLE_DB_PATH']).exists()}))")
        result = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]),
                         {"enabled": False, "store": True, "articles": 404, "admin": 404, "database": False})

    def test_published_exports_do_not_leak_saved_revision(self):
        article = self.create()
        path = "/admin/articles/api/" + str(article["id"])
        self.assertEqual(self.client.get("/articles/connection-checklist.md").status_code, 404)
        self.assertNotIn("公开摘要", self.client.get("/articles/feed.xml").text)
        response = self.client.post(path + "/publish", headers=self.headers(), json={"expected_version": 1})
        self.assertEqual(response.status_code, 200, response.text)
        markdown = self.client.get("/articles/connection-checklist.md")
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("检查认证", markdown.text)
        self.assertNotIn("editor_id", markdown.text)
        changed = {**self.snapshot, "title": "未发布秘密", "markdown": "不应公开的下一版"}
        response = self.client.post(path + "/save", headers=self.headers(),
                                    json={"expected_version": 1, "snapshot": changed})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/articles/connection-checklist.md").text, markdown.text)
        self.assertNotIn("未发布秘密", self.client.get("/articles/feed.xml").text)
        self.assertEqual(self.client.post(path + "/publish", headers=self.headers(),
                                         json={"expected_version": 1}).status_code, 409)
        self.assertEqual(self.client.post(path + "/withdraw", headers=self.headers(),
                                         json={"expected_version": 2}).status_code, 200)
        self.assertEqual(self.client.get("/articles/connection-checklist.md").status_code, 410)
        self.assertNotIn("公开摘要", self.client.get("/articles/feed.xml").text)

    def test_payments_and_unopened_orders_are_closed_for_all_methods(self):
        paths = ("/articles/pricing",
                 "/account/article-orders/1", "/admin/article-orders", "/admin/article-notifications",
                 "/api/v1/article-payments/webhook", "/articles/connection-checklist/upload")
        for path in paths:
            for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                if method in ("GET", "HEAD") and path.endswith("/upload"):
                    continue
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path, headers=self.headers())
                    self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.list_for_edit()["total"], 0)

    def test_logged_in_author_work_area_is_separate_from_review_management(self):
        anonymous = self.client.get("/account/articles/new", follow_redirects=False)
        self.assertEqual(anonymous.status_code, 302)
        self.assertTrue(anonymous.headers["location"].startswith("/auth/github/login?next="))
        response = self.client.get("/account/articles/new", headers=self.headers(8))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-author-mode="true"', response.text)
        self.assertIn('data-base-url="/account/articles"', response.text)
        self.assertIn('id="article-submit"', response.text)
        self.assertNotIn('id="article-publish"', response.text)
        self.assertNotIn('id="article-withdraw"', response.text)
        created = self.client.post("/account/articles/api/new", headers=self.headers(8),
                                   json={"slug": "work-area-author", "snapshot": self.snapshot})
        self.assertEqual(created.status_code, 201)
        article = created.json()
        other = self.store.create("other-private-draft", {**self.snapshot, "title": "别人的私稿"}, editor_id=7)
        self.assertEqual(self.client.get("/account/articles/" + str(other["id"]), headers=self.headers(8)).status_code, 404)
        listing = self.client.get("/account/articles", headers=self.headers(8))
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertNotIn("别人的私稿", listing.text)
        self.assertIn("/account/articles/" + str(article["id"]), listing.text)
        author_api = self.client.get("/account/articles/api", headers=self.headers(8))
        self.assertEqual(author_api.status_code, 200, author_api.text)
        self.assertEqual(author_api.json()["total"], 1)
        self.assertEqual(self.client.post("/account/articles/api/" + str(article["id"]) + "/submit",
                                         headers=self.headers(8), json={"expected_version": 1}).status_code, 200)
        queue = self.client.get("/admin/articles?queue=submitted", headers=self.headers())
        self.assertEqual(queue.status_code, 200)
        self.assertIn("work-area-author", queue.text)
        self.assertNotIn("别人的私稿", queue.text)
        self.assertEqual(self.client.get("/admin/articles?queue=submitted", headers=self.headers(8)).status_code, 403)
        self.assertEqual(self.client.get("/articles/contribute", follow_redirects=False).status_code, 303)
        self.assertIn("/articles/contribute", self.client.get("/articles").text)

    def test_media_remains_private_until_published_and_after_withdrawal(self):
        content = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(content, format="PNG")
        self.assertEqual(self.client.post("/admin/articles/media", content=content.getvalue()).status_code, 401)
        response = self.client.post("/admin/articles/media", headers=self.headers(), content=content.getvalue())
        self.assertEqual(response.status_code, 201, response.text)
        media_id = response.json()["id"]
        self.assertEqual(self.client.get("/articles/media/" + media_id).status_code, 404)
        self.assertEqual(self.client.get("/admin/articles/media/" + media_id).status_code, 401)
        self.assertEqual(self.client.get("/admin/articles/media/" + media_id, headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.post("/admin/articles/media", headers=self.headers(),
                                         content=b"not an image").status_code, 422)

    def test_reading_management_and_live_preview_use_safe_private_versions(self):
        article = self.create()
        editor = self.client.get("/admin/articles/" + str(article["id"]), headers=self.headers())
        self.assertEqual(editor.status_code, 200, editor.text)
        self.assertIn('name="markdown"', editor.text)
        self.assertIn("noindex,nofollow", editor.text)
        self.assertEqual(editor.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get("/admin/articles/new").status_code, 401)
        self.assertEqual(self.client.get("/admin/articles", headers=self.headers(8)).status_code, 403)
        self.assertEqual(self.client.get("/admin/articles", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/articles/connection-checklist").status_code, 404)
        preview = self.client.post("/admin/articles/preview", headers=self.headers(), json={"snapshot": self.snapshot})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("检查认证", preview.json()["html"])
        self.assertEqual(self.store.get_for_edit(article["id"])["latest"]["version"], 1)
        self.assertEqual(self.store.list_published()["total"], 0)
        self.store.publish(article["id"], 1, editor_id=7)
        reading = self.client.get("/articles/connection-checklist")
        self.assertEqual(reading.status_code, 200, reading.text)
        self.assertIn("检查认证", reading.text)
        self.assertIn('property="og:type" content="article"', reading.text)
        self.assertNotIn("/articles/contribute", reading.text)
        self.assertNotIn("/articles/pricing", reading.text)
        self.assertEqual(self.client.get("/articles?tag=MCP").status_code, 200)
        self.assertEqual(self.client.get("/articles?page=2").status_code, 404)
        self.assertEqual(self.client.get("/articles?q=private").status_code, 200)
        self.store.save(article["id"], 1, {**self.snapshot, "title": "私人下一稿"}, editor_id=7)
        self.assertNotIn("私人下一稿", self.client.get("/articles").text)
        self.store.withdraw(article["id"], 2, editor_id=7)
        self.assertEqual(self.client.get("/articles/connection-checklist").status_code, 410)

    def test_metadata_and_static_assets_do_not_leak_or_execute(self):
        snapshot = {**self.snapshot, "title": '</script><script>alert("bad")</script>',
                    "canonical_url": "https://source.example.test/original"}
        article = self.store.create("safe-metadata", snapshot, editor_id=7)
        self.store.publish(article["id"], 1, editor_id=7)
        response = self.client.get("/articles/safe-metadata")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('<script>alert("bad")</script>', response.text)
        self.assertIn('rel="canonical" href="https://source.example.test/original"', response.text)
        for asset in ("articles.css", "editor.js", "sprite.svg"):
            self.assertEqual(self.client.get("/article-assets/" + asset).status_code, 200)
        self.assertEqual(self.client.get("/article-assets/LICENSE").status_code, 404)
        self.assertEqual(self.client.get("/article-assets/../articles.py").status_code, 404)

    def test_main_site_home_sitemap_and_private_cache_are_integrated(self):
        from directory import db, routes
        original_connect = db.connect
        directory_path = str(Path(self.temporary.name) / "directory.db")

        def local_connect(*arguments, **keywords):
            keywords.pop("db_path", None)
            return original_connect(directory_path, **keywords)

        article = self.create()
        self.store.publish(article["id"], 1, editor_id=7)
        environment = {"ARTICLES_ENABLED": "1", "ARTICLE_DB_PATH": str(self.store.path), "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, environment), patch.object(db, "connect", side_effect=local_connect):
            db.init_db(directory_path)
            server = importlib.import_module("server")
            with patch.object(routes, "_home_cache", {"at": 0.0, "payload": None}):
                with TestClient(server.app) as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)
                    homepage = client.get("/")
                    self.assertEqual(homepage.status_code, 200, homepage.text)
                    self.assertIn("已审核文章", homepage.text)
                    sitemap = client.get("/sitemap.xml")
                    self.assertEqual(sitemap.status_code, 200, sitemap.text)
                    self.assertIn("/articles/connection-checklist", sitemap.text)
                    self.assertIn("no-cache", sitemap.headers["cache-control"])
                    self.assertNotIn("/articles/contribute", sitemap.text)
                    denied = client.get("/admin/articles/api", headers=self.headers(8))
                    self.assertEqual(denied.status_code, 403)
                    self.assertEqual(denied.headers["cache-control"], "private, no-store")
                    self.store.save(article["id"], 1, {**self.snapshot, "title": "不应出现在首页"}, editor_id=7)
                    self.assertNotIn("不应出现在首页", client.get("/").text)
                    self.store.withdraw(article["id"], 2, editor_id=7)
                    self.assertNotIn("/articles/connection-checklist", client.get("/sitemap.xml").text)


if __name__ == "__main__":
    unittest.main()