"""Explicit MCP catalog ingestion, with no network or production DB access."""

from copy import deepcopy
from unittest.mock import patch

from test_ingestion_pipeline import TemporaryDatabaseTestCase, _row
from directory import crawlers, db
import httpx


class CatalogIngestionTests(TemporaryDatabaseTestCase):
    def catalog(self, label="catalog", **changes):
        row = _row("mcp", label, source="smithery",
                   package_name="example/" + label)
        row.update(kind="catalog", endpoint_url=None)
        row.update(changes)
        return row

    def test_explicit_catalog_is_inserted_and_repeated_without_remote_url(self):
        row = self.catalog()
        self.assert_result(self.ingest("mcp", [row], source="smithery"), added=1)
        self.assert_result(self.ingest("mcp", [row], source="smithery"), updated=1)
        listing = self.listings("mcp")[0]
        self.assertEqual(listing["kind"], "catalog")
        self.assertIsNone(listing["endpoint_url"])
        self.assertEqual(self.issues(), [])

    def test_catalog_exception_does_not_accept_other_missing_or_invalid_endpoints(self):
        cases = [self.catalog("untyped", kind=None),
                 self.catalog("callable", kind="callable"),
                 self.catalog("empty", endpoint_url=""),
                 self.catalog("template", endpoint_url="https://api.example/{TENANT}"),
                 self.catalog("method", endpoint_url="POST /mcp")]
        self.assert_result(self.ingest("mcp", cases, source="smithery"), rejected=5)
        self.assertEqual(self.listings("mcp"), [])
        a2a = _row("a2a", "a2a-catalog", endpoint_url=None)
        a2a["kind"] = "catalog"
        self.assert_result(self.ingest("a2a", [a2a]), rejected=1)

    def test_catalog_recrawl_cannot_demote_or_erase_existing_callable(self):
        original = _row("mcp", "remote", source="smithery", transport="streamable-http",
                        auth_method="bearer_api_key", health="ok", tool_count=7)
        self.seed("mcp", original, owner_edited='{"name":"Pinned name"}')
        before = self.listings("mcp")[0]
        incoming = self.catalog("remote", source_id=original["source_id"],
                                name="Local metadata", health="down")
        self.assert_result(self.ingest("mcp", [incoming], source="smithery"), updated=1)
        after = self.listings("mcp")[0]
        for key in before:
            if key not in ("last_seen", "updated_at"):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["kind"], "callable")

    def test_catalog_can_promote_to_valid_callable(self):
        row = self.catalog("promotion")
        self.ingest("mcp", [row], source="smithery")
        remote = dict(row, kind="callable", endpoint_url="https://promotion.example/mcp")
        self.assert_result(self.ingest("mcp", [remote], source="smithery"), updated=1)
        self.assertEqual(self.listings("mcp")[0]["kind"], "callable")
        self.assertEqual(self.listings("mcp")[0]["endpoint_url"], remote["endpoint_url"])

    def test_fixed_catalog_resolves_previous_missing_endpoint_audit(self):
        row = self.catalog("fixed", kind=None)
        self.ingest("mcp", [row], source="smithery")
        fixed = dict(row, kind="catalog")
        self.assert_result(self.ingest("mcp", [fixed], source="smithery"), added=1)
        self.assertIsNotNone(self.issues()[0]["resolved_at"])

    def test_retired_and_suppressed_catalog_are_not_recreated_or_audited(self):
        row = self.catalog("retired")
        self.seed("mcp", row)
        with db.writer() as conn:
            db.retire_listing(conn, "mcp", row["slug"], "fixture", "test")
        self.assert_result(self.ingest("mcp", [row], source="smithery"), rejected=1)
        self.assertEqual(self.issues(), [])
        other = self.catalog("suppressed")
        with db.writer() as conn:
            db._add_privacy_suppression(conn, "mcp", "source",
                                       "smithery\0" + other["source_id"], "fixture", "test")
        self.assert_result(self.ingest("mcp", [other], source="smithery"), rejected=1)
        self.assertEqual(self.listings("mcp"), [])
        self.assertEqual(self.issues(), [])

    def test_smithery_producer_explicitly_marks_catalog_and_callable(self):
        payload = {"servers": [
            {"qualifiedName": "example/local", "remote": False, "isDeployed": True},
            {"qualifiedName": "example/remote", "remote": True, "isDeployed": True},
            {"qualifiedName": "example/pending", "remote": True, "isDeployed": False},
        ], "pagination": {"totalPages": 1}}
        client_factory = httpx.Client
        def factory(**kwargs):
            return client_factory(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=deepcopy(payload))), **kwargs)
        with patch.object(crawlers.httpx, "Client", side_effect=factory):
            rows = crawlers.fetch_smithery(max_pages=1)
        self.assertEqual([r["kind"] for r in rows], ["catalog", "callable", "catalog"])
        self.assert_result(self.ingest("mcp", rows, source="smithery"), added=3)
