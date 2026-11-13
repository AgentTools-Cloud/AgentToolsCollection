import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from directory import botsrated, capkey, db
from test_ingestion_pipeline import TemporaryDatabaseTestCase


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
        payload = {"total": 2, "page": 1, "pageSize": 50, "totalPages": 1,
                   "items": [self.item(kind="MCP", isTemplate=False),
                             self.item(id=8, kind="MCP", badge="dead", isTemplate=False)]}
        client = capkey.httpx.Client(transport=capkey.httpx.MockTransport(
            lambda _request: capkey.httpx.Response(200, json=payload)))
        with patch.object(capkey.httpx, "Client", return_value=client):
            rows = capkey.fetch_capkey_mcp()
        self.assertEqual(len(rows), 1)

    def test_fetch_uses_bounded_public_search_pages(self):
        requested = []

        def item(identifier):
            return {"id": identifier, "kind": "MCP", "badge": "live",
                    "isTemplate": False, "name": f"Fixture {identifier}",
                    "url": f"https://api.example/{identifier}/mcp"}

        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/search")
            self.assertEqual(request.url.params.get("kind"), "mcp")
            self.assertEqual(request.url.params.get("badge"), "live")
            page = int(request.url.params.get("page", "1"))
            requested.append(page)
            return capkey.httpx.Response(200, json={
                "total": 3, "page": page, "pageSize": 2, "totalPages": 2,
                "items": [item(1), item(2)] if page == 1 else [item(3)],
            })

        client = capkey.httpx.Client(transport=capkey.httpx.MockTransport(handler))
        with patch.object(capkey.httpx, "Client", return_value=client), \
                patch.object(capkey, "PAGE_LIMIT", 2, create=True):
            rows = capkey.fetch_capkey_mcp()
        self.assertEqual(requested, [1, 2])
        self.assertEqual({row["source_id"] for row in rows}, {"1", "2", "3"})
        self.assertTrue(all(row.get("_partial_metadata") for row in rows))

    def test_partial_capkey_metadata_preserves_existing_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "test.db")
            db.init_db(path)
            with db.writer(path) as conn:
                row = capkey._row(self.item())
                row.update(description="Existing description", transport="sse",
                           auth_method="api-key")
                db.upsert_mcp_server(conn, row)
                refresh = capkey._row(self.item())
                refresh.update(description=None, transport=None, auth_method=None,
                               _partial_metadata=True)
                db.upsert_mcp_server(conn, refresh)
            with db.connect(path, read_only=True) as conn:
                stored = conn.execute("SELECT * FROM mcp_servers").fetchone()
                self.assertEqual(stored["description"], "Existing description")
                self.assertEqual(stored["transport"], "sse")
                self.assertEqual(stored["auth_method"], "api-key")

    def fallback_catalog(self, *, truncated=False, duplicate=False, expired=False,
                         broken=False, oversized=False, malformed_template=False):
        requested = []
        real_client = capkey.httpx.Client

        def record(identifier, catalog=False):
            return {"id": identifier, "kind": "mcp_http" if catalog else "MCP",
                    "badge": "live", "status": "active", "is_template": False,
                    "isTemplate": False, "name": "Fixture %d" % identifier,
                    "url": "https://api.example/%d/mcp" % identifier}

        def handler(request):
            requested.append(request.url.path)
            if request.url.path == "/api/v1/search":
                page = int(request.url.params["page"])
                return capkey.httpx.Response(200, json={
                    "total": 3, "page": page, "pageSize": 2,
                    "items": [record(1), record(2)] if page == 1 else [record(2)],
                })
            self.assertEqual(request.url.path, "/api/v1/catalog.json")
            items = [record(1, True), record(2, True), record(3, True)]
            if truncated:
                items.pop()
            if duplicate:
                items[2] = record(1, True)
            if malformed_template:
                items[0]["is_template"] = {"invalid": True}
            payload = {
                "version": "0.1", "total_count": 3,
                "generated_at": "2000-01-01T00:00:00Z" if expired else datetime.now(timezone.utc).isoformat(),
                "items": items,
            }
            if broken:
                return capkey.httpx.Response(200, content=json.dumps(payload).encode()[:-12])
            return capkey.httpx.Response(200, json=payload)

        def client_factory(*arguments, **options):
            return real_client(transport=capkey.httpx.MockTransport(handler))

        with patch.object(capkey.httpx, "Client", side_effect=client_factory), \
                patch.object(capkey, "PAGE_LIMIT", 2), \
                patch.object(capkey, "MAX_CATALOG_BYTES", 64 if oversized else capkey.MAX_CATALOG_BYTES), \
                patch.object(capkey.time, "sleep"):
            if truncated or duplicate or expired or broken or oversized or malformed_template:
                with self.assertRaises(capkey.PartialCrawl) as caught:
                    capkey.fetch_capkey_mcp()
                self.assertEqual({row["source_id"] for row in caught.exception.items}, {"1", "2"})
                self.assertIn("full catalog unavailable", caught.exception.reason)
            else:
                rows = capkey.fetch_capkey_mcp()
                self.assertEqual({row["source_id"] for row in rows}, {"1", "2", "3"})
                self.assertTrue(all(row["_partial_metadata"] for row in rows))
                self.assertTrue(all(row["transport"] is None for row in rows))
        self.assertEqual(requested, ["/api/v1/search", "/api/v1/search", "/api/v1/catalog.json"])

    def test_complete_catalog_recovers_duplicate_search_page(self):
        self.fallback_catalog()

    def test_incomplete_catalog_keeps_partial_search(self):
        self.fallback_catalog(truncated=True)

    def test_duplicate_catalog_keeps_partial_search(self):
        self.fallback_catalog(duplicate=True)

    def test_expired_catalog_keeps_partial_search(self):
        self.fallback_catalog(expired=True)

    def test_broken_catalog_keeps_partial_search(self):
        self.fallback_catalog(broken=True)

    def test_oversized_catalog_keeps_partial_search(self):
        self.fallback_catalog(oversized=True)

    def test_non_scalar_template_keeps_partial_search(self):
        self.fallback_catalog(malformed_template=True)

    def test_catalog_deadline_keeps_partial_search(self):
        partial = [capkey._row(self.item())]
        client = capkey.httpx.Client(transport=capkey.httpx.MockTransport(
            lambda request: capkey.httpx.Response(200, content=b"{}")))
        with patch.object(capkey, "_fetch_search_pages", side_effect=capkey.PartialCrawl(partial, "fixture")), \
                patch.object(capkey.httpx, "Client", return_value=client), \
                patch.object(capkey, "MAX_SECONDS", 0):
            with self.assertRaises(capkey.PartialCrawl) as caught:
                capkey.fetch_capkey_mcp()
        self.assertEqual(caught.exception.items, partial)
        self.assertIn("TimeoutError", caught.exception.reason)


