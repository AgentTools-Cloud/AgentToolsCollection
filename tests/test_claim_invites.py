"""Offline invitation lifecycle tests; all data stays in temporary databases."""
from contextlib import closing
from contextlib import redirect_stdout
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import smtplib
import threading
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import claim_invites, db, mailer
from directory.claim_invite_routes import router as claim_invite_router
from test_ingestion_pipeline import TemporaryDatabaseTestCase, _row


class ClaimInviteTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"AGENT_TOOLS_CLAIM_INVITES_DISCOVER": "1"}))
        with closing(db.connect()) as connection:
            claim_invites.init_schema(connection)
            db.bind_privacy_suppression_key(connection)
            connection.commit()

    def candidate(self, label="first", host="api.example.com", publisher="io.github.publisher/first"):
        listing_id = self.seed("mcp", _row(
            "mcp", label, endpoint="https://" + host + "/mcp/" + label,
            source="mcp-registry", source_id=publisher,
        ), health="ok")
        with db.writer(immediate=True) as connection:
            self.assertTrue(claim_invites.enqueue_listing(connection, "mcp", "mcp-registry", listing_id))
            return connection.execute("SELECT id FROM claim_invite_candidates WHERE listing_id=?", (listing_id,)).fetchone()[0], listing_id

    def reserve(self, candidate_id, email="hello@example.com", url="https://example.com/contact"):
        with db.writer(immediate=True) as connection:
            return claim_invites.reserve_contact(connection, candidate_id, email, url, evidence={
                "url": url, "emails": [email], "sha256": "0" * 64, "fetched_at": int(claim_invites.time.time()),
            })

    def approved(self):
        candidate_id, listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with db.writer(immediate=True) as connection:
            rendered = claim_invites.prepare_preview(connection, invitation_id)
            self.assertTrue(claim_invites.approve(connection, invitation_id, rendered["content_hash"]))
        return invitation_id, listing_id, rendered["content_hash"]

    def contact_evidence(self):
        return {"url": "https://example.com/contact", "emails": ["hello@example.com"],
                "sha256": "0" * 64, "fetched_at": int(claim_invites.time.time())}

    def route_client(self):
        app = FastAPI()
        app.include_router(claim_invite_router)
        return TestClient(app)

    def test_cli_preview_writes_private_file_without_smtp(self):
        candidate_id, _listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        output = str(Path(self.path).parent / "preview.json")
        parser = argparse.ArgumentParser()
        claim_invites.add_cli(parser.add_subparsers(dest="cmd", required=True))
        args = parser.parse_args(["claim-invites", "preview", str(invitation_id), "--output", output])
        log = io.StringIO()
        with patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()), \
                patch.object(mailer, "send_claim_invitation") as sender, redirect_stdout(log):
            self.assertEqual(claim_invites.run_cli(args), 0)
        sender.assert_not_called()
        rendered = json.loads(Path(output).read_text())
        self.assertEqual(rendered["recipient"], "hello@example.com")
        self.assertNotIn("UNSUBSCRIBE_TOKEN", rendered["text"])
        self.assertNotIn("hello@example.com", log.getvalue())
        self.assertNotIn(rendered["text"], log.getvalue())
        self.assertEqual(Path(output).stat().st_mode & 0o077, 0)
        self.assertEqual(self.query("SELECT state FROM claim_invite_outbox")[0]["state"], "draft")

    def test_cli_has_no_bulk_or_anonymous_send_action(self):
        parser = argparse.ArgumentParser()
        claim_invites.add_cli(parser.add_subparsers(dest="cmd", required=True))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["claim-invites", "send", "1", "--expect-hash", "0" * 64, "--all"])

    def test_concurrent_sends_only_reserve_one_attempt(self):
        invitation_id, _listing_id, content_hash = self.approved()
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def deliver(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "accepted", "reason": None}

        with patch.object(mailer, "claim_invitation_configured", return_value=True), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()), \
                patch.object(mailer, "send_claim_invitation", side_effect=deliver) as sender:
            first = threading.Thread(target=lambda: outcomes.append(claim_invites.send_once(invitation_id, content_hash)))
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "already_attempted")
            finally:
                release.set()
                first.join(3)
            self.assertFalse(first.is_alive())
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(outcomes[0]["status"], "accepted")

    def test_expired_approval_and_rejection_never_send(self):
        invitation_id, _listing_id, content_hash = self.approved()
        with db.writer(immediate=True) as connection:
            connection.execute("UPDATE claim_invite_outbox SET approved_at=? WHERE id=?",
                               (int(claim_invites.time.time()) - 86401, invitation_id))
        with patch.object(mailer, "send_claim_invitation") as sender:
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "blocked")
            with db.writer(immediate=True) as connection:
                self.assertTrue(claim_invites.reject(connection, invitation_id))
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "blocked")
        sender.assert_not_called()
        self.assertIsNone(self.query("SELECT email FROM claim_invite_outbox")[0]["email"])

    def test_secondary_listing_deletion_cancels_group_and_keeps_host_digest(self):
        first, _listing_id = self.candidate()
        invitation_id = self.reserve(first)
        second, secondary_id = self.candidate("secondary", host="other.example.com", publisher="io.github.other/secondary")
        self.reserve(second)
        with db.writer(immediate=True) as connection:
            connection.execute("DELETE FROM mcp_servers WHERE id=?", (secondary_id,))
        row = self.query("SELECT state,email FROM claim_invite_outbox WHERE id=?", (invitation_id,))[0]
        self.assertEqual(row["state"], "canceled")
        self.assertIsNone(row["email"])
        self.assertEqual(len(self.query("SELECT host_key FROM claim_invite_items")), 2)

    def test_review_import_creates_only_drafts_and_is_idempotent(self):
        listing_id = self.seed("mcp", _row("mcp", "imported", endpoint="https://api.example.com/mcp/imported",
                                         source="mcp-registry", source_id="io.github.publisher/imported"), health="ok")
        payload = {"invitations": [{"review_id": "CI-001", "state": "pending_review", "recipient": "hello@example.com",
                    "contacts": [{"email": "hello@example.com", "contact_url": "https://example.com/contact"}],
                    "listings": [{"kind": "mcp", "listing_id": listing_id, "host": "api.example.com"}]},
                   {"review_id": "CI-005", "state": "deferred_contact"}]}
        path = Path(self.path).parent / "review.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.dict(os.environ, {"AGENT_TOOLS_CLAIM_INVITES_DISCOVER": "0"}), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()) as probe, \
                patch.object(mailer, "send_claim_invitation") as sender:
            first = claim_invites.stage_review(str(path), expected)
            second = claim_invites.stage_review(str(path), expected)
        sender.assert_not_called()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(first["results"][0]["status"], "draft")
        self.assertEqual(first["results"][1]["status"], "deferred")
        self.assertEqual(second["results"][0]["status"], "already_staged")
        self.assertEqual(self.query("SELECT state,approved_hash FROM claim_invite_outbox")[0], {"state": "draft", "approved_hash": None})

    def test_smtp_dispatch_guard_prevents_data_submission(self):
        conf = {"host": "smtp.example.com", "port": 465, "user": "fixture", "pass": "fixture",
                "from": mailer.CLAIM_INVITE_FROM}
        with patch.object(mailer, "_conf", return_value=conf), patch.object(mailer.smtplib, "SMTP_SSL") as smtp, \
                patch.object(mailer, "_record_external_result"):
            result = mailer.send_claim_invitation("hello@example.com", "Subject", "Text", "<p>Text</p>",
                                                  message_id="<fixture@mail.agent-tools.cloud>", before_dispatch=lambda: False)
        smtp.return_value.__enter__.return_value.sendmail.assert_not_called()
        self.assertEqual(result["reason"], "dispatch_guard_blocked")

    def test_unsubscribe_page_get_never_unsubscribes_or_exposes_email(self):
        invitation_id, _listing_id, _content_hash = self.approved()
        with closing(db.connect(read_only=True)) as connection:
            token = claim_invites.unsubscribe_token(connection, invitation_id)
        with self.route_client() as client:
            response = client.get(claim_invites.UNSUBSCRIBE_PATH + "#" + token)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.text)
        self.assertNotIn("hello@example.com", response.text)
        self.assertIn("Stop claim invitations", response.text)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertFalse(self.query("SELECT * FROM claim_invite_suppressions"))

    def test_unsubscribe_confirmation_post_is_idempotent(self):
        invitation_id, _listing_id, _content_hash = self.approved()
        with closing(db.connect(read_only=True)) as connection:
            token = claim_invites.unsubscribe_token(connection, invitation_id)
        with self.route_client() as client:
            first = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": token})
            second = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("Invitations stopped", first.text)
        self.assertNotIn(token, first.text)
        self.assertEqual(len(self.query("SELECT * FROM claim_invite_suppressions")), 1)
        self.assertIsNone(self.query("SELECT email FROM claim_invite_outbox")[0]["email"])

    def test_unsubscribe_rejects_cross_origin_and_oversized_posts(self):
        with self.route_client() as client:
            cross = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": "a" * 64}, headers={"Origin": "https://evil.example"})
            large = client.post(claim_invites.UNSUBSCRIBE_PATH, content="token=" + "a" * 2048,
                                headers={"content-type": "application/x-www-form-urlencoded"})
            invalid = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": "invalid"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(large.status_code, 413)
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(self.query("SELECT * FROM claim_invite_suppressions"))

    def test_no_referrer_browser_origin_is_only_allowed_for_same_origin(self):
        invitation_id, _listing_id, _content_hash = self.approved()
        with closing(db.connect(read_only=True)) as connection:
            token = claim_invites.unsubscribe_token(connection, invitation_id)
        with self.route_client() as client:
            cross = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": token},
                                headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
            same = client.post(claim_invites.UNSUBSCRIBE_PATH, data={"token": token},
                               headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.headers["referrer-policy"], "no-referrer")

    def test_unapproved_and_changed_messages_cannot_send(self):
        candidate_id, listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with patch.object(mailer, "send_claim_invitation") as sender:
            self.assertEqual(claim_invites.send_once(invitation_id, "0" * 64)["status"], "blocked")
            with db.writer(immediate=True) as connection:
                rendered = claim_invites.prepare_preview(connection, invitation_id)
                self.assertFalse(claim_invites.approve(connection, invitation_id, "0" * 64))
                self.assertTrue(claim_invites.approve(connection, invitation_id, rendered["content_hash"]))
                connection.execute("UPDATE mcp_servers SET name='Changed listing' WHERE id=?", (listing_id,))
            self.assertEqual(claim_invites.send_once(invitation_id, rendered["content_hash"])["status"], "blocked")
        sender.assert_not_called()
        self.assertIsNone(self.query("SELECT attempted_at FROM claim_invite_outbox")[0]["attempted_at"])

    def test_one_attempt_is_committed_before_smtp_and_never_repeated(self):
        invitation_id, _listing_id, content_hash = self.approved()

        def deliver(*args, **kwargs):
            state = self.query("SELECT state,attempted_at FROM claim_invite_outbox")[0]
            self.assertEqual(state["state"], "sending")
            self.assertIsNotNone(state["attempted_at"])
            self.assertTrue(kwargs["before_dispatch"]())
            return {"status": "accepted", "reason": None}

        with patch.object(mailer, "claim_invitation_configured", return_value=True), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()), \
                patch.object(mailer, "send_claim_invitation", side_effect=deliver) as sender:
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "accepted")
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "already_attempted")
        self.assertEqual(sender.call_count, 1)
        self.assertIsNone(self.query("SELECT email FROM claim_invite_outbox")[0]["email"])

    def test_ambiguous_dispatch_has_no_retry(self):
        invitation_id, _listing_id, content_hash = self.approved()
        with patch.object(mailer, "claim_invitation_configured", return_value=True), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()), \
                patch.object(mailer, "send_claim_invitation", side_effect=TimeoutError("fixture")) as sender:
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "indeterminate")
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "already_attempted")
        self.assertEqual(sender.call_count, 1)

    def test_optout_before_dispatch_blocks_the_smtp_data(self):
        invitation_id, _listing_id, content_hash = self.approved()

        def deliver(*args, **kwargs):
            with db.writer(immediate=True) as connection:
                token = claim_invites.unsubscribe_token(connection, invitation_id)
                self.assertTrue(claim_invites.unsubscribe(connection, token))
            self.assertFalse(kwargs["before_dispatch"]())
            return {"status": "failed", "reason": "dispatch_guard_blocked"}

        with patch.object(mailer, "claim_invitation_configured", return_value=True), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value=self.contact_evidence()), \
                patch.object(mailer, "send_claim_invitation", side_effect=deliver):
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "failed")

    def test_claimed_or_unpublished_contacts_block_sending(self):
        invitation_id, listing_id, content_hash = self.approved()
        with patch.object(mailer, "claim_invitation_configured", return_value=True), \
                patch.object(claim_invites, "fetch_public_contact_page", return_value={**self.contact_evidence(), "emails": []}), \
                patch.object(mailer, "send_claim_invitation") as sender:
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["reason"], "contact_no_longer_public")
            with db.writer(immediate=True) as connection:
                connection.execute("UPDATE mcp_servers SET owner_verified=1 WHERE id=?", (listing_id,))
            self.assertEqual(claim_invites.send_once(invitation_id, content_hash)["status"], "blocked")
        sender.assert_not_called()

    def test_claim_mail_transport_distinguishes_acceptance_and_uncertainty(self):
        conf = {"host": "smtp.example.com", "port": 465, "user": "fixture", "pass": "fixture",
                "from": mailer.CLAIM_INVITE_FROM}
        for error, expected in ((None, "accepted"), (TimeoutError("fixture timeout"), "indeterminate"),
                                (smtplib.SMTPRecipientsRefused({"fixture": (550, b"refused")}), "failed")):
            with self.subTest(status=expected), patch.object(mailer, "_conf", return_value=conf), \
                    patch.object(mailer.smtplib, "SMTP_SSL") as smtp, patch.object(mailer, "_record_external_result"):
                session = smtp.return_value.__enter__.return_value
                session.sendmail.side_effect = error
                session.sendmail.return_value = {}
                result = mailer.send_claim_invitation("hello@example.com", "Subject", "Text", "<p>Text</p>",
                                                      message_id="<fixture@mail.agent-tools.cloud>")
                self.assertEqual(result["status"], expected)
                self.assertEqual(session.sendmail.call_args.args[1], ["hello@example.com"])
                message = session.sendmail.call_args.args[2]
                self.assertIn("Reply-To: contact@agent-tools.cloud", message)
                self.assertNotIn("\nCc:", message)
                self.assertNotIn("\nBcc:", message)

    def test_claim_mail_acceptance_survives_cleanup_failure(self):
        conf = {"host": "smtp.example.com", "port": 465, "user": "fixture", "pass": "fixture",
                "from": mailer.CLAIM_INVITE_FROM}
        with patch.object(mailer, "_conf", return_value=conf), patch.object(mailer.smtplib, "SMTP_SSL") as smtp, \
                patch.object(mailer, "_record_external_result"):
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            smtp.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected("fixture cleanup")
            result = mailer.send_claim_invitation("hello@example.com", "Subject", "Text", "<p>Text</p>",
                                                  message_id="<fixture@mail.agent-tools.cloud>")
        self.assertEqual(result["status"], "accepted")

    def test_claim_mail_rejects_other_service_sender_before_smtp(self):
        conf = {"host": "smtp.example.com", "port": 465, "user": "fixture", "pass": "fixture",
                "from": "noreply@another.example"}
        with patch.object(mailer, "_conf", return_value=conf), patch.object(mailer.smtplib, "SMTP_SSL") as smtp, \
                patch.object(mailer, "_record_external_result"):
            result = mailer.send_claim_invitation("hello@example.com", "Subject", "Text", "<p>Text</p>",
                                                  message_id="<fixture@mail.agent-tools.cloud>")
        self.assertEqual(result["status"], "failed")
        smtp.assert_not_called()

    def test_discovery_disabled_by_default(self):
        with patch.dict(os.environ, {"AGENT_TOOLS_CLAIM_INVITES_DISCOVER": "0"}):
            self.assertFalse(claim_invites.enqueue_listing(None, "mcp", "mcp-registry", 1))

    def test_ingestion_queues_only_new_accepted_listings_without_smtp(self):
        row = _row("mcp", "ingested", endpoint="https://api.example.com/mcp",
                   source="mcp-registry", source_id="io.github.publisher/ingested")
        with patch.object(mailer, "_send", side_effect=AssertionError("must not send")):
            added = self.ingest("mcp", [row], source="mcp-registry")
            updated = self.ingest("mcp", [row], source="mcp-registry")
            rejected = self.ingest("mcp", [_row("mcp", "invalid", endpoint="/mcp")], source="mcp-registry")
        self.assertEqual(added["added"], 1)
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(rejected["rejected"], 1)
        self.assertEqual(len(self.query("SELECT id FROM claim_invite_candidates")), 1)

    def test_invitation_queue_failure_does_not_rollback_listing(self):
        def failing_capture(connection, *args):
            connection.execute("INSERT INTO claim_invite_suppressions VALUES('probe',1)")
            raise RuntimeError("fixture queue failure")

        row = _row("mcp", "queue-failure", endpoint="https://api.example.com/mcp",
                   source="mcp-registry", source_id="io.github.publisher/queue-failure")
        with patch.object(claim_invites, "enqueue_listing", side_effect=failing_capture):
            result = self.ingest("mcp", [row], source="mcp-registry")
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(self.listings("mcp")), 1)
        self.assertFalse(self.query("SELECT id FROM claim_invite_candidates"))
        self.assertFalse(self.query("SELECT * FROM claim_invite_suppressions"))

    def test_disabled_discovery_does_not_call_queue(self):
        with patch.dict(os.environ, {"AGENT_TOOLS_CLAIM_INVITES_DISCOVER": "0"}), \
                patch.object(claim_invites, "enqueue_listing", side_effect=AssertionError("disabled")):
            result = self.ingest("mcp", [_row("mcp", "disabled", endpoint="https://api.example.com/mcp")], source="mcp-registry")
        self.assertEqual(result["added"], 1)
        self.assertFalse(self.query("SELECT id FROM claim_invite_candidates"))

    def test_drafts_do_not_send_and_preview_masks_email(self):
        candidate_id, _listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with patch.object(mailer, "_send", side_effect=AssertionError("must not send")) as sender:
            with closing(db.connect(read_only=True)) as connection:
                result = claim_invites.preview(connection, invitation_id)
        self.assertEqual(sender.call_count, 0)
        self.assertEqual(result["recipient_hint"], "he***@example.com")
        self.assertNotIn("hello@example.com", result["text"])
        self.assertIn("/mcp/servers/first", result["text"])
        self.assertEqual(self.query("SELECT state FROM claim_invite_outbox")[0]["state"], "draft")

    def test_permanent_mailbox_host_and_publisher_deduplication(self):
        first, _listing_id = self.candidate()
        self.assertIsNotNone(self.reserve(first))
        same_host, _listing_id = self.candidate("second", publisher="io.github.other/second")
        self.assertIsNone(self.reserve(same_host, "support@example.com"))
        same_publisher, _listing_id = self.candidate("third", host="other.example.com", publisher="io.github.publisher/third")
        self.assertIsNone(self.reserve(same_publisher, "team@example.com"))
        same_mail, _listing_id = self.candidate("fourth", host="last.example.com", publisher="io.github.last/fourth")
        self.assertIsNone(self.reserve(same_mail))
        self.assertEqual(len(self.query("SELECT id FROM claim_invite_outbox")), 1)

    def test_verified_contacts_group_listings_without_sending(self):
        first, _listing_id = self.candidate()
        invitation_id = self.reserve(first)
        second, _listing_id = self.candidate("second", host="other.example.com", publisher="io.github.other/second")
        self.assertIsNone(self.reserve(second))
        with closing(db.connect(read_only=True)) as connection:
            result = claim_invites.preview(connection, invitation_id)
        self.assertEqual(len(result["listings"]), 2)
        self.assertIn("/mcp/servers/first", result["text"])
        self.assertIn("/mcp/servers/second", result["text"])
        self.assertEqual(len(self.query("SELECT id FROM claim_invite_outbox")), 1)

    def test_public_contact_proof_is_required(self):
        candidate_id, _listing_id = self.candidate()
        with db.writer(immediate=True) as connection, self.assertRaises(ValueError):
            claim_invites.reserve_contact(connection, candidate_id, "hello@example.com", "https://example.com/contact")
        self.assertFalse(self.query("SELECT id FROM claim_invite_outbox"))

    def test_registry_namespace_does_not_prove_cross_domain_ownership(self):
        first, _listing_id = self.candidate()
        self.assertIsNotNone(self.reserve(first))
        second, _listing_id = self.candidate(
            "another-owner", host="api.example.org", publisher="io.github.publisher/another-owner",
        )
        self.assertIsNotNone(self.reserve(second, "hello@example.org", "https://example.org/contact"))
        self.assertEqual({row["publisher"] for row in self.query("SELECT publisher FROM claim_invite_outbox")},
                         {"example.com", "example.org"})

    def test_contacts_need_same_registered_domain_and_provenance(self):
        candidate_id, _listing_id = self.candidate()
        for email, url in (("victim@unrelated.com", "https://example.com/contact"),
                           ("hello@example.com", "https://unrelated.com/contact"),
                           ("hello@example.com\r\nBcc: victim@unrelated.com", "https://example.com/contact")):
            with self.subTest(email=email), self.assertRaises(ValueError):
                self.reserve(candidate_id, email, url)
        self.assertFalse(self.query("SELECT id FROM claim_invite_outbox"))

    def test_private_public_suffix_separates_tenants(self):
        self.assertEqual(claim_invites.normalize_contact("hello@alpha.workers.dev", "api.alpha.workers.dev"), "hello@alpha.workers.dev")
        with self.assertRaises(ValueError):
            claim_invites.normalize_contact("hello@beta.workers.dev", "alpha.workers.dev")

    def test_public_contact_must_actually_appear_on_first_party_page(self):
        page = ('<a href="mailto:hello@example.com?subject=Hello">Contact</a>'
                '<p>Support: support@example.com</p>'
                '<a href="mailto:victim@unrelated.com">Other company</a>'
                '<p>privacy@example.com security@example.com no-reply@example.com</p>'
                '<p>takedown@example.com dmca@example.com dpo@example.com</p>'
                '<script>hidden@example.com</script>'
                '<pre>sample@example.com</pre>')
        contacts = claim_invites.extract_public_contacts(
            page, host="api.example.com", contact_url="https://example.com/contact",
        )
        self.assertEqual(contacts, ["hello@example.com", "support@example.com"])
        self.assertEqual(claim_invites.extract_public_contacts(
            "<p>No email published</p>", host="api.example.com",
            contact_url="https://example.com/contact",
        ), [])
        with self.assertRaises(ValueError):
            claim_invites.extract_public_contacts(
                "hello@example.com", host="api.example.com",
                contact_url="https://unrelated.com/contact",
            )

    def test_public_json_contact_fields_not_arbitrary_example_text(self):
        contacts = claim_invites.extract_public_contacts(
            '{"contact":{"email":"hello@example.com"},"example":"sample@example.com"}',
            host="api.example.com", contact_url="https://example.com/contact.json",
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(contacts, ["hello@example.com"])

    def test_contact_fetch_is_bounded_and_does_not_send_mail(self):
        def respond(request):
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<a href="mailto:hello@example.com">Contact</a><a href="/about">About</a>')

        with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                patch.object(mailer, "_send", side_effect=AssertionError("must not send")):
            page = claim_invites.fetch_public_contact_page(
                "https://example.com/contact", host="api.example.com", http_client=client,
            )
        self.assertEqual(page["emails"], ["hello@example.com"])
        self.assertEqual(page["links"], ["https://example.com/about"])
        self.assertEqual(len(page["sha256"]), 64)

    def test_contact_fetch_rejects_cross_domain_redirect_before_request(self):
        requests = []

        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://unrelated.com/contact"})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client, self.assertRaises(ValueError):
            claim_invites.fetch_public_contact_page(
                "https://example.com/contact", host="api.example.com", http_client=client,
            )
        self.assertEqual(requests, ["https://example.com/contact"])

    def test_contact_fetch_rejects_oversized_pages_and_paid_responses(self):
        for response in (
                httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 262145),
                httpx.Response(402, headers={"content-type": "application/json"}, text="{}")):
            with self.subTest(status=response.status_code), \
                    httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client, \
                    self.assertRaises(ValueError):
                claim_invites.fetch_public_contact_page(
                    "https://example.com/contact", host="api.example.com", http_client=client,
                )

    def test_verified_and_shared_hosts_are_not_invited(self):
        candidate_id, listing_id = self.candidate()
        with db.writer() as connection:
            connection.execute("UPDATE mcp_servers SET owner_verified=1 WHERE id=?", (listing_id,))
        self.assertIsNone(self.reserve(candidate_id))
        with db.writer() as connection:
            connection.execute("UPDATE mcp_servers SET owner_verified=0 WHERE id=?", (listing_id,))
            connection.execute("INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) VALUES('api.example.com',2,2,'unreviewed',1)")
        self.assertIsNone(self.reserve(candidate_id))

    def test_safety_scan_is_checked_before_reserving_contact(self):
        candidate_id, _listing_id = self.candidate()
        with patch.object(db, "_safety_mark", return_value={"verdict": "suspicious"}):
            self.assertIsNone(self.reserve(candidate_id))
        self.assertFalse(self.query("SELECT id FROM claim_invite_outbox"))

    def test_report_eligibility_uses_the_same_claim_protections(self):
        _candidate_id, listing_id = self.candidate()
        with closing(db.connect(read_only=True)) as connection:
            listing = dict(connection.execute("SELECT * FROM mcp_servers WHERE id=?", (listing_id,)).fetchone())
            eligible, reason = claim_invites.evaluate_listing(connection, "mcp", listing)
            self.assertEqual(reason, "eligible")
            self.assertEqual(eligible["invite_host"], "api.example.com")
            with patch.object(db, "_safety_mark", return_value={"verdict": "suspicious"}):
                self.assertEqual(claim_invites.evaluate_listing(connection, "mcp", listing), (None, "safety_flag"))
            listing["owner_verified"] = 1
            self.assertEqual(claim_invites.evaluate_listing(connection, "mcp", listing), (None, "claimed"))

    def test_real_unsubscribe_preview_is_stable_and_bound_to_content(self):
        candidate_id, listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with db.writer(immediate=True) as connection:
            token = claim_invites.new_unsubscribe_token(connection, invitation_id)
        with closing(db.connect(read_only=True)) as connection:
            first = claim_invites.preview(connection, invitation_id)
            second = claim_invites.preview(connection, invitation_id)
        self.assertEqual(first, second)
        self.assertIn("#" + token, first["text"])
        self.assertNotIn("UNSUBSCRIBE_TOKEN", first["text"])
        self.assertNotEqual(self.query("SELECT unsubscribe_nonce FROM claim_invite_outbox")[0]["unsubscribe_nonce"], token)
        with db.writer() as connection:
            connection.execute("UPDATE mcp_servers SET name='Changed listing' WHERE id=?", (listing_id,))
        with closing(db.connect(read_only=True)) as connection:
            changed = claim_invites.preview(connection, invitation_id)
        self.assertNotEqual(first["content_hash"], changed["content_hash"])

    def test_unsubscribe_is_idempotent_and_retains_only_digest(self):
        candidate_id, _listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with db.writer(immediate=True) as connection:
            token = claim_invites.new_unsubscribe_token(connection, invitation_id)
            self.assertTrue(claim_invites.unsubscribe(connection, token))
            self.assertTrue(claim_invites.unsubscribe(connection, token))
            self.assertFalse(claim_invites.unsubscribe(connection, token + "wrong"))
        self.assertEqual(len(self.query("SELECT * FROM claim_invite_suppressions")), 1)
        self.assertIsNone(self.query("SELECT email FROM claim_invite_outbox")[0]["email"])
        self.assertNotEqual(self.query("SELECT unsubscribe_hash FROM claim_invite_outbox")[0]["unsubscribe_hash"], token)

    def test_deleting_listing_scrubs_contact_but_keeps_dedup(self):
        candidate_id, listing_id = self.candidate()
        invitation_id = self.reserve(candidate_id)
        with db.writer() as connection:
            connection.execute("DELETE FROM mcp_servers WHERE id=?", (listing_id,))
        result = self.query("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,))[0]
        self.assertEqual(result["state"], "canceled")
        self.assertIsNone(result["email"])
        self.assertIsNone(result["host"])
        self.assertTrue(result["email_key"])
        self.assertFalse(self.query("SELECT * FROM claim_invite_candidates"))

    def test_queue_requires_caller_transaction(self):
        with closing(db.connect(read_only=True)) as connection, self.assertRaises(RuntimeError):
            claim_invites.reserve_contact(connection, 1, "hello@example.com", "https://example.com/contact")

    def test_unbound_privacy_key_refuses_draft(self):
        candidate_id, _listing_id = self.candidate()
        with db.writer() as connection:
            connection.execute("DELETE FROM meta WHERE key='privacy_suppression_key_id'")
        with self.assertRaises(RuntimeError):
            self.reserve(candidate_id)
        self.assertFalse(self.query("SELECT id FROM claim_invite_outbox"))


if __name__ == "__main__":
    unittest.main()