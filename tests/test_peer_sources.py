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
            json.dumps({"apex": "a.io", "checks": [
                {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "https://a.io/mcp"}},
                {"id": "D1", "status": "pass", "evidence": {}}]}),
            json.dumps({"apex": "b.io", "checks": [
                {"id": "D3", "status": "fail", "evidence": {"endpointUrl": "https://b.io/mcp"}}]}),
            json.dumps({"apex": "c.io", "checks": [
                {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "http://c.io/mcp"}}]}),
            "not json",
        ]
        rows = peers.parse_mcp_census(lines)
        self.assertEqual([r["endpoint_url"] for r in rows], ["https://a.io/mcp"])
        r = rows[0]
        self.assertEqual(r["source"], "mcp-census")
        self.assertEqual(r["source_id"], "https://a.io/mcp")
        self.assertFalse(r["x402_supported"])
        self.assertEqual(r["transport"], "streamable-http")

    def test_csoai_keeps_live_states_and_skips_tunnels_and_ips(self):
        lines = [json.dumps(x) for x in (
            {"endpoint": "https://mcp.grafana.com/mcp", "state": "AUTH_REQUIRED"},
            {"endpoint": "https://x.io/sse", "state": "RESPONDED"},
            {"endpoint": "https://down.io/mcp", "state": "UNREACHABLE"},
            {"endpoint": "https://foo-bar.trycloudflare.com/mcp", "state": "RESPONDED"},
            {"endpoint": "https://1.2.3.4/mcp", "state": "RESPONDED"},
            {"endpoint": "https://abc.a.free.pinggy.link/mcp", "state": "RESPONDED"},
        )]
        rows = peers.parse_csoai(lines)
        self.assertEqual([r["endpoint_url"] for r in rows],
                         ["https://mcp.grafana.com/mcp", "https://x.io/sse"])
        self.assertEqual(rows[1]["transport"], "sse")

    def test_per_host_cap(self):
        lines = [json.dumps({"endpoint": f"https://big.io/m{i}/mcp", "state": "RESPONDED"})
                 for i in range(50)]
        self.assertEqual(len(peers.parse_csoai(lines)), peers.PER_HOST)

    def test_mcp_census_two_endpoints_on_one_apex_stay_two_rows(self):
        lines = [json.dumps({"apex": "a.io", "checks": [
            {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "https://a.io/mcp"}},
            {"id": "D3", "status": "pass", "evidence": {"endpointUrl": "https://a.io/sse"}}]})]
        rows = peers.parse_mcp_census(lines)
        self.assertEqual(len({r["source_id"] for r in rows}), 2)

    def test_agentalog_takes_every_record(self):
        items = [
            {"chave": "a", "cartao": "https://a.io/.well-known/agent-card.json",
             "creditos": [{"nome": "Agenstry", "licenca": "CC-BY-4.0"}]},
            {"chave": "b", "cartao": "https://b.io/.well-known/agent-card.json",
             "creditos": [{"nome": "hultra", "licenca": "ODbL 1.0"}]},
            {"chave": "c", "url": "https://c.io/a2a", "creditos": "odd"},
            "not a dict",
            {"chave": "d", "cartao": {"weird": 1}},
        ]
        self.assertEqual([l["card_url"] for l in peers.agentalog_leads(items)],
                         ["https://a.io/.well-known/agent-card.json",
                          "https://b.io/.well-known/agent-card.json",
                          "https://c.io"])

    def test_urls_with_secrets_private_hosts_and_peer_skips(self):
        self.assertEqual(peers._https_url("https://a.io/mcp?api_key=SECRET#x"),
                         "https://a.io/mcp")
        row = peers.mcp_row("https://a.io/mcp?token=abc", "csoai-census")
        self.assertEqual(row["endpoint_url"], "https://a.io/mcp")
        self.assertEqual(row["auth_method"], "api-key")
        for bad in ("https://localhost/mcp", "https://svc.internal/mcp", "https://intranet/mcp",
                    "https://[abc/mcp", "https://x.a.free.pinggy.link/mcp",
                    "https://pi.tail15a9c9.ts.net/mcp", "https://1-2-3-4.sslip.io/mcp",
                    "https://a.io/" + "x" * 600, None, 5, {"u": 1}):
            self.assertIsNone(peers._https_url(bad), bad)
        self.assertIsNone(peers._origin("localhost"))

    def test_trailing_slash_kept_and_path_case_distinct(self):
        self.assertEqual(peers._https_url("https://a.io/mcp/"), "https://a.io/mcp/")
        lines = [json.dumps({"endpoint": u, "state": "RESPONDED"})
                 for u in ("https://A.io/MCP", "https://a.io/mcp", "https://a.io/mcp/")]
        self.assertEqual(len(peers.parse_csoai(lines)), 2)

    def test_malformed_records_are_skipped(self):
        lines = [json.dumps({"apex": "a", "checks": 5}),
                 json.dumps({"apex": "b", "checks": [{"id": "D3", "status": "pass", "evidence": []}]}),
                 json.dumps({"apex": "c", "checks": [{"id": "D3", "status": "pass",
                                                      "evidence": {"endpointUrl": "https://c.io/mcp"}}]})]
        self.assertEqual([r["endpoint_url"] for r in peers.parse_mcp_census(lines)],
                         ["https://c.io/mcp"])
        mcp, _, _ = peers.assay_split([{"target": "https://m.io/mcp", "mcp_tools": "4"},
                                       {"target": ["x"]}, None])
        self.assertEqual(mcp, [])

    def test_assay_split(self):
        board = [
            {"target": "https://m.io/mcp", "mcp_tools": 4, "a2a_card": True, "x402_gated": False},
            {"target": "p.io/api/thing", "mcp_tools": 0, "a2a_card": False, "x402_gated": True},
            {"target": "https://n.io", "mcp_tools": None, "a2a_card": False, "x402_gated": False},
        ]
        mcp, a2a_leads, x402 = peers.assay_split(board)
        self.assertEqual([r["endpoint_url"] for r in mcp], ["https://m.io/mcp"])
        self.assertEqual([l["card_url"] for l in a2a_leads], ["https://m.io"])
        self.assertEqual([r["url"] for r in x402], ["https://p.io"])
        self.assertEqual(x402[0]["resource_samples"],
                         [{"url": "https://p.io/api/thing", "kind": "x402-resource"}])
        self.assertEqual(x402[0]["well_known_url"], "https://p.io/.well-known/x402")

    def test_agentindex_split_ignores_mcp_only_domains(self):
        res = [{"domain": "pay.io", "protocols": ["x402", "mpp"]},
               {"domain": "agent.io", "protocols": "a2a"},
               {"domain": "tool.io", "protocols": ["mcp"]},
               {"domain": "bad host", "protocols": ["x402"]}]
        x402, leads = peers.agentindex_split(res)
        self.assertEqual([r["url"] for r in x402], ["https://pay.io"])
        self.assertEqual([l["card_url"] for l in leads], ["https://agent.io"])

    def test_x402lint_rows_one_per_host_and_no_tunnels(self):
        rows = peers.x402lint_rows([{"host": "api.io"}, {"host": "api.io"},
                                    {"host": "x-y.trycloudflare.com"}, {"host": None}])
        self.assertEqual([r["url"] for r in rows], ["https://api.io"])
        self.assertEqual(rows[0]["source"], "x402lint")

    def test_drop_known(self):
        leads = [{"card_url": "https://known.io"},
                 {"card_url": "https://new.io"},
                 {"card_url": "https://x.io/.well-known/agent-card.json"},
                 {"card_url": "https://y.io/.well-known/agent-card.json"}]
        kept = peers._drop_known(leads, {"x.io/.well-known/agent-card.json"},
                                 {"known.io"})
        self.assertEqual([l["card_url"] for l in kept],
                         ["https://new.io", "https://y.io/.well-known/agent-card.json"])


