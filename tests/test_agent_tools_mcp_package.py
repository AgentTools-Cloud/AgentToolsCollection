"""PyPI package agent-tools-mcp: create_api_key and directory_stats tools."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-tools-mcp"))
from agent_tools_mcp import server as pkg  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class PackageToolsTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.respond = lambda req: httpx.Response(200, json={})

        def handler(req):
            self.seen.append(req)
            return self.respond(req)

        transport = httpx.MockTransport(handler)
        self.enterContext(patch.object(
            pkg, "_client", lambda: httpx.AsyncClient(transport=transport)))
        self.enterContext(patch.dict("os.environ", {"AGENT_TOOLS_API_KEY": ""}))
        server = pkg.build_server()
        self.tools = {t.name: t for t in run(server.list_tools())}
        self.server = server

    def call(self, tool, **args):
        out = run(self.server.call_tool(tool, args))
        # mcp 1.x returns (content, structured) or a content list
        if isinstance(out, tuple):
            out = out[1] if isinstance(out[1], dict) else out[0]
            if isinstance(out, dict) and set(out) == {"result"}:
                out = out["result"]
        if isinstance(out, list):
            out = json.loads(out[0].text)
        return out

    def test_tools_listed(self):
        self.assertTrue({"create_api_key", "directory_stats"} <= set(self.tools))

    def test_create_api_key(self):
        self.respond = lambda req: httpx.Response(201, json={"api_key": "atk_x", "prefix": "atk_x"})
        out = self.call("create_api_key", name="bot")
        self.assertEqual(out["api_key"], "atk_x")
        self.assertEqual(self.seen[-1].url.path, "/api/v1/keys")
        self.assertEqual(json.loads(self.seen[-1].content), {"name": "bot"})
        self.respond = lambda req: httpx.Response(429, json={"detail": {"message": "Too many"}})
        out = self.call("create_api_key")
        self.assertEqual((out["error"], out["message"]), ("rate_limited", "Too many"))

    def test_directory_stats_routes_and_key(self):
        self.respond = lambda req: httpx.Response(200, json={"ok": True})
        self.call("directory_stats", view="mcp")
        r = self.seen[-1]
        self.assertEqual((r.url.path, r.url.params.get("view"), r.url.params.get("full")),
                         ("/api/v1/stats/summary", "mcp", None))
        self.assertNotIn("authorization", r.headers)
        self.call("directory_stats", topic="Payment", api_key="atk_k")
        r = self.seen[-1]
        self.assertEqual((r.url.path, r.url.params.get("full")), ("/stats/topics/payment.json", "true"))
        self.assertEqual(r.headers["authorization"], "Bearer atk_k")
        self.call("directory_stats", month="2026-11")
        self.assertEqual(self.seen[-1].url.path, "/stats/reports/2026-11.json")


if __name__ == "__main__":
    unittest.main()
