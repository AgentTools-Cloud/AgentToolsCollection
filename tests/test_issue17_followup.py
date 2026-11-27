"""Issue #17 follow-up: dual-era MCP revision and freshness from secondary sources."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import httpx

from directory import crawlers, db, jobs, public_http


class DualEraRevisionTests(unittest.TestCase):
    """initialize negotiates a legacy revision; server/discover also lists 2026-07-28."""

    def probe(self, discover_modern: bool):
        seen = []

        def handle(request):
            body = json.loads(request.content)
            seen.append(body["method"])
            if body["method"] == "initialize":
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}})
            if body["method"] == "server/discover":
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 3,
                    "result": {"supportedVersions": ["2025-11-25", "2026-07-28"]}})
            if body["method"] == "tools/list":
                return httpx.Response(200, request=request, json={
                    "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "a"}]}})
            return httpx.Response(202, request=request)

        def factory(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

        with patch.object(public_http, "client", side_effect=factory):
            res = crawlers.probe_mcp_health("https://mcp.example.test/mcp",
                                            discover_modern=discover_modern)
        return res, seen

    def test_weekly_discover_finds_the_newer_revision(self):
        res, seen = self.probe(discover_modern=True)
        self.assertEqual(res["protocol_version"], "2026-07-28")
        self.assertTrue(res["protocol_discovered"])
        # tools are still listed in the legacy session that initialize opened
        self.assertIn("notifications/initialized", seen)
        self.assertEqual((res["conformance"], res["tool_count"]), ("pass", 1))

    def test_other_days_do_not_send_discover(self):
        res, seen = self.probe(discover_modern=False)
        self.assertEqual(res["protocol_version"], "2025-11-25")
        self.assertFalse(res["protocol_discovered"])
        self.assertNotIn("server/discover", seen)

    def test_newer_stored_revision_survives_until_next_discover(self):
        legacy = {"protocol_version": "2025-11-25", "protocol_discovered": False}
        self.assertEqual(jobs._protocol_version(legacy, "2026-07-28"), "2026-07-28")
        checked = {"protocol_version": "2025-11-25", "protocol_discovered": True}
        self.assertEqual(jobs._protocol_version(checked, "2026-07-28"), "2025-11-25")
        self.assertIsNone(jobs._protocol_version({"protocol_version": None}, "2026-07-28"))
        self.assertEqual(jobs._protocol_version({"protocol_version": "2026-07-28"}, None), "2026-07-28")

    def test_discover_is_due_once_a_week_per_server(self):
        day = 86400
        due = [d for d in range(14) if jobs._discover_due(5, 1_000 * day + d * day)]
        self.assertEqual(len(due), 2)
        self.assertEqual(due[1] - due[0], 7)


class SecondarySourceFreshnessTests(unittest.TestCase):
    def test_other_directory_listing_bumps_last_seen(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE mcp_servers (id INTEGER PRIMARY KEY, last_seen INTEGER)")
        conn.execute("INSERT INTO mcp_servers VALUES (1, 100), (2, NULL)")
        db._seen_by_other_source(conn, 1, 500)
        db._seen_by_other_source(conn, 2, 500)
        db._seen_by_other_source(conn, 1, 300)  # never moves backwards
        rows = dict(conn.execute("SELECT id, last_seen FROM mcp_servers").fetchall())
        self.assertEqual(rows, {1: 500, 2: 500})


if __name__ == "__main__":
    unittest.main()
