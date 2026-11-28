"""First-hand fields from MCP handshakes, and the change-history panel."""

import json
from contextlib import closing
from unittest.mock import patch

from directory import db, jobs, routes
from test_ingestion_pipeline import TemporaryDatabaseTestCase


def _probe(**info):
    return {"status": "ok", "tool_count": 3, "protocol_version": "2025-06-18",
            "server_info": info}


class McpOfficialTests(TemporaryDatabaseTestCase):
    def add(self, **extra):
        row = {"slug": "weather", "name": "weather-mcp", "description": "Directory copy.",
               "endpoint_url": "https://mcp.weather.example/mcp", "source": "smithery",
               "source_id": "weather"}
        row.update(extra)
        with db.writer() as c:
            _, sid = db.upsert_mcp_server(c, row)
        return sid

    def row(self, sid):
        with closing(db.connect(read_only=True)) as c:
            return dict(c.execute("SELECT * FROM mcp_servers WHERE id=?", (sid,)).fetchone())

    def test_only_stated_fields_are_taken(self):
        self.assertEqual(jobs._mcp_official({"status": "down"}), {})
        got = jobs._mcp_official(_probe(name="weather", title="Weather Now",
                                        websiteUrl="https://weather.example"))
        self.assertEqual(got, {"name": "Weather Now", "homepage_url": "https://weather.example",
                               "tool_count": 3, "protocol_version": "2025-06-18"})
        self.assertNotIn("name", jobs._mcp_official(_probe(title="MCP Server")))
        self.assertNotIn("homepage_url", jobs._mcp_official(_probe(websiteUrl="javascript:x")))

    def test_server_statement_wins_and_is_marked_official(self):
        sid = self.add()
        off = jobs._mcp_official(_probe(title="Weather Now", description="Live forecasts."))
        with db.writer() as c:
            db.set_change_source(c, "official:mcp-probe")
            jobs._apply_mcp_official(c, [(sid, off)], 0)
        row = self.row(sid)
        self.assertEqual((row["name"], row["description"]), ("Weather Now", "Live forecasts."))
        self.assertTrue({"name", "description", "tool_count"} <= set(json.loads(row["official_fields"])))
        # A later directory crawl does not overwrite what the server stated.
        self.add(name="weather-mcp", description="Directory copy again.")
        row = self.row(sid)
        self.assertEqual((row["name"], row["description"]), ("Weather Now", "Live forecasts."))

    def test_owner_edits_beat_the_handshake(self):
        sid = self.add()
        with db.writer() as c:
            c.execute("UPDATE mcp_servers SET owner_edited=?, description='Owner text.' WHERE id=?",
                      (json.dumps(["description"]), sid))
            jobs._apply_mcp_official(c, [(sid, jobs._mcp_official(
                _probe(title="Weather Now", description="Live forecasts.")))], 0)
        row = self.row(sid)
        self.assertEqual((row["name"], row["description"]), ("Weather Now", "Owner text."))
        self.assertNotIn("description", json.loads(row["official_fields"]))

    def test_panel_shows_field_and_source_but_not_values(self):
        sid = self.add()
        with db.writer() as c:
            db.set_change_source(c, "official:mcp-probe")
            c.execute("UPDATE mcp_servers SET description='secret-new-text' WHERE id=?", (sid,))
        with patch.object(routes, "_conn", side_effect=lambda: db.connect(read_only=True)):
            changes = routes._listing_changes("mcp", sid)
        self.assertEqual(changes[0]["field"], "description")
        self.assertEqual(changes[0]["source"], "the server's MCP handshake")
        self.assertEqual(changes[-1]["field"], "listed")
        self.assertEqual(changes[-1]["source"], "directory: smithery")
        html = routes.TEMPLATES.get_template("_changes.html").render(changes=changes)
        self.assertIn("Change history", html)
        self.assertNotIn("secret-new-text", html)
        self.assertNotIn("Directory copy", html)
        self.assertEqual(routes.TEMPLATES.get_template("_changes.html").render(changes=[]).strip(), "")


if __name__ == "__main__":
    import unittest
    unittest.main()
