import unittest

from directory.a2a import _auth_scheme_names, card_to_row

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


if __name__ == "__main__":
    unittest.main()
