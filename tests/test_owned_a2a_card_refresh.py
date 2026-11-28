"""Owner-verified A2A listings follow their own live Agent Card (offline)."""

import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from directory import db, jobs

CARD_URL = "https://agent.example/.well-known/agent-card.json"
ENDPOINT = "https://agent.example/a2a"


def _card(**over):
    card = {
        "name": "Support Agent",
        "description": "New card description.",
        "version": "2.0.0",
        "documentationUrl": "https://agent.example/docs",
        "provider": {"organization": "Example", "url": "https://agent.example"},
        "supportedInterfaces": [{"url": ENDPOINT, "protocolBinding": "JSONRPC",
                                 "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False},
        "skills": [{"id": "ask", "name": "Ask a question", "description": "Ask.", "tags": ["faq"]},
                   {"id": "plan", "name": "Deployment plan", "description": "Plan.", "tags": []}],
    }
    card.update(over)
    return card


class OwnedCardRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="owned-card-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "test.db")
        for p in (patch.dict(os.environ, {"AGENT_TOOLS_DB_PATH": self.path,
                                          "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret"}),
                  patch.object(db, "DEFAULT_DB_PATH", self.path)):
            p.start()
            self.addCleanup(p.stop)
        # connect()/writer() bind DEFAULT_DB_PATH at import time; force every
        # open onto the temporary file so no test can reach a real database.
        real_connect, real_writer = db.connect, db.writer

        def connect(db_path=None, read_only=False, set_wal=True, *, timeout=30.0):
            return real_connect(self.path, read_only=read_only, set_wal=set_wal, timeout=timeout)

        def writer(db_path=None, immediate=False, *, timeout=None, priority=None):
            return real_writer(self.path, immediate=immediate, timeout=timeout, priority=priority)

        for p in (patch.object(db, "connect", side_effect=connect),
                  patch.object(db, "writer", side_effect=writer)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db(self.path)
        with db.writer() as c:
            _, self.id = db.upsert_a2a_agent(c, {
                "slug": "support-agent", "name": "Support Agent", "description": "Owner text.",
                "card_url": CARD_URL, "endpoint_url": ENDPOINT, "source": "submission",
                "source_id": CARD_URL,
                "skills": [{"id": "plan", "name": "Old plan", "description": "Old."}],
            })
            c.execute("UPDATE a2a_agents SET owner_verified=1, owner_edited=? WHERE id=?",
                      (json.dumps(["description"]), self.id))

    def row(self):
        with closing(db.connect(read_only=True)) as c:
            return dict(c.execute("SELECT * FROM a2a_agents WHERE id=?", (self.id,)).fetchone())

    def run_health(self, card):
        probe = {"status": "ok", "latency_ms": 10, "http_status": 200, "conformance": "pass",
                 "card_url": CARD_URL, "card": card, "callable": True, "endpoint": ENDPOINT}
        with patch.object(jobs.a2a_mod, "probe_a2a_health", return_value=probe), \
                patch.object(jobs.a2a_readiness, "probe_jsonrpc",
                             return_value={"state": "auth_required", "latency_ms": 5,
                                           "http_status": 401, "detail": ""}), \
                patch.object(jobs.public_http, "client", MagicMock()), \
                patch.object(jobs, "_maintain_down_since", create=True):
            jobs.cmd_health_a2a()

    def test_skills_follow_card_but_owner_edits_stay(self):
        self.run_health(_card())
        row = self.row()
        self.assertEqual([s["name"] for s in json.loads(row["skills"])],
                         ["Ask a question", "Deployment plan"])
        self.assertIn("faq", row["skill_names"])
        self.assertEqual(row["documentation_url"], "https://agent.example/docs")
        self.assertEqual(row["description"], "Owner text.")  # hand-edited by the owner
        self.assertEqual(row["endpoint_url"], ENDPOINT)
        self.assertEqual(row["slug"], "support-agent")

    def test_card_that_moved_endpoint_changes_nothing(self):
        before = self.row()
        moved = _card(supportedInterfaces=[{"url": "https://other.example/a2a"}])
        self.run_health(moved)
        after = self.row()
        for field in ("skills", "skill_names", "documentation_url", "endpoint_url"):
            self.assertEqual(after[field], before[field])

    def test_unverified_listings_are_not_touched_by_health(self):
        with db.writer() as c:
            c.execute("UPDATE a2a_agents SET owner_verified=0 WHERE id=?", (self.id,))
        before = self.row()
        self.run_health(_card())
        self.assertEqual(self.row()["skills"], before["skills"])

    def test_missing_card_name_keeps_stored_name(self):
        card = _card()
        card.pop("name")
        self.run_health(card)
        self.assertEqual(self.row()["name"], "Support Agent")


if __name__ == "__main__":
    unittest.main()
