"""Regression tests for the per-URL retirement lookup connection lifetime.

Run with unittest; all database access is confined to temporary files.
AGENT_TOOLS_CRAWLERS_FILE optionally selects a staged crawler module.
"""
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from directory import db

if os.environ.get("AGENT_TOOLS_CRAWLERS_FILE"):
    spec = importlib.util.spec_from_file_location(
        "directory._connection_regression_crawlers",
        os.environ["AGENT_TOOLS_CRAWLERS_FILE"],
    )
    crawlers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crawlers)
else:
    from directory import crawlers


class RetirementConnectionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="retirement-connections-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE probe (id INTEGER)")
        conn.close()

    def assert_closed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def check_lookup(self, result=None, error=None):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with patch.object(db, "connect", return_value=conn) as connect:
            with patch.object(db, "find_active_retirement",
                              return_value=result, side_effect=error) as find:
                actual = crawlers.url_retired("https://example.com/api")
        connect.assert_called_once_with(read_only=True)
        find.assert_called_once_with(conn, "x402", urls=("https://example.com/api",))
        self.assertEqual(actual, error is None and result is not None)
        self.assert_closed(conn)

    def test_closes_when_retired(self):
        self.check_lookup(result={"id": 1})

    def test_closes_when_not_retired(self):
        self.check_lookup()

    def test_closes_when_query_raises(self):
        self.check_lookup(error=sqlite3.OperationalError("test query failure"))

    def test_connect_failure_preserves_fallback(self):
        with patch.object(db, "connect", side_effect=sqlite3.OperationalError("test open failure")):
            self.assertFalse(crawlers.url_retired("https://example.com/api"))

    def test_repeated_lookups_do_not_rely_on_gc(self):
        connections = []
        self.addCleanup(lambda: [conn.close() for conn in connections])
        original_connect = db.connect

        def connect(*, read_only):
            conn = original_connect(str(self.path), read_only=read_only)
            connections.append(conn)  # Keep alive: GC cannot hide a leak.
            return conn

        def lookup(conn, kind, urls):
            conn.execute("SELECT 1").fetchone()
            return None

        fd_dir = Path("/proc/self/fd")
        before = len(list(fd_dir.iterdir())) if fd_dir.exists() else None
        with patch.object(db, "connect", side_effect=connect):
            with patch.object(db, "find_active_retirement", side_effect=lookup):
                for _ in range(256):
                    self.assertFalse(crawlers.url_retired("https://example.com/api"))
        if before is not None:
            self.assertLessEqual(len(list(fd_dir.iterdir())), before + 2)
        for conn in connections:
            self.assert_closed(conn)


if __name__ == "__main__":
    unittest.main(verbosity=2)