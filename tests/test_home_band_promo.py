"""Home page "From the data" band and promotion slots."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from directory import promo, stats
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SNAP = {
    "as_of": "2026-11-13 01:00 UTC",
    "kpis": {"all": {"indexed": 1000, "reachable": 740, "verified": 500, "ready": 430}},
    "topics": {"rows": [{"topic": t, "x402": i, "mcp": 10 * i, "a2a": 0}
                        for i, t in enumerate(["search", "data", "ai", "web", "code", "pdf"], 1)]},
}


class HomeBandTests(unittest.TestCase):
    def test_band_numbers(self):
        hist = [("2026-10-30", {"x402": {"reachable": 1}, "backfill": True}),
                ("2026-11-11", {"all": {"indexed": 990, "ready": 420}}),
                ("2026-11-13", SNAP["kpis"])]
        band = stats.home_band(SNAP, hist)
        self.assertEqual(band["usable"], {"reachable_pct": 74, "ready_pct": 43, "indexed": 1000})
        self.assertEqual((band["report"]["ready_change"], band["report"]["since"]), (10, "2026-11-11"))
        self.assertEqual(band["report"]["title"], "November 2026")
        self.assertEqual([t["topic"] for t in band["topics"]], ["search", "data", "ai", "web", "code"])
        self.assertEqual(band["topics"][0]["reachable"], 11)

    def test_band_with_one_day_has_no_change(self):
        band = stats.home_band(SNAP, [("2026-11-13", SNAP["kpis"])])
        self.assertIsNone(band["report"]["ready_change"])
        self.assertIsNone(stats.home_band(None, []))


class PromoTests(unittest.TestCase):
    def setUp(self):
        promo._cache.update(at=0.0, path=None, items={})
        self.addCleanup(promo._cache.update, at=0.0, path=None, items={})

    def write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_house_items_by_default(self):
        with patch.dict(os.environ, {"AGENT_TOOLS_PROMO_FILE": ""}):
            p = promo.slot("home-after-index")
        self.assertFalse(p["sponsored"])
        self.assertIsNone(p["rel"])
        self.assertTrue(p["href"].startswith("/"))
        self.assertIsNone(promo.slot("stats-top"))

    def test_sponsored_item_is_labelled_and_validated(self):
        path = self.write({
            "home-after-index": {"title": "Fast RPC", "text": "x", "href": "https://example.com/a",
                                 "sponsor": "Example Inc"},
            "home-before-blog": {"title": "No sponsor", "href": "https://example.com/b"},
            "stats-top": {"title": "Not a slot", "href": "https://example.com/c", "sponsor": "S"},
        })
        with patch.dict(os.environ, {"AGENT_TOOLS_PROMO_FILE": path}):
            paid = promo.slot("home-after-index")
            other = promo.slot("home-before-blog")
        self.assertTrue(paid["sponsored"])
        self.assertEqual(paid["rel"], "sponsored nofollow noopener")
        self.assertEqual(paid["sponsor"], "Example Inc")
        self.assertFalse(other["sponsored"])  # invalid paid item falls back to house

    def test_expired_http_and_broken_files_fall_back(self):
        for data in ({"home-after-index": {"title": "t", "href": "https://e.com", "sponsor": "s",
                                           "ends": "2000-01-01"}},
                     {"home-after-index": {"title": "t", "href": "http://e.com", "sponsor": "s"}},
                     "{not json"):
            promo._cache.update(at=0.0, path=None, items={})
            with self.subTest(data=data), patch.dict(os.environ, {"AGENT_TOOLS_PROMO_FILE": self.write(data)}):
                self.assertFalse(promo.slot("home-after-index")["sponsored"])

    def test_macro_renders_label_and_rel(self):
        env = Environment(loader=FileSystemLoader(str(ROOT / "directory/templates")), autoescape=True)
        macro = env.get_template("_macros.html").module.promo_slot
        html = str(macro({"title": "T", "text": "", "href": "https://e.com", "cta": "Go",
                          "sponsor": "Acme", "sponsored": True, "rel": "sponsored nofollow noopener"}))
        self.assertIn("Sponsored · Acme", html)
        self.assertIn('rel="sponsored nofollow noopener"', html)
        house = str(macro(promo.HOUSE["home-before-blog"] | {"sponsored": False, "rel": None}))
        self.assertNotIn("Sponsored", house)
        self.assertNotIn("rel=", house)
        self.assertEqual(str(macro(None)).strip(), "")


if __name__ == "__main__":
    unittest.main()
