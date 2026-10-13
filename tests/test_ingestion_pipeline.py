"""Offline ingestion regressions; no production database, crawler, or network.

Run with unittest discovery restricted to this file. Full DB/job coverage needs
the application's existing dependencies (including httpx and PyYAML); missing
dependencies are errors, not silently skipped tests. EndpointValidationTests can
also run alone with only idna installed. Historical duplicate fixtures use SQL
intentionally: the current upserts cannot create those legacy states.
"""

from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


@contextmanager
def _forbid_network():
    """Fail even if application code swallows the blocked I/O exception."""
    with ExitStack() as stack:
        guards = []
        targets = [
            (socket, name) for name in (
                "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                "gethostbyaddr", "create_connection",
            )
        ]
        targets += [
            (socket.socket, name) for name in (
                "connect", "connect_ex", "sendto", "sendmsg",
            ) if hasattr(socket.socket, name)
        ]
        # Keep the socket class intact: ssl subclasses it during imports.
        # Connection/datagram guards block I/O; also deny subprocess escapes.
        targets += [(subprocess, "Popen"), (os, "system")]
        for owner, name in targets:
            guards.append(stack.enter_context(patch.object(
                owner, name, side_effect=AssertionError(
                    "network/subprocess access forbidden in ingestion tests"
                ),
            )))
        try:
            yield
        finally:
            if any(guard.called for guard in guards):
                raise AssertionError("unexpected network/subprocess attempt")


with _forbid_network():
    from directory import db, ingestion, wellknown_binding


TABLES = {"x402": "services", "mcp": "mcp_servers", "a2a": "a2a_agents"}
FTS = {"x402": "services_fts", "mcp": "mcp_fts", "a2a": "a2a_fts"}


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _row(kind, label, endpoint=None, source="fixture", **extra):
    endpoint = endpoint or f"https://{label}.example/{kind}"
    row = {
        "slug": label,
        "name": label,
        "description": "Fixture description",
        "source": source,
        "source_id": f"identity-{label}",
        "source_url": f"https://catalog.example/records/{label}",
        "url" if kind == "x402" else "endpoint_url": endpoint,
    }
    if kind == "a2a":
        row.update(
            card_url=f"https://{label}.example/.well-known/agent-card.json",
            skills=[{"id": "echo", "name": "Echo", "tags": ["original"]}],
            capabilities={"streaming": True},
        )
    else:
        row["tags"] = ["original"]
    row.update(extra)
    return row


def _upsert(kind):
    return {
        "x402": db.upsert_service,
        "mcp": db.upsert_mcp_server,
        "a2a": db.upsert_a2a_agent,
    }[kind]


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(_forbid_network())


class EndpointValidationTests(OfflineTestCase):
    def assert_rejected(self, value, reason):
        with self.assertRaises(db.MetadataValidationError) as caught:
            ingestion.validate_endpoint(value)
        self.assertEqual(str(caught.exception), reason)

    def test_rejects_url_templates(self):
        for endpoint in (
            "https://api.example/{tenant}/mcp",
            "https://${HOST}.example/mcp",
            "https://api.example/mcp?key={API_KEY}",
        ):
            with self.subTest(endpoint=endpoint):
                self.assert_rejected(endpoint, "needs_configuration:url_template")

    def test_rejects_underscore_authorities(self):
        for host in ("bad_host.example", "api.bad_domain.example"):
            with self.subTest(host=host):
                self.assert_rejected(f"https://{host}/mcp", "invalid_http_authority")

    def test_rejects_long_label_but_accepts_63_byte_boundary(self):
        ingestion.validate_endpoint("https://" + "a" * 63 + ".example/mcp")
        self.assert_rejected("https://" + "a" * 64 + ".example/mcp",
                             "hostname_label_too_long")
        self.assert_rejected("https://" + ".".join(["a" * 63] * 4) + "/mcp",
                             "invalid_http_authority")

    def test_rejects_method_prefixed_and_whitespace_endpoints(self):
        for endpoint in (
            "GET https://api.example/mcp", "POST /mcp",
            "https://api.example/a b", " https://api.example/mcp",
            "https://api.example/mcp\n",
        ):
            with self.subTest(endpoint=endpoint):
                self.assert_rejected(endpoint, "endpoint_contains_whitespace")

    def test_rejects_relative_and_non_http_endpoints(self):
        for endpoint in ("/mcp", "api.example/mcp", "//api.example/mcp",
                         "npm:package", "ftp://api.example/mcp"):
            with self.subTest(endpoint=endpoint):
                self.assert_rejected(endpoint, "requires_absolute_http_endpoint")

    def test_rejects_missing_values_credentials_fragments_and_invalid_ports(self):
        for value in (None, "", "  ", 42, {}, []):
            with self.subTest(value=value):
                self.assert_rejected(value, "missing_endpoint")
        for endpoint in ("https://user:pass@api.example/mcp",
                         "https://api.example/mcp#fragment"):
            with self.subTest(endpoint=endpoint):
                self.assert_rejected(endpoint, "endpoint_credentials_or_fragment")
        for endpoint in ("https://api.example:bad/mcp", "https://api.example:65536/mcp"):
            with self.subTest(endpoint=endpoint):
                self.assert_rejected(endpoint, "invalid_http_authority")

    def test_accepts_case_and_preserves_path_query_identity(self):
        cases = {
            "HTTPS://API.Example:443/MCP/?Token=AbC&x=%2F":
                "https://api.example/MCP?Token=AbC&x=%2F",
            "https://api.example/?query=AbC": "https://api.example?query=AbC",
            "http://API.Example:80/Mcp": "http://api.example/Mcp",
        }
        for endpoint, expected in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(ingestion.validate_endpoint(endpoint))
                self.assertEqual(db._exact_endpoint(endpoint), expected)
        keys = {db._exact_endpoint(endpoint) for endpoint in (
            "https://api.example/MCP?Token=AbC", "https://api.example/mcp?Token=AbC",
            "https://api.example/MCP?Token=abc", "https://api.example/MCP?token=AbC",
        )}
        self.assertEqual(len(keys), 4)


class TemporaryDatabaseTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="ingestion-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "test.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret",
        }))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        real_connect, real_writer = db.connect, db.writer

        def checked_path(path):
            if path is not None and str(Path(path).resolve()) != self.path:
                raise AssertionError("non-temporary database access forbidden")
            return self.path

        def connect(db_path=None, read_only=False, set_wal=True):
            return real_connect(checked_path(db_path), read_only=read_only, set_wal=set_wal)

        def writer(db_path=None, immediate=False):
            return real_writer(checked_path(db_path), immediate=immediate)

        self.enterContext(patch.object(db, "connect", side_effect=connect))
        self.writer = self.enterContext(patch.object(db, "writer", side_effect=writer))
        self.sleep = self.enterContext(patch.object(db.time, "sleep"))
        db.init_db(self.path)

    def query(self, sql, params=()):
        with closing(db.connect(read_only=True)) as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def listings(self, kind):
        return self.query(f"SELECT * FROM {TABLES[kind]} ORDER BY id")

    def issues(self):
        return self.query("SELECT * FROM ingestion_issues ORDER BY kind, identity_hash, content_hash")

    def sources(self):
        return self.query("SELECT * FROM listing_sources ORDER BY kind, listing_id, source")

    def bindings(self, kind=None):
        return self.query(
            "SELECT * FROM wellknown_endpoint_sources "
            + ("WHERE kind=? " if kind else "")
            + "ORDER BY kind, identity_hash, endpoint_hash",
            (kind,) if kind else (),
        )

    def ingest(self, kind, rows, source="fixture", upsert=None):
        with db.writer(immediate=True) as conn:
            return ingestion.ingest_batch(conn, kind, source, rows, upsert or _upsert(kind))

    def ingest_wellknown(self, kind, rows):
        return self.ingest(kind, rows, source="wellknown",
                           upsert=lambda conn, row: wellknown_binding.upsert_listing(conn, kind, row))

    def seed(self, kind, row, **columns):
        with db.writer() as conn:
            created, lid = _upsert(kind)(conn, deepcopy(row))
            self.assertTrue(created, "fixture must create a distinct listing")
            if columns:
                assignments = ",".join(f"{name}=?" for name in columns)
                conn.execute(f"UPDATE {TABLES[kind]} SET {assignments} WHERE id=?",
                             (*columns.values(), lid))
            return lid

    def legacy(self, kind, row, **columns):
        """Insert legacy raw endpoints/duplicate identities without normalizing."""
        data = {key: row[key] for key in (
            "slug", "name", "source", "source_id", "endpoint_url",
        )}
        data.update(created_at=10, updated_at=11, last_seen=12,
                    description="Legacy metadata", health="ok", latency_ms=17)
        if kind == "a2a":
            data["card_url"] = row["card_url"]
        data.update(columns)
        with db.writer() as conn:
            cursor = conn.execute(
                f"INSERT INTO {TABLES[kind]} ({','.join(data)}) "
                f"VALUES ({','.join('?' for _ in data)})", tuple(data.values()),
            )
            lid = cursor.lastrowid
            conn.execute(
                "INSERT INTO listing_sources VALUES(?,?,?,?,?,?,?)",
                (kind, lid, row["source"], row["source_id"], row["source_url"], 10, 12),
            )
            return lid

    def add_source(self, kind, lid, sid):
        with db.writer() as conn:
            conn.execute("INSERT INTO listing_sources VALUES(?,?,?,?,?,?,?)",
                         (kind, lid, "wellknown", sid, "https://legacy.example/record", 10, 12))

    def bind(self, kind, row):
        original = deepcopy(row)
        with db.writer(immediate=True) as conn:
            result = wellknown_binding.upsert_listing(conn, kind, deepcopy(row))
        self.assertEqual(row, original)
        return result

    def assert_result(self, result, added=0, updated=0, rejected=0, errors=()):
        self.assertEqual(result, dict(added=added, updated=updated, rejected=rejected,
                                      errors=list(errors)))

    def assert_issue(self, issue, kind, source, row, reason, disposition="rejected"):
        self.assertEqual((issue["kind"], issue["source"], issue["disposition"], issue["reason"]),
                         (kind, source, disposition, reason))
        self.assertEqual(issue["identity_hash"], _sha(
            row.get("source_id") or row.get("slug") or row.get("endpoint_url") or row.get("url")
        ))
        self.assertEqual(issue["content_hash"], _sha(
            json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
        ))
        for key in ("identity_hash", "content_hash"):
            self.assertRegex(issue[key], r"^[0-9a-f]{64}$")
        serialized = json.dumps(issue, ensure_ascii=False)
        for key in ("slug", "name", "source_id", "source_url", "url", "endpoint_url"):
            if row.get(key):
                self.assertNotIn(row[key], serialized, f"raw {key} leaked into audit")

    def assert_no_listing_artifacts(self, kind, row):
        self.assertEqual(self.query(f"SELECT id FROM {TABLES[kind]} WHERE slug=?",
                                    (row["slug"],)), [])
        self.assertEqual(self.query(
            "SELECT * FROM listing_sources WHERE kind=? AND source_id=?",
            (kind, row["source_id"]),
        ), [])
        self.assertEqual(self.query(
            f"SELECT rowid FROM {FTS[kind]} WHERE {FTS[kind]} MATCH ?",
            ('"' + row["name"] + '"',),
        ), [])


