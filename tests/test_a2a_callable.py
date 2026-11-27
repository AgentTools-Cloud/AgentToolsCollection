import unittest
from unittest.mock import patch

import httpx

from directory import a2a

CARD = "https://agent.example.test/.well-known/agent-card.json"


def _probe(card_json, status=200):
    def response(request):
        if str(request.url) == CARD:
            return httpx.Response(status, json=card_json)
        return httpx.Response(404)

    with patch.object(a2a.public_http, "client", side_effect=lambda **kw:
                      httpx.Client(transport=httpx.MockTransport(response),
                                   follow_redirects=True)):
        return a2a.probe_a2a_health(CARD)


class A2ACallableTests(unittest.TestCase):
    def test_card_without_endpoint_is_degraded_not_healthy(self):
        # An API index / manifest with a name but no declared A2A endpoint.
        res = _probe({"name": "zFinia API", "role": "machine-newswire-and-learning-api",
                      "agent_card": "https://agent.example.test/discovery.json"})
        self.assertEqual(res["status"], "degraded")
        self.assertEqual(res["conformance"], "partial")
        self.assertFalse(res["callable"])
        self.assertIsNone(res["endpoint"])

    def test_card_with_url_is_ok_and_callable(self):
        res = _probe({"name": "Agent", "url": "https://agent.example.test/a2a",
                      "skills": [{"id": "s", "name": "S"}]})
        self.assertEqual((res["status"], res["conformance"]), ("ok", "pass"))
        self.assertTrue(res["callable"])
        self.assertEqual(res["endpoint"], "https://agent.example.test/a2a")

    def test_v1_supported_interfaces_endpoint_counts(self):
        res = _probe({"name": "Agent", "skills": [],
                      "supportedInterfaces": [{"url": "https://agent.example.test/rpc",
                                               "protocolBinding": "JSONRPC",
                                               "protocolVersion": "1.0"}]})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["endpoint"], "https://agent.example.test/rpc")

    def test_relative_url_resolves_against_card_origin(self):
        res = _probe({"name": "Agent", "url": "/a2a"})
        self.assertEqual(res["endpoint"], "https://agent.example.test/a2a")

    def test_non_http_url_is_not_an_endpoint(self):
        res = _probe({"name": "Agent", "url": "message/send"})
        self.assertEqual(res["status"], "degraded")
        self.assertFalse(res["callable"])

    def test_public_agent_marks_callable(self):
        self.assertTrue(a2a.public_agent({"slug": "a", "endpoint_url": "https://x.test/a2a"})["callable"])
        out = a2a.public_agent({"slug": "b", "endpoint_url": None})
        self.assertFalse(out["callable"])
        self.assertIn("does not declare a callable A2A endpoint", out["call_note"])


if __name__ == "__main__":
    unittest.main()
