"""Listing pages and the public API no longer show per-listing sources ("Listed in"), 2026-11-21.

The list covered only the directories we crawl, mixed in our own discovery channels
(a2a, mcp, submission, x402) and kept directories that had since dropped the entry.
The provenance stays in listing_sources for internal use.
"""
import os
import sys
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from directory import db, routes  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402


class ListedInHidden(_TempDB):
    def setUp(self):
        super().setUp()
        now = int(time.time())
        with db.writer() as c:
            db.upsert_service(c, {
                "slug": "seller", "name": "Seller lookups", "url": "https://seller.example/v1/pay",
                "description": "Paid lookups", "source": "x402scan", "source_id": "s1",
                "health": "ok"})
            sid = c.execute("SELECT id FROM services WHERE slug='seller'").fetchone()[0]
            c.execute("INSERT INTO listing_sources(kind, listing_id, source, source_id, "
                      "first_seen, last_seen) VALUES ('x402', ?, 'a2a', "
                      "'agenstry:seller.example', ?, ?)", (sid, now, now))
            db.upsert_mcp_server(c, {
                "slug": "weather-remote", "name": "Weather remote",
                "description": "weather forecasts", "endpoint_url": "https://w.example/mcp",
                "health": "ok", "source": "mcp-registry", "source_id": "w"})
            db.upsert_a2a_agent(c, {
                "slug": "helper-a2a", "name": "Helper", "endpoint_url": "https://helper.example/a2a",
                "card_url": "https://helper.example/.well-known/agent-card.json",
                "source": "agenstry", "source_id": "helper.example"})
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.enterContext(patch.object(routes.search_terms, "record"))

    def test_pages_do_not_render_listed_in(self):
        for path in ("/services/seller", "/mcp/servers/weather-remote", "/a2a/agents/helper-a2a"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn("Listed in", r.text, path)
            self.assertNotIn("Directories this entry was found in", r.text, path)

    def test_api_does_not_return_sources(self):
        for path in ("/api/v1/services/seller", "/api/v1/mcp/servers/weather-remote",
                     "/api/v1/a2a/agents/helper-a2a"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn("sources", r.json(), path)
        found = self.client.get("/api/v1/search", params={"q": "Seller"}).json()["services"]
        self.assertEqual([s["slug"] for s in found], ["seller"])
        self.assertNotIn("sources", found[0])
        for kind in ("x402", "mcp", "a2a"):
            items = self.client.get("/api/v1/export", params={"kind": kind}).json()["items"]
            self.assertEqual(len(items), 1, kind)
            self.assertNotIn("sources", items[0], kind)

    def test_provenance_is_still_recorded(self):
        with closing(db.connect(read_only=True)) as c:
            names = {r["source"] for r in db.listing_sources(c, "x402", c.execute(
                "SELECT id FROM services WHERE slug='seller'").fetchone()[0])}
        self.assertEqual(names, {"x402scan", "a2a"})


if __name__ == "__main__":
    unittest.main()
