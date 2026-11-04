from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from directory import db, limits


NOW = 1700000000
SCHEMA = """
CREATE TABLE rate_limits (
    key TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX idx_rate_limits_updated ON rate_limits(updated_at);
"""


def create_primary(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def consume_process_limit(arguments):
    path, now = arguments
    with patch.object(db.time, "time", return_value=now):
        state = limits.check_ip_limits(
            "198.51.100.9", "processes", (("day", 2, 86400),), db_path=path)
    return state is None


class RateLimitIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rate-store-test-")
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "primary.db")
        create_primary(self.path)
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        self.enterContext(patch.object(db.time, "time", return_value=NOW))

    def check(self, scope="submit-mcp", limit=3, path=None):
        return limits.check_ip_limits(
            "198.51.100.1", scope, (("day", limit, 86400),),
            db_path=path or self.path)

    def seed(self, count, key="submit-mcp:day:198.51.100.1"):
        with db.writer(self.path) as conn:
            for _ in range(count):
                db.hit_rate_limit(conn, key, 100, 86400, now=NOW)

    def count(self, path, key="submit-mcp:day:198.51.100.1"):
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE key=?", (key,)).fetchone()
        return row[0] if row else 0

    def test_primary_write_lock_does_not_block_isolated_limits(self):
        limits.activate_rate_limit_store(self.path)
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.assertIsNone(self.check())
            self.assertLess(time.monotonic() - started, 1.0)
            blocker.rollback()
        self.assertEqual(self.count(self.path), 0)
        self.assertEqual(self.count(limits.rate_limit_db_path(self.path)), 1)

    def test_activation_preserves_existing_counts(self):
        self.seed(2)
        result = limits.activate_rate_limit_store(self.path)
        self.assertTrue(result["active"])
        self.assertEqual(result["rows_copied"], 1)
        self.assertIsNone(self.check())
        self.assertEqual(self.check()["remaining"], 0)
        self.assertEqual(self.count(self.path), 2)

    def test_repeated_activation_does_not_reset_counters(self):
        limits.activate_rate_limit_store(self.path)
        self.assertIsNone(self.check(limit=1))
        limits.activate_rate_limit_store(self.path)
        self.assertEqual(self.check(limit=1)["remaining"], 0)

    def test_deactivation_merges_counts_without_reset(self):
        self.seed(1)
        limits.activate_rate_limit_store(self.path)
        self.assertIsNone(self.check())
        self.assertIsNone(self.check())
        result = limits.deactivate_rate_limit_store(self.path)
        self.assertFalse(result["active"])
        self.assertEqual(self.count(self.path), 3)
        self.assertEqual(self.check()["remaining"], 0)

    def test_deactivation_does_not_reduce_unrelated_main_counters(self):
        self.seed(5, key="payto:unrelated")
        limits.activate_rate_limit_store(self.path)
        self.seed(1, key="payto:unrelated")
        limits.deactivate_rate_limit_store(self.path)
        self.assertEqual(self.count(self.path, "payto:unrelated"), 6)

    def test_different_primary_paths_do_not_share_quotas(self):
        second = str(Path(self.path).with_name("second.db"))
        create_primary(second)
        limits.activate_rate_limit_store(self.path)
        limits.activate_rate_limit_store(second)
        self.assertIsNone(self.check(limit=1))
        self.assertIsNone(self.check(limit=1, path=second))
        self.assertEqual(self.check(limit=1)["remaining"], 0)

    def test_missing_activated_store_is_fail_closed(self):
        limits.activate_rate_limit_store(self.path)
        rate_path = Path(limits.rate_limit_db_path(self.path))
        rate_path.unlink()
        with self.assertRaises(HTTPException) as raised:
            self.check()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.headers["Retry-After"], "2")
        self.assertFalse(rate_path.exists())
        self.assertEqual(self.count(self.path), 0)

    def test_disabled_limits_do_not_require_a_store(self):
        limits.activate_rate_limit_store(self.path)
        Path(limits.rate_limit_db_path(self.path)).unlink()
        self.assertIsNone(self.check(limit=0))

    def test_four_processes_share_one_quota(self):
        limits.activate_rate_limit_store(self.path)
        with ProcessPoolExecutor(
                max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(consume_process_limit, [(self.path, NOW)] * 4))
        self.assertEqual(sum(results), 2)
        self.assertEqual(self.count(
            limits.rate_limit_db_path(self.path), "processes:day:198.51.100.9"), 2)

    def test_migration_fence_does_not_allow_untracked_requests(self):
        limits.activate_rate_limit_store(self.path)
        with limits._migration_lock(self.path, exclusive=True, timeout=1.0):
            with self.assertRaises(HTTPException) as raised:
                self.check()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.count(limits.rate_limit_db_path(self.path)), 0)

    def test_counter_database_is_private(self):
        limits.activate_rate_limit_store(self.path)
        mode = stat.S_IMODE(Path(limits.rate_limit_db_path(self.path)).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_batches_stay_100_and_pause_after_connection_close(self):
        from directory import ingestion, jobs

        real_writer = db.writer
        connections = []
        sizes = []
        pauses = []

        def writer(*_args, **kwargs):
            return real_writer(self.path, **kwargs)

        def ingest(conn, _kind, _source, batch, _upsert):
            connections.append(conn)
            sizes.append(len(batch))
            conn.execute(
                "INSERT INTO rate_limits VALUES(?,?,?,?)",
                (f"batch:{len(sizes)}", NOW, len(batch), NOW),
            )
            return {"added": 0, "updated": len(batch), "rejected": 0, "errors": []}

        def pause(seconds):
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[-1].execute("SELECT 1")
            pauses.append(seconds)

        with patch.object(db, "writer", side_effect=writer), \
             patch.object(ingestion, "ingest_batch", side_effect=ingest), \
             patch.object(jobs.time, "sleep", side_effect=pause):
            result = jobs._ingest_rows("mcp", "fixture", list(range(150)), None)

        self.assertEqual(sizes, [100, 50])
        self.assertEqual(pauses, [0.05])
        self.assertEqual(result["updated"], 150)