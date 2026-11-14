"""LLM analyst behind our A2A agent: tool loop, number check, memory, cache, limits, errors."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a, a2a_agent as ag  # noqa: E402


def _resp(content=None, calls=None, tin=100, tout=20):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": tin, "completion_tokens": tout,
                                                      "total_cost": 0.01}}


def _final(answer, summary="s", clarify=None):
    if clarify:
        return _resp(f"CLARIFY: {clarify}")
    return _resp(f"{answer}\nSUMMARY: {summary}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [mock.patch.object(ag, "_state_path", return_value=os.path.join(self.tmp.name, "s.db")),
                  mock.patch.object(ag, "configured", return_value=True),
                  mock.patch.object(ag, "_snap", return_value={"as_of": "2026-11-14 01:00 UTC"}),
                  mock.patch.dict(os.environ, {"AGENT_TOOLS_A2A_DAILY_CALLS": "1000"})]
        for p in self.p:
            p.start()
        self.tool = mock.patch.object(ag, "run_tool", side_effect=lambda n, a: {"reachable": 53642,
                                                                                 "indexed": 62428})
        self.tool.start()

    def tearDown(self):
        self.tool.stop()
        for p in self.p:
            p.stop()
        self.tmp.cleanup()


class LoopTests(Base):
    def test_tool_loop_and_citations(self):
        seq = [_resp(calls=[("kpis", {"view": "mcp"})]),
               _final("53,642 of 62,428 MCP servers are reachable (about 86%) [1].")]
        with mock.patch.object(ag, "_chat", side_effect=seq) as chat:
            out = ag.run("how many MCP servers work?", context_id="c1", ip="1.1.1.1")
        self.assertIn("53,642", out["text"])
        self.assertEqual(out["data"]["sources"][0]["tool"], "kpis")
        self.assertNotIn("unverified_numbers", out["data"])
        self.assertNotIn("usage", out["data"])          # no token/cost data, no model name
        self.assertEqual(chat.call_count, 2)
        # tool result went back to the model with its index
        msgs = chat.call_args_list[1][0][0]
        self.assertTrue(msgs[-1]["content"].startswith("[1] "))

    def test_unsupported_number_triggers_one_repair(self):
        seq = [_resp(calls=[("kpis", {"view": "mcp"})]), _final("There are 99,999 MCP servers [1]."),
               _final("There are 62,428 MCP servers [1].")]
        with mock.patch.object(ag, "_chat", side_effect=seq) as chat:
            out = ag.run("how many MCP servers?", ip="1.1.1.2")
        self.assertEqual(chat.call_count, 3)
        self.assertIn("62,428", out["text"])
        self.assertNotIn("unverified_numbers", out["data"])

    def test_anonymous_memory_uses_the_id_we_issue(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("hi", summary="goal: MCP growth")]):
            first = ag.run("hello", context_id="ctx", ip="1.1.1.3")      # caller-chosen id: replaced
        cid = first["context_id"]
        self.assertRegex(cid, r"^atc-ctx-[0-9a-f]{32}$")
        self.assertEqual(first["data"]["context"]["scope"], "issued")
        self.assertIn("note", first["data"]["context"])
        with mock.patch.object(ag, "_chat", side_effect=[_final("ok")]) as chat:
            second = ag.run("and x402?", context_id=cid, ip="1.1.1.3")
        self.assertIn("goal: MCP growth", chat.call_args[0][0][1]["content"])
        self.assertEqual((second["context_id"], second["data"]["context"]["memory"]), (cid, "continued"))

    def test_anonymous_cannot_reuse_a_common_id(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("a", summary="secret plan")]):
            ag.run("question one", context_id="ctx-1", ip="1.1.1.10")
        with mock.patch.object(ag, "_chat", side_effect=[_final("b")]) as chat:
            out = ag.run("question two", context_id="ctx-1", ip="1.1.1.11")
        self.assertNotIn("secret plan", chat.call_args[0][0][1]["content"])
        self.assertEqual(out["data"]["context"]["memory"], "new")
        self.assertNotEqual(out["context_id"], "ctx-1")

    def test_account_ids_are_scoped_per_account(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("a", summary="acct 7 plan")]):
            first = ag.run("question one", context_id="ctx-1", ip="1.1.1.12", account=7)
        self.assertEqual((first["context_id"], first["data"]["context"]["scope"]), ("ctx-1", "account"))
        with mock.patch.object(ag, "_chat", side_effect=[_final("b")]) as chat:
            ag.run("question two", context_id="ctx-1", ip="1.1.1.13", account=8)   # other account, same id
        self.assertNotIn("acct 7 plan", chat.call_args[0][0][1]["content"])
        with mock.patch.object(ag, "_chat", side_effect=[_final("c")]) as chat:
            again = ag.run("and then?", context_id="ctx-1", ip="1.1.1.14", account=7)  # same account, new IP
        self.assertIn("acct 7 plan", chat.call_args[0][0][1]["content"])
        self.assertEqual(again["data"]["context"]["memory"], "continued")

    def test_cache_hit_costs_nothing(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("answer A")]):
            first = ag.run("State of MCP?", ip="1.1.1.4")
        with mock.patch.object(ag, "_chat") as chat:
            again = ag.run("state of   mcp?", ip="1.1.1.5")
        chat.assert_not_called()
        self.assertEqual(first["text"], again["text"])
        self.assertNotEqual(first["context_id"], again["context_id"])   # each caller gets its own conversation

    def test_llm_error_is_an_error(self):
        with mock.patch.object(ag, "_chat", side_effect=RuntimeError("down")):
            out = ag.run("anything", ip="1.1.1.6")
        self.assertEqual(out["error"][2], "ANALYST_UNAVAILABLE")
        self.assertEqual(out["error"][3]["retryAfterSeconds"], 60)

    def test_last_turn_has_no_tools(self):
        seq = [_resp(calls=[("kpis", {"view": "all"})])] * ag.MAX_TURNS + [_final("done")]
        with mock.patch.object(ag, "_chat", side_effect=seq) as chat:
            ag.run("loop forever", ip="1.1.1.7")
        self.assertFalse(chat.call_args_list[-1].kwargs.get("tools", True))

    def test_hourly_limit_for_reports(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final(f"r{i}") for i in range(5)]):
            outs = [ag.run(f"write a report on topic {i}", skill="research_report", ip="9.9.9.9")
                    for i in range(4)]
        self.assertEqual(["error" in o for o in outs], [False, False, False, True])
        code, msg, reason, meta = outs[-1]["error"]
        self.assertEqual((code, reason, meta["limitPerHour"]), (-32029, "RATE_LIMITED", 3))
        self.assertTrue(1 <= meta["retryAfterSeconds"] <= 3600)

    def test_daily_limit(self):
        with mock.patch.dict(os.environ, {"AGENT_TOOLS_A2A_DAILY_CALLS": "0"}):
            out = ag.run("anything", ip="1.1.1.8")
        self.assertEqual(out["error"][2], "DAILY_LIMIT_REACHED")
        self.assertEqual(out["error"][3]["limitPerDay"], 0)
        self.assertNotIn("subscri", out["error"][1].lower())

    def test_daily_limit_is_shared_and_cache_stays_free(self):
        with mock.patch.dict(os.environ, {"AGENT_TOOLS_A2A_DAILY_CALLS": "2"}), \
                mock.patch.object(ag, "_chat", side_effect=[_final("a"), _final("b"), _final("c")]):
            first = ag.run("question one", ip="5.5.5.1")
            ag.run("question two", ip="5.5.5.2")
            third = ag.run("question three", ip="5.5.5.3")      # another client, cap is shared
            again = ag.run("question one", ip="5.5.5.4")        # cached: served past the cap
        self.assertEqual(third["error"][2], "DAILY_LIMIT_REACHED")
        self.assertEqual(again["text"], first["text"])


class EndpointTests(Base):
    def test_reply_carries_context_id(self):
        req = {"jsonrpc": "2.0", "id": 9, "method": "SendMessage", "params": {"message": {
            "role": "ROLE_USER", "messageId": "m", "contextId": "mine", "parts": [{"text": "how is the market?"}]}}}
        with mock.patch.object(ag, "_chat", side_effect=[_final("x"), _final("y")]):
            anon = a2a.handle_jsonrpc(req, "1.0", "4.4.4.1")
            with mock.patch.object(ag, "account_for_key", return_value=42):
                keyed = a2a.handle_jsonrpc(req, "1.0", "4.4.4.2", "some-key")
        self.assertRegex(anon["result"]["message"]["contextId"], r"^atc-ctx-")
        self.assertEqual(keyed["result"]["message"]["contextId"], "mine")


    def test_text_goes_to_llm_and_raw_report_does_not(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("LLM answer")]):
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {
                "message": {"parts": [{"kind": "text", "text": "how is the market?"}]}}}, None, "2.2.2.2")
        self.assertEqual(res["result"]["parts"][0]["text"], "LLM answer")
        with mock.patch.object(ag, "_chat") as chat, \
                mock.patch.object(a2a.a2a_research, "respond", return_value={"text": "raw", "data": {}}):
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {
                "message": {"parts": [{"kind": "data", "data": {"skill": "trend_report"}}]}}})
        chat.assert_not_called()
        self.assertEqual(res["result"]["parts"][0]["text"], "raw")

    def test_refusal_is_a_jsonrpc_error_not_a_template(self):
        with mock.patch.dict(os.environ, {"AGENT_TOOLS_A2A_DAILY_CALLS": "0"}), \
                mock.patch.object(a2a.a2a_research, "respond") as tmpl:
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "SendMessage", "params": {
                "message": {"role": "ROLE_USER", "parts": [{"text": "how is the market?"}]}}}, "1.0", "3.3.3.3")
        tmpl.assert_not_called()
        self.assertNotIn("result", res)
        self.assertEqual(res["error"]["code"], -32029)
        self.assertEqual(res["error"]["data"][0]["reason"], "DAILY_LIMIT_REACHED")


class ParseTests(unittest.TestCase):
    def test_final_formats(self):
        self.assertEqual(ag._parse_final("A [1].\nSUMMARY: s"), {"answer": "A [1].", "clarify": None, "summary": "s"})
        self.assertEqual(ag._parse_final("CLARIFY: which month?")["clarify"], "which month?")
        self.assertEqual(ag._parse_final("truncated answer without summa")["answer"], "truncated answer without summa")


class CheckTests(unittest.TestCase):
    def test_numbers(self):
        res = [{"reachable": 53642, "indexed": 62428, "pct": 58.0}]
        self.assertEqual(ag.unsupported_numbers("53,642 of 62,428 (86%), 58% verified [1] in 2026", res), [])
        self.assertEqual(ag.unsupported_numbers("about 8,786 are down", res), [])   # difference
        self.assertEqual(ag.unsupported_numbers("71,000 servers", res), ["71,000"])

    def test_pick_skill(self):
        self.assertEqual(ag.pick_skill("audit https://agent-tools.cloud/mcp/servers/x", None), "listing_audit")
        self.assertEqual(ag.pick_skill("x402 vs MCP latency", None), "compare")
        self.assertEqual(ag.pick_skill("写一份支付工具调研报告", None), "research_report")
        self.assertEqual(ag.pick_skill("where are the gaps?", None), "market_scan")
        self.assertEqual(ag.pick_skill("hi", {"skill": "compare"}), "compare")
        self.assertEqual(ag.pick_skill("how many MCP servers?", None), "ask_analyst")


if __name__ == "__main__":
    unittest.main()
