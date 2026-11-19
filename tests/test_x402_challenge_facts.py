"""What a 402 challenge states about itself -- its x402 version and the resource
description -- is recorded by the daily health probe and read by the readiness
score. Offline: mocked HTTP, temporary database."""
import base64
import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import crawlers, db, jobs, listing_readiness as L  # noqa: E402
from test_listing_readiness import NOW, by_key, x402_row  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402

URL = "https://pay.example.test/api/read"
ACCEPT = {"scheme": "exact", "network": "eip155:8453", "amount": "2000",
          "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
          "payTo": "0x" + "ab" * 20, "maxTimeoutSeconds": 300}
V2 = {"x402Version": 2, "error": "Payment required",
      "resource": {"url": URL, "description": "Read one page as Markdown"},
      "accepts": [ACCEPT]}
V1 = {"x402Version": 1,
      "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "2000",
                   "resource": URL, "description": "Weather by city",
                   "payTo": "0x" + "ab" * 20, "asset": ACCEPT["asset"]}]}


def answer(status=402, header=None, body=None, method="GET"):
    headers = {}
    if header is not None:
        headers["PAYMENT-REQUIRED"] = base64.b64encode(json.dumps(header).encode()).decode()
    content = json.dumps(body).encode() if body is not None else b""
    return httpx.Response(status, headers=headers, content=content,
                          request=httpx.Request(method, URL))


class ChallengeFacts(unittest.TestCase):
    def test_v2_header_and_body(self):
        self.assertEqual(crawlers.challenge_facts(answer(header=V2, body=V2)),
                         {"x402_version": 2, "description": "Read one page as Markdown"})

    def test_v2_header_alone_as_in_a_head_answer(self):
        self.assertEqual(crawlers.challenge_facts(answer(header=V2, method="HEAD")),
                         {"x402_version": 2, "description": "Read one page as Markdown"})

    def test_v1_body_describes_the_resource_in_accepts(self):
        self.assertEqual(crawlers.challenge_facts(answer(body=V1)),
                         {"x402_version": 1, "description": "Weather by city"})

    def test_nothing_stated_gives_nothing(self):
        self.assertEqual(crawlers.challenge_facts(answer(body={"error": "pay first"})), {})
        broken = httpx.Response(402, headers={"PAYMENT-REQUIRED": "not base64!"},
                                content=b"<html>pay</html>", request=httpx.Request("GET", URL))
        self.assertEqual(crawlers.challenge_facts(broken), {})


class HealthProbe(unittest.TestCase):
    def setUp(self):
        crawlers._WK_CACHE.clear()

    def probe(self, reply, **kw):
        transport = httpx.MockTransport(lambda request: reply)
        with patch.object(crawlers.public_http, "client",
                          side_effect=lambda **k: httpx.Client(transport=transport, **k)):
            return crawlers.probe_health(URL, **kw)

    def test_a_402_verdict_carries_what_the_challenge_states(self):
        res = self.probe(answer(header=V2, body=V2), method_hint="GET", inspect_payment=False)
        self.assertEqual((res["status"], res["http_status"], res["x402"]), ("ok", 402, True))
        self.assertEqual((res["x402_version"], res["description"]),
                         (2, "Read one page as Markdown"))
        res = self.probe(answer(header=V2), inspect_payment=False)   # HEAD-first path
        self.assertEqual((res["http_status"], res["x402_version"]), (402, 2))

    def test_other_answers_carry_nothing(self):
        res = self.probe(httpx.Response(200, json={"ok": True}), method_hint="GET",
                         inspect_payment=False)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("x402_version", res)
        self.assertNotIn("description", res)


class HealthJob(_TempDB):
    def run_health(self, result):
        with patch.object(jobs.crawlers, "probe_health", return_value=result), \
                patch.object(jobs, "cmd_ownership_recheck"), patch.object(jobs, "cmd_rescore"), \
                patch.object(jobs, "_maintain_down_since"):
            self.assertEqual(jobs.cmd_health(only_unknown=True), 0)
        return self.q("SELECT health, x402_ok, x402_version, challenge_description "
                      "FROM services WHERE slug='pay'")[0]

    def test_the_health_job_keeps_what_was_stated_last(self):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "pay", "name": "Pay", "url": URL,
                                  "source": "test", "source_id": "pay", "health": "unknown"})
        ok = {"status": "ok", "latency_ms": 1, "http_status": 402, "x402": True}
        row = self.run_health(dict(ok, x402_version=2, description="Read one page as Markdown"))
        self.assertEqual((row["x402_ok"], row["x402_version"], row["challenge_description"]),
                         (1, 2, "Read one page as Markdown"))
        with db.writer() as c:
            c.execute("UPDATE services SET health='unknown' WHERE slug='pay'")
        row = self.run_health(ok)   # this time the challenge stated nothing
        self.assertEqual((row["x402_version"], row["challenge_description"]),
                         (2, "Read one page as Markdown"))


class Readiness(unittest.TestCase):
    def row(self, **extra):
        samples = [{"url": URL, "method": "GET", "accepts": [ACCEPT]}] + [
            {"url": f"https://pay.example.test/r{i}", "kind": "x402-resource", "description": "d"}
            for i in range(2)]
        return x402_row(url=URL, resource_samples=json.dumps(samples), resource_count=225, **extra)

    def test_version_comes_from_our_own_probe_when_no_sample_records_one(self):
        k = by_key(L.assess_x402(self.row(), now=NOW))
        self.assertEqual(k["protocol_version"]["status"], "n/a")
        k = by_key(L.assess_x402(self.row(x402_version=2), now=NOW))
        self.assertEqual((k["protocol_version"]["points"], k["protocol_version"]["detail"]),
                         (10, "Speaks x402 v2."))
        k = by_key(L.assess_x402(self.row(x402_version=1), now=NOW))
        self.assertEqual((k["protocol_version"]["points"], k["protocol_version"]["status"]),
                         (5, "partial"))

    def test_the_listings_own_resource_is_described_by_its_challenge(self):
        k = by_key(L.assess_x402(self.row(), now=NOW))
        self.assertEqual((k["skills"]["points"], k["skills"]["detail"]),
                         (8, "225 paid resources; 1 sampled lack a description."))
        k = by_key(L.assess_x402(self.row(challenge_description="Read one page as Markdown"),
                                 now=NOW))
        self.assertEqual((k["skills"]["points"], k["skills"]["detail"]),
                         (10, "Declares 225 paid resource(s)."))

    def test_a_challenge_description_does_not_cover_other_resources(self):
        samples = json.dumps([{"url": "https://pay.example.test/other", "kind": "x402-resource"}] * 3)
        k = by_key(L.assess_x402(x402_row(url=URL, resource_samples=samples, resource_count=3,
                                          challenge_description="Read one page as Markdown"),
                                 now=NOW))
        self.assertEqual(k["skills"]["points"], 8)


if __name__ == "__main__":
    unittest.main()
