"""Safety scans of documentation hostnames must not create directory listings."""

from unittest.mock import patch

from directory import db, safety_service
from test_ingestion_pipeline import TemporaryDatabaseTestCase


def _probe(_url):
    return {"status": "degraded", "http_status": 405, "latency_ms": 5, "tools": None}


class SafetyScanIndexingTests(TemporaryDatabaseTestCase):
    def scan(self, url):
        with patch.object(safety_service.directory_crawlers, "probe_mcp_health", _probe):
            return safety_service.scan_endpoint(url, db_path=self.path, with_llm=False)

    def count(self):
        with db.connect(self.path, read_only=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0]

    def test_reserved_hosts_are_scanned_but_not_indexed(self):
        for url in ("https://example.com/", "https://tools.example.com/mcp",
                    "https://example.org/mcp", "https://api.invalid/mcp",
                    "https://demo.test/mcp"):
            out = self.scan(url)
            self.assertEqual(out["source"], "new_scan", url)
            self.assertIn("verdict", out)
            self.assertFalse(out["indexed"], url)
            self.assertIsNone(out["slug"], url)
        self.assertEqual(self.count(), 0)

    def test_real_reachable_host_is_still_indexed(self):
        out = self.scan("https://mcp.real-vendor.io/mcp")
        self.assertTrue(out["indexed"])
        self.assertEqual(self.count(), 1)

    def test_lookalike_host_is_not_treated_as_reserved(self):
        self.assertFalse(safety_service.is_reserved_host("myexample.com"))
        self.assertFalse(safety_service.is_reserved_host("example.com.evil.net"))
        self.assertTrue(safety_service.is_reserved_host("EXAMPLE.COM."))
