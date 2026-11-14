import re
import unittest
from pathlib import Path

from directory import paid_verify

T = Path(__file__).resolve().parent.parent / "directory" / "templates"


class PaidVerifyLimitDocsTests(unittest.TestCase):
    def test_about_explains_the_cap_and_who_owns_spend_control(self):
        html = (T / "about.html").read_text()
        self.assertIn('id="why-one-dollar"', html)
        self.assertIn("one-off test call, not a way to pay for services", html)
        self.assertIn("general payment channel", html)
        self.assertIn("in your own wallet or client", html)

    def test_terms_say_we_host_neither_side_and_link_the_cap(self):
        html = (T / "terms.html").read_text()
        self.assertIn("We host neither the payer nor the payee side", html)
        self.assertIn('href="/about#paid-verification"', html)

    def test_documented_cap_matches_code(self):
        dollars = paid_verify.MAX_AMOUNT_ATOMIC / 1_000_000
        for name in ("about.html", "terms.html"):
            html = (T / name).read_text()
            self.assertIn(f"${dollars:.2f}", html, name)
            self.assertFalse(re.search(r"\$(?!1\.00)\d+\.\d\d cap", html), name)


if __name__ == "__main__":
    unittest.main()
