"""Endpoint text is searchable on the origin listing; submissions are limited
per payTo as well as per IP (2026-11-03)."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import db, fold_origin, jobs, routes

ORIGIN = "https://seller.example"
PAY = "0x719227f384b66BFEF00093D6d8d32fA9dC2B40e7"


def payment(pay_to=PAY):
    return {"pay_to": pay_to, "accepts": [{"payTo": pay_to, "network": "eip155:8453"}],
            "max_amount_usdc": 0.02}


class _TempDB(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="resources-search-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret"}))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        real_connect, real_writer = db.connect, db.writer
        self.real_connect = real_connect
        self.enterContext(patch.object(db, "connect", side_effect=lambda db_path=None, read_only=False, set_wal=True: real_connect(self.path, read_only=read_only, set_wal=set_wal)))
        self.enterContext(patch.object(db, "writer", side_effect=lambda db_path=None, immediate=False: real_writer(self.path, immediate=immediate)))
        self.enterContext(patch.object(db.time, "sleep"))
        db.init_db(self.path)

    def q(self, sql, params=()):
        with closing(db.connect(read_only=True)) as c:
            return [dict(r) for r in c.execute(sql, params)]

    def search(self, text):
        with closing(db.connect(read_only=True)) as c:
            return [r["slug"] for r in db.search(c, q=text, limit=50)]


class ResourcesTextSearch(_TempDB):
    def test_endpoint_description_finds_the_origin_listing(self):
        with db.writer() as c:
            db.upsert_service(c, {
                "slug": "seller", "name": "Seller utilities", "url": ORIGIN + "/v1/verify",
                "description": "Verify a USDC payment", "source": "fixture", "source_id": "s",
                "resources_text": db.resources_text([
                    {"url": ORIGIN + "/v1/btcfeecalc", "description": "Bitcoin fee calculator"},
                    {"url": ORIGIN + "/v1/lnisps", "description": "Lightning hosting by ISP"}])})
            db.upsert_service(c, {"slug": "other", "name": "Other", "url": "https://o.example/x",
                                  "description": "unrelated", "source": "fixture", "source_id": "o"})
        self.assertEqual(self.search("bitcoin fee calculator"), ["seller"])
        self.assertEqual(self.search("lightning ISP"), ["seller"])
        self.assertEqual(self.search("btcfeecalc"), ["seller"])

    def test_name_match_still_outranks_endpoint_text(self):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "weather-api", "name": "Weather API", "url": "https://w.example/x",
                                  "description": "forecast", "source": "fixture", "source_id": "w"})
            db.upsert_service(c, {"slug": "kitchen-sink", "name": "Kitchen sink", "url": "https://k.example/x",
                                  "description": "many tools", "source": "fixture", "source_id": "k",
                                  "resources_text": "v1 weather weather now and forecast"})
        with closing(db.connect(read_only=True)) as c:
            rows = db.search(c, q="weather", sort="relevance", limit=10)
        self.assertEqual([r["slug"] for r in rows][0], "weather-api")

    def test_migration_adds_column_to_existing_index_and_rebuilds(self):
        # Simulate a pre-migration database: old FTS without resources_text.
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript("""
                DROP TRIGGER services_ai; DROP TRIGGER services_ad; DROP TRIGGER services_au;
                DROP TABLE services_fts;
                CREATE VIRTUAL TABLE services_fts USING fts5(
                  name, name_zh, description, description_zh, tags, category,
                  content='services', content_rowid='id', tokenize='porter unicode61');
            """)
            c.execute("INSERT INTO services(slug,name,url,source,source_id,resources_text,created_at,updated_at) "
                      "VALUES('pre','Pre','https://p.example/x','fixture','p','v1 polymarket odds',0,0)")
            c.commit()
        db.init_db(self.path)
        self.assertEqual(self.search("polymarket"), ["pre"])
        db.init_db(self.path)  # idempotent
        self.assertEqual(self.search("polymarket"), ["pre"])

    def test_resources_text_is_bounded_and_deduplicated(self):
        t = db.resources_text([{"url": ORIGIN + "/v1/a", "description": "x" * 1000}] * 3
                              + [ORIGIN + "/v1/b?k=1"], limit=1000)
        lines = t.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(len(t), 1000)
        self.assertEqual(lines[0], "v1 a | " + "x" * 200)
        self.assertEqual(lines[1], "v1 b")

    def test_bare_path_is_filled_by_later_description(self):
        t = db.resources_text([ORIGIN + "/v1/btcfeecalc",
                               {"url": ORIGIN + "/v1/btcfeecalc", "description": "Bitcoin fee calculator"}])
        self.assertEqual(t, "v1 btcfeecalc | Bitcoin fee calculator")
        # feeding its own output back changes nothing
        self.assertEqual(db.resources_text(t.splitlines()), t)

    def test_feeding_output_back_with_new_entries_does_not_duplicate(self):
        ents = [{"url": ORIGIN + "/v1/p%d" % i, "description": "thing %d" % i} for i in range(50)]
        t1 = db.resources_text(ents)
        t2 = db.resources_text(t1.splitlines() + ents + [ORIGIN + "/v1/p3"])
        self.assertEqual(t1, t2)
        t3 = db.resources_text(t1.splitlines() + [{"url": ORIGIN + "/v1/new", "description": "fresh"}])
        self.assertEqual(len(t3.splitlines()), 51)

    def test_many_endpoints_keep_their_descriptions(self):
        ents = [{"url": ORIGIN + "/v1/p%03d" % i, "description": "endpoint number %d " % i + "y" * 150}
                for i in range(600)]
        t = db.resources_text(ents)
        self.assertEqual(len(t.splitlines()), 600)
        self.assertIn("endpoint number 599", t)


class MCPKeywordSearch(_TempDB):
    def add_mcp(self, slug, name, **fields):
        with db.writer() as connection:
            db.upsert_mcp_server(connection, {
                "slug": slug, "name": name,
                "endpoint_url": "https://%s.example/mcp" % slug,
                "source": "fixture", "source_id": slug, "health": "ok",
                **fields,
            })
            for field in ("tools_text", "quality_score", "created_at", "updated_at"):
                if field in fields:
                    connection.execute("UPDATE mcp_servers SET " + field + "=? WHERE slug=?",
                                       (fields[field], slug))

    def mcp_search(self, **params):
        app = FastAPI()
        app.include_router(routes.router)
        response = TestClient(app).get("/api/v1/mcp/search", params=params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_keyword_default_uses_relevance_not_source_confidence(self):
        self.add_mcp("procurement-title", "Procurement directory", confidence=0.55)
        self.add_mcp("general-tools", "General utilities", confidence=1.0,
                     description="Procurement is one of many utilities")
        default = self.mcp_search(q="procurement")
        relevance = self.mcp_search(q="procurement", sort="relevance")
        self.assertEqual(default["servers"][0]["slug"], "procurement-title")
        self.assertEqual([row["slug"] for row in default["servers"]],
                         [row["slug"] for row in relevance["servers"]])
        self.assertEqual(default["sort"], "default")

    def test_complete_word_does_not_match_an_unrelated_brand_prefix(self):
        self.add_mcp("tender-directory", "Government tenders")
        self.add_mcp("tenderly-tools", "Tenderly blockchain tools",
                     description="Tenderly development utilities")
        body = self.mcp_search(q="tenders")
        self.assertEqual([row["slug"] for row in body["servers"]],
                         ["tender-directory"])
        self.assertEqual(body["total_matched"], 1)

    def test_title_hit_outranks_repeated_tool_text(self):
        self.add_mcp("procurement-title", "Procurement directory",
                     tools_text="utility " * 1000)
        self.add_mcp("general-tools", "General utilities",
                     tools_text="procurement " * 100)
        for sort in ("default", "relevance"):
            with self.subTest(sort=sort):
                body = self.mcp_search(q="procurement", sort=sort)
                self.assertEqual(body["servers"][0]["slug"], "procurement-title")

    def test_keyword_default_is_consistent_across_search_paths(self):
        self.add_mcp("mcp-title", "Procurement directory", confidence=0.55)
        self.add_mcp("mcp-general", "General utilities", confidence=1.0,
                     description="Procurement utilities")
        with db.writer() as connection:
            for slug, name, description, confidence in (
                    ("service-title", "Procurement directory", "Contracts", 0.55),
                    ("service-general", "General utilities", "Procurement utilities", 1.0)):
                db.upsert_service(connection, {
                    "slug": slug, "name": name, "description": description,
                    "url": "https://%s.example/api" % slug,
                    "source": "fixture", "source_id": slug,
                    "health": "ok", "confidence": confidence})
            for slug, name, description, confidence in (
                    ("a2a-title", "Procurement directory", "Contracts", 0.55),
                    ("a2a-general", "General utilities", "Procurement utilities", 1.0)):
                connection.execute(
                    "INSERT INTO a2a_agents(slug,name,description,card_url,source,source_id,"
                    "health,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (slug, name, description, "https://%s.example/card.json" % slug,
                     "fixture", slug, "ok", confidence, 1, 1))
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        for path, result_key in (("/api/v1/search", "services"),
                                 ("/api/v1/mcp/search", "servers"),
                                 ("/api/v1/a2a/search", "agents"),
                                 ("/api/v1/resources/search", "resources")):
            with self.subTest(path=path):
                default = client.get(path, params={"q": "procurement"}).json()
                relevance = client.get(path, params={"q": "procurement", "sort": "relevance"}).json()
                self.assertEqual([row["slug"] for row in default[result_key]],
                                 [row["slug"] for row in relevance[result_key]])
                self.assertTrue(default[result_key][0]["slug"].endswith("-title"))
                self.assertFalse({field for row in default[result_key] for field in row
                                  if field.startswith("_") or field in ("search_rank", "search_priority")})

    def test_without_keyword_keeps_existing_browse_order(self):
        self.add_mcp("procurement-title", "Procurement directory", confidence=0.55)
        self.add_mcp("general-tools", "General utilities", confidence=1.0)
        for query in (None, "", "   ", "?!"):
            with self.subTest(query=query):
                params = {} if query is None else {"q": query}
                default = self.mcp_search(**params)
                relevance = self.mcp_search(**params, sort="relevance")
                self.assertEqual([row["slug"] for row in default["servers"]],
                                 ["general-tools", "procurement-title"])
                self.assertEqual(default["servers"], relevance["servers"])

    def test_explicit_sorts_and_pages_remain_stable(self):
        for index in range(5):
            self.add_mcp("procurement-%d" % index, "Procurement %d" % index,
                         confidence=0.5, quality_score=index * 10,
                         created_at=index, updated_at=index)
        for sort in ("default", "relevance", "quality", "newest", "name"):
            with self.subTest(sort=sort):
                full = self.mcp_search(q="procurement", sort=sort, limit=10)
                first = self.mcp_search(q="procurement", sort=sort, limit=2)
                second = self.mcp_search(q="procurement", sort=sort, limit=2, offset=2)
                self.assertEqual(first["servers"] + second["servers"], full["servers"][:4])
                self.assertEqual(first["total_matched"], 5)
                self.assertEqual(second["total_matched"], 5)
                if sort in ("quality", "newest"):
                    self.assertEqual([row["slug"] for row in full["servers"]],
                                     ["procurement-%d" % index for index in range(4, -1, -1)])
                elif sort == "name":
                    self.assertEqual([row["slug"] for row in full["servers"]],
                                     ["procurement-%d" % index for index in range(5)])

    def test_exact_name_group_is_complete_outside_page_window(self):
        self.add_mcp("first-directory", "Procurement directory")
        self.add_mcp("second-directory", "Procurement directory")
        body = self.mcp_search(q="Procurement directory", limit=1)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["exact_name_matches"]["count"], 2)
        self.assertTrue(body["exact_name_matches"]["complete"])

    def test_duplicate_endpoints_do_not_leave_holes_in_pages(self):
        with db.writer() as connection:
            for index in range(5):
                db.upsert_service(connection, {
                    "slug": "procurement-%d" % index,
                    "name": "Procurement tools",
                    "url": "https://service-%d.example/api" % index,
                    "mcp_url": "https://procurement-%d.example/mcp" % max(0, index - 2),
                    "source": "fixture", "source_id": "procurement-%d" % index,
                })
        full = self.mcp_search(q="procurement", limit=10)
        first = self.mcp_search(q="procurement", limit=2)
        second = self.mcp_search(q="procurement", limit=2, offset=2)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["servers"] + second["servers"], full["servers"])
        self.assertEqual(first["total_matched"], 3)

    def test_total_matched_counts_all_unique_endpoints_before_pagination(self):
        with db.writer() as connection:
            for index in range(4):
                db.upsert_service(connection, {
                    "slug": "procurement-%d" % index,
                    "name": "Procurement tools %d" % index,
                    "url": "https://service-%d.example/api" % index,
                    "mcp_url": "https://procurement-%d.example/mcp" % min(index, 2),
                    "source": "fixture", "source_id": "procurement-%d" % index,
                })
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        for limit, offset in ((1, 0), (1, 1), (2, 0), (10, 0), (1, 3)):
            with self.subTest(limit=limit, offset=offset):
                response = client.get("/api/v1/mcp/search", params={
                    "q": "procurement", "limit": limit, "offset": offset})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total_matched"], 3)


class FoldKeepsText(_TempDB):
    def add(self, path, sub_id, desc):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "seller-sub%d" % sub_id, "name": path,
                                  "url": ORIGIN + path, "source": "submission",
                                  "source_id": "sub:%d" % sub_id, "payment": payment(),
                                  "description": desc,
                                  "resources_text": db.resources_text([{"url": ORIGIN + path, "description": desc}])})

    def test_folded_rows_text_stays_searchable(self):
        self.add("/v1/verify", 1, "Verify a USDC payment")
        self.add("/v1/btcfeecalc", 2, "Bitcoin fee calculator")
        self.add("/v1/weather", 3, "Weather forecast by city")
        with db.writer() as c:
            fold_origin.fold(c, ORIGIN, PAY, {})
        self.assertEqual(len(self.q("SELECT id FROM services")), 1)
        self.assertEqual(self.search("bitcoin fee calculator"), ["seller-sub1"])
        self.assertEqual(self.search("weather forecast"), ["seller-sub1"])


class PayToLimit(_TempDB):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(jobs, "SUBMIT_PAYTO_LIMIT_PER_DAY", 2))

    def submit_via_route(self, path, ip, pay_to=PAY, origin=ORIGIN):
        app = FastAPI(); app.include_router(routes.router)
        verdict = {"status": "verified", "evidence": ["402"], "verified_url": origin + path,
                   "payment": payment(pay_to)}
        async def _noop(_u):
            return None
        with patch.object(routes, "_require_public_url", _noop), \
                patch.object(routes, "_conn", lambda: self.real_connect(self.path, read_only=True)), \
                patch.object(routes.directory_crawlers, "url_retired", lambda _u: False), \
                patch.object(jobs.crawlers, "verify_x402", lambda *_a, **_k: verdict), \
                patch.object(jobs.crawlers, "fetch_wellknown_resources", return_value={}), \
                patch.object(jobs.crawlers, "check_health", return_value="ok"), \
                patch.object(jobs.mailer, "send_approval_email", return_value=True), \
                patch.object(jobs.mailer, "send_admin_notification", return_value=True), \
                patch.object(routes.limits, "client_ip_from_request", lambda _r: ip):
            return TestClient(app).post("/api/v1/submit", json={
                "name": path, "url": origin + path, "contact": "op@seller.example",
                "description": "d " + path})

    def test_rotating_ips_cannot_exceed_payto_limit(self):
        codes = [self.submit_via_route("/v1/p%d" % i, "2a09:bac5::%x" % i).status_code
                 for i in range(4)]
        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2:], [429, 429])
        st = [r["status"] for r in self.q("SELECT status FROM submissions ORDER BY id")]
        self.assertEqual(st, ["approved", "approved", "rate_limited", "rate_limited"])
        # the first two folded into one listing; the limited ones listed nothing
        self.assertEqual(len(self.q("SELECT id FROM services")), 1)

    def test_other_payto_is_not_affected(self):
        for i in range(3):
            self.submit_via_route("/v1/p%d" % i, "198.51.100.%d" % i)
        r = self.submit_via_route("/v1/x", "198.51.100.9", pay_to="0x" + "3" * 40,
                                  origin="https://another.example")
        self.assertEqual(r.status_code, 200)

    def test_limiter_failure_does_not_block_listing(self):
        with patch.object(db, "hit_rate_limit", side_effect=RuntimeError("down")):
            self.assertIsNone(jobs._payto_submission_limited(1, {"payment": payment()}))


if __name__ == "__main__":
    unittest.main()
