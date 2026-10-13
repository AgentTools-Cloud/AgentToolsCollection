"""Offline Agent Card selection regressions; all HTTP uses MockTransport."""

import unittest
from unittest.mock import patch

import httpx
from directory import a2a, public_http


class AgentCardSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(httpx.HTTPTransport, "handle_request",
                                      side_effect=AssertionError("real network forbidden")))
        self.visited = []

    def fetch(self, documents, url="https://agent.example", **kwargs):
        def respond(request):
            self.visited.append(str(request.url))
            item = documents.get(str(request.url), documents.get(request.url.path))
            if isinstance(item, Exception):
                raise item
            return httpx.Response(200, json=item) if item is not None else httpx.Response(404)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = a2a.fetch_agent_card(url, client=client, **kwargs)
            self.assertFalse(client.is_closed)
            return result

    def test_homepage_name_only_json_does_not_preempt_valid_card(self):
        card = {"name": "Real Agent", "url": "https://agent.example/a2a"}
        result = self.fetch({"/": {"name": "Ordinary site"},
                             "/.well-known/agent-card.json": card})
        self.assertEqual(result, (card, "https://agent.example/.well-known/agent-card.json"))
        self.assertEqual(len(self.visited), 1)

    def test_homepage_metadata_without_endpoint_is_not_a_card(self):
        self.assertEqual(self.fetch({"/": {"name": "Site", "skills": ["metadata"]}}),
                         (None, None))

    def test_invalid_standard_card_does_not_preempt_valid_compatible_card(self):
        valid = {"name": "Agent", "url": "/a2a"}
        result = self.fetch({"/.well-known/agent-card.json": {"name": "Agent", "url": "POST /mcp"},
                             "/.well-known/agent.json": valid})
        self.assertEqual(result, (valid, "https://agent.example/.well-known/agent.json"))

    def test_supported_interfaces_card_is_accepted(self):
        card = {"name": "Agent", "supportedInterfaces": [{"url": "https://agent.example/a2a"}]}
        self.assertEqual(self.fetch({"/.well-known/agent-card.json": card})[0], card)

    def test_invalid_genuine_cards_remain_available_for_ingestion_audit(self):
        for endpoint in (None, "POST /mcp", "wss://agent.example/a2a", "https://bad_host.example/a2a"):
            with self.subTest(endpoint=endpoint):
                card = {"name": "Agent", "url": endpoint}
                result = self.fetch({"/.well-known/agent-card.json": card},
                                    include_invalid_for_audit=True)
                self.assertEqual(result, (card, "https://agent.example/.well-known/agent-card.json"))

    def test_direct_card_query_is_preserved_and_never_replaced_by_root_card(self):
        url = "https://agent.example/agents/tenant.json?tenant=AbC"
        invalid = {"name": "Tenant Agent", "url": "wss://agent.example/tenant"}
        result = self.fetch({url: invalid, "/.well-known/agent-card.json":
                             {"name": "Other Tenant", "url": "https://agent.example/other"}},
                            url, include_invalid_for_audit=True)
        self.assertEqual(result, (invalid, url))
        self.assertEqual(self.visited, [url])

    def test_direct_non_json_card_path_remains_isolated(self):
        url = "https://agent.example/tenant/card"
        card = {"name": "Tenant Agent", "url": "https://agent.example/tenant"}
        self.assertEqual(self.fetch({url: card}, url), (card, url))
        self.assertEqual(self.visited, [url])

    def test_valid_root_card_is_fallback_after_card_paths(self):
        card = {"name": "Root Agent", "url": "https://agent.example/a2a"}
        self.assertEqual(self.fetch({"/": card}), (card, "https://agent.example"))
        self.assertEqual(len(self.visited), len(a2a.CARD_PATHS) + 1)

    def test_declared_but_invalid_homepage_endpoint_remains_auditable(self):
        card = {"name": "Root Agent", "url": "POST /mcp"}
        self.assertEqual(self.fetch({"/": card}, include_invalid_for_audit=True),
                         (card, "https://agent.example"))

    def test_strict_unsafe_signal_is_not_swallowed(self):
        error = public_http.UnsafeURL("fixture unsafe redirect")
        with self.assertRaises(public_http.UnsafeURL):
            self.fetch({"/.well-known/agent-card.json": error}, reject_unsafe=True)

    def test_invalid_live_card_is_absent_for_nonvalidating_callers(self):
        self.assertEqual(self.fetch({"/.well-known/agent-card.json":
                                     {"name": "Agent", "url": None}}), (None, None))

    def test_explicit_path_and_tracking_query_homepages_discover_origin_card(self):
        card = {"name": "Agent", "url": "https://agent.example/a2a"}
        for url in ("https://agent.example/docs/", "https://agent.example/?utm_source=catalog"):
            with self.subTest(url=url):
                self.visited.clear()
                self.assertEqual(self.fetch({"/.well-known/agent-card.json": card},
                                             url, homepage=True)[0], card)
                self.assertEqual(self.visited, ["https://agent.example/.well-known/agent-card.json"])
