"""Claim invitation: readiness line and claim how-to."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import mailer  # noqa: E402

LISTING = "https://agent-tools.cloud/mcp/servers/demo"
UNSUB = "https://agent-tools.cloud/email/claim-invitations/unsubscribe#tok"


class InvitationReadiness(unittest.TestCase):
    def test_readiness_line_present(self):
        subject, text, markup = mailer.claim_invitation_message(
            "Demo", "the official MCP Registry", LISTING, UNSUB, {"grade": "C", "score": 68})
        self.assertIn("Its readiness grade is C (68/100)", text)
        self.assertIn(LISTING + "#readiness", text)
        self.assertIn("<b>C</b> (68/100)", markup)
        self.assertIn("https://agent-tools.cloud/docs/claim", text)
        self.assertIn("DNS TXT record", text)

    def test_readiness_line_absent_without_score(self):
        _s, text, markup = mailer.claim_invitation_message(
            "Demo", "the official MCP Registry", LISTING, UNSUB)
        self.assertNotIn("readiness", text)
        self.assertNotIn("#readiness", markup)

    def test_group_single_passes_readiness(self):
        listing = {"name": "Demo", "source_name": "Smithery", "listing_url": LISTING,
                   "readiness": {"grade": "<b>", "score": 50}}
        _s, text, markup = mailer.claim_invitation_group_message([listing], UNSUB)
        self.assertIn("(50/100)", text)
        self.assertNotIn("<b><b>", markup)  # grade is escaped


if __name__ == "__main__":
    unittest.main()
