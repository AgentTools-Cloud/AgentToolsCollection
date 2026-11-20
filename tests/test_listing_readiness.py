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
        self.assertEqual(rep["applicable_max"], 85)
        self.assertEqual(k["identity"]["points"], 8)
        self.assertEqual(k["security"]["points"], 4)
        self.assertNotIn("freshness", k)
        # 10+25+10+15+10+8+4 = 82 of 85
        self.assertEqual(rep["score"], round(82 * 100 / 85, 1))
        self.assertEqual(rep["grade"], "A")

    def test_declared_open_auth(self):
        # "none" is the owner edit form's value for a server that needs no credentials.
        for auth in ("none", "None ", "open", "public", "public-discovery"):
            k = by_key(L.assess_mcp(mcp_row(auth_method=auth), now=NOW))
            self.assertEqual((k["security"]["points"], k["security"]["status"]), (2, "partial"), auth)
            self.assertIn("Declared open", k["security"]["detail"])
        for auth in (None, "", "null", "unknown"):
            k = by_key(L.assess_mcp(mcp_row(auth_method=auth), now=NOW))
            self.assertEqual((k["security"]["points"], k["security"]["status"]), (0, "info"), auth)

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

    def test_auth_gated_items_say_so_and_still_count(self):
        for sc, gate in ((401, "Requires authentication (HTTP 401): "),
                         (402, "Requires payment (HTTP 402): ")):
            rep = L.assess_mcp(mcp_row(conformance="fail", http_status=sc, tools_json=None,
                                       tool_count=None, protocol_version=None),
                               uptime_ok=10, uptime_total=10, now=NOW)
            k = by_key(rep)
            for key in ("valid_card", "protocol_version", "skills"):
                self.assertEqual(k[key]["points"], 0)
                self.assertTrue(k[key]["auth_gated"])
                self.assertTrue(k[key]["detail"].startswith(gate), k[key]["detail"])
                self.assertIn("not measured", k[key]["detail"])
            self.assertEqual(rep["applicable_max"], 85)
            got = sum(c["points"] for c in rep["criteria"] if c.get("applicable", True))
            self.assertEqual(rep["score"], round(got * 100 / 85, 1))
        broken = by_key(L.assess_mcp(mcp_row(conformance="fail", http_status=500, tools_json=None,
                                             tool_count=None, protocol_version=None), now=NOW))
        self.assertEqual(broken["skills"]["detail"], "No tools listed by tools/list.")
        self.assertNotIn("auth_gated", broken["protocol_version"])

    def test_auth_discovery_scores_what_an_anonymous_client_can_check(self):
        gated = dict(conformance="fail", http_status=401, tools_json=None, tool_count=None,
                     protocol_version=None, auth_method=None)
        full = {"challenge": True, "resource_metadata": True, "server_metadata": True,
                "pkce": True, "registration": True, "v": 1}
        rep = L.assess_mcp(mcp_row(auth_discovery_json=json.dumps(full), **gated),
                           uptime_ok=10, uptime_total=10, now=NOW)
        k = by_key(rep)
        self.assertEqual((k["valid_card"]["points"], k["valid_card"]["status"]), (5, "partial"))
        self.assertTrue(k["valid_card"]["auth_gated"])
        self.assertIn("3 of 3 checks passed", k["valid_card"]["detail"])
        self.assertIn("initialize and tools/list need credentials", k["valid_card"]["detail"])
        self.assertEqual((k["live"]["state"], k["live"]["points"], k["live"]["status"]),
                         ("auth_discoverable", 25, "pass"))
        self.assertEqual((k["security"]["points"], k["security"]["status"]), (5, "pass"))
        for key in ("protocol_version", "skills"):
            self.assertEqual(k[key]["points"], 0)
            self.assertTrue(k[key]["auth_gated"])
        # 5 + 25 + 0 + 15 + 0 + 8 + 5 = 58 of 85
        self.assertEqual(rep["score"], round(58 * 100 / 85, 1))
        # db.mcp_row_to_dict hands over the parsed form
        parsed = L.assess_mcp(mcp_row(auth_discovery=full, **gated), uptime_ok=10,
                              uptime_total=10, now=NOW)
        self.assertEqual(parsed["score"], rep["score"])

        k = by_key(L.assess_mcp(mcp_row(auth_discovery_json=json.dumps(
            {"challenge": True, "server_metadata": True, "pkce": True}), **gated), now=NOW))
        self.assertEqual(k["valid_card"]["points"], 3)
        self.assertIn("2 of 3 checks passed (Bearer challenge, authorization-server metadata); "
                      "missing protected-resource metadata", k["valid_card"]["detail"])
        self.assertEqual((k["live"]["state"], k["live"]["points"]), ("auth_gated", 15))
        self.assertEqual(k["security"]["points"], 4)
        self.assertIn("no automatic client registration", k["security"]["detail"])

        nothing = by_key(L.assess_mcp(mcp_row(auth_discovery_json=json.dumps(
            {"challenge": False}), **gated), now=NOW))
        self.assertEqual((nothing["valid_card"]["points"], nothing["valid_card"]["status"]),
                         (0, "info"))
        self.assertEqual(nothing["security"]["points"], 0)
        declared = by_key(L.assess_mcp(mcp_row(auth_discovery_json=json.dumps(full),
                                               **dict(gated, auth_method="mtls")), now=NOW))
        self.assertEqual(declared["security"]["points"], 5)
        # Only a 401 is scored on discovery; an old result is ignored once the server opens up.
        for sc, conf in ((403, "fail"), (402, "fail"), (200, "pass")):
            k = by_key(L.assess_mcp(mcp_row(auth_discovery_json=json.dumps(full),
                                            **dict(gated, http_status=sc, conformance=conf)),
                                    now=NOW))
            self.assertNotEqual(k["live"]["state"], "auth_discoverable", sc)
            self.assertEqual(k["valid_card"]["points"], 10 if sc == 200 else 0, sc)

    def test_auth_gated_fixes_point_to_the_discovery(self):
        gated = dict(conformance="fail", http_status=401, tools_json=None, tool_count=None,
                     protocol_version=None)
        full = {"challenge": True, "resource_metadata": True, "server_metadata": True}
        k = by_key(L.assess_mcp(mcp_row(auth_discovery=full, **gated), now=NOW))
        self.assertNotIn("fix", k["valid_card"])
        self.assertNotIn("fix", k["live"])
        k = by_key(L.assess_mcp(mcp_row(auth_discovery={"challenge": True}, **gated), now=NOW))
        self.assertIn("did not find: protected-resource metadata, authorization-server metadata.",
                      k["valid_card"]["fix"])
        self.assertIn("Bearer challenge and protected-resource metadata", k["live"]["fix"])
        unchecked = by_key(L.assess_mcp(mcp_row(**gated), now=NOW))
        self.assertIn("we check weekly", unchecked["valid_card"]["fix"])
        self.assertIn("fix", unchecked["live"])
        for sc in (402, 403):
            k = by_key(L.assess_mcp(mcp_row(**dict(gated, http_status=sc)), now=NOW))
            self.assertNotIn("fix", k["valid_card"], sc)
            self.assertNotIn("fix", k["live"], sc)

    def test_down_and_payment_evidence(self):
        rep = L.assess_mcp(mcp_row(health="down", http_status=None, conformance=None,
                                   x402_supported=1, pricing='{"offers": [1]}'),
                           sources=["smithery"], now=NOW)
        k = by_key(rep)
        self.assertEqual(k["live"]["points"], 0)
        self.assertEqual(rep["payment_evidence"]["grade"], "agent_card")

    def test_stored_report_drops_retired_criterion(self):
        stored = {"kind": "a2a", "score": 84, "grade": "B", "applicable_max": 100,
                  "checked_at": NOW, "methodology": "Nine criteria", "criteria": [
                      {"key": "live_jsonrpc", "points": 25, "max_points": 25},
                      {"key": "uptime", "points": 15, "max_points": 15},
                      {"key": "freshness", "points": 0, "max_points": 5},
                      {"key": "security", "points": 2, "max_points": 5}]}
        rep = L.apply_age_decay(stored, NOW)
        self.assertEqual([c["key"] for c in rep["criteria"]], ["live_jsonrpc", "uptime", "security"])
        self.assertEqual(rep["applicable_max"], 45)
        self.assertEqual(rep["score"], round(42 * 100 / 45, 1))
        self.assertEqual(rep["methodology"], A.METHODOLOGY)
        current = L.assess_mcp(mcp_row(), now=NOW)
        self.assertEqual(L.apply_age_decay(current, NOW)["score"], current["score"])

    def test_untagged_tools_cap(self):
        tools = [{"name": "a", "description": "d"}, {"name": "b"}, {"name": "c", "description": ""}]
        rep = L.assess_mcp(mcp_row(tools_json=json.dumps(tools)), now=NOW)
        self.assertEqual(by_key(rep)["skills"]["points"], 8)


