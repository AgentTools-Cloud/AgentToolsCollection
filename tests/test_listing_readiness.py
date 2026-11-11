"""MCP / x402 readiness score and the extras shared with A2A readiness."""
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a_readiness as A, crawlers, listing_readiness as L  # noqa: E402

NOW = 1_800_000_000
DAY = 86400


def mcp_row(**extra):
    row = {
        "id": 1, "health": "ok", "http_status": 200, "conformance": "pass",
        "latency_ms": 120, "latency_p95_ms": 180, "protocol_version": L.MCP_REVISIONS[-1],
        "tools_json": json.dumps([{"name": f"t{i}", "description": "d"} for i in range(4)]),
        "tool_count": 4, "source_code_url": "https://github.com/demo/mcp",
        "package_name": None, "homepage_url": None, "owner_verified": 1,
        "last_seen": NOW - DAY, "x402_supported": 0, "pricing": None,
        "auth_method": "oauth", "health_checked": NOW,
    }
    row.update(extra)
    return row


def x402_row(**extra):
    row = {
        "id": 7, "health": "ok", "http_status": 402, "x402_ok": 1, "latency_ms": 300,
        "payment": json.dumps({"network": "eip155:8453", "max_amount_usdc": 0.01,
                               "pay_to": "0x" + "ab" * 20}),
        "resource_count": 3,
        "resource_samples": json.dumps([{"url": f"https://p.example/r{i}", "kind": "x402-resource",
                                         "x402_version": 2, "description": "d"} for i in range(3)]),
        "well_known_url": "https://p.example/.well-known/x402", "openapi_url": None,
        "descriptor_checked_at": NOW - DAY, "owner_verified": 0, "owner_declaration": None,
        "last_seen": NOW - DAY, "health_checked": NOW,
    }
    row.update(extra)
    return row


def by_key(report):
    return {c["key"]: c for c in report["criteria"]}


class SharedExtras(unittest.TestCase):
    def test_mcp_revisions_track_crawler(self):
        self.assertEqual(L.MCP_REVISIONS[-1], crawlers.MCP_LATEST_REVISION)

    def test_responsiveness_bands(self):
        self.assertEqual(L.responsiveness(None)["band"], "unmeasured")
        self.assertEqual(L.responsiveness(2500)["band"], "responsive")
        self.assertEqual(L.responsiveness(4000)["penalty"], 1)
        self.assertEqual(L.responsiveness(7000)["band"], "very_slow")
        self.assertEqual(L.responsiveness(9000)["band"], "near_timeout")
        self.assertEqual(L.responsiveness(9000)["penalty"], 3)

    def test_decay(self):
        self.assertEqual(L.decay_factor(10 * DAY), 1.0)
        self.assertAlmostEqual(L.decay_factor(75 * DAY), 0.7)
        self.assertEqual(L.decay_factor(400 * DAY), 0.4)
        fresh = L.assess_mcp(mcp_row(), uptime_ok=10, uptime_total=10, now=NOW)
        old = L.apply_age_decay(fresh, NOW + 120 * DAY)
        self.assertEqual(by_key(old)["live"]["points"], 10.0)
        self.assertLess(old["score"], fresh["score"])
        # applying twice must not compound
        again = L.apply_age_decay(old, NOW + 120 * DAY)
        self.assertEqual(by_key(again)["live"]["points"], 10.0)

    def test_payment_evidence_order(self):
        self.assertIsNone(L.payment_evidence())
        ev = L.payment_evidence(declared=True, sources={"cdp-bazaar", "x402scan", "submission"})
        self.assertEqual(ev["grade"], "agent_card")
        self.assertEqual([e["grade"] for e in ev["evidence"]],
                         ["agent_card", "bazaar_feed", "catalog_listing"])
        self.assertIsNone(L.payment_evidence(sources={"submission", "wellknown"}))
        self.assertTrue(L.payment_evidence(challenge=True)["self_declared_live"])

    def test_refused_probes(self):
        self.assertTrue(L.is_refused("degraded", 429))
        self.assertTrue(L.is_refused("degraded", 403))
        self.assertFalse(L.is_refused("ok", 403))
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE mcp_health_history(server_id, checked_at, status, latency_ms, http_status)")
        c.executemany("INSERT INTO mcp_health_history VALUES(?,?,?,?,?)", [
            (1, NOW - 10, "ok", 1, 200), (1, NOW - 20, "degraded", 1, 429),
            (1, NOW - 30, "down", None, None), (1, NOW - 40 * DAY, "down", None, None)])
        self.assertEqual(L.history_mcp(c, now=NOW), {1: (1, 2)})


