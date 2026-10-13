import unittest

from directory import cards


class ServiceCardCurrencyTests(unittest.TestCase):
    def test_call_template_uses_actual_currency(self):
        template = cards.build_call_template({
            "url": "https://example.test/pay",
            "price_min": 0.02,
            "currency": "USDT0",
        })
        self.assertIn("0.02 USDT0/call", template["http_x402"]["curl_probe"])
        self.assertNotIn("USDC", template["http_x402"]["curl_probe"])

    def test_call_template_uses_advertised_get_method(self):
        template = cards.build_call_template({
            "url": "https://example.test/pay",
            "resource_samples": [{
                "url": "https://example.test/pay", "method": "GET",
            }],
        })
        self.assertEqual(template["http_x402"]["method"], "GET")
        self.assertIn("-X GET", template["http_x402"]["curl_probe"])

    def test_service_card_does_not_invent_usdc_currency(self):
        card = cards.build_service_card({
            "slug": "mixed", "name": "Mixed", "url": "https://example.test",
            "price_min": 0.001, "price_max": 3.0,
            "currency": None, "payment": {},
        })
        self.assertNotIn("currency", card["payment"])

    def test_row_currency_wins_over_stale_payment_currency(self):
        card = cards.build_service_card({
            "slug": "entropy", "name": "Entropy",
            "url": "https://example.test/signal",
            "price_min": 0.02, "price_max": 0.02,
            "currency": "USDT0", "payment": {"currency": None},
        })
        self.assertEqual(card["payment"]["currency"], "USDT0")

    def test_fresh_call_fields_win_over_stale_call_info(self):
        card = cards.build_service_card({
            "slug": "entropy", "name": "Entropy",
            "url": "https://example.test/signal",
            "resource_count": 1,
            "resource_samples": [{
                "url": "https://example.test/signal", "method": "GET",
            }],
            "call_info": {"resource_count": 0},
        })
        self.assertEqual(card["call"]["resource_count"], 1)
        self.assertEqual(card["call"]["template"]["http_x402"]["method"], "GET")

    def test_nested_resource_count_is_fallback_when_top_level_missing(self):
        card = cards.build_service_card({
            "slug": "nested", "name": "Nested", "url": "https://example.test",
            "resource_count": None,
            "call_info": {"resource_count": 3},
        })
        self.assertEqual(card["call"]["resource_count"], 3)

    def test_payment_falls_back_to_nested_chains_and_facilitator(self):
        card = cards.build_service_card({
            "slug": "nested", "name": "Nested", "url": "https://example.test",
            "payment": {"chains": ["base"], "facilitator": "https://fac.example"},
        })
        self.assertEqual(card["payment"]["chains"], ["base"])
        self.assertEqual(card["payment"]["facilitator"], "https://fac.example")


if __name__ == "__main__":
    unittest.main()