"""Per-origin fold review: which multi-row origins are one seller (2026-11-03)."""
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from directory import db, fold_origin, fold_review

EVM = "0x719227f384b66BFEF00093D6d8d32fA9dC2B40e7"
EVM2 = "0x" + "2" * 40
SOL = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def pay(*addrs):
    return {"pay_to": addrs[0] if addrs else None,
            "accepts": [{"payTo": a} for a in addrs]}


class _T(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fold-review-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret"}))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        rc, rw = db.connect, db.writer
        self.enterContext(patch.object(db, "connect", side_effect=lambda db_path=None, read_only=False, set_wal=True: rc(self.path, read_only=read_only, set_wal=set_wal)))
        self.enterContext(patch.object(db, "writer", side_effect=lambda db_path=None, immediate=False: rw(self.path, immediate=immediate)))
        self.enterContext(patch.object(db.time, "sleep"))
        db.init_db(self.path)
        self.n = 0

    PROBED = ("owner_verified", "x402_ok", "health")

    def svc(self, url, payment=None, source="submission", **kw):
        # probe/claim-owned columns are not written by upsert; set them after
        probed = {k: kw.pop(k) for k in self.PROBED if k in kw}
        self.n += 1
        with db.writer() as c:
            _, sid = db.upsert_service(c, {"slug": "s%d" % self.n, "name": url, "url": url,
                                           "source": source, "source_id": "x%d" % self.n,
                                           "payment": payment, **kw})
            for k, v in probed.items():
                c.execute("UPDATE services SET %s=? WHERE id=?" % k, (v, sid))

    def mcp(self, url):
        self.n += 1
        with db.writer() as c:
            c.execute("INSERT INTO mcp_servers(slug,name,endpoint_url,source,source_id,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,0,0)", ("m%d" % self.n, url, url, "fixture", "m%d" % self.n))

    def decisions(self):
        with closing(db.connect(read_only=True)) as c:
            return {i["origin"]: (i["decision"], i["reason"]) for i in fold_review.review(c)}


class Rules(_T):
    def test_single_seller_paths_fold(self):
        self.svc("https://a.example/v1/x", pay(EVM))
        self.svc("https://a.example/v1/y", pay(EVM))
        self.svc("https://a.example", None, source="cdp-bazaar")   # crawler root, no payTo
        self.assertEqual(self.decisions()["https://a.example"][0], "fold")

    def test_evm_plus_solana_of_one_seller_folds(self):
        self.svc("https://b.example/v1/x", pay(EVM, SOL))
        self.svc("https://b.example/v1/y", pay(EVM))
        self.assertEqual(self.decisions()["https://b.example"][0], "fold")

    def test_two_evm_payees_are_kept(self):
        self.svc("https://c.example/v1/x", pay(EVM))
        self.svc("https://c.example/v1/y", pay(EVM2))
        self.assertEqual(self.decisions()["https://c.example"],
                         ("keep", "several payTo addresses on one chain family"))

    def test_gateway_listed_as_many_mcp_servers_is_kept(self):
        for p in ("acled", "adzuna", "advice"):
            self.mcp("https://gw.example/%s/mcp" % p)
            self.svc("https://gw.example/%s/mcp" % p, pay(EVM), source="mcp")
        self.assertEqual(self.decisions()["https://gw.example"][0], "keep")
        self.assertIn("gateway", self.decisions()["https://gw.example"][1])

    def test_no_payto_is_kept(self):
        self.svc("https://docs.example/a", None, source="awesome-x402")
        self.svc("https://docs.example/b", None, source="awesome-x402")
        self.assertEqual(self.decisions()["https://docs.example"][1], "no payTo on any row")

    def test_shared_host_is_kept(self):
        with db.writer() as c:
            c.execute("INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
                      "VALUES('git.example',2,2,'shared',0)")
        self.svc("https://git.example/u1/repo", pay(EVM))
        self.svc("https://git.example/u2/repo", pay(EVM))
        self.assertEqual(self.decisions()["https://git.example"], ("keep", "shared host"))

    def test_partial_owner_verification_is_kept(self):
        self.svc("https://d.example/v1/x", pay(EVM), owner_verified=1)
        self.svc("https://d.example/v1/y", pay(EVM))
        self.assertEqual(self.decisions()["https://d.example"][1], "owner verified only some rows")

    def test_address_families(self):
        f = fold_review._family
        self.assertEqual(f(EVM), "evm")
        self.assertEqual(f(SOL), "solana")
        self.assertEqual(f("rUXKCZetEbwQ7Yb3zFNA5RtgnaAx8yYqQ"), "xrp")
        self.assertEqual(f("G" + "A" * 55), "stellar")
        self.assertEqual(f("A" * 58), "algorand")
        self.assertEqual(f("nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9"), "nano")
        self.assertEqual(f("something-unknown"), "other")


class ApplyAndAfter(_T):
    def test_apply_folds_and_mirror_cannot_resplit(self):
        self.svc("https://e.example", None, source="cdp-bazaar", x402_ok=0, health="ok")
        self.svc("https://e.example/v1/x", pay(EVM), x402_ok=1, health="ok")
        self.svc("https://e.example/v1/y", pay(EVM), x402_ok=1, health="ok")
        with closing(db.connect(read_only=True)) as c:
            plan = fold_review.review(c)
        with patch.object(fold_review.crawlers, "fetch_wellknown_resources", return_value={}):
            out = fold_review.apply(plan, log=lambda *_a: None)
        self.assertEqual(out["absorbed_rows"], 2)
        with closing(db.connect(read_only=True)) as c:
            rows = c.execute("SELECT slug, url FROM services").fetchall()
            self.assertEqual(len(rows), 1)
            # survivor is a working paid endpoint, not the bare root
            self.assertEqual(rows[0]["url"], "https://e.example/v1/x")
            self.assertEqual(db.resolve_moved_service_slug(c, "s1"), rows[0]["slug"])
            self.assertEqual(db.resolve_moved_service_slug(c, "s3"), rows[0]["slug"])
            # fold pointers are not public provenance
            self.assertFalse([s for s in db.listing_sources(c, "x402", 2)
                              if s["source"].startswith("x402-fold")])
        # a later crawler/mirror write of an absorbed endpoint attaches, not re-mints
        with db.writer() as c:
            created, sid = db.upsert_service(c, {"slug": "again", "name": "y", "url": "https://e.example/v1/y",
                                                 "source": "mcp", "source_id": "mcp:y", "description": "new desc"})
        self.assertFalse(created)
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM services").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT url FROM services").fetchone()[0], "https://e.example/v1/x")

    def test_reverify_mirror_of_folded_path_is_a_duplicate_not_a_conflict(self):
        from directory import reverify_x402
        self.svc("https://g.example/v1/x", pay(EVM), x402_ok=1, health="ok")
        self.svc("https://g.example/v1/y", pay(EVM), x402_ok=1, health="ok")
        with db.writer() as c:
            fold_origin.fold(c, "https://g.example", EVM, None)
        with db.writer() as c:
            out = reverify_x402._mirror_service_in_tx(c, {
                "slug": "y-x402", "name": "y", "url": "https://g.example/v1/y",
                "source": "mcp", "source_id": "mcp:g-y", "payment": pay(EVM)})
        self.assertTrue(out.get("duplicate"))
        self.assertFalse(out.get("identity_conflict"))
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM services").fetchone()[0], 1)

    def test_legacy_fold_sources_migrate_out_of_provenance(self):
        self.svc("https://f.example/v1/x", pay(EVM))
        with db.writer() as c:
            c.execute("INSERT INTO listing_sources(kind,listing_id,source,source_id,source_url,first_seen,last_seen) "
                      "VALUES('x402',1,'x402-fold:old-slug','x402:old-slug','https://f.example/v1/old',5,5)")
        db.init_db(self.path)
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM listing_sources WHERE source LIKE 'x402-fold:%'").fetchone()[0], 0)
            self.assertEqual(db.resolve_moved_service_slug(c, "old-slug"), "s1")
            self.assertIsNotNone(db.find_service_by_url(c, "https://f.example/v1/old"))

    def test_gateway_is_never_folded_even_if_asked(self):
        for p in ("a", "b", "c"):
            self.mcp("https://gw2.example/%s/mcp" % p)
            self.svc("https://gw2.example/%s/mcp" % p, pay(EVM), source="mcp")
        with db.writer() as c:
            out = fold_origin.fold(c, "https://gw2.example", EVM, None, include_unpaid=True)
        self.assertEqual(out.get("skipped"), "gateway")


if __name__ == "__main__":
    unittest.main()