class PeerGateTests(unittest.TestCase):
    def test_not_due_returns_empty_without_network(self):
        with patch.object(peers, "_due", return_value=False), \
                patch.object(peers, "_client", side_effect=AssertionError("no network")):
            self.assertEqual(peers.fetch_mcp_census(), [])
            self.assertEqual(peers.fetch_csoai_census(), [])
            self.assertEqual(peers.fetch_x402lint(), [])
            self.assertEqual(peers.fetch_agentindex_x402(), [])
            self.assertEqual(peers.agentindex_fetch_leads(), [])
            self.assertEqual(peers.agentalog_fetch_leads(), [])

    def test_mark_only_after_settle_ok(self):
        marks = []
        with patch.object(peers, "_mark", side_effect=marks.append):
            peers._pending("x402lint", "x402lint")
            peers.settle("x402lint", False)
            self.assertEqual(marks, [])
            peers._pending("x402lint", "x402lint")
            peers.settle("x402lint", True)
            self.assertEqual(marks, ["x402lint"])
            peers.settle("x402lint", True)  # nothing pending
            self.assertEqual(marks, ["x402lint"])

    def test_retry_after_negative_does_not_crash(self):
        class R:
            def __init__(self, code, ra=None):
                self.status_code, self.headers = code, ({"retry-after": ra} if ra else {})
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)
        class C:
            def __init__(self):
                self.n = 0
            def get(self, url, params=None):
                self.n += 1
                return R(429, "-5") if self.n == 1 else R(200)
        with patch.object(peers.time, "sleep") as sl:
            self.assertEqual(peers._get(C(), "https://x.io").status_code, 200)
        sl.assert_called_with(0.0)

    def test_owner_removed_hosts_are_dropped(self):
        with patch.object(peers, "_owner_removed_hosts", return_value={"gone.io"}):
            rows = [{"url": "https://gone.io"}, {"url": "https://kept.io"}]
            self.assertEqual(peers._drop_removed(rows, "url"), [{"url": "https://kept.io"}])
        with patch.object(peers, "_owner_removed_hosts", side_effect=RuntimeError("db")):
            with self.assertRaises(RuntimeError):
                peers._drop_removed([{"url": "https://a.io"}], "url")

    def test_a2a_drops_known_before_cap_and_settles(self):
        leads = [{"card_url": f"https://big.io/a/{i}/.well-known/agent-card.json",
                  "source_id": str(i), "source_url": None} for i in range(30)]
        known = {f"big.io/a/{i}/.well-known/agent-card.json" for i in range(20)}
        got = {}
        class FakeConn:
            def execute(self, *a):
                class R:
                    def fetchall(self_inner):
                        return [(f"https://{k}", None) for k in known]
                return R()
            def close(self):
                pass
        def fake_crawl(name, ls):
            got[name] = [l["source_id"] for l in ls]
            return {"candidates": len(ls), "resolved": 0, "inserted": 0, "updated": 0, "rejected": 0}
        from directory import a2a
        with patch.object(peers.db, "connect", return_value=FakeConn()), \
                patch.object(peers, "A2A_LEAD_SOURCES", {"agentalog": lambda: leads}), \
                patch.object(peers, "_owner_removed_hosts", return_value=set()), \
                patch.object(peers, "settle") as st, \
                patch.object(a2a, "crawl_card_leads", side_effect=fake_crawl):
            peers.crawl_a2a_peers()
        self.assertEqual(got["agentalog"], [str(i) for i in range(20, 30)])
        st.assert_called_with("a2a:agentalog", True)

    def test_registered_in_jobs(self):
        from directory import jobs  # noqa: F401 -- registration happens on import
        for name in ("mcp-census", "csoai-census", "assay"):
            self.assertIn(name, crawlers.MCP_CRAWLERS)
        for name in ("x402lint", "agentindex", "assay"):
            self.assertIn(name, crawlers.ALL_CRAWLERS)


if __name__ == "__main__":
    unittest.main()
