"""reverify's x402 mirror joins the listing of a seller already listed on the
same origin with the same payTo instead of minting a "<slug>-x402" twin
(2026-11-27).

Directories such as CDP Bazaar list a site's root; reverify then finds the paid
path and used to create a second row for it. fold_review merged 4,033 of these
on 11-03 and 956 more on 11-27; this stops new ones being created.
"""
import os
import sys
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(__file__))

from directory import db, reverify_x402  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402

PAYTO = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


def _pay(addr):
    return {"network": "eip155:8453", "pay_to": addr, "max_amount_usdc": 0.01}


def _root(c, host="seller.example", addr=PAYTO):
    db.upsert_service(c, {
        "slug": host.replace(".", "-") + "-bazaar", "name": "Seller",
        "url": "https://%s" % host, "source": "cdp-bazaar",
        "source_id": "bazaar:" + host, "payment": _pay(addr) if addr else None,
    })


def _mirror(c, url, addr=PAYTO, slug="seller-example-bazaar"):
    t = {"slug": slug, "name": "Seller", "endpoint": "https://seller.example",
         "delivery": "x402", "source": "x402", "source_id": slug}
    verdict = {"status": "verified", "verified_url": url, "payment": _pay(addr)}
    return reverify_x402._mirror_service_in_tx(
        c, reverify_x402._build_service(t, verdict))


def _count(c):
    return c.execute("SELECT count(*) FROM services").fetchone()[0]


class SameOriginMirror(_TempDB):
    def test_paid_path_joins_existing_seller(self):
        with db.writer() as c:
            _root(c)
            res = _mirror(c, "https://seller.example/api/run")
            self.assertTrue(res.get("same_origin"), res)
            self.assertEqual(res["slug"], "seller-example-bazaar")
            self.assertEqual(_count(c), 1)
            row = c.execute("SELECT url, x402_ok FROM services").fetchone()
            self.assertEqual(row["url"].rstrip("/"), "https://seller.example")
            self.assertEqual(row["x402_ok"], 1)
        # A second night finds the source identity on that row and still
        # leaves the seller listing's URL alone.
        with db.writer() as c:
            _mirror(c, "https://seller.example/api/run")
            self.assertEqual(_count(c), 1)
            self.assertEqual(c.execute("SELECT url FROM services").fetchone()[0].rstrip("/"),
                             "https://seller.example")

    def test_other_payto_still_gets_its_own_row(self):
        with db.writer() as c:
            _root(c)
            res = _mirror(c, "https://seller.example/api/run", addr=OTHER)
            self.assertTrue(res.get("created"), res)
            self.assertEqual(_count(c), 2)

    def test_origin_without_payto_is_not_joined(self):
        with db.writer() as c:
            _root(c, addr=None)
            res = _mirror(c, "https://seller.example/api/run")
            self.assertTrue(res.get("created"), res)
            self.assertEqual(_count(c), 2)

    def test_other_origin_is_not_joined(self):
        with db.writer() as c:
            _root(c, host="other.example")
            res = _mirror(c, "https://seller.example/api/run")
            self.assertTrue(res.get("created"), res)
        with closing(db.connect(read_only=True)) as c:
            self.assertEqual(_count(c), 2)


if __name__ == "__main__":
    unittest.main()
