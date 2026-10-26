import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import botsrated, capkey, db


class CapkeyTests(unittest.TestCase):
    def item(self, **extra):
        row = {
            "id": 7,
            "kind": "mcp_http",
            "url": "https://api.example/mcp",
            "name": "Example",
            "description": "A server",
            "status": "active",
            "badge": "live",
            "is_template": False,
        }
        row.update(extra)
        return row

    def test_accepts_only_live_concrete_mcp(self):
        row = capkey._row(self.item())
        self.assertIsNotNone(row)
        self.assertEqual(row["endpoint_url"], "https://api.example/mcp")
        self.assertEqual(row["source"], "capkey")
        for item in (
            self.item(kind="x402_http"),
            self.item(badge="unstable"),
            self.item(status="inactive"),
            self.item(is_template=True),
            self.item(url="https://api.example/mcp?token={token}"),
            self.item(url="https://api.example/mcp?access_token=secret"),
            self.item(url="https://api.example/mcp?demo_token=public-demo-value"),
            self.item(url="https://api.example/{tenant}/mcp"),
        ):
            self.assertIsNone(capkey._row(item), item)

    def test_fetch_maps_catalog(self):
        payload = {"items": [self.item(), self.item(id=8, badge="dead")]}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch.object(capkey.httpx.Client, "get", return_value=Response()):
            rows = capkey.fetch_capkey_mcp()
        self.assertEqual(len(rows), 1)


class BotsRatedTests(unittest.TestCase):
    def test_mcp_summary_requires_exact_endpoint(self):
        payload = {
            "found": True,
            "served_at": "2026-10-26T12:00:00Z",
            "servers": [{
                "url": "https://mcp.example/mcp",
                "measured": {
                    "last_state": "ok", "last_probe_at": "2026-10-26T11:00:00Z",
                    "tool_count": 12, "latency_p50_ms": 50,
                    "recent_window": {"ok_rate": 1.0},
                },
                "freshness": {"stale": False},
                "latest_receipt_url": "https://botsrated.com/receipt/abc",
            }],
        }
        summary = botsrated.summarize(payload, "https://mcp.example/mcp", "mcp")
        self.assertEqual(summary["state"], "ok")
        self.assertEqual(summary["tool_count"], 12)
        self.assertIsNone(botsrated.summarize(payload, "https://mcp.example/other", "mcp"))

    def test_x402_summary_and_namespace_merge(self):
        payload = {
            "found": True,
            "served_at": "2026-10-26T12:00:00Z",
            "valid_402": True,
            "terms_consistent": True,
            "latest_probe": {"status": 402, "ts": "2026-10-26T11:00:00Z", "latency_ms": 80},
            "recent_window": {"valid_402_rate": 1.0},
        }
        summary = botsrated.summarize(payload, "https://paid.example/x", "x402")
        merged = json.loads(botsrated._merge_quality(
            json.dumps({"zero": {"calls": 3}}), summary))
        self.assertEqual(merged["zero"], {"calls": 3})
        self.assertTrue(merged["botsrated"]["valid_402"])

    def test_mcp_quality_column_migrates_and_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "test.db")
            db.init_db(path)
            with db.connect(path) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_servers)")}
                self.assertIn("quality", cols)
                row = {
                    "slug": "example", "name": "Example",
                    "endpoint_url": "https://mcp.example/mcp",
                    "source": "capkey", "source_id": "7",
                    "quality": {"botsrated": {"state": "ok"}},
                }
                db.upsert_mcp_server(conn, row)
                stored = conn.execute("SELECT * FROM mcp_servers").fetchone()
                self.assertEqual(
                    db.mcp_row_to_dict(stored)["quality"],
                    {"botsrated": {"state": "ok"}},
                )

    def test_crawl_keeps_botsrated_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "test.db")
            db.init_db(path)
            with db.connect(path) as conn:
                base = {"slug": "s", "name": "S", "url": "https://p.example/x",
                        "source": "zero", "source_id": "1"}
                db.upsert_service(conn, dict(base, quality={"zero": {"a": 1}}))
                conn.execute("UPDATE services SET quality=?", (json.dumps(
                    {"zero": {"a": 1}, "botsrated": {"valid_402": True}}),))
                db.upsert_service(conn, dict(base, quality={"zero": {"a": 2}}))
                q = json.loads(conn.execute("SELECT quality FROM services").fetchone()[0])
                self.assertEqual(q["zero"], {"a": 2})
                self.assertTrue(q["botsrated"]["valid_402"])
                m = {"slug": "m", "name": "M", "endpoint_url": "https://m.example/mcp",
                     "source": "capkey", "source_id": "9"}
                db.upsert_mcp_server(conn, dict(m))
                conn.execute("UPDATE mcp_servers SET quality=?",
                             (json.dumps({"botsrated": {"state": "ok"}}),))
                db.upsert_mcp_server(conn, dict(m))
                q = json.loads(conn.execute("SELECT quality FROM mcp_servers").fetchone()[0])
                self.assertEqual(q["botsrated"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
