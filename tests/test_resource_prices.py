import unittest

from directory import cards, crawlers, db

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _accept(amount):
    return {"scheme": "exact", "network": "eip155:8453", "asset": USDC_BASE,
            "amount": str(amount), "payTo": "0x" + "1" * 40}


def _row(**extra):
    row = {
        "slug": "tiers", "name": "Tiers", "url": "https://example.test/a",
        "price_min": 1.0, "price_max": 1.0, "owner_verified": 0,
        "resource_samples": [
            {"url": "https://example.test/a", "accepts": [_accept(1_000_000)]},
            {"url": "https://example.test/b", "accepts": [_accept(2_000_000)]},
            {"url": "https://example.test/c", "accepts": [_accept(3_000_000)]},
            {"url": "https://example.test/d", "accepts": [_accept(5_000_000)]},
        ],
    }
    row.update(extra)
    return row


class ResourcePriceTests(unittest.TestCase):
    def test_each_resource_gets_its_own_price(self):
        row = _row()
        db._apply_resource_prices(row)
        self.assertEqual([s["price_usd"] for s in row["resource_samples"]],
                         [1.0, 2.0, 3.0, 5.0])

    def test_headline_span_covers_every_resource(self):
        row = _row()
        db._apply_resource_prices(row)
        self.assertEqual((row["price_min"], row["price_max"]), (1.0, 5.0))

    def test_span_is_only_widened(self):
        # The stored sample list is capped; a dearer tier beyond it must survive.
        row = _row(price_min=0.5, price_max=9.0)
        db._apply_resource_prices(row)
        self.assertEqual((row["price_min"], row["price_max"]), (0.5, 9.0))

    def test_owner_verified_price_is_kept(self):
        row = _row(owner_verified=1)
        db._apply_resource_prices(row)
        self.assertEqual((row["price_min"], row["price_max"]), (1.0, 1.0))
        self.assertEqual(row["resource_samples"][3]["price_usd"], 5.0)

    def test_unpriced_samples_leave_row_alone(self):
        row = _row(resource_samples=[{"url": "https://example.test/a"}])
        db._apply_resource_prices(row)
        self.assertEqual((row["price_min"], row["price_max"]), (1.0, 1.0))
        self.assertNotIn("price_usd", row["resource_samples"][0])

    def test_unknown_asset_is_not_priced(self):
        acc = _accept(5_000_000)
        acc["asset"] = "0x" + "9" * 40
        row = _row(resource_samples=[{"url": "https://example.test/a",
                                      "accepts": [acc]}])
        db._apply_resource_prices(row)
        self.assertNotIn("price_usd", row["resource_samples"][0])
        self.assertEqual(row["price_max"], 1.0)

    def test_service_card_exposes_span_and_per_resource_price(self):
        row = _row()
        db._apply_resource_prices(row)
        card = cards.build_service_card(row)
        self.assertEqual(card["payment"]["price_min_usd"], 1.0)
        self.assertEqual(card["payment"]["price_max_usd"], 5.0)
        self.assertEqual([s["price_usd"] for s in card["call"]["resource_samples"]],
                         [1.0, 2.0, 3.0, 5.0])

    def test_accept_summary_reads_v2_amount(self):
        summary = crawlers._accept_summary(_accept(3_000_000), "https://example.test/c")
        self.assertEqual(summary["estimated_usd"], 3.0)


if __name__ == "__main__":
    unittest.main()
