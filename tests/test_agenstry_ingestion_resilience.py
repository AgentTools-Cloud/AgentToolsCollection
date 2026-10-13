"""Offline Agenstry ingestion regressions using fresh, temporary SQLite databases.

Requires the parent's MetadataValidationError and directory.ingestion helper.
Only network I/O and explicit failure injection are mocked; card conversion,
upserts, retirement checks, transactions and retries use the actual functions.
"""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from directory import a2a, agenstry, db


class AgenstryIngestionResilienceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agenstry-resilience-")
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "test.db")
        db.init_db(self.path)
        real_writer = db.writer

        def writer(*, immediate=False):
            return real_writer(self.path, immediate=immediate)

        self.writer = self.enterContext(patch.object(db, "writer", side_effect=writer))
        self.enterContext(patch.object(db.time, "sleep"))
        # Any accidental unmocked HTTP request fails rather than reaching a host.
        self.enterContext(patch.object(
            httpx.HTTPTransport, "handle_request",
            side_effect=AssertionError("real network access is forbidden"),
        ))
        self.real_upsert = db.upsert_a2a_agent

    @staticmethod
    def cards(count):
        return {
            f"agent-{index}.example": {
                "name": f"Agent {index}",
                "url": f"https://agent-{index}.example/a2a",
                "skills": [{"id": "echo", "tags": ["original"]}],
                "capabilities": {"streaming": True},
            }
            for index in range(count)
        }

    def crawl(self, cards):
        def respond(request):
            card = cards[request.url.host]
            if isinstance(card, Exception):
                raise card
            if card is None or request.url.path != "/.well-known/agent-card.json":
                return httpx.Response(404)
            return httpx.Response(200, json=card)

        def client(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)

        with (
            patch.object(agenstry, "_fetch_sitemap_paths", return_value=[
                f"https://agenstry.com/agents/{host}" for host in cards
            ]),
            patch.object(agenstry.public_http, "client", side_effect=client),
        ):
            return agenstry.crawl_agenstry_a2a(workers=1)

    def sources(self):
        with closing(db.connect(self.path, read_only=True)) as conn:
            return [row[0] for row in conn.execute(
                "SELECT source_id FROM a2a_agents ORDER BY source_id"
            )]

    def latest_run(self):
        with closing(db.connect(self.path, read_only=True)) as conn:
            return dict(conn.execute(
                "SELECT * FROM crawl_runs WHERE source='a2a:agenstry' ORDER BY id DESC LIMIT 1"
            ).fetchone())

    def assert_run(self, status, added=0, updated=0, errors=()):
        run = self.latest_run()
        self.assertEqual(run["source"], "a2a:agenstry")
        self.assertEqual(run["status"], status)
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(run["added"], added)
        self.assertEqual(run["updated"], updated)
        self.assertEqual(run["errors"], "\n".join(errors) if errors else None)

    def assert_result_run(self, result):
        self.assertIsInstance(result["errors"], list)
        self.assertTrue(all(isinstance(error, str) for error in result["errors"]))
        self.assert_run("partial" if result["errors"] else "ok",
                        result["inserted"], result["updated"], result["errors"])

    def assert_only_good_rows(self):
        self.assertEqual(self.sources(), ["agent-0.example", "agent-2.example"])
        with closing(db.connect(self.path, read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM a2a_fts").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM listing_sources WHERE kind='a2a'"
            ).fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM a2a_fts WHERE a2a_fts MATCH '\"Agent 1\"'"
            ).fetchone()[0], 0)

    def test_good_invalid_good_preserves_peers_and_reports_rejection(self):
        cards = self.cards(3)
        cards["agent-1.example"]["url"] = "https://" + "x" * 64 + ".example/a2a"
        result = self.crawl(cards)
        self.assertEqual({key: result[key] for key in (
            "candidates", "resolved", "inserted", "updated", "rejected"
        )}, {
            "candidates": 3, "resolved": 3, "inserted": 2, "updated": 0,
            "rejected": 1,
        })
        self.assert_only_good_rows()
        self.assert_result_run(result)

    def test_savepoint_undoes_partial_row_before_recording_issue(self):
        def upsert(conn, row):
            result = self.real_upsert(conn, row)
            if row["source_id"] == "agent-1.example":
                raise db.MetadataValidationError("injected after SQL")
            return result

        with patch.object(db, "upsert_a2a_agent", side_effect=upsert):
            result = self.crawl(self.cards(3))
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assert_only_good_rows()
        self.assert_result_run(result)

    def test_retired_record_does_not_rollback_peers_or_resurrect(self):
        cards = self.cards(3)
        row = a2a.card_to_row(
            cards["agent-1.example"],
            "https://agent-1.example/.well-known/agent-card.json",
            source="agenstry", source_id="agent-1.example",
        )
        with db.writer() as conn:
            self.real_upsert(conn, row)
            db.retire_listing(conn, "a2a", row["slug"], "fixture retirement", "test")
        result = self.crawl(cards)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assert_only_good_rows()
        self.assert_result_run(result)

    def test_lock_retry_uses_pristine_deepcopies_and_counts_only_commit(self):
        cards = self.cards(3)
        cards["agent-1.example"]["url"] = "https://" + "x" * 64 + ".example/a2a"
        attempts = []
        converted = []
        locked = False
        real_convert = a2a.card_to_row

        def convert(*args, **kwargs):
            row = real_convert(*args, **kwargs)
            converted.append((row, deepcopy(row)))
            return row

        def upsert(conn, row):
            nonlocal locked
            if row["source_id"] == "agent-0.example":
                attempts.append(deepcopy(row))
                if not locked:
                    row["skills"][0]["tags"].append("failed-attempt")
            result = self.real_upsert(conn, row)
            if row["source_id"] == "agent-2.example" and not locked:
                locked = True
                raise sqlite3.OperationalError("database is locked")
            return result

        with (
            patch.object(a2a, "card_to_row", side_effect=convert),
            patch.object(db, "upsert_a2a_agent", side_effect=upsert),
            patch.object(agenstry, "_write_a2a_batch", wraps=agenstry._write_a2a_batch) as write,
        ):
            result = self.crawl(cards)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        for row, original in converted:
            self.assertEqual(row, original)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assert_only_good_rows()
        self.assert_result_run(result)
        with closing(db.connect(self.path, read_only=True)) as conn:
            row = db.get_a2a_by_slug(conn, "agent-0")
            self.assertEqual(row["skills"][0]["tags"], ["original"])

    def test_batches_commit_independently_and_updates_remain_counted(self):
        size = agenstry._A2A_WRITE_BATCH
        self.assertEqual(size, 100)
        visible = []

        def upsert(conn, row):
            index = int(row["source_id"].split(".")[0].split("-")[1])
            if index % size == 0:
                visible.append(len(self.sources()))
            return self.real_upsert(conn, row)

        cards = self.cards(size * 2 + 1)
        with patch.object(db, "upsert_a2a_agent", side_effect=upsert):
            first = self.crawl(cards)
        self.assertEqual(visible, [0, size, size * 2])
        self.assertEqual(first["inserted"], len(cards))
        self.assert_result_run(first)
        second = self.crawl(cards)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], len(cards))
        self.assertEqual(second["rejected"], 0)
        self.assertEqual(second["errors"], [])
        self.assert_result_run(second)
        with closing(db.connect(self.path, read_only=True)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM crawl_runs WHERE source='a2a:agenstry'"
            ).fetchone()[0], 2)

    def test_shared_helper_errors_are_aggregated_and_recorded_as_partial(self):
        real_ingest = agenstry.ingestion.ingest_batch
        batch_sizes = []

        def ingest(conn, kind, source, rows, upsert):
            self.assertTrue(conn.in_transaction)
            self.assertEqual((kind, source), ("a2a", "agenstry"))
            self.assertIs(upsert, db.upsert_a2a_agent)
            batch_sizes.append(len(rows))
            result = real_ingest(conn, kind, source, rows, upsert)
            result["errors"].append(f"fixture batch {len(batch_sizes)} issue")
            return result

        with patch.object(agenstry.ingestion, "ingest_batch", side_effect=ingest):
            result = self.crawl(self.cards(201))
        self.assertEqual(batch_sizes, [100, 100, 1])
        self.assertEqual(result["inserted"], 201)
        self.assertEqual(result["errors"], [
            "fixture batch 1 issue", "fixture batch 2 issue", "fixture batch 3 issue",
        ])
        self.assert_result_run(result)

    def test_unexpected_operational_error_propagates_preserving_prior_batch(self):
        size = agenstry._A2A_WRITE_BATCH
        calls = []

        def upsert(conn, row):
            calls.append(row["source_id"])
            result = self.real_upsert(conn, row)
            if row["source_id"] == f"agent-{size + 2}.example":
                conn.execute("SELECT * FROM nonexistent_resilience_table")
            return result

        with patch.object(db, "upsert_a2a_agent", side_effect=upsert):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                self.crawl(self.cards(size + 3))
        self.assertEqual(len(calls), size + 3)  # Non-lock errors are not retried.
        self.assertEqual(set(self.sources()), set(self.cards(size)))
        run = self.latest_run()
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["added"], size)
        self.assertEqual(run["updated"], 0)
        self.assertIsNotNone(run["finished_at"])
        self.assertIn("OperationalError", run["errors"])
        self.assertIn("nonexistent_resilience_table", run["errors"])

    def test_programming_system_and_unclassified_value_errors_propagate(self):
        for error in (
            sqlite3.ProgrammingError("bad SQL"), sqlite3.InterfaceError("bad binding"),
            ValueError("not a classified metadata rejection"), TypeError("programming bug"),
            OSError("disk failure"), MemoryError("out of memory"),
        ):
            with self.subTest(error=type(error).__name__):
                calls = []

                def upsert(conn, row):
                    calls.append(row["source_id"])
                    result = self.real_upsert(conn, row)
                    if row["source_id"] == "agent-1.example":
                        raise error
                    return result

                with patch.object(db, "upsert_a2a_agent", side_effect=upsert):
                    with self.assertRaises(type(error)):
                        self.crawl(self.cards(3))
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.sources(), [])
                self.assert_run("error", errors=[f"agenstry a2a: {error!r}"])

    def test_probe_programming_errors_are_not_silently_skipped(self):
        with patch.object(a2a, "card_to_row", side_effect=TypeError("conversion bug")):
            with self.assertRaisesRegex(TypeError, "conversion bug"):
                self.crawl(self.cards(1))
        self.assertEqual(self.sources(), [])
        self.assert_run("error", errors=["agenstry a2a: TypeError('conversion bug')"])

    def test_expected_probe_failure_is_reported_but_absent_card_is_not(self):
        cards = self.cards(3)
        cards["agent-1.example"] = None
        # fetch_agent_card normally handles HTTP errors and returns no card.
        real_fetch = a2a.fetch_agent_card

        def fetch(url, **kwargs):
            if url == "https://agent-2.example":
                raise httpx.ConnectError("fixture probe failed")
            return real_fetch(url, **kwargs)

        with patch.object(a2a, "fetch_agent_card", side_effect=fetch):
            result = self.crawl(cards)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("agent-2.example", result["errors"][0])
        self.assert_result_run(result)

    def test_sitemap_failure_records_error_and_propagates_original_exception(self):
        error = httpx.ReadTimeout("sitemap fixture")
        with patch.object(agenstry, "_fetch_sitemap_paths", side_effect=error):
            with self.assertRaises(httpx.ReadTimeout) as raised:
                agenstry.crawl_agenstry_a2a()
        self.assertIs(raised.exception, error)
        self.assert_run("error", errors=[f"agenstry a2a: {error!r}"])
        self.assertEqual(self.sources(), [])

    def test_run_is_started_before_fetching_and_empty_sitemap_is_ok(self):
        def sitemap():
            run = self.latest_run()
            self.assertEqual(run["status"], "running")
            self.assertIsNone(run["finished_at"])
            return []

        with patch.object(agenstry, "_fetch_sitemap_paths", side_effect=sitemap):
            result = agenstry.crawl_agenstry_a2a(workers=1)
        self.assertEqual(result, {
            "candidates": 0, "resolved": 0, "inserted": 0, "updated": 0,
            "rejected": 0, "errors": [],
        })
        self.assert_run("ok")

    def test_all_candidates_missing_cards_is_partial_not_success(self):
        cards = dict.fromkeys(self.cards(3))
        result = self.crawl(cards)
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["resolved"], 0)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("no live cards resolved from 3 candidates", result["errors"][0])
        self.assert_result_run(result)

    def test_individual_missing_card_does_not_make_success_partial(self):
        cards = self.cards(3)
        cards["agent-1.example"] = None
        result = self.crawl(cards)
        self.assertEqual(result["resolved"], 2)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["errors"], [])
        self.assert_only_good_rows()
        self.assert_run("ok", added=2)

    def test_status_write_failure_does_not_mask_original_source_error(self):
        error = httpx.ReadTimeout("sitemap fixture")
        with (
            patch.object(agenstry, "_fetch_sitemap_paths", side_effect=error),
            patch.object(db, "record_crawl_finish", side_effect=sqlite3.OperationalError("disk failure")),
            self.assertLogs("directory.agenstry", level="ERROR"),
        ):
            with self.assertRaises(httpx.ReadTimeout) as raised:
                agenstry.crawl_agenstry_a2a()
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()