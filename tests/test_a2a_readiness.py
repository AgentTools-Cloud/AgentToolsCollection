"""Nine-criterion A2A readiness score."""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a, a2a_readiness as R  # noqa: E402

NOW = 1_800_000_000


def v1_card(**extra):
    card = {
        "name": "Demo", "description": "Demo agent", "version": "1.2.0",
        "supportedInterfaces": [{"url": "https://demo.example/a2a", "protocolBinding": "JSONRPC",
                                 "protocolVersion": "1.0"}],
        "provider": {"organization": "Demo Ltd", "url": "https://demo.example"},
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": [{"id": f"s{i}", "name": f"S{i}", "description": "d", "tags": ["t"]} for i in range(3)],
    }
    card.update(extra)
    return card


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class CardCriteria(unittest.TestCase):
    def test_valid_card(self):
        self.assertEqual(R.score_valid_card(v1_card())["points"], 10)
        partial = R.score_valid_card({"name": "x", "skills": [{"id": "a"}]})
        self.assertEqual(partial["points"], 7)
        self.assertEqual(R.score_valid_card(None)["points"], 0)

    def test_protocol_version(self):
        self.assertEqual(R.score_protocol_version(v1_card())["points"], 10)
        patch = v1_card(supportedInterfaces=[{"url": "u", "protocolVersion": "1.0.0"}])
        self.assertEqual(R.score_protocol_version(patch)["points"], 9)
        self.assertEqual(R.score_protocol_version({"protocolVersion": "1.0"})["points"], 7)
        self.assertEqual(R.score_protocol_version({"protocolVersion": "0.3.0"})["points"], 5)
        self.assertEqual(R.score_protocol_version({"version": "2.0"})["points"], 0)
        dual = v1_card(protocolVersion="0.3.0", supportedInterfaces=[
            {"url": "u", "protocolVersion": "1.0"}, {"url": "u", "protocolVersion": "0.3"}])
        self.assertEqual(R.score_protocol_version(dual)["points"], 10)

    def test_skills(self):
        self.assertEqual(R.score_skills(v1_card())["points"], 10)
        two = v1_card(skills=[{"id": "a", "name": "A", "description": "d", "tags": ["x"]}] * 2)
        self.assertEqual(R.score_skills(two)["points"], 5)
        untagged = v1_card(skills=[{"id": f"s{i}", "name": "S"} for i in range(4)])
        self.assertEqual(R.score_skills(untagged)["points"], 8)
        self.assertEqual(R.score_skills({"skills": []})["points"], 0)

    def test_security(self):
        self.assertEqual(R.score_security(v1_card())["points"], 0)
        self.assertEqual(R.score_security(v1_card(securityRequirements=[]))["points"], 2)
        self.assertEqual(R.score_security(v1_card(securitySchemes={"k": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}))["points"], 2)
        oauth = {"o": {"oauth2SecurityScheme": {"flows": {"authorizationCode": {"pkceRequired": True}}}}}
        self.assertEqual(R.score_security(v1_card(securitySchemes=oauth))["points"], 4)
        self.assertEqual(R.score_security(v1_card(securitySchemes={"m": {"mtlsSecurityScheme": {}}}))["points"], 5)

    def test_identity_and_freshness(self):
        self.assertEqual(R.score_identity(v1_card(), False)["points"], 5)
        self.assertEqual(R.score_identity(v1_card(), True)["points"], 8)
        self.assertEqual(R.score_identity({}, True)["points"], 3)
        self.assertEqual(R.score_freshness(v1_card(), NOW - 3600, True, NOW)["points"], 5)
        self.assertEqual(R.score_freshness(v1_card(), NOW - 10 * 86400, False, NOW)["points"], 2)
        self.assertEqual(R.score_freshness(v1_card(), NOW - 90 * 86400, False, NOW)["points"], 0)


class SignatureTests(unittest.TestCase):
    def test_unsigned_and_unverified(self):
        self.assertEqual(R.score_signature(v1_card(), "https://demo.example/.well-known/agent-card.json", None)["points"], 0)
        signed = v1_card(signatures=[{"protected": "e30", "signature": "AA"}])
        self.assertEqual(R.score_signature(signed, "https://demo.example/c", None)["points"], 3)

    def _round_trip(self, unencoded: bool):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "k1"
        card = v1_card()
        header = {"alg": "ES256", "typ": "JOSE", "kid": "k1",
                  "jku": "https://demo.example/.well-known/jwks.json"}
        # unencoded: RFC 7797 b64=false; otherwise a standard JWS with the
        # payload section dropped (what the a2a-sdk signer publishes).
        token = jwt.PyJWS().encode(R.canonical_card(card), key, algorithm="ES256",
                                   headers=header, is_payload_detached=unencoded)
        protected, _, signature = token.split(".")
        card["signatures"] = [{"protected": protected, "signature": signature}]
        fetch = lambda url: {"keys": [jwk]} if url.endswith("/.well-known/jwks.json") else None  # noqa: E731
        ok = R.score_signature(card, "https://demo.example/.well-known/agent-card.json", fetch)
        self.assertEqual(ok["points"], 10, ok["detail"])
        card["name"] = "Tampered"
        bad = R.score_signature(card, "https://demo.example/.well-known/agent-card.json", fetch)
        self.assertEqual(bad["points"], 3)

    def test_es256_round_trip_detached(self):
        self._round_trip(unencoded=False)

    def test_es256_round_trip_unencoded_payload(self):
        self._round_trip(unencoded=True)

    def test_same_site_uses_registrable_domain(self):
        self.assertTrue(R._same_site("https://a2a.octagonai.co/jwks.json",
                                     "https://www.octagonai.co/card"))
        self.assertFalse(R._same_site("https://tripdedee.com/jwks.json",
                                      "https://sarupdee.com/card"))
        self.assertFalse(R._same_site("https://a.onrender.com/jwks.json",
                                      "https://b.onrender.com/card"))

    def test_foreign_jku_rejected(self):
        header = base64.urlsafe_b64encode(json.dumps(
            {"alg": "ES256", "kid": "k", "jku": "https://evil.example/jwks.json"}).encode()).decode().rstrip("=")
        card = v1_card(signatures=[{"protected": header, "signature": "AA"}])
        res = R.score_signature(card, "https://demo.example/c", lambda u: {"keys": []})
        self.assertEqual(res["points"], 3)
        self.assertIn("provider", res["detail"])


