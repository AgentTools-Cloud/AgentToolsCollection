"""Home page source line: public sources only, largest single-source share."""
import unittest

from directory import stats


class SourceSummaryTests(unittest.TestCase):
    def test_counts_public_sources_and_largest_share(self):
        snap = {
            "kpis": {"mcp": {"indexed": 100}, "a2a": {"indexed": 10}},
            "sources": {
                "mcp": {"by_source": [
                    {"source": "mcp-registry", "label": "mcp-registry", "listings": 60},
                    {"source": "agenstry", "label": "agenstry", "listings": 30},
                    {"source": "wellknown", "label": "own /.well-known crawl", "listings": 90},
                    {"source": "submission", "label": "provider submissions", "listings": 5}]},
                "a2a": {"by_source": [
                    {"source": "agenstry", "label": "agenstry", "listings": 7},
                    {"source": "manual", "label": "manual", "listings": 9}]},
            },
        }
        got = stats.source_summary(snap)
        self.assertEqual(got["count"], 2)            # mcp-registry, agenstry
        self.assertEqual((got["max_share"], got["max_source"], got["max_kind"]), (70, "agenstry", "a2a"))

    def test_missing_snapshot(self):
        self.assertIsNone(stats.source_summary(None))
        self.assertIsNone(stats.source_summary({"sources": {}, "kpis": {}}))


if __name__ == "__main__":
    unittest.main()
