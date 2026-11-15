"""/.well-known/ard.json - Agent Resource Discovery manifest (draft-bu-dawn-ard-discovery-00)."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URN = re.compile(r"^urn:air:agent-tools\.cloud:[a-z0-9-]+:[a-z0-9-]+$")


class ArdManifestTests(unittest.TestCase):
    def test_media_type_and_entries_follow_the_draft(self):
        import server

        resp = server.well_known_ard()
        self.assertEqual(resp.media_type, "application/ard+json")
        doc = json.loads(resp.body)
        self.assertTrue(doc["entries"])
        ids = set()
        for e in doc["entries"]:
            self.assertRegex(e["identifier"], URN)
            self.assertNotIn(e["identifier"], ids)
            ids.add(e["identifier"])
            self.assertTrue(e["displayName"] and e["type"])
            self.assertTrue(("url" in e) ^ ("data" in e))  # exactly one of url / data
            self.assertTrue(e["url"].startswith("https://agent-tools.cloud/"))
            self.assertTrue(2 <= len(e["representativeQueries"]) <= 5, e["identifier"])
        types = {e["type"] for e in doc["entries"]}
        self.assertIn("application/mcp-server-card+json", types)
        self.assertIn("application/a2a-agent-card+json", types)

    def test_both_paths_served_and_advertised(self):
        from fastapi.testclient import TestClient
        import server

        c = TestClient(server.app)
        for path in ("/.well-known/ard.json", "/.well-known/ai-catalog.json"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("application/ard+json"))
        self.assertIn("Agentmap: https://agent-tools.cloud/.well-known/ard.json", c.get("/robots.txt").text)


if __name__ == "__main__":
    unittest.main()
