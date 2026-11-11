"""A slash-only endpoint variant from a source keeps the healthy stored form."""
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from directory import db


def _row(endpoint):
    return {"slug": "slash-fixture", "name": "Slash fixture", "description": "d",
            "source": "fixture", "source_id": "identity-slash",
            "source_url": "https://catalog.example/slash", "endpoint_url": endpoint,
            "tags": ["t"]}


class TrailingSlashEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcp-slash-")
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "test.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret",
        }))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        db.init_db(self.path)

    def _upsert(self, endpoint):
        with closing(db.connect(self.path)) as conn:
            created, listing_id = db.upsert_mcp_server(conn, _row(endpoint))
            conn.commit()
            return created, listing_id

    def _set_health(self, listing_id, health):
        with closing(db.connect(self.path)) as conn:
            conn.execute("UPDATE mcp_servers SET health=? WHERE id=?", (health, listing_id))
            conn.commit()

    def _endpoint(self, listing_id):
        with closing(db.connect(self.path, read_only=True)) as conn:
            return conn.execute("SELECT endpoint_url FROM mcp_servers WHERE id=?",
                                (listing_id,)).fetchone()[0]

    def test_healthy_stored_form_survives_slash_variant(self):
        _, lid = self._upsert("https://api.example/mcp")
        self._set_health(lid, "ok")
        created, again = self._upsert("https://api.example/mcp/")
        self.assertFalse(created)
        self.assertEqual(again, lid)
        self.assertEqual(self._endpoint(lid), "https://api.example/mcp")

    def test_unhealthy_stored_form_takes_source_variant(self):
        _, lid = self._upsert("https://api.example/mcp/")
        self._set_health(lid, "degraded")
        self._upsert("https://api.example/mcp")
        self.assertEqual(self._endpoint(lid), "https://api.example/mcp")

    def test_real_path_change_still_applies(self):
        _, lid = self._upsert("https://api.example/mcp")
        self._set_health(lid, "ok")
        self._upsert("https://api.example/v2/mcp")
        self.assertEqual(self._endpoint(lid), "https://api.example/v2/mcp")


if __name__ == "__main__":
    unittest.main()
