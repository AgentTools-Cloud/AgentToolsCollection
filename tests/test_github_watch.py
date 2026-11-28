"""GitHub issue/PR watch -> Notice rows (offline, temporary database only)."""

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from directory import db, github_watch, notices


def _issue(number, state="open", pr=False, merged=False, title="Bug report"):
    item = {"number": number, "state": state, "title": title, "comments": 2,
            "created_at": "2026-11-28T01:00:00Z", "user": {"login": "someone"},
            "body": "secret body https://example.com/x?key=abc"}
    if pr:
        item["pull_request"] = {"merged_at": "2026-11-28T02:00:00Z" if merged else None}
    return item


class GithubWatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="github-watch-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "test.db")
        real_connect, real_writer = db.connect, db.writer

        def connect(db_path=None, read_only=False, set_wal=True, *, timeout=30.0):
            return real_connect(self.path, read_only=read_only, set_wal=set_wal, timeout=timeout)

        def writer(db_path=None, immediate=False, *, timeout=None, priority=None):
            return real_writer(self.path, immediate=immediate, timeout=timeout, priority=priority)

        for p in (patch.dict(os.environ, {"AGENT_TOOLS_DB_PATH": self.path,
                                          "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret",
                                          "AGENT_TOOLS_GITHUB_WATCH_REPOS": "Org/Repo"}),
                  patch.object(db, "DEFAULT_DB_PATH", self.path),
                  patch.object(db, "connect", side_effect=connect),
                  patch.object(db, "writer", side_effect=writer)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db(self.path)

    def client(self, items):
        response = MagicMock()
        response.json.return_value = items
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.get.return_value = response
        return client

    def feed(self):
        with closing(db.connect(read_only=True)) as conn:
            return [n for n in notices.export_feed(conn)["notices"] if n["category"] == "github"]

    def test_open_items_are_active_and_closed_ones_resolve(self):
        stats = github_watch.run(client=self.client([_issue(7), _issue(8, pr=True)]))
        self.assertEqual((stats["items"], stats["open"], stats["errors"]), (2, 2, 0))
        rows = self.feed()
        self.assertEqual(len(rows), 2)  # rolling rows only, no duplicate history entry
        self.assertTrue(all(r["status"] == "active" and r["severity"] == "warning" for r in rows))
        self.assertEqual(sorted(r["title"] for r in rows), ["GitHub 新 PR · Repo", "GitHub 新 issue · Repo"])
        self.assertIn("Org/Repo #7：Bug report", " ".join(r["summary"] for r in rows))
        self.assertNotIn("secret body", " ".join(r["summary"] for r in rows))

        github_watch.run(client=self.client([_issue(7, state="closed"),
                                             _issue(8, state="closed", pr=True, merged=True)]))
        rows = {r["data"]["number"]: r for r in self.feed()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[7]["status"], "resolved")
        self.assertIn("已关闭", rows[7]["summary"])
        self.assertIn("已合并", rows[8]["summary"])
        self.assertEqual(rows[8]["severity"], "info")
        self.assertEqual(rows[7]["title"], "GitHub issue · Repo")
        self.assertEqual(rows[8]["title"], "GitHub PR · Repo")

    def test_fetch_failure_is_reported_once_per_day(self):
        client = MagicMock()
        client.get.side_effect = RuntimeError("down")
        self.assertEqual(github_watch.run(client=client)["errors"], 1)
        github_watch.run(client=client)
        rows = self.feed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["occurrences"], 2)


if __name__ == "__main__":
    unittest.main()
