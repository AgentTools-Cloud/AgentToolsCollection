"""User writes go ahead of crawl and job writes (2026-11-22).

Every night requests that write -- claim verification, submissions -- failed with 503
while the crawl committed batch after batch: SQLite has no writer priority, so the job
won the lock again right after each commit. Jobs now start no new transaction while a
request is waiting, and keep their transactions short.
"""
import functools
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from directory import db, jobs, routes  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402


class _Path(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="write-priority-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        self.real_writer = db.writer
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE t (who TEXT, at REAL)")
            c.commit()

    def job(self, stop, hold=0.3):
        """Back-to-back background transactions, the way a crawl writes."""
        def run():
            while not stop.is_set():
                with self.real_writer(self.path, immediate=True, priority="background") as c:
                    c.execute("INSERT INTO t VALUES ('job', ?)", (time.monotonic(),))
                    time.sleep(hold)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        return thread


class Turnstile(_Path):
    def test_job_waits_while_a_request_is_waiting(self):
        entered, release = threading.Event(), threading.Event()

        def request():
            with db.foreground_write(self.path):
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(entered.wait(5))
        threading.Timer(0.4, release.set).start()
        began = time.monotonic()
        db._yield_to_foreground(self.path)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        thread.join(5)

    def test_job_does_not_wait_without_requests(self):
        began = time.monotonic()
        db._yield_to_foreground(self.path)
        self.assertLess(time.monotonic() - began, 0.2)

    def test_request_gets_the_lock_after_the_running_job_transaction(self):
        self.job(threading.Event())
        time.sleep(0.1)
        began = time.monotonic()
        with db.foreground_write(self.path):
            with db.writer(self.path, immediate=True, timeout=5) as c:
                c.execute("INSERT INTO t VALUES ('request', ?)", (time.monotonic(),))
        self.assertLess(time.monotonic() - began, 1.0)

    def test_job_style_write_inside_a_request_does_not_wait_on_itself(self):
        began = time.monotonic()
        with db.foreground_write(self.path):
            with db.writer(self.path, immediate=True, priority="background") as c:
                c.execute("INSERT INTO t VALUES ('request', 0)")
        self.assertLess(time.monotonic() - began, 0.5)


class SubmissionWrite(_Path):
    def test_request_write_succeeds_while_a_crawl_is_writing(self):
        real_writer = db.writer
        with patch.object(db, "DEFAULT_DB_PATH", self.path), \
                patch.object(db, "writer", functools.partial(real_writer, self.path)):
            self.job(threading.Event())
            time.sleep(0.1)
            began = time.monotonic()
            routes._submission_write(
                lambda c: c.execute("INSERT INTO t VALUES ('request', 0)"))
            self.assertLess(time.monotonic() - began, 1.0)


class SlowJobTransaction(_Path):
    def test_long_background_transaction_is_logged_with_its_caller(self):
        def crawl_step():
            with db.writer(self.path, immediate=True, priority="background") as c:
                c.execute("INSERT INTO t VALUES ('job', 0)")
        with patch.object(db, "SLOW_BACKGROUND_WRITE", 0.0), \
                self.assertLogs("directory.db", "WARNING") as logs:
            crawl_step()
        self.assertIn("crawl_step", logs.output[0])


class WriteBatches(unittest.TestCase):
    def sizes(self, seconds_per_row, n=400):
        clock = [0.0]
        out = []
        with patch.object(db.time, "monotonic", lambda: clock[0]):
            for batch in db.write_batches(list(range(n)), 100, 1.0):
                out.append(batch)
                clock[0] += len(batch) * seconds_per_row
        self.assertEqual([x for batch in out for x in batch], list(range(n)))
        return [len(batch) for batch in out]

    def test_slow_rows_shrink_the_batch_to_about_a_second(self):
        sizes = self.sizes(0.03)
        self.assertEqual(sizes[:3], [100, 50, 25])
        self.assertTrue(all(size * 0.03 <= 1.0 for size in sizes[2:]))

    def test_fast_rows_keep_full_batches(self):
        self.assertEqual(set(self.sizes(0.001)), {100})


class Rescore(_TempDB):
    def setUp(self):
        super().setUp()
        with db.writer() as c:
            for i in range(5):
                db.upsert_service(c, {"slug": f"s{i}", "name": f"S{i}", "health": "ok",
                                      "url": f"https://s{i}.example/pay",
                                      "source": "test", "source_id": f"s{i}"})
                db.upsert_mcp_server(c, {"slug": f"m{i}", "name": f"M{i}", "health": "ok",
                                         "endpoint_url": f"https://m{i}.example/mcp",
                                         "source": "test", "source_id": f"m{i}"})
            c.execute("UPDATE services SET quality_score=NULL")
            c.execute("UPDATE mcp_servers SET quality_score=-1")

    def test_writes_changed_scores_only_and_matches_full_rescore(self):
        jobs.cmd_rescore()
        with closing(db.connect(read_only=True)) as c:
            want = {t: {i: s for s, i, _ in f(c)} for t, f in db.SCORE_TABLES}
        for table, _ in db.SCORE_TABLES:
            got = {r["id"]: r["quality_score"] for r in self.q(f"SELECT id, quality_score FROM {table}")}
            self.assertEqual(got, want[table], table)
        self.assertEqual([db.rescore_changed(t, f)[1] for t, f in db.SCORE_TABLES], [0, 0, 0])

    def test_score_changed_meanwhile_is_kept(self):
        def stale(conn):
            rows = db._mcp_scores(conn)
            with db.writer() as c:
                c.execute("UPDATE mcp_servers SET quality_score=42 WHERE id=?", (rows[0][1],))
            return rows
        db.rescore_changed("mcp_servers", stale, chunk=1)
        kept = self.q("SELECT quality_score FROM mcp_servers WHERE quality_score=42")
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
