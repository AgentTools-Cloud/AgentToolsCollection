import json
import unittest
from unittest.mock import patch

from directory import crawlers, peers


def _no_retired(url):
    return False


class PeerParserTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(crawlers, "url_retired", side_effect=_no_retired)
        p.start()
        self.addCleanup(p.stop)

    def test_mcp_census_takes_only_passing_d3_endpoints(self):
        lines = [
            json.dumps({"apex": "a.example", "checks": [
                {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "https://a.example/mcp"}},
                {"id": "D1", "status": "pass", "evidence": {}}]}),
            json.dumps({"apex": "b.example", "checks": [
                {"id": "D3", "status": "fail", "evidence": {"endpointUrl": "https://b.example/mcp"}}]}),
            json.dumps({"apex": "c.example", "checks": [
                {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "http://c.example/mcp"}}]}),
            "not json",
        ]
        rows = peers.parse_mcp_census(lines)
        self.assertEqual([r["endpoint_url"] for r in rows], ["https://a.example/mcp"])
        r = rows[0]
        self.assertEqual(r["source"], "mcp-census")
        self.assertEqual(r["source_id"], "a.example")
        self.assertFalse(r["x402_supported"])
        self.assertEqual(r["transport"], "streamable-http")

    def test_csoai_keeps_live_states_and_skips_tunnels_and_ips(self):
        lines = [json.dumps(x) for x in (
            {"endpoint": "https://mcp.grafana.com/mcp", "state": "AUTH_REQUIRED"},
            {"endpoint": "https://x.example/sse", "state": "RESPONDED"},
            {"endpoint": "https://down.example/mcp", "state": "UNREACHABLE"},
            {"endpoint": "https://foo-bar.trycloudflare.com/mcp", "state": "RESPONDED"},
            {"endpoint": "https://1.2.3.4/mcp", "state": "RESPONDED"},
            {"endpoint": "https://abc.a.free.pinggy.link/mcp", "state": "RESPONDED"},
        )]
        rows = peers.parse_csoai(lines)
        self.assertEqual([r["endpoint_url"] for r in rows],
                         ["https://mcp.grafana.com/mcp", "https://x.example/sse"])
        self.assertEqual(rows[1]["transport"], "sse")

    def test_per_host_cap(self):
        lines = [json.dumps({"endpoint": f"https://big.example/m{i}/mcp", "state": "RESPONDED"})
                 for i in range(50)]
        self.assertEqual(len(peers.parse_csoai(lines)), peers.PER_HOST)

    def test_inite_record_urls(self):
        rec = {"interfaces": {"mcp": [{"remotes": ["https://mcp.airtable.com/mcp"]},
                                      {"remotes": [{"url": "https://mcp2.airtable.com/sse"}]}]},
               "mcpMeasured": {"server": "https://mcp.airtable.com/mcp"}}
        self.assertEqual(peers.parse_inite_record(rec),
                         ["https://mcp.airtable.com/mcp", "https://mcp2.airtable.com/sse"])
        self.assertEqual(peers.parse_inite_record({}), [])

    def test_sealkeeper_uses_domain_when_card_is_their_own_record(self):
        agents = [
            {"id": "1", "domain": None, "cardUrl": "https://api.sealkeeper.run/v1/agents/1"},
            {"id": "2", "domain": "agent.example", "cardUrl": "https://api.sealkeeper.run/v1/agents/2"},
            {"id": "3", "domain": None, "cardUrl": "https://pub.example/.well-known/agent-card.json"},
        ]
        leads = peers.sealkeeper_leads(agents)
        self.assertEqual([l["card_url"] for l in leads],
                         ["https://agent.example", "https://pub.example/.well-known/agent-card.json"])
        self.assertEqual(leads[0]["source_id"], "2")

    def test_agentalog_skips_share_alike_records(self):
        items = [
            {"chave": "a", "cartao": "https://a.example/.well-known/agent-card.json",
             "creditos": [{"nome": "Agenstry", "licenca": "CC-BY-4.0"}]},
            {"chave": "b", "cartao": "https://b.example/.well-known/agent-card.json",
             "creditos": [{"nome": "hultra", "licenca": "ODbL 1.0"}]},
            {"chave": "c", "url": "https://c.example/a2a", "creditos": []},
        ]
        self.assertEqual([l["card_url"] for l in peers.agentalog_leads(items)],
                         ["https://a.example/.well-known/agent-card.json", "https://c.example/a2a"])

    def test_assay_split(self):
        board = [
            {"target": "https://m.example/mcp", "mcp_tools": 4, "a2a_card": True, "x402_gated": False},
            {"target": "p.example/api/thing", "mcp_tools": 0, "a2a_card": False, "x402_gated": True},
            {"target": "https://n.example", "mcp_tools": None, "a2a_card": False, "x402_gated": False},
        ]
        mcp, a2a_leads, x402 = peers.assay_split(board)
        self.assertEqual([r["endpoint_url"] for r in mcp], ["https://m.example/mcp"])
        self.assertEqual([l["card_url"] for l in a2a_leads], ["https://m.example"])
        self.assertEqual([r["url"] for r in x402], ["https://p.example"])
        self.assertEqual(x402[0]["resource_samples"],
                         [{"url": "https://p.example/api/thing", "kind": "x402-resource"}])
        self.assertEqual(x402[0]["well_known_url"], "https://p.example/.well-known/x402")

    def test_agentindex_split_ignores_mcp_only_domains(self):
        res = [{"domain": "pay.example", "protocols": ["x402", "mpp"]},
               {"domain": "agent.example", "protocols": ["a2a"]},
               {"domain": "tool.example", "protocols": ["mcp"]},
               {"domain": "bad host", "protocols": ["x402"]}]
        x402, leads = peers.agentindex_split(res)
        self.assertEqual([r["url"] for r in x402], ["https://pay.example"])
        self.assertEqual([l["card_url"] for l in leads], ["https://agent.example"])

    def test_x402lint_rows_one_per_host_and_no_tunnels(self):
        rows = peers.x402lint_rows([{"host": "api.example"}, {"host": "api.example"},
                                    {"host": "x-y.trycloudflare.com"}, {"host": None}])
        self.assertEqual([r["url"] for r in rows], ["https://api.example"])
        self.assertEqual(rows[0]["source"], "x402lint")

    def test_drop_known(self):
        leads = [{"card_url": "https://known.example"},
                 {"card_url": "https://new.example"},
                 {"card_url": "https://x.example/.well-known/agent-card.json"},
                 {"card_url": "https://y.example/.well-known/agent-card.json"}]
        kept = peers._drop_known(leads, {"https://x.example/.well-known/agent-card.json"},
                                 {"known.example"})
        self.assertEqual([l["card_url"] for l in kept],
                         ["https://new.example", "https://y.example/.well-known/agent-card.json"])


class PeerGateTests(unittest.TestCase):
    def test_not_due_returns_empty_without_network(self):
        with patch.object(peers, "_due", return_value=False), \
                patch.object(peers, "_client", side_effect=AssertionError("no network")):
            self.assertEqual(peers.fetch_mcp_census(), [])
            self.assertEqual(peers.fetch_csoai_census(), [])
            self.assertEqual(peers.fetch_inite_mcp(), [])
            self.assertEqual(peers.fetch_x402lint(), [])
            self.assertEqual(peers.sealkeeper_fetch_leads(), [])
            self.assertEqual(peers.agentalog_fetch_leads(), [])

    def test_registered_in_jobs(self):
        from directory import jobs  # noqa: F401 -- registration happens on import
        for name in ("mcp-census", "csoai-census", "inite", "assay"):
            self.assertIn(name, crawlers.MCP_CRAWLERS)
        for name in ("x402lint", "agentindex", "assay"):
            self.assertIn(name, crawlers.ALL_CRAWLERS)


if __name__ == "__main__":
    unittest.main()
