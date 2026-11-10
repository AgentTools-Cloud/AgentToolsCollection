"""A2A 1.0 + 0.3 dual serving for agent-tools.cloud's own agent."""
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a  # noqa: E402

SERVICES = [{"slug": "acme-weather", "name": "Acme Weather", "category": "data", "chains": ["base"]}]
AGENTS = [{"slug": "acme-agent", "name": "Acme Agent", "endpoint_url": "https://acme.example/a2a"}]


@contextlib.contextmanager
def _fake_connect(read_only=True):
    yield object()


def _patched():
    return mock.patch.multiple(
        a2a.db, connect=_fake_connect,
        search=mock.Mock(return_value=SERVICES),
        search_a2a=mock.Mock(return_value=AGENTS))


def _send(method, parts, version=None, **msg):
    message = {"role": "ROLE_USER" if method == "SendMessage" else "user",
               "messageId": "m1", "parts": parts, **msg}
    with _patched():
        return a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 7, "method": method,
                                   "params": {"message": message}}, version)


class CardTests(unittest.TestCase):
    def test_card_declares_v1_first_and_keeps_legacy(self):
        card = a2a.own_agent_card("https://agent-tools.cloud")
        self.assertEqual(card["supportedInterfaces"], [
            {"url": "https://agent-tools.cloud/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": "https://agent-tools.cloud/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
        ])
        self.assertEqual(card["protocolVersion"], "0.3.0")
        self.assertEqual(card["url"], "https://agent-tools.cloud/a2a")
        self.assertNotIn("stateTransitionHistory", card["capabilities"])
        self.assertIs(card["capabilities"]["extendedAgentCard"], False)
        for key in ("name", "description", "version", "capabilities",
                    "defaultInputModes", "defaultOutputModes", "skills"):
            self.assertIn(key, card)
        for skill in card["skills"]:
            self.assertTrue(skill["tags"])

    def test_own_card_parses_as_v1(self):
        card = a2a.own_agent_card()
        self.assertEqual(a2a._card_protocol_version(card), "1.0")
        self.assertEqual(a2a._card_endpoint(card, "https://agent-tools.cloud/.well-known/agent-card.json"),
                         "https://agent-tools.cloud/a2a")


class NegotiationTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(a2a.negotiate_version(None, "message/send"), "0.3")
        self.assertEqual(a2a.negotiate_version("", "SendMessage"), "1.0")
        self.assertEqual(a2a.negotiate_version("1.0", "SendMessage"), "1.0")
        self.assertEqual(a2a.negotiate_version("1.0.1", "SendMessage"), "1.0")
        self.assertEqual(a2a.negotiate_version("1", "SendMessage"), "1.0")
        self.assertEqual(a2a.negotiate_version("0.3", "message/send"), "0.3")
        self.assertEqual(a2a.negotiate_version("0.3.0", "message/send"), "0.3")
        self.assertIsNone(a2a.negotiate_version("0.5", "SendMessage"))
        self.assertIsNone(a2a.negotiate_version("2.0", "SendMessage"))
        self.assertIsNone(a2a.negotiate_version("garbage", "SendMessage"))

    def test_unsupported_version_error(self):
        resp = _send("SendMessage", [{"text": "weather"}], "0.5")
        self.assertEqual(resp["error"]["code"], -32009)
        info = resp["error"]["data"][0]
        self.assertEqual(info["reason"], "VERSION_NOT_SUPPORTED")
        self.assertEqual(info["domain"], "a2a-protocol.org")
        self.assertEqual(info["metadata"]["supportedVersions"], "1.0,0.3")


class V1Tests(unittest.TestCase):
    def test_send_message_v1_shape(self):
        resp = _send("SendMessage", [{"text": "weather api"}], "1.0", contextId="ctx-1")
        self.assertEqual(resp["id"], 7)
        msg = resp["result"]["message"]
        self.assertEqual(set(resp["result"]), {"message"})
        self.assertEqual(msg["role"], "ROLE_AGENT")
        self.assertEqual(msg["contextId"], "ctx-1")
        self.assertTrue(msg["messageId"])
        self.assertNotIn("kind", msg)
        self.assertEqual(msg["parts"][0]["mediaType"], "text/plain")
        self.assertIn("Found 1 x402 service(s) and 1 A2A agent(s)", msg["parts"][0]["text"])
        data = msg["parts"][1]
        self.assertEqual(data["mediaType"], "application/json")
        self.assertEqual(data["data"]["services"][0]["slug"], "acme-weather")
        for part in msg["parts"]:
            self.assertNotIn("kind", part)

    def test_send_message_without_header_uses_v1(self):
        resp = _send("SendMessage", [{"text": "weather"}])
        self.assertEqual(resp["result"]["message"]["role"], "ROLE_AGENT")
        self.assertTrue(resp["result"]["message"]["contextId"])

    def test_v1_data_part_triggers_safety_scan(self):
        verdict = {"endpoint_url": "https://x.example/mcp", "verdict": "clean", "score": 0, "source": "stored"}
        with mock.patch("directory.safety_service.scan_endpoint", return_value=verdict) as scan:
            resp = _send("SendMessage", [{"data": {"skill": "scan_mcp_safety",
                                                    "endpoint_url": "https://x.example/mcp"}}], "1.0")
        scan.assert_called_once()
        self.assertIn("Safety verdict", resp["result"]["message"]["parts"][0]["text"])

    def test_empty_text_is_invalid_params(self):
        resp = _send("SendMessage", [{"url": "https://x/doc.pdf"}], "1.0")
        self.assertEqual(resp["error"]["code"], -32602)

    def test_missing_message(self):
        resp = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}, "1.0")
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_task_reference(self):
        resp = _send("SendMessage", [{"text": "hi"}], "1.0", taskId="t-9")
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertEqual(resp["error"]["data"][0]["reason"], "TASK_NOT_FOUND")

    def test_task_and_capability_methods(self):
        cases = {
            "GetTask": (-32001, "TASK_NOT_FOUND"),
            "CancelTask": (-32001, "TASK_NOT_FOUND"),
            "SendStreamingMessage": (-32004, "UNSUPPORTED_OPERATION"),
            "SubscribeToTask": (-32004, "UNSUPPORTED_OPERATION"),
            "GetExtendedAgentCard": (-32004, "UNSUPPORTED_OPERATION"),
            "CreateTaskPushNotificationConfig": (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED"),
            "ListTaskPushNotificationConfigs": (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED"),
        }
        for method, (code, reason) in cases.items():
            resp = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": method,
                                       "params": {"id": "t1"}}, "1.0")
            self.assertEqual(resp["error"]["code"], code, method)
            self.assertEqual(resp["error"]["data"][0]["reason"], reason, method)

    def test_list_tasks_empty(self):
        resp = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 4, "method": "ListTasks", "params": {}}, "1.0")
        self.assertEqual(resp["result"], {"tasks": [], "nextPageToken": "", "pageSize": 50, "totalSize": 0})
        bad = a2a.handle_jsonrpc({"jsonrpc": "2.0", "id": 4, "method": "ListTasks",
                                  "params": {"pageSize": 500}}, "1.0")
        self.assertEqual(bad["error"]["code"], -32602)

    def test_legacy_method_under_v1_header_not_found(self):
        resp = _send("message/send", [{"kind": "text", "text": "hi"}], "1.0")
        self.assertEqual(resp["error"]["code"], -32601)


class LegacyTests(unittest.TestCase):
    def test_message_send_unchanged(self):
        resp = _send("message/send", [{"kind": "text", "text": "weather"}])
        msg = resp["result"]
        self.assertEqual(msg["kind"], "message")
        self.assertEqual(msg["role"], "agent")
        self.assertEqual(msg["parts"][0], {"kind": "text", "text": msg["parts"][0]["text"]})
        self.assertEqual(msg["parts"][1]["kind"], "data")

    def test_message_send_explicit_03_header(self):
        resp = _send("message/send", [{"kind": "text", "text": "weather"}], "0.3")
        self.assertEqual(resp["result"]["kind"], "message")

    def test_v1_method_under_03_header_not_found(self):
        resp = _send("SendMessage", [{"text": "weather"}], "0.3")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_legacy_empty_text(self):
        resp = _send("message/send", [], None)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_invalid_request(self):
        self.assertEqual(a2a.handle_jsonrpc([], None)["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