class IngestionBatchTests(TemporaryDatabaseTestCase):
    def test_wrong_typed_endpoint_is_audited_without_rolling_back_good_peers(self):
        for kind in TABLES:
            for number, bad_value in enumerate((42, True, ["bad"], {"url": "bad"})):
                with self.subTest(kind=kind, bad_value=bad_value):
                    label = f"wrong-type-{kind}-{number}"
                    rows = [_row(kind, label + "-first"), _row(kind, label + "-bad"),
                            _row(kind, label + "-last")]
                    rows[1]["url" if kind == "x402" else "endpoint_url"] = bad_value
                    self.assert_result(self.ingest(kind, rows), added=2, rejected=1)
                    issue = next(item for item in self.issues()
                                 if item["identity_hash"] == _sha(rows[1]["source_id"]))
                    self.assertEqual(issue["reason"], "missing_endpoint")
                    self.assertIsNone(issue["endpoint_hash"])

    def test_good_bad_good_commits_peers_with_hash_only_audit_all_kinds(self):
        for kind in TABLES:
            with self.subTest(kind=kind):
                rows = [_row(kind, f"{kind}-{label}") for label in ("first", "secretbad", "last")]
                key = "url" if kind == "x402" else "endpoint_url"
                rows[1][key] = "https://private-fixture.example/{TENANT}?token=private-token"
                rows[1]["description"] = "Private fixture 文本"
                rows[1]["headers"] = {"Authorization": "Bearer private-credential"}
                original = deepcopy(rows)
                with patch.object(ingestion.time, "time", return_value=100):
                    result = self.ingest(kind, rows)
                self.assert_result(result, added=2, rejected=1)
                self.assertEqual(rows, original)
                self.assertEqual([r["slug"] for r in self.listings(kind)],
                                 [rows[0]["slug"], rows[2]["slug"]])
                self.assert_no_listing_artifacts(kind, rows[1])
                issue = next(r for r in self.issues() if r["kind"] == kind)
                self.assert_issue(issue, kind, "fixture", rows[1], "needs_configuration:url_template")
                self.assertEqual((issue["first_seen"], issue["last_seen"], issue["occurrences"]),
                                 (100, 100, 1))
                self.assertIsNone(issue["resolved_at"])
                for private in ("private-token", "private-credential", "Private fixture 文本"):
                    self.assertNotIn(private, json.dumps(self.issues(), ensure_ascii=False))
        self.assertEqual(len(self.issues()), 3)

    def test_repeated_rejection_updates_one_audit_row(self):
        row = _row("mcp", "repeat-private", "https://bad_host.example/mcp")
        for timestamp in (100, 200, 300):
            with patch.object(ingestion.time, "time", return_value=timestamp):
                self.assert_result(self.ingest("mcp", [row]), rejected=1)
        self.assertEqual(len(self.issues()), 1)
        issue = self.issues()[0]
        self.assert_issue(issue, "mcp", "fixture", row, "invalid_http_authority")
        self.assertEqual((issue["first_seen"], issue["last_seen"], issue["occurrences"]),
                         (100, 300, 3))
        self.assertIsNone(issue["resolved_at"])

    def test_fixed_identity_resolves_all_versions_and_reappearance_reopens(self):
        bad = _row("mcp", "fix-private", "https://api.example/{TENANT}")
        changed = dict(bad, description="A different private payload")
        with patch.object(ingestion.time, "time", return_value=100):
            self.ingest("mcp", [bad, changed])
        self.assertEqual(len(self.issues()), 2)
        fixed = dict(bad, slug="fixed-public-slug", endpoint_url="https://fixed.example/mcp")
        with patch.object(ingestion.time, "time", return_value=200):
            self.assert_result(self.ingest("mcp", [fixed]), added=1)
        self.assertEqual({issue["resolved_at"] for issue in self.issues()}, {200})
        with patch.object(ingestion.time, "time", return_value=300):
            self.assert_result(self.ingest("mcp", [bad]), rejected=1)
        by_content = {issue["content_hash"]: issue for issue in self.issues()}
        reopened = by_content[_sha(json.dumps(bad, sort_keys=True, ensure_ascii=False))]
        self.assertIsNone(reopened["resolved_at"])
        self.assertEqual(reopened["occurrences"], 2)
        self.assertEqual(sum(issue["resolved_at"] == 200 for issue in self.issues()), 1)
        self.assertEqual(len(self.listings("mcp")), 1)

    def test_metadata_error_after_partial_sql_rolls_back_row_fts_and_provenance(self):
        for kind in TABLES:
            with self.subTest(kind=kind):
                rows = [_row(kind, f"{kind}-{label}") for label in ("before", "doomed", "after")]

                def upsert(conn, row):
                    result = _upsert(kind)(conn, row)
                    if row["source_id"] == rows[1]["source_id"]:
                        conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (kind, "partial write"))
                        raise db.MetadataValidationError("fixture_metadata_rejected")
                    return result

                self.assert_result(self.ingest(kind, rows, upsert=upsert), added=2, rejected=1)
                self.assert_no_listing_artifacts(kind, rows[1])
                self.assertEqual(self.query("SELECT * FROM meta WHERE key=?", (kind,)), [])
                issue = next(r for r in self.issues() if r["kind"] == kind)
                self.assert_issue(issue, kind, "fixture", rows[1], "fixture_metadata_rejected")

    def test_unclassified_errors_rollback_the_batch_without_metadata_audit(self):
        for error in (ValueError("unclassified"), TypeError("bug"), OSError("disk failure"),
                      sqlite3.OperationalError("disk I/O error")):
            with self.subTest(error=type(error).__name__):
                rows = [_row("mcp", label) for label in ("prior", "failed", "unreached")]
                calls = []

                def upsert(conn, row):
                    calls.append(row["slug"])
                    result = db.upsert_mcp_server(conn, row)
                    if row["source_id"] == rows[1]["source_id"]:
                        raise error
                    return result

                with self.assertRaises(type(error)) as caught:
                    self.ingest("mcp", rows, upsert=upsert)
                self.assertIs(caught.exception, error)
                self.assertEqual(calls, ["prior", "failed"])
                self.assertEqual(self.listings("mcp"), [])
                self.assertEqual(self.sources(), [])
                self.assertEqual(self.issues(), [])

    def test_retirement_precedes_validation_and_is_not_ingestion_audited(self):
        for kind in TABLES:
            with self.subTest(kind=kind):
                retired = _row(kind, f"{kind}-retired")
                self.seed(kind, retired)
                with db.writer() as conn:
                    db.retire_listing(conn, kind, retired["slug"], "fixture retirement", "test")
                invalid = dict(retired)
                invalid["url" if kind == "x402" else "endpoint_url"] = "https://api.example/{PRIVATE}"
                rows = [_row(kind, f"{kind}-safe-before"), invalid, _row(kind, f"{kind}-safe-after")]
                self.assert_result(self.ingest(kind, rows), added=2, rejected=1)
                self.assert_no_listing_artifacts(kind, retired)
                self.assertEqual(self.issues(), [])
                self.assertEqual(len(self.query(
                    "SELECT * FROM retired_listings WHERE kind=? AND status='active'", (kind,)
                )), 1)

    def test_ingest_batch_starts_transaction_without_implicit_row_commits(self):
        with closing(db.connect()) as conn:
            self.assertFalse(conn.in_transaction)
            result = ingestion.ingest_batch(conn, "mcp", "fixture",
                                             [_row("mcp", "pending")], db.upsert_mcp_server)
            self.assert_result(result, added=1)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(self.listings("mcp"), [])
            conn.rollback()
        self.assertEqual(self.listings("mcp"), [])


class IngestionJobTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        from directory import jobs
        self.jobs = jobs
        for name in ("cmd_health", "cmd_health_mcp", "cmd_health_a2a"):
            guard = self.enterContext(patch.object(jobs, name, side_effect=AssertionError(
                "health probes forbidden in ingestion tests"
            )))
            self.addCleanup(guard.assert_not_called)

    def run_rows(self, kind, rows, upsert=None, batch_size=100):
        return self.jobs._ingest_rows(kind, "fixture", rows, upsert or _upsert(kind),
                                      batch_size=batch_size)

    def watermark(self):
        with closing(db.connect(read_only=True)) as conn:
            return db.get_meta(conn, "mcp_registry:updated_since")

    def set_watermark(self, value):
        with db.writer() as conn:
            db.set_meta(conn, "mcp_registry:updated_since", value)

    def latest_registry_run(self):
        return self.query(
            "SELECT * FROM crawl_runs WHERE source='mcp:mcp-registry' ORDER BY id DESC LIMIT 1"
        )[0]

    def crawl_registry(self, crawler):
        with patch.dict(self.jobs.crawlers.MCP_CRAWLERS, {"mcp-registry": crawler}):
            self.assertEqual(self.jobs.cmd_crawl_mcp("mcp-registry", probe_new=False), 0)

    def test_lock_retry_uses_deepcopies_and_only_counts_committed_rows_and_audit(self):
        rows = [_row("a2a", label) for label in ("retry-first", "retry-bad", "retry-last")]
        rows[1]["endpoint_url"] = "https://api.example/{TENANT}"
        original = deepcopy(rows)
        seen = []
        locked = False

        def upsert(conn, row):
            nonlocal locked
            if row["source_id"] == rows[0]["source_id"]:
                seen.append(deepcopy(row))
                self.assertEqual(self.listings("a2a"), [])
                self.assertEqual(self.issues(), [])
                if not locked:
                    row["skills"][0]["tags"].append("failed-attempt")
                    row["capabilities"]["streaming"] = False
            result = db.upsert_a2a_agent(conn, row)
            if row["source_id"] == rows[2]["source_id"] and not locked:
                locked = True
                raise sqlite3.OperationalError("database is locked")
            return result

        self.writer.reset_mock()
        result = self.run_rows("a2a", rows, upsert)
        self.assert_result(result, added=2, rejected=1)
        self.assertEqual(self.writer.call_count, 2)
        self.assertTrue(all(call.kwargs.get("immediate") for call in self.writer.call_args_list))
        self.sleep.assert_called_once()
        self.assertEqual(seen, [original[0], original[0]])
        self.assertEqual(rows, original)
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(self.issues()[0]["occurrences"], 1)
        persisted = self.listings("a2a")
        self.assertEqual(len(persisted), 2)
        self.assertEqual(json.loads(persisted[0]["skills"])[0]["tags"], ["original"])
        self.assertEqual(json.loads(persisted[0]["capabilities"]), {"streaming": True})
        self.assert_no_listing_artifacts("a2a", rows[1])

    def test_exhausted_lock_retry_rolls_back_all_sql_and_rejection_audit(self):
        rows = [_row("mcp", "retry-template", "https://api.example/{TENANT}"),
                _row("mcp", "always-locked")]
        calls = []

        def upsert(conn, row):
            calls.append(deepcopy(row))
            db.upsert_mcp_server(conn, row)
            raise sqlite3.OperationalError("database is locked")

        self.assert_result(self.run_rows("mcp", rows, upsert),
                           errors=["batch failed: OperationalError"])
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(row == rows[1] for row in calls))
        self.assertEqual(self.sleep.call_count, 5)
        self.assertEqual(self.listings("mcp"), [])
        self.assertEqual(self.sources(), [])
        self.assertEqual(self.issues(), [])

    def test_failed_later_batch_retains_prior_commit_and_stops_following_batches(self):
        rows = [_row("mcp", f"batch-{n}") for n in range(5)]
        calls = []

        def upsert(conn, row):
            calls.append(row["slug"])
            result = db.upsert_mcp_server(conn, row)
            if row["source_id"] == rows[3]["source_id"]:
                raise sqlite3.OperationalError("disk I/O error")
            return result

        self.assert_result(self.run_rows("mcp", rows, upsert, batch_size=2), added=2,
                           errors=["batch failed: OperationalError"])
        self.assertEqual(calls, [r["slug"] for r in rows[:4]])
        self.assertEqual([r["slug"] for r in self.listings("mcp")], [r["slug"] for r in rows[:2]])
        self.assertEqual(self.issues(), [])
        self.sleep.assert_not_called()
        for row in rows[2:]:
            self.assert_no_listing_artifacts("mcp", row)

    def test_registry_watermark_retained_when_ingest_rows_reports_failure(self):
        old, new = "2026-10-01T00:00:00Z", "2026-10-12T00:00:00Z"
        self.set_watermark(old)
        rows = [_row("mcp", "registry-error", source="mcp-registry", _updated_at=new)]
        crawler = Mock(return_value=rows)
        failure = dict(added=0, updated=0, rejected=0, errors=["batch failed: OperationalError"])
        with patch.object(self.jobs, "_ingest_rows", return_value=failure) as ingest:
            self.crawl_registry(crawler)
        crawler.assert_called_once_with(updated_since=old)
        ingest.assert_called_once_with("mcp", "mcp-registry", rows, db.upsert_mcp_server)
        self.assertEqual(self.watermark(), old)
        run = self.latest_registry_run()
        self.assertEqual((run["status"], run["added"], run["updated"]), ("partial", 0, 0))
        self.assertEqual(run["errors"], "batch failed: OperationalError")
        self.assertIsNotNone(run["finished_at"])

    def test_registry_real_ingestion_failure_retains_watermark_and_rolls_back(self):
        old = "2026-10-01T00:00:00Z"
        self.set_watermark(old)
        rows = [_row("mcp", f"registry-{n}", source="mcp-registry",
                     _updated_at=f"2026-10-12T00:00:0{n}Z") for n in range(2)]
        real_upsert = db.upsert_mcp_server

        def fail_after_sql(conn, row):
            result = real_upsert(conn, row)
            if row["source_id"] == rows[1]["source_id"]:
                raise sqlite3.OperationalError("disk I/O error")
            return result

        crawler = Mock(return_value=rows)
        with patch.object(db, "upsert_mcp_server", side_effect=fail_after_sql):
            self.crawl_registry(crawler)
        crawler.assert_called_once_with(updated_since=old)
        self.assertEqual(self.watermark(), old)
        self.assertEqual(self.latest_registry_run()["status"], "partial")
        self.assertEqual(self.listings("mcp"), [])
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.sources(), [])
        self.sleep.assert_not_called()

    def test_registry_permanent_template_is_audited_then_watermark_advances(self):
        old, new = "2026-10-01T00:00:00Z", "2026-10-12T00:00:03Z"
        self.set_watermark(old)
        good = _row("mcp", "registry-good", source="mcp-registry",
                    _updated_at="2026-10-12T00:00:02Z")
        bad = _row("mcp", "registry-private-template", "https://api.example/{SECRET}",
                   source="mcp-registry", _updated_at=new)
        rows = [good, bad]
        original = deepcopy(rows)
        real_set_meta = db.set_meta
        audited_before_advance = []

        def set_meta(conn, key, value):
            if key == "mcp_registry:updated_since":
                audited_before_advance.append(self.issues())
            return real_set_meta(conn, key, value)

        crawler = Mock(return_value=rows)
        with patch.object(db, "set_meta", side_effect=set_meta):
            self.crawl_registry(crawler)
        crawler.assert_called_once_with(updated_since=old)
        self.assertEqual(rows, original)
        self.assertEqual(self.watermark(), new)
        self.assertEqual(len(audited_before_advance), 1)
        self.assertEqual(len(audited_before_advance[0]), 1)
        self.assert_issue(self.issues()[0], "mcp", "mcp-registry", bad,
                          "needs_configuration:url_template")
        run = self.latest_registry_run()
        self.assertEqual((run["status"], run["added"], run["updated"], run["errors"]),
                         ("ok", 1, 0, None))
        self.assert_no_listing_artifacts("mcp", bad)
        next_crawl = Mock(return_value=[])
        self.crawl_registry(next_crawl)
        next_crawl.assert_called_once_with(updated_since=new)

    def test_registry_partial_crawler_never_advances_watermark(self):
        old = "2026-10-01T00:00:00Z"
        self.set_watermark(old)
        row = _row("mcp", "partial-registry", source="mcp-registry",
                   _updated_at="2026-10-12T00:00:00Z")
        crawler = Mock(side_effect=self.jobs.crawlers.PartialCrawl([row], "fixture page missing"))
        self.crawl_registry(crawler)
        crawler.assert_called_once_with(updated_since=old)
        self.assertEqual(self.watermark(), old)
        run = self.latest_registry_run()
        self.assertEqual((run["status"], run["added"]), ("partial", 1))
        self.assertIn("incomplete crawl", run["errors"])
        self.assertEqual(len(self.listings("mcp")), 1)


