import unittest

from directory.a2a import _auth_scheme_names, _detect_x402, card_to_row

CARD_URL = "https://example.test/.well-known/agent.json"


class AuthSchemeShapesTest(unittest.TestCase):
    """securitySchemes should be a name->scheme object, but agents ship lists too.

    A list used to raise AttributeError inside card_to_row, which aborted the
    whole agenstry A2A crawl (one bad card stalled 1500+ rows for four days).
    """

    def test_spec_dict_form_uses_keys(self):
        card = {"name": "a", "securitySchemes": {"bearerAuth": {}, "oauth": {}}}
        self.assertEqual(_auth_scheme_names(card), ["bearerAuth", "oauth"])

    def test_list_of_scheme_objects(self):
        card = {
            "name": "a",
            "securitySchemes": [
                {"type": "apiKey", "name": "Authorization", "location": "header"}
            ],
        }
        self.assertEqual(_auth_scheme_names(card), ["apiKey"])

    def test_list_of_strings(self):
        card = {"name": "a", "securitySchemes": ["apiKey", " oauth2 "]}
        self.assertEqual(_auth_scheme_names(card), ["apiKey", "oauth2"])

    def test_empty_and_unusable_shapes_give_none(self):
        for value in ({}, [], None, 42, "bearer", [1, None, {}]):
            with self.subTest(value=value):
                self.assertIsNone(_auth_scheme_names({"name": "a", "securitySchemes": value}))
        self.assertIsNone(_auth_scheme_names({"name": "a"}))

    def test_card_to_row_never_raises_on_bad_shapes(self):
        for value in ({"k": {}}, [{"type": "apiKey"}], ["apiKey"], [], None, 42, [1, None]):
            with self.subTest(value=value):
                row = card_to_row({"name": "a", "securitySchemes": value}, CARD_URL)
                self.assertIn("auth_schemes", row)

    def test_optional_registry_payment_metadata_does_not_mark_endpoint_x402(self):
        card = {
            "name": "Property Check",
            "url": "https://agents.example.test/a2a",
            "capabilities": {
                "extensions": [{
                    "required": False,
                    "uri": "https://a2a-registry.org/extensions/registry/v1",
                    "params": {
                        "payment": {
                            "protocols": ["x402"],
                            "resource": "https://agents.example.test/v1/property",
                        },
                    },
                }],
            },
            "securitySchemes": {
                "bearer": {
                    "httpAuthSecurityScheme": {
                        "scheme": "Bearer",
                        "description": "Buy credits with x402 at /v1/property",
                    },
                },
            },
        }
        self.assertEqual(_detect_x402(card), (False, None))

    def test_endpoint_x402_security_scheme_is_detected(self):
        card = {
            "name": "Paid Agent",
            "securitySchemes": {
                "x402": {
                    "type": "x402",
                    "payTo": "0x123",
                },
            },
            "security": [{"x402": []}],
        }
        self.assertEqual(_detect_x402(card), (True, "0x123"))

    def test_list_endpoint_x402_security_scheme_is_detected(self):
        card = {
            "name": "Paid Agent",
            "securitySchemes": [{
                "type": "x402",
                "payTo": "0x456",
            }],
        }
        self.assertEqual(_detect_x402(card), (True, "0x456"))

    def test_http_type_with_x402_scheme_is_detected(self):
        card = {
            "name": "Paid Agent",
            "securitySchemes": [{
                "type": "http", "scheme": "x402",
                "payTo": "0x789",
            }],
        }
        self.assertEqual(_detect_x402(card), (True, "0x789"))


if __name__ == "__main__":
    unittest.main()
