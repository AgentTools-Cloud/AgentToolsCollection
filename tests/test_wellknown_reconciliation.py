"""Conservative binding reconciliation and endpoint-scoped issue resolution."""

from copy import deepcopy
from unittest.mock import patch

from test_ingestion_pipeline import TemporaryDatabaseTestCase, TABLES, _row, _upsert
from directory import db, ingestion


class WellknownReconciliationTests(TemporaryDatabaseTestCase):
    def test_endpoint_hash_migration_is_idempotent_and_preserves_legacy_audits(self):
        with db.writer() as conn:
            conn.executescript("""
                DROP TABLE ingestion_issues;
                CREATE TABLE ingestion_issues (
                    kind TEXT,source TEXT,identity_hash TEXT,content_hash TEXT,
                    disposition TEXT,reason TEXT,first_seen INTEGER,last_seen INTEGER,
                    occurrences INTEGER DEFAULT 1,resolved_at INTEGER,
                    PRIMARY KEY(kind,source,identity_hash,content_hash,reason)
                );
                INSERT INTO ingestion_issues VALUES
                    ('mcp','wellknown','id-one','content-one','hold','reason',1,2,3,NULL),
                    ('mcp','wellknown','id-two','content-two','hold','reason',1,2,3,4);
            """)
        prior = self.query("SELECT * FROM ingestion_issues ORDER BY identity_hash")
        db.init_db(self.path)
        db.init_db(self.path)
        after = self.query("SELECT * FROM ingestion_issues ORDER BY identity_hash")
        self.assertEqual([{k: v for k, v in row.items() if k != "endpoint_hash"}
                          for row in after], prior)
        self.assertTrue(all(row["endpoint_hash"] is None for row in after))

    def drift(self, kind="mcp", label="drift"):
        registry = "mcp-registry" if kind == "mcp" else "a2aregistry"
        original = _row(kind, label, source=registry)
        lid = self.seed(kind, original)
        incoming = dict(original, source="wellknown", source_id="wellknown-" + label)
        self.assertEqual(self.bind(kind, incoming), (False, lid))
        changed = dict(original, endpoint_url=f"https://moved-{label}.example/{kind}")
        with db.writer() as conn:
            created, moved_id = _upsert(kind)(conn, deepcopy(changed))
        self.assertFalse(created)
        self.assertEqual(moved_id, lid)
        return original, incoming, lid

    def test_drift_rebinds_only_to_unique_independent_exact_existing_target(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                original, incoming, old_id = self.drift(kind, "rebind-" + kind)
                replacement = _row(kind, "replacement-" + kind, original["endpoint_url"], source="submission")
                new_id = self.seed(kind, replacement)
                before, sources = self.listings(kind), self.sources()
                old_binding = self.bindings(kind)[0]
                self.assert_result(self.ingest_wellknown(kind, [incoming]), updated=1)
                self.assert_result(self.ingest_wellknown(kind, [incoming]), updated=1)
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                binding = self.bindings(kind)[0]
                self.assertEqual(binding["listing_id"], new_id)
                self.assertNotEqual(new_id, old_id)
                for field in ("identity_hash", "endpoint_hash", "source_id", "first_seen"):
                    self.assertEqual(binding[field], old_binding[field])

    def test_without_existing_replacement_drift_stays_held(self):
        _, incoming, _ = self.drift()
        before = self.bindings()
        result = self.ingest_wellknown("mcp", [incoming])
        self.assert_result(result, errors=["source binding held: wellknown_binding_target_changed"])
        self.assertEqual(self.bindings(), before)

    def test_duplicate_normalized_replacements_remain_held(self):
        original, incoming, _ = self.drift(label="duplicates")
        self.legacy("mcp", _row("mcp", "duplicate-1", original["endpoint_url"], source="manual"))
        self.legacy("mcp", _row("mcp", "duplicate-2", original["endpoint_url"] + "/", source="manual"))
        before = self.bindings()
        self.assert_result(self.ingest_wellknown("mcp", [incoming]),
                           errors=["source binding held: wellknown_binding_target_changed"])
        self.assertEqual(self.bindings(), before)

    def test_owner_modified_old_or_replacement_target_is_never_rebound(self):
        for target in ("old", "replacement"):
            with self.subTest(target=target):
                original, incoming, old_id = self.drift(label="owned-" + target)
                new_id = self.seed("mcp", _row("mcp", "occupant-" + target,
                                               original["endpoint_url"], source="manual"))
                with db.writer() as conn:
                    conn.execute("UPDATE mcp_servers SET owner_edited=? WHERE id=?",
                                 ('{"endpoint_url":"Pinned"}', old_id if target == "old" else new_id))
                before = self.bindings()
                self.assert_result(self.ingest_wellknown("mcp", [incoming]),
                                   errors=["source binding held: wellknown_binding_target_changed"])
                self.assertEqual(self.bindings(), before)

    def test_query_or_path_variant_is_not_an_exact_replacement(self):
        original, incoming, _ = self.drift(label="query")
        self.seed("mcp", _row("mcp", "query-variant", original["endpoint_url"] + "?tenant=AbC"))
        self.assert_result(self.ingest_wellknown("mcp", [incoming]),
                           errors=["source binding held: wellknown_binding_target_changed"])

    def test_suppressed_replacement_is_not_bound_or_ingestion_audited(self):
        original, incoming, _ = self.drift(label="private")
        replacement = _row("mcp", "private-replacement", original["endpoint_url"], source="manual")
        self.seed("mcp", replacement)
        with db.writer() as conn:
            db._add_privacy_suppression(conn, "mcp", "slug", replacement["slug"], "fixture", "test")
        before = self.bindings()
        self.assert_result(self.ingest_wellknown("mcp", [incoming]), rejected=1)
        self.assertEqual(self.bindings(), before)
        self.assertEqual(self.issues(), [])

    def test_sibling_success_cannot_resolve_another_endpoint_hold(self):
        first = _row("mcp", "first", source="wellknown", source_id="multi-endpoint")
        self.bind("mcp", first)
        second = dict(first, endpoint_url="https://second.example/mcp", slug="second")
        self.assert_result(self.ingest_wellknown("mcp", [second]),
                           errors=["source binding held: wellknown_new_endpoint_requires_review"])
        issue = self.issues()[0]
        self.assertEqual(issue["endpoint_hash"], ingestion.digest(db._exact_endpoint(second["endpoint_url"])))
        self.assert_result(self.ingest_wellknown("mcp", [first]), updated=1)
        self.assertIsNone(self.issues()[0]["resolved_at"])
        self.seed("mcp", _row("mcp", "independent-second", second["endpoint_url"], source="manual"))
        self.assert_result(self.ingest_wellknown("mcp", [second]), updated=1)
        self.assertIsNotNone(self.issues()[0]["resolved_at"])

    def test_legacy_null_endpoint_hash_only_resolves_matching_content(self):
        first = _row("mcp", "legacy-first", source="wellknown", source_id="legacy-multi")
        self.bind("mcp", first)
        second = dict(first, endpoint_url="https://legacy-second.example/mcp", slug="legacy-second")
        self.ingest_wellknown("mcp", [second])
        with db.writer() as conn:
            conn.execute("UPDATE ingestion_issues SET endpoint_hash=NULL")
        self.ingest_wellknown("mcp", [first])
        self.assertIsNone(self.issues()[0]["resolved_at"])
        self.seed("mcp", _row("mcp", "legacy-occupant", second["endpoint_url"], source="manual"))
        self.ingest_wellknown("mcp", [second])
        self.assertIsNotNone(self.issues()[0]["resolved_at"])

    def test_changed_content_at_successful_endpoint_resolves_endpoint_versions_only(self):
        first = _row("mcp", "versions-first", source="wellknown", source_id="versions-multi")
        self.bind("mcp", first)
        second = dict(first, endpoint_url="https://versions-second.example/mcp", slug="versions-second")
        third = dict(first, endpoint_url="https://versions-third.example/mcp", slug="versions-third")
        self.ingest_wellknown("mcp", [second, dict(second, description="changed"), third])
        self.seed("mcp", _row("mcp", "versions-occupant", second["endpoint_url"], source="manual"))
        self.ingest_wellknown("mcp", [dict(second, description="new successful content")])
        self.assertEqual(sum(issue["resolved_at"] is None for issue in self.issues()), 1)


class ReviewedAliasTests(TemporaryDatabaseTestCase):
    def alias(self, kind, row, lid):
        from directory import wellknown_binding
        with db.writer() as conn:
            wellknown_binding.record_reviewed_alias(conn, kind, row["source_id"], row["endpoint_url"], lid)

    def test_reviewed_alias_resolves_hold_without_touching_listings(self):
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                first = _row(kind, f"alias-first-{kind}", source="wellknown", source_id=f"alias-{kind}")
                _, lid = self.bind(kind, first)
                second = dict(first, endpoint_url=f"https://alias-alt-{kind}.example/{kind}")
                self.ingest_wellknown(kind, [second])
                held = [i for i in self.issues() if i["kind"] == kind and i["resolved_at"] is None]
                self.assertEqual(len(held), 1)
                before, sources, bindings = self.listings(kind), self.sources(), self.bindings(kind)
                self.alias(kind, second, lid)
                self.assert_result(self.ingest_wellknown(kind, [second]), updated=1)
                self.assert_result(self.ingest_wellknown(kind, [second]), updated=1)
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.sources(), sources)
                self.assertEqual(self.bindings(kind), bindings)
                self.assertFalse([i for i in self.issues() if i["kind"] == kind and i["resolved_at"] is None])

    def test_alias_is_scoped_to_exact_endpoint(self):
        first = _row("mcp", "scoped-first", source="wellknown", source_id="scoped")
        _, lid = self.bind("mcp", first)
        second = dict(first, endpoint_url="https://scoped-second.example/mcp")
        third = dict(first, endpoint_url="https://scoped-third.example/mcp")
        self.alias("mcp", second, lid)
        self.assert_result(self.ingest_wellknown("mcp", [third]),
                           errors=["source binding held: wellknown_new_endpoint_requires_review"])

    def test_reviewed_rejection_drops_endpoint_without_hold(self):
        from directory import wellknown_binding
        for kind in ("mcp", "a2a"):
            with self.subTest(kind=kind):
                first = _row(kind, f"reject-first-{kind}", source="wellknown", source_id=f"reject-{kind}")
                self.bind(kind, first)
                second = dict(first, endpoint_url=f"https://reject-alt-{kind}.example/{kind}")
                third = dict(first, endpoint_url=f"https://reject-other-{kind}.example/{kind}")
                before, bindings = self.listings(kind), self.bindings(kind)
                with db.writer() as conn:
                    wellknown_binding.record_reviewed_rejection(conn, kind, second["source_id"], second["endpoint_url"])
                self.assert_result(self.ingest_wellknown(kind, [second]), rejected=1)
                self.assertEqual(self.listings(kind), before)
                self.assertEqual(self.bindings(kind), bindings)
                self.assertFalse([i for i in self.issues() if i["kind"] == kind and i["resolved_at"] is None])
                self.assert_result(self.ingest_wellknown(kind, [third]),
                                   errors=["source binding held: wellknown_new_endpoint_requires_review"])

    def test_alias_cannot_target_rejection_sentinel(self):
        from directory import wellknown_binding
        with db.writer() as conn, self.assertRaises(ValueError):
            wellknown_binding.record_reviewed_alias(conn, "mcp", "x", "https://x.example/mcp",
                                                    wellknown_binding.REVIEW_REJECTED)

    def test_alias_to_deleted_listing_falls_back_to_hold(self):
        first = _row("mcp", "gone-first", source="wellknown", source_id="gone")
        _, lid = self.bind("mcp", first)
        second = dict(first, endpoint_url="https://gone-second.example/mcp")
        other = self.seed("mcp", _row("mcp", "gone-target", source="manual"))
        self.alias("mcp", second, other)
        with db.writer() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id=?", (other,))
        self.assert_result(self.ingest_wellknown("mcp", [second]),
                           errors=["source binding held: wellknown_new_endpoint_requires_review"])
