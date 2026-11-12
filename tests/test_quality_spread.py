"""x402 / A2A quality score: the blocks added so healthy listings stop tying at 100."""
import json
import sqlite3
import unittest

from directory import db

PAY = json.dumps({"payTo": "0x" + "ab" * 20, "network": "eip155:8453",
                  "max_amount_usdc": 0.01})


def x402(**kw):
    row = {"health": "ok", "x402_ok": 1, "well_known_url": "https://x/.well-known/x402",
           "payment": PAY, "payto_checked": 1, "payto_payers_30d": 1000,
           "payto_tx_30d": 10000, "tx_30d": 10000, "latency_ms": 150,
           "readiness_score": 100, "owner_verified": 1}
    row.update(kw)
    return row


class X402Tests(unittest.TestCase):
    def test_perfect_is_100_and_each_block_costs(self):
        self.assertEqual(db._score_x402(x402()), 100.0)
        self.assertEqual(db._score_x402(x402(owner_verified=0)), 95.0)
        self.assertEqual(db._score_x402(x402(readiness_score=80)), 97.0)
        self.assertEqual(db._score_x402(x402(latency_ms=1100)), 95.0)

    def test_perf_boundaries_and_unreachable(self):
        self.assertEqual(db._x402_perf(x402(latency_ms=200)), 1.0)
        self.assertEqual(db._x402_perf(x402(latency_ms=2000)), 0.0)
        self.assertEqual(db._x402_perf(x402(latency_ms=None)), 0.0)
        self.assertEqual(db._x402_perf(x402(health="down", latency_ms=10)), 0.0)

    def test_readiness_needs_reachable_endpoint(self):
        self.assertEqual(db._x402_ready(x402(readiness_score=None)), 0.0)
        self.assertEqual(db._x402_ready(x402(readiness_score="50")), 0.5)
        self.assertEqual(db._x402_ready(x402(health="down")), 0.0)
        self.assertEqual(db._x402_ready(x402(health="degraded")), 1.0)

    def test_shared_wallet_is_split(self):
        self.assertEqual(db._demand_counts(x402(payto_shared=4)), (250.0, 2500.0))
        self.assertLess(db._demand(x402(payto_shared=4)), db._demand(x402()))
        self.assertEqual(db._demand_counts(x402(payto_shared=None)), (1000, 10000))

    def test_latest_window_beats_stale_tx(self):
        # A third-party tx_30d must not stand in for our own 30-day reading.
        self.assertEqual(db._demand_counts(x402(tx_30d=99999, payto_tx_30d=10))[1], 10)
        self.assertEqual(db._demand_counts(x402(payto_tx_30d=None, tx_30d=7))[1], 7)
        self.assertEqual(
            db._demand_counts(x402(payto_checked=None, payto_tx_30d=None, tx_30d=7))[1], 7)

    def test_concentration_discount(self):
        self.assertEqual(db._concentration(10, 200), 1.0)          # ratio 20
        self.assertLess(db._concentration(10, 2000), 1.0)
        self.assertEqual(db._concentration(1, 10 ** 9), 0.5)       # floor
        self.assertEqual(db._concentration(0, 500), 1.0)

    def test_down_held_at_top_of_d(self):
        s = db._score_x402(x402(health="down"))
        self.assertEqual(db.grade_letter(s, "x402"), "D")
        self.assertEqual(s, db._band_ceiling("x402", "D"))

    def test_breakdown_sums_to_score(self):
        for row in (x402(), x402(payto_shared=3, latency_ms=900, readiness_score=61,
                                 owner_verified=0)):
            b = db.score_breakdown(row)
            self.assertEqual(sum(c["max"] for c in b["components"]), 100)
            self.assertAlmostEqual(sum(c["points"] for c in b["components"]),
                                   b["score"], delta=0.35)


class A2ATests(unittest.TestCase):
    def a2a(self, **kw):
        row = {"health": "ok", "conformance": "pass", "latency_ms": 40,
               "readiness_score": 100, "skills": json.dumps([{}, {}, {}]),
               "endpoint_url": "u", "capabilities": "c", "protocol_version": "1",
               "auth_schemes": "a", "documentation_url": "d", "owner_verified": 0}
        row.update(kw)
        return row

    def test_max_is_100_and_owner_never_exceeds(self):
        self.assertEqual(db._score_a2a(self.a2a()), 100.0)
        self.assertEqual(db._score_a2a(self.a2a(owner_verified=1)), 100.0)
        lower = self.a2a(readiness_score=80)
        self.assertGreater(db._score_a2a(dict(lower, owner_verified=1)),
                           db._score_a2a(lower))
        self.assertLess(db._score_a2a(dict(lower, owner_verified=1)), 100.0)

    def test_readiness_only_when_reachable(self):
        self.assertEqual(db._score_a2a(self.a2a(health="down", latency_ms=None)),
                         20 + 15)   # conformance + card only


class PaytoSharedTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(
            "CREATE TABLE services(id INTEGER PRIMARY KEY, health TEXT, payto_shared INTEGER);"
            "CREATE TABLE service_paytos(service_id INT, chain TEXT, address TEXT, last_seen INT);")
        self.now = 10 ** 9

    def add(self, sid, health, addr, chain="base", age_days=0):
        self.c.execute("INSERT OR IGNORE INTO services(id, health) VALUES(?,?)", (sid, health))
        self.c.execute("INSERT INTO service_paytos VALUES(?,?,?,?)",
                       (sid, chain, addr, self.now - age_days * 86400))

    def shared(self):
        db.update_payto_shared(self.c, now=self.now)
        return {r["id"]: r["payto_shared"] for r in self.c.execute("SELECT * FROM services")}

    def test_counts_only_reachable_recent_listings(self):
        self.add(1, "ok", "0xAA")
        self.add(2, "degraded", "0xaa")          # EVM case-folded
        self.add(3, "down", "0xaa")              # dead copy does not dilute
        self.add(4, "ok", "0xaa", age_days=40)   # stale
        self.add(5, "ok", "0xaa", chain="eth")   # other chain
        out = self.shared()
        self.assertEqual(out[1], 2)
        self.assertEqual(out[2], 2)
        self.assertEqual(out[3], 2)
        self.assertIsNone(out[4])
        self.assertIsNone(out[5])

    def test_base58_is_case_sensitive(self):
        self.add(1, "ok", "AbC", chain="solana")
        self.add(2, "ok", "abc", chain="solana")
        self.assertEqual(self.shared(), {1: None, 2: None})

    def test_resets_old_values(self):
        self.add(1, "ok", "0xaa")
        self.c.execute("UPDATE services SET payto_shared=9")
        self.assertIsNone(self.shared()[1])


if __name__ == "__main__":
    unittest.main()
