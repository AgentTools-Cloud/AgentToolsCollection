"""Offline sender/Notice tests. SMTP is always mocked; no production writes."""

from email import message_from_string
import json
import smtplib
from unittest.mock import Mock, patch
import unittest

from directory import mailer, notices


class OutboundMailNoticeTests(unittest.TestCase):
    def setUp(self):
        self.conf = self.enterContext(patch.object(mailer, '_conf', return_value={
            'host': 'smtp.fixture.invalid', 'port': 465, 'user': 'fixture',
            'pass': 'fixture-not-a-real-secret', 'from': 'sender@example.com'}))
        self.smtp = Mock()
        self.smtp.sendmail.return_value = {}
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.smtp)
        self.context.__exit__ = Mock(return_value=False)
        self.transport = self.enterContext(patch.object(mailer.smtplib, 'SMTP_SSL', return_value=self.context))
        self.store = self.enterContext(patch.object(notices, 'store', return_value=True))

    def send(self):
        return mailer.send_approval_email('owner@example.com', 'Private service', 'private-slug', 'private details')

    def test_confirmation_sent_once_to_external_recipient_only_and_notice_accepted(self):
        self.assertTrue(self.send())
        self.smtp.sendmail.assert_called_once()
        sender, recipients, raw = self.smtp.sendmail.call_args.args
        self.assertEqual(recipients, ['owner@example.com'])
        msg = message_from_string(raw)
        self.assertEqual(msg['To'], 'owner@example.com')
        self.assertIsNone(msg['Cc'])
        self.assertIsNone(msg['Bcc'])
        category, severity, title, summary = self.store.call_args.args
        self.assertEqual((category, severity, title), ('outbound_mail', 'info', '上架确认邮件'))
        self.assertIn('SMTP 已接收', summary)
        self.assertIn('送达尚未验证', summary)
        self.assert_no_private_receipt_data()

    def assert_no_private_receipt_data(self):
        recorded = str(self.store.call_args)
        for value in ('owner@example.com', 'Private service', 'private-slug', 'private details',
                      'fixture-not-a-real-secret', 'sender@example.com'):
            self.assertNotIn(value, recorded)

    def test_rejection_also_gets_local_receipt_without_self_copy(self):
        self.assertTrue(mailer.send_rejection_email('owner@example.com', 'Private service', 'private details'))
        self.assertEqual(self.store.call_args.args[2], '提交拒绝邮件')
        self.assertEqual(self.smtp.sendmail.call_args.args[1], ['owner@example.com'])
        self.assert_no_private_receipt_data()

    def test_smtp_failure_is_recorded_without_resend_or_exception_detail(self):
        self.smtp.sendmail.side_effect = smtplib.SMTPDataError(550, b'private server response')
        self.assertFalse(self.send())
        self.assertEqual(self.store.call_args.args[1], 'warning')
        self.assertIn('状态未知', self.store.call_args.args[3])
        self.assertNotIn('private server response', str(self.store.call_args))
        self.smtp.sendmail.assert_called_once()

    def test_unconfigured_sender_records_not_sent_without_smtp(self):
        self.conf.return_value = None
        self.assertFalse(self.send())
        self.transport.assert_not_called()
        self.assertEqual(self.store.call_args.kwargs['data']['not_sent'], True)

    def test_record_failure_does_not_change_success_or_send_again(self):
        for failure in (False, OSError('private failure')):
            with self.subTest(failure=failure):
                self.store.reset_mock(); self.smtp.sendmail.reset_mock()
                self.store.side_effect = failure if isinstance(failure, Exception) else None
                self.store.return_value = failure
                self.assertTrue(self.send())
                self.smtp.sendmail.assert_called_once()

    def test_disconnect_after_acceptance_is_still_accepted(self):
        self.context.__exit__.side_effect = smtplib.SMTPServerDisconnected('private disconnect')
        self.assertTrue(self.send())
        self.assertTrue(self.store.call_args.kwargs['data']['smtp_accepted'])
        self.smtp.sendmail.assert_called_once()

    def test_refused_recipient_is_not_reported_as_accepted(self):
        self.smtp.sendmail.return_value = {'owner@example.com': (550, b'private')}
        self.assertFalse(self.send())
        self.assertFalse(self.store.call_args.kwargs['data']['smtp_accepted'])

    def test_each_actual_attempt_has_distinct_anonymous_event(self):
        self.send(); self.send()
        keys = [call.kwargs['event_key'] for call in self.store.call_args_list]
        self.assertEqual(len(set(keys)), 2)

    def test_old_internal_sender_still_does_not_use_transport_or_outbound_receipt(self):
        self.assertTrue(mailer._send('admin@agent-tools.cloud', 'private', 'private', 'private'))
        self.transport.assert_not_called()
        self.assertEqual(self.store.call_args.args[0], 'system')

    def test_unknown_subject_uses_fixed_generic_label(self):
        self.assertTrue(mailer._send('owner@example.com', 'private subject', 'private body', 'private HTML'))
        self.assertEqual(self.store.call_args.args[2], '外部业务邮件')
        self.assertNotIn('private subject', str(self.store.call_args))


if __name__ == '__main__':
    unittest.main()