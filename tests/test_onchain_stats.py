"""/stats on-chain demand: counted per (chain, address), shared addresses once."""

import time
import unittest

from directory import db, stats
from test_ingestion_pipeline import TemporaryDatabaseTestCase


class OnchainStatsTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        with db.writer() as c:
            for i, slug in enumerate(("a", "b", "c")):
                c.execute("INSERT INTO services(id, slug, name, url, source, created_at, updated_at) "
                          "VALUES(?,?,?,?,'test',1,1)", (i + 1, slug, slug, f"https://{slug}.example/"))
            # 0xaa is shared by listings 1 and 2; 0xbb belongs to 3; sol is Solana.
            for sid, chain, addr in ((1, "base", "0xaa"), (2, "base", "0xaa"),
                                     (3, "base", "0xbb"), (3, "solana", "So1")):
                c.execute("INSERT INTO service_paytos VALUES(?,?,?,?,?)",
                          (sid, chain, addr, self.now, self.now))
            # payers on the listing rows (what the job writes)
            c.execute("UPDATE services SET payto_payers_30d=5 WHERE id IN (1,2)")
            db.record_payto_activity(c, {
                ("base", "0xaa"): {"ok": True, "tx": 900, "payers": 5, "raw_tx": 950},
                ("base", "0xbb"): {"ok": True, "tx": 100, "payers": 1200, "raw_tx": 100},
                ("solana", "So1"): {"ok": True, "unmeasured": True},
                ("base", "0xfailed"): {"ok": False},
            }, self.now)

    def test_counts_each_address_once(self):
        with db.connect() as c:
            out = stats.onchain_demand(c, self.now)
        self.assertTrue(out["available"])
        self.assertEqual(out["addresses"], 3)
        self.assertEqual(out["measured"], 2)
        self.assertEqual(out["unmeasured"], 1)
        self.assertEqual(out["with_payments"], 2)
        self.assertEqual(out["payments"], 1000)        # 0xaa once, not twice
        self.assertEqual(out["shared"], 1)
        self.assertEqual(out["listings_with_payers"], 2)
        self.assertEqual(out["top_share"]["1"], 90.0)
        bands = {r["label"]: r["count"] for r in out["payer_bands"]}
        self.assertEqual(bands["2\u20139"], 1)
        self.assertEqual(bands["1,000 or more"], 1)

    def test_failed_read_is_not_stored_and_stale_rows_are_dropped(self):
        with db.connect() as c:
            self.assertIsNone(c.execute(
                "SELECT 1 FROM payto_activity WHERE address='0xfailed'").fetchone())
            later = self.now + 8 * 86400
            self.assertEqual(stats.onchain_demand(c, later), {"available": False})

    def test_page_renders_the_card(self):
        from pathlib import Path
        from jinja2 import Environment, FileSystemLoader
        root = Path(__file__).resolve().parent.parent
        env = Environment(loader=FileSystemLoader(str(root / "directory/templates")), autoescape=True)
        env.globals.update(build_version="t", current_user=lambda r: None,
                           may_edit_listing=lambda *a: False, url_host=lambda u: u)
        env.filters["num"] = lambda v: f"{v:,}" if isinstance(v, int) else v
        with db.connect() as c:
            snap = stats.compute(c, self.now)

        class Req:
            class url:
                path = "/stats"
        kpis = {m: {"value": snap["kpis"]["all"][m], "series": [], "delta": None}
                for m in ("indexed", "reachable", "verified", "ready")}
        html = env.get_template("stats.html").render(
            request=Req, snap=snap, view="all", period=30, kpis=kpis,
            headlines=stats.headlines(snap, "all"), history_days=1, first_day=None,
            kinds=list(stats.KINDS), kind_label=stats.KIND_LABEL)
        self.assertIn('id="onchain"', html)
        self.assertIn("1 Solana addresses are not measured", html)

    def test_empty_table_hides_the_section(self):
        with db.writer() as c:
            c.execute("DELETE FROM payto_activity")
        with db.connect() as c:
            self.assertEqual(stats.onchain_demand(c, self.now), {"available": False})


if __name__ == "__main__":
    unittest.main()
