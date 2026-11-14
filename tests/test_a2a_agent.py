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

    def test_report_skills_get_longer_time(self):
        seen = []
        def fake_chat(messages, max_tokens, tools=True, deadline=None):
            seen.append(deadline)
            return _final("ok")
        import time as _t
        with mock.patch.object(ag, "_chat", side_effect=fake_chat):
            t0 = _t.time(); ag.run("q1", skill="research_report", ip="7.7.7.1")
            t1 = _t.time(); ag.run("q2", skill="ask_analyst", ip="7.7.7.2")
        self.assertGreater(seen[0] - t0, 80)   # 90 s: under Cloudflare's 100 s cut-off
        self.assertLess(seen[1] - t1, 60)

    def test_more_than_six_tool_calls_are_trimmed_consistently(self):
        many = [("kpis", {"view": "all"})] * 8
        seq = [_resp(calls=many), _final("done")]
        with mock.patch.object(ag, "_chat", side_effect=seq) as chat:
            out = ag.run("q many", ip="8.8.8.1")
        msgs = chat.call_args_list[1][0][0]
        asst = [m for m in msgs if m.get("tool_calls")][0]
        tools = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual(len(asst["tool_calls"]), 6)
        self.assertEqual(len(tools), 6)
        self.assertEqual(out["text"], "done")

    def test_pending_runs_count_against_limits(self):
        # a run admitted but not finished (still with the model) already uses a slot
        with mock.patch.dict(os.environ, {"AGENT_TOOLS_A2A_DAILY_CALLS": "1"}):
            first = ag._admit("8.8.8.2", "ask_analyst")
            self.assertIsInstance(first, int)
            second = ag._admit("8.8.8.3", "ask_analyst")
        self.assertEqual(second[0], "daily_limit")

    def test_failures_count_per_ip(self):
        with mock.patch.object(ag, "_chat", side_effect=RuntimeError("down")):
            for i in range(3):
                ag.run(f"report {i}", skill="research_report", ip="8.8.8.4")
        with mock.patch.object(ag, "_chat") as chat:
            out = ag.run("report 9", skill="research_report", ip="8.8.8.4")
        chat.assert_not_called()
        self.assertEqual(out["error"][2], "RATE_LIMITED")

    def test_clarify_keeps_the_question_in_memory(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final(None, clarify="Which protocol?")]):
            first = ag.run("how many are ready?", ip="8.8.8.5")
        self.assertIn("Which protocol?", first["text"])
        self.assertTrue(first["data"]["clarify"])
        with mock.patch.object(ag, "_chat", side_effect=[_final("ok")]) as chat:
            ag.run("MCP", context_id=first["context_id"], ip="8.8.8.5")
        self.assertIn("how many are ready?", chat.call_args[0][0][1]["content"])

    def test_unverified_answers_are_not_cached(self):
        seq = [_resp(calls=[("kpis", {"view": "mcp"})]), _final("There are 777,777 servers [1]."),
               _final("There are 999,999 servers [1].")]
        with mock.patch.object(ag, "_chat", side_effect=seq):
            out = ag.run("count?", ip="8.8.8.6")
        self.assertIn("unverified_numbers", out["data"])
        with mock.patch.object(ag, "_chat", side_effect=[_final("fresh")]) as chat:
            again = ag.run("count?", ip="8.8.8.7")
        self.assertEqual(again["text"], "fresh")
        chat.assert_called_once()

    def test_summary_kept_when_model_omits_it(self):
        with mock.patch.object(ag, "_chat", side_effect=[_final("a", summary="goal: x402 prices")]):
            first = ag.run("first", ip="8.8.8.8")
        with mock.patch.object(ag, "_chat", side_effect=[_resp("no summary line here")]):
            ag.run("second", context_id=first["context_id"], ip="8.8.8.8")
        with mock.patch.object(ag, "_chat", side_effect=[_final("c")]) as chat:
            ag.run("third", context_id=first["context_id"], ip="8.8.8.8")
        self.assertIn("goal: x402 prices", chat.call_args[0][0][1]["content"])

    def test_cache_key_keeps_operators(self):
        self.assertNotEqual(ag._cache_key("ask_analyst", "price < 0.01", "d", ""),
                            ag._cache_key("ask_analyst", "price > 0.01", "d", ""))
        self.assertEqual(ag._cache_key("ask_analyst", "q", "d", "Brief"), ag._cache_key("ask_analyst", "q", "d", None))

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
    def test_raw_report_alias_skips_the_model_even_with_text(self):
        with mock.patch.object(ag, "_chat") as chat, \
                mock.patch.object(a2a.a2a_research, "respond", return_value={"text": "raw", "data": {}}):
            res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "message/send", "params": {
                "message": {"parts": [{"kind": "text", "text": "please"},
                                      {"kind": "data", "data": {"id": "trend_report"}}]}}})
        chat.assert_not_called()
        self.assertEqual(res["result"]["parts"][0]["text"], "raw")

    def test_analyst_skill_without_question_is_invalid(self):
        res = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 6, "method": "message/send", "params": {
            "message": {"parts": [{"kind": "data", "data": {"skill": "compare"}}]}}})
        self.assertEqual(res["error"]["code"], -32602)

    def test_busy_when_all_slots_taken(self):
        with mock.patch.object(ag, "_slots") as slots:
            slots.acquire.return_value = False
            out = ag.run("anything at all", ip="8.8.9.1")
        self.assertEqual(out["error"][2], "ANALYST_BUSY")

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
        # free text always goes to the analyst; only an explicit data-part skill changes it
        for q in ("audit https://agent-tools.cloud/mcp/servers/x", "x402 vs MCP latency",
                  "写一份支付工具调研报告", "where are the gaps?", "how many MCP servers?"):
            self.assertEqual(ag.pick_skill(q, None), "ask_analyst", q)
        self.assertEqual(ag.pick_skill("hi", {"skill": "compare"}), "compare")
        self.assertEqual(ag.pick_skill("hi", {"skill": "research_report"}), "research_report")
        self.assertEqual(ag.pick_skill("hi", {"skill": "nonsense"}), "ask_analyst")



if __name__ == "__main__":
    unittest.main()