class RpcProbeTests(unittest.TestCase):
    def test_v1_probe_is_read_only_get_task(self):
        client = FakeClient(FakeResp(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "nf"}}))
        res = R.probe_jsonrpc(client, "https://demo.example/a2a", "v1")
        self.assertEqual(res["state"], "live")
        url, body, headers = client.calls[0]
        self.assertEqual(body["method"], "GetTask")
        self.assertEqual(headers["A2A-Version"], "1.0")

    def test_v03_probe_and_states(self):
        client = FakeClient(FakeResp(200, {"jsonrpc": "2.0", "id": 1, "result": {}}))
        self.assertEqual(R.probe_jsonrpc(client, "https://x/a2a", "v03")["state"], "live")
        self.assertEqual(client.calls[0][1]["method"], "tasks/get")
        self.assertEqual(R.probe_jsonrpc(FakeClient(FakeResp(401, None)), "https://x", "v1")["state"], "auth_gated")
        self.assertEqual(R.probe_jsonrpc(FakeClient(FakeResp(402, None)), "https://x", "v1")["state"], "auth_gated")
        self.assertEqual(R.probe_jsonrpc(FakeClient(FakeResp(200, ValueError())), "https://x", "v1")["state"], "wrong_shape")
        self.assertEqual(R.probe_jsonrpc(FakeClient(FakeResp(503, None)), "https://x", "v1")["state"], "unreachable")
        self.assertEqual(R.probe_jsonrpc(FakeClient(OSError()), "https://x", "v1")["state"], "unreachable")
        self.assertEqual(R.probe_jsonrpc(FakeClient(None), None, "v1")["state"], "unreachable")

    def test_live_points_and_latency(self):
        self.assertEqual(R.score_live_jsonrpc({"state": "live", "latency_ms": 300})["points"], 25)
        self.assertEqual(R.score_live_jsonrpc({"state": "live", "latency_ms": 6000})["points"], 23)
        self.assertEqual(R.score_live_jsonrpc({"state": "auth_gated", "latency_ms": 100})["points"], 15)
        self.assertEqual(R.score_live_jsonrpc(None)["points"], 0)

    def test_preferred_rpc(self):
        self.assertEqual(R.preferred_rpc(v1_card()), ("https://demo.example/a2a", "v1"))
        self.assertEqual(R.preferred_rpc({"url": "https://o/a2a", "protocolVersion": "0.3.0"}), ("https://o/a2a", "v03"))
        self.assertEqual(R.preferred_rpc({"url": "/a2a"}, "https://h/.well-known/agent-card.json"), ("https://h/a2a", "v03"))


class AssembleTests(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(R.score_uptime(0, 0)["points"], 0)
        prov = R.score_uptime(2, 2)
        self.assertEqual((prov["points"], prov["status"]), (15, "provisional"))
        self.assertEqual(R.score_uptime(9, 10)["points"], 13.5)

    def test_full_assessment_and_grade(self):
        rep = R.assess(v1_card(securityRequirements=[]), "https://demo.example/.well-known/agent-card.json",
                       rpc={"state": "live", "latency_ms": 200}, uptime_ok=10, uptime_total=10,
                       owner_verified=True, last_seen=NOW, x402_supported=False, now=NOW)
        # 10 + 25 + 10 + 0 + 15 + 10 + 8 + 4 + 2
        self.assertEqual(rep["score"], 84)
        self.assertEqual(rep["grade"], "B")
        self.assertEqual([c["key"] for c in rep["criteria"]], list(R.WEIGHTS))
        self.assertEqual(sum(R.WEIGHTS.values()), 100)
        self.assertEqual([R.grade(s) for s in (95, 80, 61, 45, 10)], ["A", "B", "C", "D", "F"])

    def test_own_card_scores_card_criteria(self):
        card = a2a.own_agent_card()
        self.assertEqual(R.score_valid_card(card)["points"], 10)
        self.assertEqual(R.score_protocol_version(card)["points"], 10)
        self.assertEqual(R.score_skills(card)["points"], 10)


if __name__ == "__main__":
    unittest.main()