class WellknownBindingTests(TemporaryDatabaseTestCase):
    def assert_hold(self, kind, row, reason):
        before, sources, bindings = self.listings(kind), self.sources(), self.bindings()
        result = self.ingest_wellknown(kind, [row])
        self.assert_result(result, errors=["source binding held: " + reason])
        self.assertEqual(self.listings(kind), before)
        self.assertEqual(self.sources(), sources)
        self.assertEqual(self.bindings(), bindings)
        matching = [issue for issue in self.issues()
                    if issue["kind"] == kind and issue["identity_hash"] == _sha(row["source_id"])]
        self.assertEqual(len(matching), 1)
        self.assert_issue(matching[0], kind, "wellknown", row, reason, "hold")
        self.assertIsNone(matching[0]["resolved_at"])

    def test_new_listing_and_repeat_have_one_hashed_binding(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"new-{kind}", source="wellknown")
                original = deepcopy(row)
                self.assert_result(self.ingest_wellknown(kind, [row]), added=1)
                before = self.listings(kind)
                self.assert_result(self.ingest_wellknown(kind, [row]), updated=1)
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(row, original)
                binding = self.bindings(kind)
                self.assertEqual(len(binding), 1)
                self.assertEqual(binding[0]["listing_id"], before[0]["id"])
                self.assertEqual(binding[0]["identity_hash"], _sha(row["source_id"]))
                self.assertEqual(binding[0]["endpoint_hash"], _sha(db._exact_endpoint(row["endpoint_url"])))
                serialized = json.dumps(binding)
                # The upstream ID is retained for source-only privacy
                # suppression during purge, and removed with the binding.
                self.assertEqual(binding[0]['source_id'], row['source_id'])
                self.assertNotIn(row["endpoint_url"], serialized)
                self.assertEqual(self.issues(), [])

    def test_existing_listing_metadata_slug_owner_and_provenance_are_untouched(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"owned-{kind}", source="submission", health="ok", latency_ms=17)
                lid = self.seed(kind, row, owner_verified=42,
                                owner_edited=json.dumps({"name": "Owner's name"}), quality_score=91)
                before, sources = self.listings(kind), self.sources()
                incoming = _row(kind, f"replacement-{kind}", row["endpoint_url"] + "/",
                                source="wellknown", description="Must not replace", health="down")
                self.assertEqual(self.bind(kind, incoming), (False, lid))
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                self.assertEqual(self.bindings(kind)[0]["listing_id"], lid)
                self.assertEqual(self.issues(), [])

    def test_matching_legacy_source_slot_only_refreshes_last_seen(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"legacy-slot-{kind}", source="wellknown")
                lid = self.legacy(kind, row, owner_verified=17, owner_edited='{"name":"Pinned"}')
                before = self.listings(kind)
                source_before = next(r for r in self.sources() if r["kind"] == kind)
                changed = dict(row, slug="changed-slug", name="Changed name",
                               source_url="https://upstream.example/changed")
                with patch.object(wellknown_binding.time, "time", return_value=500):
                    self.assertEqual(self.bind(kind, changed), (False, lid))
                self.assertEqual(self.listings(kind), before)
                source_after = next(r for r in self.sources() if r["kind"] == kind)
                self.assertEqual(source_after, dict(source_before, last_seen=500))

    def test_normalized_query_duplicate_prefers_primary_identity_not_exact_sql_match(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                endpoint = f"https://duplicate-{kind}.example?query=AbC"
                unrelated = _row(kind, f"unrelated-{kind}", endpoint, source="catalog")
                other = self.seed(kind, unrelated)
                owned = _row(kind, f"primary-{kind}", endpoint.replace("?", "/?"), source="wellknown")
                owner = self.legacy(kind, owned, owner_verified=3)
                self.assertLess(other, owner)
                before = self.listings(kind)
                incoming = dict(owned, endpoint_url=endpoint, name="New upstream name", slug="new-slug")
                self.assertEqual(self.bind(kind, incoming), (False, owner))
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(len(before), 2)
                self.assertEqual(self.bindings(kind)[0]["listing_id"], owner)

    def test_normalized_query_duplicate_prefers_secondary_identity_owner(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                endpoint = f"https://secondary-{kind}.example?query=AbC"
                self.seed(kind, _row(kind, f"exact-{kind}", endpoint, source="catalog"))
                owned = _row(kind, f"secondary-owner-{kind}", endpoint.replace("?", "/?"), source="submission")
                lid = self.legacy(kind, owned)
                sid = f"secondary-upstream-{kind}"
                self.add_source(kind, lid, sid)
                before = self.listings(kind)
                incoming = _row(kind, f"new-secondary-{kind}", endpoint, source="wellknown", source_id=sid)
                self.assertEqual(self.bind(kind, incoming), (False, lid))
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.bindings(kind)[0]["listing_id"], lid)

    def test_multiple_normalized_identity_owners_prefer_primary_without_deletion(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                endpoint = f"https://owners-{kind}.example?query=AbC"
                sid = f"duplicate-identity-{kind}"
                secondary = self.legacy(kind, _row(kind, f"older-secondary-{kind}", endpoint, source="catalog"))
                self.add_source(kind, secondary, sid)
                primary = self.legacy(kind, _row(
                    kind, f"newer-primary-{kind}", endpoint.replace("?", "/?"),
                    source="wellknown", source_id=sid,
                ))
                before = self.listings(kind)
                incoming = _row(kind, f"incoming-{kind}", endpoint, source="wellknown", source_id=sid)
                self.assertEqual(self.bind(kind, incoming), (False, primary))
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(len(before), 2)
                self.assertEqual(self.bindings(kind)[0]["listing_id"], primary)

    def test_one_entity_root_and_mcp_endpoints_bind_distinct_existing_rows(self):
        for kind in ("mcp", "a2a"):
            for both_primary in (False, True):
                with self.subTest(kind=kind, both_primary=both_primary):
                    label = f"multi-{kind}-{int(both_primary)}"
                    root, path = f"https://{label}.example/", f"https://{label}.example/mcp"
                    sid = f"upstream-{label}"
                    first = self.legacy(kind, _row(kind, label + "-root", root,
                                                  source="wellknown", source_id=sid))
                    second = self.legacy(kind, _row(kind, label + "-path", path,
                                                   source="wellknown" if both_primary else "catalog",
                                                   source_id=sid if both_primary else label + "-catalog"))
                    before, sources = self.listings(kind), self.sources()
                    for endpoint, lid in ((root, first), (path, second)):
                        row = _row(kind, label + "-incoming", endpoint, source="wellknown", source_id=sid)
                        self.assertEqual(self.bind(kind, row), (False, lid))
                    self.assertEqual(self.listings(kind), before)
                    # Only timestamps of existing matching legacy slots may change.
                    self.assertEqual([{k: v for k, v in r.items() if k != "last_seen"} for r in self.sources()],
                                     [{k: v for k, v in r.items() if k != "last_seen"} for r in sources])
                    bindings = [r for r in self.bindings(kind) if r["identity_hash"] == _sha(sid)]
                    self.assertEqual({r["listing_id"] for r in bindings}, {first, second})
                    self.assertEqual({r["endpoint_hash"] for r in bindings},
                                     {_sha(root.rstrip("/")), _sha(path)})
        self.assertEqual(self.issues(), [])

    def test_multiple_aliases_share_endpoint_without_overwriting_original_source_slot(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                original = _row(kind, f"alias-original-{kind}", source="wellknown")
                with patch.object(wellknown_binding.time, "time", return_value=100):
                    created, lid = self.bind(kind, original)
                self.assertTrue(created)
                before, sources = self.listings(kind), self.sources()
                aliases = [_row(kind, f"alias-{kind}-{n}", original["endpoint_url"], source="wellknown")
                           for n in (1, 2)]
                for timestamp in (200, 300):
                    with patch.object(wellknown_binding.time, "time", return_value=timestamp):
                        for row in aliases:
                            self.assertEqual(self.bind(kind, row), (False, lid))
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                bindings = self.bindings(kind)
                self.assertEqual(len(bindings), 3)
                self.assertEqual({r["listing_id"] for r in bindings}, {lid})
                self.assertEqual({r["identity_hash"] for r in bindings},
                                 {_sha(row["source_id"]) for row in [original] + aliases})
                for binding in bindings:
                    if binding["identity_hash"] != _sha(original["source_id"]):
                        self.assertEqual((binding["first_seen"], binding["last_seen"]), (200, 300))

    def test_suppression_precedes_existing_endpoint_fast_path_and_validation(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                original = _row(kind, f"unsuppressed-target-{kind}", source="catalog")
                self.seed(kind, original)
                incoming = _row(kind, f"suppressed-private-{kind}", original["endpoint_url"], source="wellknown")
                with db.writer() as conn:
                    db._add_privacy_suppression(conn, kind, "source",
                                                "wellknown\0" + incoming["source_id"], "fixture", "test")
                before, sources = self.listings(kind), self.sources()
                with (
                    patch.object(wellknown_binding, "validate_endpoint") as validate,
                    patch.object(db, "_find_exact_endpoint_row") as lookup,
                    db.writer() as conn,
                ):
                    with self.assertRaises(db.PrivacySuppressedListingError):
                        wellknown_binding.upsert_listing(conn, kind, incoming)
                validate.assert_not_called()
                lookup.assert_not_called()
                malformed = dict(incoming, endpoint_url="https://api.example/{PRIVATE}")
                with patch.object(ingestion, "validate_endpoint") as validate:
                    self.assert_result(self.ingest_wellknown(kind, [incoming, malformed]), rejected=2)
                validate.assert_not_called()
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                self.assertEqual(self.bindings(), [])
                self.assertEqual(self.issues(), [])

    def test_suppressed_target_is_rechecked_before_supplemental_binding(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                original = _row(kind, f"private-target-{kind}", source="catalog")
                self.seed(kind, original)
                with db.writer() as conn:
                    db._add_privacy_suppression(conn, kind, "slug", original["slug"], "fixture", "test")
                incoming = _row(kind, f"fresh-alias-{kind}", original["endpoint_url"], source="wellknown")
                before, sources = self.listings(kind), self.sources()
                self.assert_result(self.ingest_wellknown(kind, [incoming]), rejected=1)
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                self.assertEqual(self.bindings(), [])
                self.assertEqual(self.issues(), [])

    def test_owner_changed_endpoint_is_held_even_if_new_endpoint_already_exists(self):
        for kind in ("mcp", "a2a"):
            for flag in ("owner_verified", "owner_edited"):
                with self.subTest(kind=kind, flag=flag):
                    label = f"held-{kind}-{flag.replace('_', '-')}"
                    owned = _row(kind, label, source="wellknown")
                    self.seed(kind, owned, **{flag: 7 if flag == "owner_verified" else '{"name":"Pinned"}'})
                    other = _row(kind, label + "-occupant", source="catalog")
                    self.seed(kind, other)
                    incoming = dict(owned, endpoint_url=other["endpoint_url"], slug=label + "-incoming")
                    self.assert_hold(kind, incoming, "wellknown_owner_endpoint_changed")

    def test_preexisting_binding_target_endpoint_change_is_held(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"bound-{kind}", source="wellknown")
                _, lid = self.bind(kind, row)
                with db.writer() as conn:
                    conn.execute(f"UPDATE {TABLES[kind]} SET endpoint_url=? WHERE id=?",
                                 (f"https://moved-{kind}.example/mcp", lid))
                self.assert_hold(kind, row, "wellknown_binding_target_changed")

    def test_unseen_second_endpoint_for_existing_entity_is_held_not_rekeyed(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"root-only-{kind}", f"https://root-only-{kind}.example/", source="wellknown")
                self.bind(kind, row)
                incoming = dict(row, endpoint_url=row["endpoint_url"] + "mcp", slug=f"second-{kind}")
                self.assert_hold(kind, incoming, "wellknown_new_endpoint_requires_review")

    def test_synthetic_a2a_card_collision_is_held_without_mutating_original_listing(self):
        existing = _row("a2a", "real-agent", "https://agent.example/a2a", source="a2a-registry",
                        card_url="https://agent.example/.well-known/agent-card.json")
        self.seed("a2a", existing)
        incoming = _row("a2a", "synthetic-agent", "https://agent.example/other", source="wellknown",
                        card_url=existing["card_url"] + "/")
        self.assert_hold("a2a", incoming, "wellknown_synthetic_card_collision")
        self.assert_no_listing_artifacts("a2a", incoming)

    def test_missing_upstream_identity_is_rejected_without_listing_or_binding(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"no-identity-{kind}", source="wellknown", source_id=None)
                self.assert_result(self.ingest_wellknown(kind, [row]), rejected=1)
                self.assertEqual(self.listings(kind), [])
                self.assertEqual(self.bindings(), [])
                issue = next(r for r in self.issues() if r["kind"] == kind)
                self.assert_issue(issue, kind, "wellknown", row, "missing_upstream_identity")

    def test_path_case_and_query_variants_do_not_share_bindings(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                endpoints = [f"https://variants-{kind}.example/{path}?key={key}"
                             for path, key in (("MCP", "AbC"), ("mcp", "AbC"), ("MCP", "abc"))]
                ids = []
                for n, endpoint in enumerate(endpoints):
                    created, lid = self.bind(kind, _row(kind, f"variant-{kind}-{n}", endpoint, source="wellknown"))
                    self.assertTrue(created)
                    ids.append(lid)
                self.assertEqual(len(set(ids)), 3)
                self.assertEqual(len(self.bindings(kind)), 3)
                self.assertEqual({r["endpoint_url"] for r in self.listings(kind)}, set(endpoints))

    def test_retirement_removes_binding_and_does_not_create_ingestion_issue(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                row = _row(kind, f"retire-bound-{kind}", source="wellknown")
                self.bind(kind, row)
                self.assertEqual(len(self.bindings(kind)), 1)
                with db.writer() as conn:
                    db.retire_listing(conn, kind, row["slug"], "fixture retirement", "test")
                self.assertEqual(self.bindings(kind), [])
                self.assert_result(self.ingest_wellknown(kind, [row]), rejected=1)
                self.assertEqual(self.listings(kind), [])
                self.assertEqual(self.sources(), [])
                self.assertEqual(self.issues(), [])


if __name__ == "__main__":
    unittest.main()