"""Approval email: at most one per contact and per site per window (2026-11-02)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import db, jobs


class ApprovalEmailThrottleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="approval-mail-")
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_TOOLS_DB_PATH": self.path,
            "PRIVACY_SUPPRESSION_KEY": "offline-fixture-only-not-a-real-secret"}))
        self.enterContext(patch.object(db, "DEFAULT_DB_PATH", self.path))
        # db.connect/db.writer bind their default path at import time; pin
        # every call to the temporary database.
        real_connect, real_writer = db.connect, db.writer

        def connect(db_path=None, read_only=False, set_wal=True):
            return real_connect(self.path, read_only=read_only, set_wal=set_wal)

        def writer(db_path=None, immediate=False):
            return real_writer(self.path, immediate=immediate)

        self.enterContext(patch.object(db, "connect", side_effect=connect))
        self.enterContext(patch.object(db, "writer", side_effect=writer))
        self.enterContext(patch.object(db.time, "sleep"))
        db.init_db(self.path)
        self.enterContext(patch.object(jobs, "APPROVAL_EMAIL_WINDOW", 86400))

    def test_same_site_many_paths_gets_one_email(self):
        t = 1_800_000_000
        first = jobs._claim_approval_email("op@example.com", "https://a.example/v1/x", now=t)
        again = [jobs._claim_approval_email("op@example.com", "https://a.example/v1/p%d" % i,
                                            now=t + 60 * i) for i in range(1, 30)]
        self.assertTrue(first)
        self.assertFalse(any(again))

    def test_same_contact_different_site_is_suppressed(self):
        t = 1_800_000_000
        self.assertTrue(jobs._claim_approval_email("op@example.com", "https://a.example/x", now=t))
        self.assertFalse(jobs._claim_approval_email("OP@example.com ", "https://b.example/x", now=t + 5))

    def test_same_site_different_contact_is_suppressed(self):
        t = 1_800_000_000
        self.assertTrue(jobs._claim_approval_email("one@example.com", "https://a.example/x", now=t))
        self.assertFalse(jobs._claim_approval_email("two@example.com", "https://a.example/y", now=t + 5))

    def test_unrelated_submitters_both_get_email(self):
        t = 1_800_000_000
        self.assertTrue(jobs._claim_approval_email("one@example.com", "https://a.example/x", now=t))
        self.assertTrue(jobs._claim_approval_email("two@example.com", "https://b.example/x", now=t + 1))

    def test_window_reopens_after_24h_from_last_send(self):
        t = 1_800_000_000
        self.assertTrue(jobs._claim_approval_email("op@example.com", "https://a.example/x", now=t))
        self.assertFalse(jobs._claim_approval_email("op@example.com", "https://a.example/y", now=t + 86399))
        self.assertTrue(jobs._claim_approval_email("op@example.com", "https://a.example/z", now=t + 86401))

    def test_storage_failure_fails_open(self):
        with patch.object(db, "with_retry", side_effect=RuntimeError("db down")):
            self.assertTrue(jobs._claim_approval_email("op@example.com", "https://a.example/x"))

    def test_approve_skips_second_email_but_still_lists(self):
        sent = []
        with patch.object(jobs.mailer, "send_approval_email",
                          side_effect=lambda *a, **k: sent.append(a) or True):
            ids = []
            with db.writer() as c:
                for path in ("v1/a", "v1/b"):
                    ids.append(db.create_submission(c, {
                        "url": "https://svc.example/" + path, "name": path,
                        "contact": "op@example.com", "category": "data"}))
            with patch.object(jobs.crawlers, "fetch_wellknown_resources", return_value={}), \
                    patch.object(jobs.crawlers, "check_health", return_value="ok"):
                r1 = jobs._approve(ids[0], verdict={"verified_url": "https://svc.example/v1/a"})
                r2 = jobs._approve(ids[1], verdict={"verified_url": "https://svc.example/v1/b"})
        self.assertTrue(r1 and r2)
        self.assertEqual(len(sent), 1)
        with db.connect(read_only=True) as c:
            n = c.execute("SELECT COUNT(*) FROM services WHERE url LIKE 'https://svc.example/%'").fetchone()[0]
            st = [r[0] for r in c.execute("SELECT status FROM submissions ORDER BY id")]
        self.assertEqual(n, 2)
        self.assertEqual(st, ["approved", "approved"])

    def test_resubmitted_listing_gets_no_email(self):
        sent = []
        self.enterContext(patch.object(jobs, "APPROVAL_EMAIL_WINDOW", 0))
        with patch.object(jobs.mailer, "send_approval_email",
                          side_effect=lambda *a, **k: sent.append(a) or True):
            ids = []
            with db.writer() as c:
                for _ in range(2):
                    ids.append(db.create_submission(c, {
                        "url": "https://svc.example/v1/a", "name": "a",
                        "contact": "op@example.com", "category": "data"}))
            with patch.object(jobs.crawlers, "fetch_wellknown_resources", return_value={}), \
                    patch.object(jobs.crawlers, "check_health", return_value="ok"):
                jobs._approve(ids[0], verdict={"verified_url": "https://svc.example/v1/a"})
                jobs._approve(ids[1], verdict={"verified_url": "https://svc.example/v1/a"})
        self.assertEqual(len(sent), 1)

    def test_suppressed_recipient_is_never_sent(self):
        from directory import mailer
        with db.writer() as c:
            db.add_mail_suppression(c, "dead@example.com", "550 user unknown", "test")
            db.add_mail_suppression(c, "@nomx.example", "no MX", "test")
        env = {"SMTP_HOST": "smtp.invalid", "SMTP_USER": "u", "SMTP_PASS": "p"}
        with patch.dict(os.environ, env), \
                patch.object(mailer.smtplib, "SMTP_SSL", side_effect=AssertionError("must not connect")):
            r1 = mailer._deliver_external("Dead@Example.com", "s", "t", "h")
            r2 = mailer._deliver_external("anyone@nomx.example", "s", "t", "h")
        self.assertEqual((r1["status"], r1["reason"]), ("skipped", "recipient_suppressed"))
        self.assertEqual(r2["status"], "skipped")
        with db.connect(read_only=True) as c:
            self.assertIsNone(db.mail_suppressed(c, "ok@example.com"))


if __name__ == "__main__":
    unittest.main()
