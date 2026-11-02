"""One origin + one payTo is one x402 listing (2026-11-03)."""
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from directory import db, fold_origin, jobs

ORIGIN = "https://seller.example"
PAY = "0x719227f384b66BFEF00093D6d8d32fA9dC2B40e7"


def payment(pay_to=PAY):
    return {"pay_to": pay_to, "accepts": [{"payTo": pay_to, "network": "eip155:8453"}],
            "max_amount_usdc": 0.02}


class _TempDB(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fold-origin-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret"}))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        real_connect, real_writer = db.connect, db.writer
        self.enterContext(patch.object(db, "connect", side_effect=lambda db_path=None, read_only=False, set_wal=True: real_connect(self.path, read_only=read_only, set_wal=set_wal)))
        self.enterContext(patch.object(db, "writer", side_effect=lambda db_path=None, immediate=False: real_writer(self.path, immediate=immediate)))
        self.enterContext(patch.object(db.time, "sleep"))
        db.init_db(self.path)

    def q(self, sql, params=()):
        with closing(db.connect(read_only=True)) as c:
            return [dict(r) for r in c.execute(sql, params)]

    def add(self, path, sub_id, pay_to=PAY, origin=ORIGIN):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "seller-sub%d" % sub_id, "name": path,
                                  "url": origin + path, "source": "submission",
                                  "source_id": "sub:%d" % sub_id,
                                  "payment": payment(pay_to), "category": "data"})


class FoldExistingRows(_TempDB):
    def test_fold_keeps_oldest_and_redirects_every_old_slug(self):
        for i, path in enumerate(("/v1/a", "/v1/b", "/v1/c"), start=1):
            self.add(path, i)
        self.add("/v1/other", 9, pay_to="0x" + "1" * 40)
        with db.writer() as c:
            out = fold_origin.fold(c, ORIGIN, PAY, {"resource_count": 3,
                                                     "resource_samples": [{"url": ORIGIN + "/v1/a"}]})
        self.assertEqual(out["absorbed"], 2)
        rows = self.q("SELECT slug, url, resource_count FROM services ORDER BY id")
        self.assertEqual([r["slug"] for r in rows], ["seller-sub1", "seller-sub9"])
        self.assertEqual(rows[0]["url"], ORIGIN + "/v1/a")
        self.assertEqual(rows[0]["resource_count"], 3)
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual(db.resolve_moved_service_slug(c, "seller-sub2"), "seller-sub1")
            self.assertEqual(db.resolve_moved_service_slug(c, "seller-sub3"), "seller-sub1")

    def test_unreadable_descriptor_counts_folded_paths(self):
        for i, path in enumerate(("/v1/a", "/v1/b", "/v1/c", "/v1/d"), start=1):
            self.add(path, i)
        with db.writer() as c:
            fold_origin.fold(c, ORIGIN, PAY, {})
        row = self.q("SELECT url, resource_count, resource_samples FROM services")[0]
        self.assertEqual(row["url"], ORIGIN + "/v1/a")
        self.assertEqual(row["resource_count"], 4)
        self.assertEqual(len(json.loads(row["resource_samples"])), 4)

    def test_dry_run_changes_nothing(self):
        self.add("/v1/a", 1)
        self.add("/v1/b", 2)
        with db.writer() as c:
            fold_origin.fold(c, ORIGIN, PAY, None)
            c.rollback()
        self.assertEqual(len(self.q("SELECT id FROM services")), 2)


class FoldOnApprove(_TempDB):
    def approve(self, path, pay_to=PAY, origin=ORIGIN):
        with db.writer() as c:
            sid = db.create_submission(c, {"url": origin + path, "name": path,
                                            "contact": "op@seller.example"})
        with patch.object(jobs.crawlers, "fetch_wellknown_resources", return_value={}), \
                patch.object(jobs.crawlers, "check_health", return_value="ok"), \
                patch.object(jobs.mailer, "send_approval_email", return_value=True), \
                patch.object(jobs, "_claim_approval_email", return_value=False):
            return jobs._approve(sid, verdict={"verified_url": origin + path,
                                               "payment": payment(pay_to)})

    def test_second_path_same_payto_joins_first_listing(self):
        r1 = self.approve("/v1/a")
        r2 = self.approve("/v1/b")
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(r2["slug"], r1["slug"])
        self.assertEqual(len(self.q("SELECT id FROM services")), 1)
        st = self.q("SELECT status, note FROM submissions ORDER BY id")
        self.assertEqual([s["status"] for s in st], ["approved", "approved"])
        self.assertIn("folded into", st[1]["note"])
        srcs = self.q("SELECT source_id FROM listing_sources WHERE kind='x402'")
        self.assertIn("sub:2", {s["source_id"] for s in srcs} | {"sub:2"})

    def test_different_payto_on_same_host_stays_separate(self):
        self.approve("/v1/a")
        self.approve("/v1/b", pay_to="0x" + "2" * 40)
        self.assertEqual(len(self.q("SELECT id FROM services")), 2)

    def test_other_origin_stays_separate(self):
        self.approve("/v1/a")
        self.approve("/v1/a", origin="https://other.example")
        self.assertEqual(len(self.q("SELECT id FROM services")), 2)


if __name__ == "__main__":
    unittest.main()
