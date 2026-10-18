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

    def test_sibling_resource_cannot_replace_primary_url_or_method(self):
        template = cards.build_call_template({
            "url": "https://example.test/v1/ping",
            "resource_samples": [
                {"url": "https://example.test/v1/probe", "method": "GET"},
                {"url": "https://example.test/v1/ping", "method": "POST"},
            ],
        })
        self.assertEqual(template["http_x402"]["url"], "https://example.test/v1/ping")
        self.assertEqual(template["http_x402"]["method"], "POST")

    def test_primary_query_identity_is_not_taken_from_a_sibling(self):
        template = cards.build_call_template({
            "url": "https://example.test/v1/ping?tenant=one",
            "resource_samples": [{"url": "https://example.test/v1/ping?tenant=two", "method": "POST"}],
        })
        self.assertEqual(template["http_x402"]["url"], "https://example.test/v1/ping?tenant=one")
        self.assertEqual(template["http_x402"]["method"], "GET")

    def test_explicit_primary_call_info_wins_without_using_sibling_metadata(self):
        template = cards.build_call_template({
            "url": "https://example.test/v1/ping",
            "call_info": {"primary_url": "https://example.test/v1/ping", "method": "POST"},
            "resource_samples": [{"url": "https://example.test/v1/probe", "method": "GET"}],
        })
        self.assertEqual(template["http_x402"]["url"], "https://example.test/v1/ping")
        self.assertEqual(template["http_x402"]["method"], "POST")

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

    def test_pending_owner_fields_are_not_active_call_information(self):
        card = cards.build_service_card({
            'url': 'https://example.test/ping',
            'owner_declaration': {'version': 1, 'revision': 'private-revision', 'status': 'pending',
                'primary_url': 'https://example.test/ping', 'fields': {'method': 'POST', 'price_usdc': 0.003}},
        })
        self.assertEqual(card['call']['template']['http_x402']['method'], 'GET')
        self.assertEqual(card['interface_declaration']['status'], 'pending')
        self.assertNotIn('owner_declaration', card)
        self.assertNotIn('fields', card['interface_declaration'])
        self.assertNotIn('private-revision', str(card))

    def test_matching_empty_post_probe_has_json_body_but_sibling_does_not(self):
        row = {'url': 'https://example.test/ping', 'resource_samples': [
            {'url': 'https://example.test/ping', 'method': 'POST', 'probe_body': {}},
        ]}
        self.assertIn("--data '{}'", cards.build_call_template(row)['http_x402']['curl_probe'])
        row['resource_samples'][0]['url'] = 'https://example.test/sibling'
        self.assertNotIn('--data', cards.build_call_template(row)['http_x402']['curl_probe'])

    def test_primary_alias_order_does_not_choose_an_arbitrary_method(self):
        samples = [{'url': 'https://example.test/ping', 'method': value} for value in ('GET', 'POST')]
        row = {'url': 'https://example.test/ping', 'resource_samples': samples}
        first = cards.build_call_template(row)
        row['resource_samples'] = list(reversed(samples))
        self.assertEqual(cards.build_call_template(row), first)
        self.assertIn('fallback', first['http_x402']['method_hint'])

    def test_probe_url_is_shell_quoted_not_double_quote_interpolated(self):
        probe = cards.build_call_template({'url': 'https://example.test/ping?q=$(touch+bad)'})['http_x402']['curl_probe']
        self.assertIn("'https://example.test/ping?q=$(touch+bad)'", probe)


if __name__ == "__main__":
    unittest.main()