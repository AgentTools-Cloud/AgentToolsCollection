"""Sitemap index: every reachable, readiness A/B listing, one URL per endpoint."""
import re
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import HTTPException

from directory import routes


class SitemapIndexTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:", check_same_thread=False)
        cols = "id INTEGER PRIMARY KEY, slug TEXT, updated_at INT, health TEXT, readiness_grade TEXT, quality_score REAL"
        self.c.executescript(
            f"CREATE TABLE services({cols}, url TEXT);"
            f"CREATE TABLE mcp_servers({cols}, endpoint_url TEXT);"
            f"CREATE TABLE a2a_agents({cols}, endpoint_url TEXT, card_url TEXT);")
        rows = [  # slug, health, grade, quality, url
            ("ok-a", "ok", "A", 80, "https://a.example/x"),
            ("ok-b", "ok", "B", 60, "https://b.example/x"),
            ("ok-c", "ok", "C", 90, "https://c.example/x"),
            ("down-a", "down", "A", 90, "https://d.example/x"),
            ("dup-low", "ok", "A", 50, "https://e.example/x/"),
            ("dup-high", "ok", "A", 70, "HTTPS://E.example/x"),
            ("amp&slug", "ok", "A", 10, "https://f.example/x"),
        ]
        self.c.executemany(
            "INSERT INTO services(slug, updated_at, health, readiness_grade, quality_score, url) "
            "VALUES(?, 1794441600, ?, ?, ?, ?)", rows)
        self.c.execute("INSERT INTO a2a_agents(slug, health, readiness_grade, card_url) "
                       "VALUES('agent', 'ok', 'B', 'https://g.example/card')")

        @contextmanager
        def conn():
            yield self.c
        self.patch = patch.object(routes, "_conn", conn)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def locs(self, body):
        return re.findall(r"<loc>([^<]+)</loc>", body)

    def test_only_reachable_ready_listings_one_per_endpoint(self):
        body = routes.sitemap_listings_xml("x402-1").body.decode()
        slugs = {u.rsplit("/", 1)[1] for u in self.locs(body)}
        self.assertEqual(slugs, {"ok-a", "ok-b", "dup-high", "amp&amp;slug"})
        self.assertIn("<lastmod>2026-11-12</lastmod>", body)
        self.assertIn("https://agent-tools.cloud/a2a/agents/agent",
                      routes.sitemap_listings_xml("a2a-1").body.decode())

    def test_index_lists_pages_and_one_file_per_chunk(self):
        with patch.object(routes, "SITEMAP_CHUNK", 2):
            locs = self.locs(routes.sitemap_xml(None).body.decode())
            self.assertEqual([u.rsplit("/", 1)[1] for u in locs],
                             ["pages.xml", "x402-1.xml", "x402-2.xml", "mcp-1.xml", "a2a-1.xml"])
            second = self.locs(routes.sitemap_listings_xml("x402-2").body.decode())
            self.assertEqual(len(second), 2)
            with self.assertRaises(HTTPException):
                routes.sitemap_listings_xml("x402-3")
        self.assertEqual(self.locs(routes.sitemap_listings_xml("mcp-1").body.decode()), [])
        for bad in ("pages-1", "x402-0", "x402-a", "services-1"):
            with self.assertRaises(HTTPException):
                routes.sitemap_listings_xml(bad)


if __name__ == "__main__":
    unittest.main()
