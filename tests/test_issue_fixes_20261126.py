"""Issue fixes: x402 v2 payment header, MCP auth from the anonymous probe, /bot page, MCP connect snippet."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import httpx

from directory import cards, crawlers, public_http, resources

ROOT = Path(__file__).resolve().parents[1]


class X402PaymentHeaderTests(unittest.TestCase):
    def flow(self, version):
        return cards.build_call_template({"url": "https://example.test/pay", "x402_version": version})[
            "http_x402"]["flow"]

    def test_v2_card_retries_with_payment_signature(self):
        flow = self.flow(2)
        self.assertIn("PAYMENT-REQUIRED", flow[0])
        self.assertIn("`PAYMENT-SIGNATURE: <base64 payload>`", flow[3])
        self.assertNotIn("X-PAYMENT", " ".join(flow))

    def test_v1_card_keeps_x_payment(self):
        flow = self.flow(1)
        self.assertIn("`X-PAYMENT: <base64 payload>`", flow[3])
        self.assertNotIn("PAYMENT-SIGNATURE", " ".join(flow))

    def test_unknown_version_names_both_headers(self):
        step = self.flow(None)[3]
        self.assertIn("PAYMENT-SIGNATURE", step)
        self.assertIn("X-PAYMENT", step)

    def test_header_helper_tolerates_text_versions(self):
        self.assertEqual(cards.x402_payment_header({"x402_version": "2"}), "PAYMENT-SIGNATURE")
        self.assertIsNone(cards.x402_payment_header({"x402_version": "v2"}))


class McpAuthFromProbeTests(unittest.TestCase):
    def access(self, **row):
        base = {"slug": "s", "name": "n", "endpoint_url": "https://mcp.example.test/mcp"}
        return resources.normalize_mcp_server({**base, **row})

    def test_401_with_oauth_metadata_is_not_open(self):
        item = self.access(auth_method=None, http_status=401,
                           auth_discovery={"server_metadata": True, "resource_metadata": True})
        self.assertEqual(item["access"]["model"], "human_key")
        self.assertEqual(item["access"]["auth"], "oauth")
        self.assertEqual(item["call_hint"]["mcp"]["auth"], "oauth")

    def test_raw_row_json_and_403_without_metadata(self):
        self.assertEqual(self.access(http_status=401, auth_discovery_json='{"server_metadata": true}')[
            "access"]["auth"], "oauth")
        item = self.access(auth_method="", http_status=403)
        self.assertEqual((item["access"]["model"], item["access"]["auth"]), ("human_key", "required"))

    def test_open_probe_and_owner_declaration_are_kept(self):
        self.assertEqual(self.access(auth_method=None, http_status=200)["access"]["model"], "open")
        self.assertEqual(self.access(auth_method="none", http_status=401)["access"]["model"], "open")
        self.assertEqual(self.access(auth_method="api_key", http_status=401)["access"]["auth"], "api_key")


class BotPageTests(unittest.TestCase):
    def test_bot_page_lists_the_crawler_user_agent(self):
        tree = ast.parse((ROOT / "directory/routes.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "bot_page")
        templates = Jinja2Templates(directory=str(ROOT / "directory/templates"))
        templates.env.globals.update(build_version="test", current_user=lambda r: None)
        ns = {"__name__": "directory.routes", "__package__": "directory", "router": APIRouter(),
              "Request": Request, "HTMLResponse": HTMLResponse, "TEMPLATES": templates}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "directory/routes.py"), "exec"), ns)
        app = FastAPI()
        app.include_router(ns["router"])
        resp = TestClient(app).get("/bot")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(crawlers.UA, resp.text)
        self.assertTrue(crawlers.UA.endswith("(+https://agent-tools.cloud/bot)"))


class McpConnectSnippetTests(unittest.TestCase):
    def test_initialize_snippet_has_required_params(self):
        html = (ROOT / "directory/templates/mcp_server.html").read_text()
        self.assertNotIn('"params":{}}', html)
        for key in ('"protocolVersion"', '"capabilities"', '"clientInfo"'):
            self.assertIn(key, html)


class McpModernRevisionTests(unittest.TestCase):
    """A 2026-07-28 server rejects initialize for that revision but answers server/discover."""

    def probe(self, modern: bool):
        seen = []

        def handle(request):
            body = json.loads(request.content)
            seen.append((body["method"], request.headers.get("mcp-method")))
            if body["method"] == "initialize":
                if modern:
                    return httpx.Response(200, request=request, json={"jsonrpc": "2.0", "id": 1, "error": {
                        "code": -32022, "message": "not available through the initialize handshake"}})
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}})
            if body["method"] == "server/discover":
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 3, "result": {"supportedVersions": ["2026-07-28", "bogus"]}})
            if body["method"] == "tools/list":
                if modern and request.headers.get("mcp-method") != "tools/list":
                    return httpx.Response(400, request=request)
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "a"}, {"name": "b"}]}})
            return httpx.Response(202, request=request)

        def factory(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

        with patch.object(public_http, "client", side_effect=factory):
            return crawlers.probe_mcp_health("https://mcp.example.test/mcp"), seen

    def test_modern_server_reports_discovered_revision_and_tools(self):
        res, seen = self.probe(modern=True)
        self.assertEqual(res["protocol_version"], "2026-07-28")
        self.assertEqual((res["conformance"], res["tool_count"]), ("pass", 2))
        self.assertIn(("server/discover", "server/discover"), seen)
        self.assertNotIn("notifications/initialized", [m for m, _ in seen])

    def test_legacy_server_keeps_the_initialize_path(self):
        res, seen = self.probe(modern=False)
        self.assertEqual((res["protocol_version"], res["tool_count"]), ("2025-06-18", 2))
        self.assertNotIn("server/discover", [m for m, _ in seen])


if __name__ == "__main__":
    unittest.main()
