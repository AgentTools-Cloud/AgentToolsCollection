"""Listing change history and official-field protection."""

import json
import unittest
from contextlib import closing

from directory import db
from test_ingestion_pipeline import TemporaryDatabaseTestCase

URL = "https://seller.example/pay"


def _changes(listing_id, kind="x402"):
    with closing(db.connect(read_only=True)) as c:
        return [(r["field"], r["source"]) for r in reversed(db.listing_changes(c, kind, listing_id, 100))]


class ChangeHistoryTests(TemporaryDatabaseTestCase):
    def crawl(self, **fields):
        row = {"slug": "seller-scan", "name": "Seller", "url": URL,
               "source": "x402scan", "source_id": "o1", "category": "ai"}
        row.update(fields)
        with db.writer() as c:
            _, sid = db.upsert_service(c, row)
        return sid

    def test_creation_and_changes_are_recorded_with_their_source(self):
        sid = self.crawl()
        self.crawl(name="Seller v2", category="data")
        self.assertEqual(_changes(sid), [("_created", "source:x402scan"),
                                         ("name", "source:x402scan"),
                                         ("category", "source:x402scan")])
        with closing(db.connect(read_only=True)) as c:
            name = [r for r in db.listing_changes(c, "x402", sid) if r["field"] == "name"][0]
        self.assertEqual((name["old"], name["new"]), ("Seller", "Seller v2"))

    def test_unchanged_values_and_measured_fields_are_not_history(self):
        sid = self.crawl()
        self.crawl()
        with db.writer() as c:
            c.execute("UPDATE services SET health='ok', latency_ms=12, tx_30d=5 WHERE id=?", (sid,))
        self.assertEqual(_changes(sid), [("_created", "source:x402scan")])

    def test_writer_without_a_source_is_unattributed_not_the_previous_one(self):
        sid = self.crawl()
        with db.writer() as c:
            c.execute("UPDATE services SET description='manual' WHERE id=?", (sid,))
        self.assertEqual(_changes(sid)[-1], ("description", "unattributed"))

    def test_rolled_back_change_leaves_no_history(self):
        sid = self.crawl()
        with self.assertRaises(RuntimeError):
            with db.writer() as c:
                db.set_change_source(c, "admin:test")
                c.execute("UPDATE services SET name='x' WHERE id=?", (sid,))
                raise RuntimeError
        self.assertEqual(_changes(sid), [("_created", "source:x402scan")])

    def test_owner_edit_is_attributed(self):
        sid = self.crawl()
        with db.writer() as c:
            db.set_change_source(c, "owner:web")
            c.execute("UPDATE services SET description='by owner' WHERE id=?", (sid,))
        self.assertEqual(_changes(sid)[-1], ("description", "owner:web"))


class OfficialFieldTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as c:
            _, self.sid = db.upsert_service(c, {
                "slug": "seller-scan", "name": "Seller", "url": URL, "source": "x402scan",
                "source_id": "o1", "category": "ai", "price_min": 0.02, "price_max": 0.10})
            db.set_change_source(c, "official:x402-descriptor")
            c.execute("UPDATE services SET price_min=0.005, price_max=0.03, payment=? WHERE id=?",
                      (json.dumps({"pay_to": "0xabc"}), self.sid))
            db.mark_official(c, "services", self.sid, ("price_min", "price_max", "payment"))

    def row(self):
        with closing(db.connect(read_only=True)) as c:
            return dict(c.execute("SELECT * FROM services WHERE id=?", (self.sid,)).fetchone())

    def test_crawl_keeps_official_values_but_updates_the_rest(self):
        with db.writer() as c:
            db.upsert_service(c, {"slug": "seller-scan", "name": "Seller renamed", "url": URL,
                                  "source": "x402scan", "source_id": "o1", "category": "finance",
                                  "price_min": 0.02, "price_max": 0.10,
                                  "payment": {"pay_to": "0xold"}})
        r = self.row()
        self.assertEqual((r["price_min"], r["price_max"]), (0.005, 0.03))
        self.assertEqual(json.loads(r["payment"])["pay_to"], "0xabc")
        self.assertEqual((r["name"], r["category"]), ("Seller renamed", "finance"))
        self.assertEqual(json.loads(r["official_fields"]), ["payment", "price_max", "price_min"])
        sources = [s for f, s in _changes(self.sid) if f == "price_min"]
        self.assertEqual(sources, ["official:x402-descriptor"])

    def test_owner_edit_still_wins(self):
        with db.writer() as c:
            c.execute("UPDATE services SET owner_edited=?, price_min=0.001 WHERE id=?",
                      (json.dumps(["price_min"]), self.sid))
            db.upsert_service(c, {"slug": "seller-scan", "name": "Seller", "url": URL,
                                  "source": "x402scan", "source_id": "o1", "price_min": 0.02})
        self.assertEqual(self.row()["price_min"], 0.001)


if __name__ == "__main__":
    unittest.main()
