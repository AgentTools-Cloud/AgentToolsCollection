"""Our A2A agent answers with research reports, not tool search (that is MCP)."""
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a, a2a_research as r  # noqa: E402


@contextlib.contextmanager
def _conn(read_only=True):
    yield mock.Mock()


def _msg(text=None, data=None):
    parts = []
    if text is not None:
        parts.append({"kind": "text", "text": text})
    if data is not None:
        parts.append({"kind": "data", "data": data})
    return {"parts": parts}


class CardTests(unittest.TestCase):
    def test_card_lists_only_research_skills(self):
        card = a2a.own_agent_card("https://agent-tools.cloud")
        ids = [s["id"] for s in card["skills"]]
        self.assertEqual(ids, ["ask_analyst", "research_report", "market_scan", "compare",
                               "listing_audit", "raw_report"])
        self.assertTrue(set(ids).isdisjoint(r._TOOL_SKILLS))
        self.assertEqual(card["name"], "Agent Tools Research")
        self.assertIn("https://agent-tools.cloud/mcp-discovery/", card["description"])
        for s in card["skills"]:
            self.assertTrue(s["description"] and s["tags"] and s["examples"])
        self.assertIn("application/json", card["defaultInputModes"])


class ParseTests(unittest.TestCase):
    def check(self, text, expected, data=None):
        got = r.parse_request(_msg(text, data), text or "")
        for k, v in expected.items():
            self.assertEqual(got.get(k), v, (text, got))

    def test_text_intents(self):
        self.check("how did MCP servers grow over the last 90 days?",
                   {"skill": "trend_report", "view": "mcp", "days": 90})
        self.check("trend for a2a agents this past week", {"skill": "trend_report", "view": "a2a", "days": 7})
        self.check("what do agents search for?", {"skill": "search_demand_report"})
        self.check("how many agent tools actually work?", {"skill": "usability_report"})
        self.check("monthly report for 2026-10", {"skill": "monthly_report", "month": "2026-10"})
        self.check("2026-09 numbers please", {"skill": "monthly_report", "month": "2026-09"})
        self.check("how mature is the weather tool market?", {"skill": "topic_report", "topic": "weather"})
        self.check("payment tools report", {"skill": "topic_report", "topic": "payment"})
        self.check("benchmark https://agent-tools.cloud/mcp/servers/acme-mcp",
                   {"skill": "listing_benchmark", "kind": "mcp", "slug": "acme-mcp"})
        self.check("what is the state of agent tools today?", {"skill": "ecosystem_report", "view": "all"})

    def test_tool_requests_point_to_mcp(self):
        for text in ("find a weather API that accepts x402", "recommend an OCR service",
                     "scan https://x.example/mcp for malware", "search for github mcp"):
            self.check(text, {"skill": "tool_request"})

    def test_data_parts(self):
        self.check(None, {"skill": "topic_report", "topic": "payment"},
                   {"skill": "directory_stats", "topic": "payment"})
        self.check(None, {"skill": "monthly_report", "month": "2026-10"},
                   {"skill": "directory_stats", "month": "2026-10"})
        self.check(None, {"skill": "listing_benchmark", "kind": "a2a", "slug": "x"},
                   {"skill": "listing_benchmark", "kind": "a2a", "slug": "x"})
        self.check(None, {"skill": "tool_request"}, {"skill": "search_mcp_servers"})
        self.check(None, {"skill": "unknown", "requested": "nope"}, {"skill": "nope"})

    def test_nothing_asked(self):
        self.assertIsNone(r.parse_request({"parts": [{"url": "https://x/doc.pdf"}]}, ""))