class McpReadiness(unittest.TestCase):
    def test_full_marks_scale_to_100(self):
        rep = L.assess_mcp(mcp_row(), uptime_ok=10, uptime_total=10,
                           sources=["mcp-registry"], now=NOW)
        k = by_key(rep)
        self.assertFalse(k["signature"]["applicable"])
        self.assertEqual(rep["applicable_max"], 90)
        self.assertEqual(k["identity"]["points"], 8)
        self.assertEqual(k["security"]["points"], 4)
        # 10+25+10+15+10+8+4(fresh, no flag)+4 = 86 of 90
        self.assertEqual(rep["score"], round(86 * 100 / 90, 1))
        self.assertEqual(rep["grade"], "A")

    def test_auth_gated_and_old_revision(self):
        rep = L.assess_mcp(mcp_row(conformance="fail", http_status=401, tools_json=None,
                                   tool_count=None, protocol_version="2025-06-18",
                                   owner_verified=0, auth_method=None),
                           uptime_ok=3, uptime_total=3, sources=["mcp-registry"], now=NOW)
        k = by_key(rep)
        self.assertEqual(k["live"]["state"], "auth_gated")
        self.assertEqual(k["live"]["points"], 15)
        self.assertEqual(k["valid_card"]["points"], 0)
        self.assertEqual(k["protocol_version"]["points"], 6)
        self.assertEqual(k["identity"]["points"], 7)
        self.assertEqual(k["uptime"]["status"], "provisional")
        self.assertEqual(k["skills"]["points"], 0)

    def test_down_and_payment_evidence(self):
        rep = L.assess_mcp(mcp_row(health="down", http_status=None, conformance=None,
                                   x402_supported=1, pricing='{"offers": [1]}'),
                           sources=["smithery"], now=NOW)
        k = by_key(rep)
        self.assertEqual(k["live"]["points"], 0)
        self.assertEqual(rep["payment_evidence"]["grade"], "agent_card")
        self.assertEqual(k["freshness"]["points"], 5)

    def test_untagged_tools_cap(self):
        tools = [{"name": "a", "description": "d"}, {"name": "b"}, {"name": "c", "description": ""}]
        rep = L.assess_mcp(mcp_row(tools_json=json.dumps(tools)), now=NOW)
        self.assertEqual(by_key(rep)["skills"]["points"], 8)


class X402Readiness(unittest.TestCase):
    def test_full(self):
        rep = L.assess_x402(x402_row(), uptime_ok=9, uptime_total=9,
                            last_402_at=NOW - DAY, sources=["cdp-bazaar"], now=NOW)
        k = by_key(rep)
        self.assertEqual(rep["applicable_max"], 85)
        self.assertEqual(k["live"]["state"], "live")
        self.assertEqual(k["protocol_version"]["points"], 10)
        self.assertEqual(k["identity"]["points"], 5)
        self.assertEqual(rep["payment_evidence"]["grade"], "x402_challenge")
        self.assertEqual([e["grade"] for e in rep["payment_evidence"]["evidence"]],
                         ["x402_challenge", "x402_manifest", "bazaar_feed"])
        # 10+25+10+15+10+5+5 = 80 of 85
        self.assertEqual(rep["score"], round(80 * 100 / 85, 1))

    def test_unknown_version_is_na_and_findings(self):
        rep = L.assess_x402(x402_row(resource_samples=None, resource_count=None,
                                     payment=json.dumps({"network": "eip155:84532"}),
                                     http_status=200),
                            now=NOW)
        k = by_key(rep)
        self.assertFalse(k["protocol_version"]["applicable"])
        self.assertEqual(rep["applicable_max"], 75)
        self.assertEqual(k["valid_card"]["points"], 7)
        self.assertEqual(len(k["valid_card"]["findings"]), 3)
        self.assertEqual(k["live"]["state"], "seen_before")
        self.assertEqual(k["skills"]["points"], 5)

    def test_no_payment(self):
        rep = L.assess_x402(x402_row(payment=None, x402_ok=0, http_status=200,
                                     descriptor_checked_at=None), now=NOW)
        k = by_key(rep)
        self.assertEqual(k["valid_card"]["points"], 0)
        self.assertEqual(k["live"]["state"], "wrong_shape")


class A2AExtras(unittest.TestCase):
    def test_a2a_report_has_extras(self):
        card = {"name": "D", "description": "d", "version": "1", "skills": [],
                "capabilities": {"extensions": [{"uri": "https://x402.org/ext"}]},
                "supportedInterfaces": [{"url": "https://d.example/a2a",
                                         "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]}
        rep = A.assess(card, "https://d.example/.well-known/agent-card.json",
                       rpc={"state": "live", "latency_ms": 3000, "http_status": 200},
                       uptime_ok=1, uptime_total=1, owner_verified=False, last_seen=NOW,
                       x402_supported=False, now=NOW, sources={"agenstry"})
        self.assertEqual(rep["responsiveness"]["band"], "slow")
        self.assertEqual(rep["payment_evidence"]["grade"], "agent_card")
        valid = by_key(rep)["valid_card"]
        self.assertIn("missing skills[]", valid["findings"])


if __name__ == "__main__":
    unittest.main()
