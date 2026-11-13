"""Detail pages outside the sitemap set ask search engines not to index them."""
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from starlette.requests import Request

from directory import routes


def _request(accept="text/html"):
    return Request({"type": "http", "method": "GET", "path": "/mcp/servers/glama-x",
                    "headers": [(b"accept", accept.encode())], "query_string": b"",
                    "server": ("testserver", 80), "scheme": "http", "root_path": "",
                    "app": None})


class DetailNoindexTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:", check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        cols = "id INTEGER PRIMARY KEY, health TEXT, readiness_grade TEXT, quality_score REAL"
        self.c.executescript(
            f"CREATE TABLE services({cols}, url TEXT);"
            f"CREATE TABLE mcp_servers({cols}, endpoint_url TEXT);"
            f"CREATE TABLE a2a_agents({cols}, endpoint_url TEXT, card_url TEXT);")
        self.c.executemany(
            "INSERT INTO mcp_servers(id, health, readiness_grade, quality_score, endpoint_url) "
            "VALUES(?,?,?,?,?)", [
                (1, "ok", "A", 90, "https://a.example/mcp"),
                (2, "ok", "B", 80, "https://a.example/mcp/"),   # same endpoint, lower score
                (3, "ok", "C", 99, "https://c.example/mcp"),
                (4, "down", "A", 99, "https://d.example/mcp"),
                (5, "ok", "B", 70, "https://e.example/mcp"),
                (6, "ok", "B", 70, "https://e.example/mcp"),    # tie: lower id wins
                (7, "ok", "A", 60, None),
            ])
        self.c.executemany(
            "INSERT INTO a2a_agents(id, health, readiness_grade, quality_score, endpoint_url, card_url) "
            "VALUES(?,?,?,?,?,?)", [
                (1, "ok", "A", 90, None, "https://g.example/card"),
                (2, "ok", "A", 50, None, "https://g.example/card"),
            ])

        @contextmanager
        def conn():
            yield self.c
        self.patch = patch.object(routes, "_conn", conn)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_only_the_sitemap_set_is_indexable(self):
        expected = {1: False, 2: True, 3: True, 4: True, 5: False, 6: True, 7: False, 99: True}
        for listing_id, noindex in expected.items():
            self.assertEqual(routes._detail_noindex("mcp", listing_id), noindex, listing_id)
        self.assertFalse(routes._detail_noindex("a2a", 1))
        self.assertTrue(routes._detail_noindex("a2a", 2))

    def test_purged_source_slugs_are_gone(self):
        self.assertTrue("glama-foo-glama-ai".startswith(routes._PURGED_MCP_PREFIXES))
        html = routes._detail_gone(_request(), "mcp server not found")
        self.assertEqual(html.status_code, 410)
        self.assertIn("noindex", html.headers["x-robots-tag"])
        api = routes._detail_gone(_request("application/json"), "mcp server not found")
        self.assertEqual((api.status_code, api.body), (410, b'{"detail":"mcp server not found"}'))

    def test_base_template_honours_noindex(self):
        source = routes.TEMPLATES.env.loader.get_source(routes.TEMPLATES.env, "base.html")[0]
        self.assertIn("'noindex,follow' if noindex else 'index,follow'", source)


if __name__ == "__main__":
    unittest.main()
