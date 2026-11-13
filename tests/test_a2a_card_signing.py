"""Signed Agent Card for agent-tools.cloud (A2A 1.0 JWS signatures)."""
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from directory import a2a, a2a_readiness, card_signing  # noqa: E402

BASE = "https://agent-tools.cloud"
CARD_URL = BASE + "/.well-known/agent-card.json"


def _write_key(path, curve=ec.SECP256R1()):
    key = ec.generate_private_key(curve)
    with open(path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_path = os.path.join(self.tmp.name, "k.pem")
        _write_key(self.key_path)
        self.env = mock.patch.dict(os.environ, {card_signing.KEY_PATH_ENV: self.key_path})
        self.env.start()
        card_signing.reset_for_tests()

    def tearDown(self):
        self.env.stop()
        card_signing.reset_for_tests()
        self.tmp.cleanup()

    def _signed(self):
        return card_signing.sign_card(a2a.own_agent_card(BASE), BASE)

    def _sdk_verify(self, card):
        from google.protobuf.json_format import ParseDict
        from jwt import PyJWK
        from a2a.types import AgentCard
        from a2a.utils import signing

        jwks = card_signing.public_jwks()

        def key_provider(kid, jku):
            self.assertEqual(jku, BASE + "/.well-known/jwks.json")
            return PyJWK(next(k for k in jwks["keys"] if k["kid"] == kid))

        proto = ParseDict(card, AgentCard(), ignore_unknown_fields=True)
        signing.create_signature_verifier(key_provider, ["ES256"])(proto)

    def test_two_signatures_with_same_key_and_header(self):
        card = self._signed()
        self.assertEqual(len(card["signatures"]), 2)
        import json
        kid = card_signing.public_jwks()["keys"][0]["kid"]
        for sig in card["signatures"]:
            hdr = json.loads(a2a_readiness._b64url_decode(sig["protected"]))
            self.assertEqual(hdr, {"alg": "ES256", "typ": "JOSE", "kid": kid,
                                   "jku": BASE + "/.well-known/jwks.json"})

    def test_a2a_sdk_verifier_accepts(self):
        self._sdk_verify(self._signed())

    def test_raw_json_verifier_accepts(self):
        card = self._signed()
        jwks = card_signing.public_jwks()
        res = a2a_readiness.score_signature(card, CARD_URL, lambda url: jwks)
        self.assertEqual((res["points"], res["status"]), (10, "pass"), res)

    def test_tampered_card_fails_both_verifiers(self):
        card = self._signed()
        card["supportedInterfaces"][0]["url"] = "https://evil.example/a2a"
        card["url"] = "https://evil.example/a2a"
        from a2a.utils.signing import InvalidSignaturesError
        with self.assertRaises(InvalidSignaturesError):
            self._sdk_verify(card)
        jwks = card_signing.public_jwks()
        res = a2a_readiness.score_signature(card, CARD_URL, lambda url: jwks)
        self.assertNotEqual(res["status"], "pass")

    def _raw_sig_ok(self, card):
        import base64
        import jwt
        sig = card["signatures"][1]
        pub = jwt.PyJWK(card_signing.public_jwks()["keys"][0]).key
        enc = base64.urlsafe_b64encode(a2a_readiness.canonical_card(card)).rstrip(b"=").decode()
        try:
            jwt.PyJWS().decode_complete(f"{sig['protected']}.{enc}.{sig['signature']}",
                                        key=pub, algorithms=["ES256"])
            return True
        except jwt.PyJWTError:
            return False

    def test_legacy_field_tamper_caught_by_raw_signature(self):
        # Legacy 0.3 top-level fields are outside the 1.0 proto, so only the
        # raw-JSON signature covers them (the SDK signature ignores them).
        card = self._signed()
        self.assertTrue(self._raw_sig_ok(card))
        card["preferredTransport"] = "HTTP+JSON"
        self.assertFalse(self._raw_sig_ok(card))

    def test_jwks_is_public_only(self):
        keys = card_signing.public_jwks()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(set(keys[0]), {"kty", "crv", "x", "y", "use", "alg", "kid"})
        self.assertNotIn("d", keys[0])

    def test_resigning_is_cached_and_input_not_mutated(self):
        card = a2a.own_agent_card(BASE)
        before = copy.deepcopy(card)
        a = card_signing.sign_card(card, BASE)
        b = card_signing.sign_card(card, BASE)
        self.assertEqual(card, before)
        self.assertEqual(a["signatures"], b["signatures"])

    def test_existing_signatures_are_replaced(self):
        card = a2a.own_agent_card(BASE)
        card["signatures"] = [{"protected": "x", "signature": "y"}]
        self._sdk_verify(card_signing.sign_card(card, BASE))


class MissingKeyTests(unittest.TestCase):
    def tearDown(self):
        card_signing.reset_for_tests()

    def _with_key_path(self, path):
        card_signing.reset_for_tests()
        return mock.patch.dict(os.environ, {card_signing.KEY_PATH_ENV: path})

    def test_missing_key_serves_unsigned_card(self):
        with self._with_key_path("/nonexistent/key.pem"):
            card = card_signing.sign_card(a2a.own_agent_card(BASE), BASE)
            self.assertNotIn("signatures", card)
            self.assertEqual(card_signing.public_jwks(), {"keys": []})
            self.assertEqual(card["name"], "Agent Tools Research")

    def test_wrong_curve_key_serves_unsigned_card(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "k.pem")
            _write_key(p, ec.SECP384R1())
            with self._with_key_path(p):
                card = card_signing.sign_card(a2a.own_agent_card(BASE), BASE)
                self.assertNotIn("signatures", card)


if __name__ == "__main__":
    unittest.main()