class X402Readiness(unittest.TestCase):
    def test_full(self):
        rep = L.assess_x402(x402_row(), uptime_ok=9, uptime_total=9,
                            last_402_at=NOW - DAY, sources=["cdp-bazaar"], now=NOW)
        k = by_key(rep)
        self.assertEqual(rep["applicable_max"], 80)
        self.assertEqual(k["live"]["state"], "live")
        self.assertEqual(k["protocol_version"]["points"], 10)
        self.assertEqual(k["identity"]["points"], 5)
        self.assertEqual(rep["payment_evidence"]["grade"], "x402_challenge")
        self.assertEqual([e["grade"] for e in rep["payment_evidence"]["evidence"]],
                         ["x402_challenge", "x402_manifest", "bazaar_feed"])
        # 10+25+10+15+10+5 = 75 of 80
        self.assertEqual(rep["score"], round(75 * 100 / 80, 1))

    def test_unknown_version_is_na_and_findings(self):
        rep = L.assess_x402(x402_row(resource_samples=None, resource_count=None,
                                     payment=json.dumps({"network": "eip155:84532"}),
                                     http_status=200),
                            now=NOW)
        k = by_key(rep)
        self.assertFalse(k["protocol_version"]["applicable"])
        self.assertEqual(rep["applicable_max"], 70)
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
                       uptime_ok=1, uptime_total=1, owner_verified=False,
                       x402_supported=False, now=NOW, sources={"agenstry"})
        self.assertEqual(rep["responsiveness"]["band"], "slow")
        self.assertEqual(rep["payment_evidence"]["grade"], "agent_card")
        valid = by_key(rep)["valid_card"]
        self.assertIn("missing skills[]", valid["findings"])


if __name__ == "__main__":
    unittest.main()