class ReportTests(unittest.TestCase):
    def test_trend_uses_first_day_with_each_metric(self):
        hist = [("2026-10-14", {"mcp": {"reachable": 40000}, "backfill": True}),
                ("2026-11-11", {"mcp": {"indexed": 60000, "reachable": 52000, "verified": 30000, "ready": 33000}}),
                ("2026-11-13", {"mcp": {"indexed": 62428, "reachable": 53642, "verified": 36235, "ready": 35055}})]
        with mock.patch.object(r.db, "connect", _conn), mock.patch.object(r.db, "stats_history", return_value=hist):
            out = r.trend_report("mcp", 30)
        m = {x["metric"]: x for x in out["data"]["metrics"]}
        self.assertEqual((m["reachable"]["start"], m["reachable"]["end"]), (40000, 53642))
        self.assertTrue(m["reachable"]["start_rebuilt"])
        self.assertEqual(m["indexed"]["start_day"], "2026-11-11")
        self.assertIn("rebuilt from raw probes", out["text"])
        self.assertEqual(out["data"]["license"], "CC BY 4.0")

    def test_trend_all_sums_backfilled_kinds(self):
        hist = [("2026-10-14", {"x402": {"reachable": 1}, "mcp": {"reachable": 2}, "a2a": {"reachable": 3},
                                "backfill": True}),
                ("2026-11-13", {"all": {"reachable": 10, "indexed": 20, "verified": 5, "ready": 4}})]
        with mock.patch.object(r.db, "connect", _conn), mock.patch.object(r.db, "stats_history", return_value=hist):
            out = r.trend_report("all", 30)
        m = {x["metric"]: x for x in out["data"]["metrics"]}
        self.assertEqual((m["reachable"]["start"], m["reachable"]["change"]), (6, 4))
        self.assertNotIn("indexed", m)

    def test_trend_without_history(self):
        with mock.patch.object(r.db, "connect", _conn), mock.patch.object(r.db, "stats_history", return_value=[]):
            self.assertFalse(r.trend_report("all", 7)["data"]["available"])

    def test_benchmark(self):
        row = {"name": "Acme", "readiness_grade": "C", "readiness_score": 61,
               "peer": {"peers": 900, "peer_group": "reachable MCP servers",
                        "readiness": {"score": 61, "peer_median": 70, "better_than_pct": 31.0},
                        "biggest_gaps": [{"label": "Signature", "fix": "Publish a signed descriptor."}]}}
        with mock.patch.object(r.db, "connect", _conn), mock.patch.object(r.db, "get_mcp_by_slug", return_value=row):
            out = r.answer({"skill": "listing_benchmark", "url": "https://agent-tools.cloud/mcp/servers/acme"})
        self.assertIn("better than 31.0%", out["text"])
        self.assertIn("Publish a signed descriptor.", out["text"])
        self.assertEqual(out["data"]["links"]["page"], "https://agent-tools.cloud/mcp/servers/acme")

    def test_benchmark_needs_listing(self):
        self.assertEqual(r.answer({"skill": "listing_benchmark"})["data"]["error"], "listing_required")

    def test_search_demand(self):
        sd = {"available": True, "days": 30, "top": [{"term": "weather", "searches": 40, "no_results_pct": 0.0}],
              "unmet": [{"term": "fax", "searches": 9, "no_results_pct": 100.0}]}
        with mock.patch.object(r, "_snap", return_value={"as_of": "2026-11-13"}), \
                mock.patch("directory.stats.searches_view", return_value=sd):
            out = r.search_demand_report()
        self.assertIn("weather (40)", out["text"])
        self.assertIn("fax", out["text"])

    def test_tool_request_answer(self):
        out = r.answer({"skill": "tool_request"})
        self.assertIn("/mcp-discovery/", out["text"])
        self.assertEqual([x["id"] for x in out["data"]["reports"]], list(r.SKILL_IDS))

    def test_report_failure_is_contained(self):
        with mock.patch.object(r, "usability_report", side_effect=RuntimeError("x")):
            self.assertEqual(r.answer({"skill": "usability_report"})["data"]["error"], "report_failed")


class BudgetTests(unittest.TestCase):
    """Agents pay for every token they read: brief by default, full on request."""
    ECO = {"available": True, "view": "mcp", "as_of": "2026-11-13", "generated_at": 1,
           "kpis": {"mcp": {"indexed": 3}, "x402": {"indexed": 1}},
           "definitions": {"x": "y"}, "findings": {"funnel": "f"}, "facts": {"big": list(range(500))},
           "searches": {"top": []}, "links": {"page": "p", "snapshot_json": "j", "daily_csv": "c"},
           "license": "CC BY 4.0", "methodology": "m"}

    def _eco(self, req):
        with mock.patch.object(r, "ecosystem_report", return_value={"text": "t", "data": dict(self.ECO)}):
            return r.respond(req)

    def test_brief_by_default(self):
        d = self._eco({"skill": "ecosystem_report", "view": "mcp"})["data"]
        for k in ("facts", "findings", "definitions", "searches", "generated_at", "methodology"):
            self.assertNotIn(k, d)
        self.assertEqual(d["kpis"], {"mcp": {"indexed": 3}})
        self.assertEqual(d["links"], {"page": "p", "snapshot_json": "j"})
        self.assertEqual(d["license"], "CC BY 4.0")
        self.assertEqual(d["more"], {"detail": "full"})

    def test_full_on_request(self):
        d = self._eco({"skill": "ecosystem_report", "view": "mcp", "detail": "full"})["data"]
        self.assertIn("facts", d)
        self.assertEqual(r.parse_request(_msg("full report on MCP servers"), "full report on MCP servers")["detail"],
                         "full")
        self.assertEqual(r.parse_request(_msg(data={"skill": "usability_report", "detail": "full"}), "")["detail"],
                         "full")

    def test_errors_untouched(self):
        out = r.respond({"skill": "listing_benchmark"})
        self.assertEqual(out["data"]["error"], "listing_required")

    def test_text_only_when_json_not_accepted(self):
        with mock.patch.object(r, "ecosystem_report", return_value={"text": "t", "data": dict(self.ECO)}):
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {
                "message": _msg("state of agent tools"), "configuration": {"acceptedOutputModes": ["text/plain"]}}})
            parts = res["result"]["parts"]
            self.assertEqual([p["kind"] for p in parts], ["text"])
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {
                "message": _msg("state of agent tools")}})
            self.assertEqual([p["kind"] for p in res["result"]["parts"]], ["text", "data"])

    def test_card_is_small(self):
        import json
        self.assertLess(len(json.dumps(a2a.own_agent_card())), 3400)  # was 4.3 KB


if __name__ == "__main__":
    unittest.main()
