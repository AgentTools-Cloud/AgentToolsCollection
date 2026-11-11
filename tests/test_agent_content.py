"""Machine-readable methodology stays in step with the human /about page."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a_readiness as A, db, listing_readiness as L, methodology  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class MethodologyTests(unittest.TestCase):
    def setUp(self):
        self.m = methodology.build()
        about = (ROOT / "directory/templates/about.html").read_text()
        self.about = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", about))

    def test_serialisable_and_complete(self):
        json.dumps(self.m)
        self.assertEqual(set(self.m["quality_score"]["components"]), {"x402", "mcp", "a2a"})
        self.assertEqual(set(self.m["readiness_score"]["criteria"]), {"x402", "mcp", "a2a"})

    def test_quality_components_match_about_page(self):
        for kind, parts in self.m["quality_score"]["components"].items():
            self.assertEqual(sum(p["max"] for p in parts), 100, kind)
            for p in parts:
                self.assertRegex(self.about, re.escape(p["name"]) + r" " + str(p["max"]), (kind, p["name"]))

    def test_grade_bands_come_from_code(self):
        bands = self.m["quality_score"]["grade_bands"]
        for kind, rows in db._GRADE_BANDS_BY_KIND.items():
            self.assertEqual([(b["min"], b["grade"]) for b in bands[kind]], list(rows))
        crit = self.m["readiness_score"]["criteria"]
        self.assertEqual(sum(c["max_points"] for c in crit["a2a"]), 100)
        self.assertEqual([c["key"] for c in crit["mcp"]], list(L.WEIGHTS))
        self.assertEqual(self.m["readiness_score"]["grade_bands"][0], {"min": A.GRADES[0][0], "grade": "A"})


if __name__ == "__main__":
    unittest.main()