class BotsRatedTests(unittest.TestCase):
    def test_candidate_groups_are_interleaved(self):
        queues = {
            "mcp": [(-1, "mcp", "a.example", []),
                    (-1, "mcp", "b.example", [])],
            "x402": [(-1, "x402", "c.example", []),
                     (-1, "x402", "d.example", [])],
        }
        self.assertEqual(
            [item[1] for item in botsrated._interleave(queues, 4)],
            ["mcp", "x402", "mcp", "x402"],
        )

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


class BotsRatedRateLimitTests(unittest.TestCase):
    def client(self, responses):
        import httpx
        seq = iter(responses)
        return httpx.Client(transport=httpx.MockTransport(lambda req: next(seq)))

    def test_long_retry_after_stops_without_waiting(self):
        import httpx
        with patch.object(botsrated.time, "sleep") as sleep:
            with self.assertRaises(botsrated.RateLimited) as ctx:
                botsrated._get(self.client([httpx.Response(429, headers={"retry-after": "48080"})]),
                               "https://botsrated.example/x")
        self.assertEqual(ctx.exception.retry_after, 48080)
        sleep.assert_not_called()

    def test_short_retry_after_is_retried_then_succeeds(self):
        import httpx
        with patch.object(botsrated.time, "sleep") as sleep:
            r = botsrated._get(self.client([httpx.Response(429, headers={"retry-after": "5"}),
                                            httpx.Response(200, json={})]),
                               "https://botsrated.example/x")
        self.assertEqual(r.status_code, 200)
        sleep.assert_called_once_with(5)

    def test_persistent_short_429_raises_after_last_attempt(self):
        import httpx
        with patch.object(botsrated.time, "sleep"):
            with self.assertRaises(botsrated.RateLimited):
                botsrated._get(self.client([httpx.Response(429)] * 4),
                               "https://botsrated.example/x")



class BotsRatedPauseTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(botsrated.db, "init_db"))

    def test_sync_skips_while_paused(self):
        with db.writer() as conn:
            db.set_meta(conn, botsrated.PAUSE_META_KEY, str(int(botsrated.time.time()) + 600))
        with patch.object(botsrated, "fetch_covered_hosts") as hosts:
            result = botsrated.sync_botsrated_quality(limit=5)
        hosts.assert_not_called()
        self.assertEqual(result["skipped"], "rate_limited")

    def test_rate_limit_mid_run_pauses_and_keeps_fetched(self):
        import httpx
        with db.writer() as conn:
            for host in ("a.example", "b.example"):
                db.upsert_service(conn, dict(slug=host, name=host, url=f"https://{host}/x",
                                             source="fixture", source_id=host, health="ok"))
        payload = {"found": True, "valid_402": True, "served_at": "2026-11-12T00:00:00Z"}
        calls = {"n": 0}

        def fake_get(client, url, params=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise botsrated.RateLimited(3600)
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        with patch.object(botsrated, "fetch_covered_hosts", return_value={"a.example", "b.example"}), \
                patch.object(botsrated, "_get", side_effect=fake_get), \
                patch.object(botsrated.time, "sleep"):
            result = botsrated.sync_botsrated_quality(limit=5)
        self.assertEqual(result["attempted_groups"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertGreater(result["paused_until"], int(botsrated.time.time()) + 3000)
        rows = self.query("SELECT value FROM meta WHERE key=?", (botsrated.PAUSE_META_KEY,))
        self.assertEqual(int(rows[0]["value"]), result["paused_until"])


class BotsRatedExitCodeTests(unittest.TestCase):
    def run_cli(self, result):
        from directory import jobs
        with patch.object(jobs.botsrated_mod, "sync_botsrated_quality",
                          return_value=result), \
                patch("builtins.print"):
            return jobs.main(["sync-botsrated"])

    def test_rate_limited_skip_exits_zero(self):
        self.assertEqual(self.run_cli({"skipped": "rate_limited",
                                       "paused_until": 1}), 0)

    def test_errors_still_exit_one(self):
        self.assertEqual(self.run_cli({"errors": 2}), 1)
        self.assertEqual(self.run_cli({"errors": 0}), 0)
