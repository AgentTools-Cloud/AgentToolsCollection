"""/.well-known/security.txt (RFC 9116)."""
import datetime
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SecurityTxtTests(unittest.TestCase):
    def test_contact_expires_and_canonical(self):
        import server

        resp = server.well_known_security_txt()
        self.assertEqual(resp.media_type, "text/plain")
        body = resp.body.decode()
        lines = dict(line.split(": ", 1) for line in body.strip().splitlines())
        self.assertEqual(lines["Contact"], "mailto:admin@agent-tools.cloud")
        self.assertEqual(lines["Canonical"], "https://agent-tools.cloud/.well-known/security.txt")
        expires = datetime.datetime.strptime(lines["Expires"], "%Y-%m-%dT%H:%M:%SZ")
        days = (expires - datetime.datetime.utcnow()).days
        self.assertTrue(30 < days < 365, days)


if __name__ == "__main__":
    unittest.main()
