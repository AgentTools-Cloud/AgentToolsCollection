"""Owners can edit the tags of x402 and MCP listings they verified."""
from contextlib import closing
import json

from directory import db
from test_ingestion_pipeline import TemporaryDatabaseTestCase


HOST = "tags-owner.example"


class OwnerTagTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','tags-fixture','tags-fixture','active',1)").lastrowid
            self.oid = conn.execute(
                "INSERT INTO domain_ownership(user_id,host,method,token_hash,status,"
                "created_at,verified_at) VALUES(?,?,'wellknown_file','fixture','verified',1,1)",
                (self.uid, HOST)).lastrowid
            self.mcp = conn.execute(
                "INSERT INTO mcp_servers(slug,name,endpoint_url,source,tags,created_at,updated_at) "
                "VALUES('tags-mcp','Tags MCP',?,'submission',?,1,1)",
                ("https://%s/mcp" % HOST, json.dumps(["mcp", "api"]))).lastrowid
            self.svc = conn.execute(
                "INSERT INTO services(slug,name,url,source,source_id,created_at,updated_at) "
                "VALUES('tags-x402','Tags x402',?,'submission','tags:x402',1,1)",
                ("https://%s/pay" % HOST,)).lastrowid

    def edit(self, kind, listing_id, changes):
        with db.writer() as conn:
            return db.apply_listing_edits(conn, kind, listing_id, self.uid, self.oid,
                                          changes, via="api")

    def row(self, table, listing_id):
        with closing(db.connect(read_only=True)) as conn:
            return dict(conn.execute("SELECT * FROM %s WHERE id=?" % table,
                                     (listing_id,)).fetchone())

    def test_list_input_is_normalized_audited_and_searchable(self):
        applied, rejected = self.edit("mcp", self.mcp, {
            "tags": ["  RFP ", "procurement", "Government   contracts", "rfp"]})
        self.assertEqual((applied, rejected), (["tags"], []))
        row = self.row("mcp_servers", self.mcp)
        self.assertEqual(json.loads(row["tags"]), ["RFP", "procurement", "Government contracts"])
        self.assertIn("tags", json.loads(row["owner_edited"]))
        with closing(db.connect(read_only=True)) as conn:
            edit = conn.execute("SELECT old_value,new_value FROM listing_edits WHERE field='tags'").fetchone()
            hits = conn.execute("SELECT rowid FROM mcp_fts WHERE mcp_fts MATCH 'procurement'").fetchall()
        self.assertEqual(json.loads(edit["old_value"]), ["mcp", "api"])
        self.assertIn(self.mcp, [hit[0] for hit in hits])

    def test_comma_text_and_clearing(self):
        self.assertEqual(self.edit("x402", self.svc, {"tags": "payments, data , payments"})[0], ["tags"])
        self.assertEqual(json.loads(self.row("services", self.svc)["tags"]), ["payments", "data"])
        self.assertEqual(self.edit("x402", self.svc, {"tags": ""})[0], ["tags"])
        self.assertIsNone(self.row("services", self.svc)["tags"])

    def test_unchanged_tags_are_not_recorded(self):
        self.assertEqual(self.edit("mcp", self.mcp, {"tags": " mcp ,api, API"}), ([], []))

    def test_invalid_tags_are_rejected_without_changes(self):
        for value in (["x"], ["<script>"], ["a" * 40], [str(i) * 2 for i in range(10)], [1], {"a": 1}):
            with self.subTest(value=value):
                applied, rejected = self.edit("mcp", self.mcp, {"tags": value})
                self.assertEqual(applied, [])
                self.assertTrue(rejected)
        self.assertEqual(json.loads(self.row("mcp_servers", self.mcp)["tags"]), ["mcp", "api"])

    def test_owner_tags_survive_a_recrawl(self):
        self.edit("mcp", self.mcp, {"tags": ["procurement"]})
        existing = self.row("mcp_servers", self.mcp)
        incoming = {"tags": json.dumps(["crawler"])}
        db._restore_owner_edits(incoming, existing)
        self.assertEqual(json.loads(incoming["tags"]), ["procurement"])

    def test_a2a_has_no_tag_field(self):
        self.assertNotIn("tags", db._EDITABLE_FIELDS["a2a"])
