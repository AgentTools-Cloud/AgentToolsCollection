"""Private notice persistence/export and no-SMTP regressions, offline only."""

from contextlib import closing
import json
from unittest.mock import Mock, patch

from directory import db, ingestion, mailer, notices
from test_ingestion_pipeline import TemporaryDatabaseTestCase, _row


class InternalNoticeTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.smtp = self.enterContext(patch.object(mailer.smtplib, 'SMTP_SSL',
                                      side_effect=AssertionError('SMTP is forbidden')))

    def feed(self):
        with closing(db.connect(read_only=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            return notices.export_feed(conn)

    def test_admin_notice_records_ids_not_submitted_title_contact_or_request(self):
        payload = {'name': 'Private service', 'url': 'https://service.example/api',
                   'contact': 'private@example.com', '_client_ip': '10.20.30.40'}
        with db.writer() as conn:
            submission = db.create_submission(conn, payload)
        self.assertTrue(mailer.send_admin_notification(
            'x402 submission', 'https://SECRET.example/?token=private-token', 'rejected',
            [('URL', payload['url']), ('Contact', payload['contact']),
             ('Evidence', 'Authorization: private-cookie'), ('Submission', f'#{submission}')]))
        raw = json.dumps(self.query('SELECT * FROM internal_notices'))
        for secret in ('SECRET', 'private-token', 'private@example.com', '10.20.30.40', 'private-cookie'):
            self.assertNotIn(secret, raw)
        event = self.feed()['notices'][0]
        self.assertIn('rejected', event['summary'])
        self.assertIn('pending', event['summary'])
        self.assertNotIn(f'#{submission}', json.dumps(event, ensure_ascii=False))
        self.smtp.assert_not_called()

    def test_out_of_order_repeat_never_moves_last_seen_backwards(self):
        with patch.object(notices.time, 'time', return_value=2_000_000_100):
            self.assertTrue(notices.store('submission', 'info', 'repeat', 'same', event_key='k'))
        with patch.object(notices.time, 'time', return_value=2_000_000_050):
            self.assertTrue(notices.store('submission', 'info', 'repeat', 'same', event_key='k'))
        with closing(db.connect(read_only=True)) as conn:
            row = conn.execute('SELECT first_seen,last_seen,occurrences FROM internal_notices').fetchone()
        self.assertEqual(tuple(row), (2_000_000_100, 2_000_000_100, 2))

    def test_watchdog_alert_is_stored_without_raw_errors_or_smtp(self):
        text = 'agent-tools 后台任务异常\n\n- database failed https://secret.example/?token=abcdef\n- 来源端点失配警告: a2a\n'
        self.assertTrue(mailer.send_internal_notice('[agent-tools] 后台任务异常: database failed', text, '<script>private</script>'))
        raw = json.dumps(self.feed(), ensure_ascii=False)
        for value in ('secret.example', 'abcdef', '<script>', 'database failed'):
            self.assertNotIn(value, raw)
        self.assertIn('巡检记录 2 项', raw)
        self.smtp.assert_not_called()

    def test_watchdog_classifies_operational_failures_without_raw_detail(self):
        problems = ['mcp health 整轮中断 3 次',
                    'agent-tools-backup.service 上次结束状态 = private-result',
                    '加密异地备份最近完成已超过 30h，或成功记录缺失/无效',
                    'services 最新写入已是 50.0 小时前（阈值 30）']
        self.assertTrue(notices.publish_watchdog('后台任务异常', problems))
        event = self.feed()['notices'][0]
        self.assertEqual(event['data']['backup_unavailable'], 1)
        self.assertEqual(event['data']['stage_abort'], 1)
        self.assertIn('异地备份过期或记录无效', event['summary'])
        self.assertIn('主表写入过期', event['summary'])
        self.assertNotIn('private-result', json.dumps(event))
        self.smtp.assert_not_called()

    def test_old_direct_admin_sender_cannot_fall_back_to_smtp(self):
        with patch.object(mailer, '_conf', side_effect=AssertionError('SMTP config access forbidden')):
            self.assertTrue(mailer._send('admin@agent-tools.cloud', 'secret', 'raw contact', 'raw HTML'))
        self.smtp.assert_not_called()

    def test_external_submitter_email_uses_existing_transport_unchanged(self):
        with patch.object(mailer, '_send', return_value=True) as send:
            self.assertTrue(mailer.send_approval_email('owner@example.com', 'Service', 'fixture'))
        self.assertEqual(send.call_args.args[0], 'owner@example.com')
        self.assertIn("You're listed", send.call_args.args[1])

    def test_storage_failure_returns_false_and_never_sends_email(self):
        with patch.object(db, 'writer', side_effect=OSError('fixture failure')):
            self.assertFalse(mailer.send_admin_notification('MCP submission', 'Site', 'listed', []))
        self.smtp.assert_not_called()

    def test_repeat_notice_deduplicates_and_updates_occurrence(self):
        for _ in range(3):
            self.assertTrue(notices.store('system', 'info', 'Fixed event', 'Fixed summary', event_key='same'))
        rows = self.query('SELECT * FROM internal_notices')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['occurrences'], 3)

    def test_outbound_receipt_is_durable_and_exported_without_recipient_or_body(self):
        for state in ('accepted', 'failed_or_unknown', 'skipped'):
            self.assertTrue(notices.publish_mail_result('approval', state, 'fixture-' + state))
        rows = self.query('SELECT * FROM internal_notices')
        self.assertEqual(len(rows), 3)
        feed = [row for row in self.feed()['notices'] if row['category'] == 'outbound_mail']
        self.assertEqual(len(feed), 3)
        self.assertTrue(any(row['data']['smtp_accepted'] for row in feed))
        self.assertTrue(any(row['data']['not_sent'] for row in feed))
        self.assertTrue(all(row['title'] == '上架确认邮件' for row in feed))
        self.assertTrue(all(row['listing_id'] is None and row['submission_id'] is None for row in rows))
        self.smtp.assert_not_called()

    def test_safe_summary_redacts_url_mail_key_and_ip(self):
        summary = 'https://private.example/?token=secret owner@example.com 10.20.30.40 sk-' + 'x' * 40
        notices.store('system', 'warning', 'Fixed warning', summary)
        raw = json.dumps(self.feed())
        for secret in ('private.example', 'owner@example.com', '10.20.30.40', 'x' * 40):
            self.assertNotIn(secret, raw)

    def test_listing_purge_and_submission_delete_remove_linked_notices(self):
        row = _row('mcp', 'private-listed', source='submission')
        lid = self.seed('mcp', row)
        notices.store('submission', 'info', 'MCP 提交', 'listed', listing_kind='mcp', listing_id=lid)
        with db.writer() as conn:
            db.privacy_purge_listing(conn, 'mcp', row['slug'], 'test')
        self.assertEqual(self.query('SELECT * FROM internal_notices'), [])
        with db.writer() as conn:
            sid = db.create_submission(conn, {'name': 'Fixture', 'url': 'https://fixture.example/api'})
        notices.store('submission', 'info', 'x402 提交', 'pending', submission_id=sid)
        with db.writer() as conn:
            conn.execute('DELETE FROM submissions WHERE id=?', (sid,))
        self.assertEqual(self.query('SELECT * FROM internal_notices'), [])

    def test_active_sources_survive_without_new_email_and_export_is_read_only(self):
        with db.writer() as conn:
            run = db.record_crawl_start(conn, 'known-broken')
            db.record_crawl_finish(conn, run, 0, 0, ['https://private.example/?token=raw-secret'], 'error')
        before = self.query('SELECT * FROM crawl_runs')
        feed = self.feed()
        self.assertTrue(any(row['category'] == 'crawl' and row['status'] == 'active' for row in feed['notices']))
        self.assertNotIn('raw-secret', json.dumps(feed))
        self.assertEqual(self.query('SELECT * FROM crawl_runs'), before)

    def test_removed_crawl_source_is_not_reported_as_active(self):
        with db.writer() as conn:
            for source, status in (('agenstry:crawl', 'partial'),
                                   ('mcp:mcp-catalog', 'error'), ('mcp:pulsemcp', 'partial'),
                                   ('a2a:wellknown', 'partial'), ('mcp:smithery', 'error')):
                run = db.record_crawl_start(conn, source)
                db.record_crawl_finish(conn, run, 0, 0, ['gone'], status)
        before = self.query('SELECT * FROM crawl_runs')
        titles = [row['title'] for row in self.feed()['notices'] if row['category'] == 'crawl']
        self.assertFalse(any(name in title for title in titles
                             for name in ('agenstry:crawl', 'mcp-catalog', 'pulsemcp', 'a2a:wellknown')))
        self.assertTrue(any('mcp:smithery' in title for title in titles))
        self.assertEqual(self.query('SELECT * FROM crawl_runs'), before)

    def test_history_retention_does_not_hide_old_active_ingestion(self):
        with db.writer() as conn:
            conn.execute('INSERT INTO ingestion_issues '
                         '(kind,source,identity_hash,content_hash,disposition,reason,first_seen,last_seen) '
                         "VALUES('mcp','wellknown','a','b','hold','fixture',1,2)")
        feed = self.feed()
        self.assertTrue(any(row['category'] == 'ingestion' and row['status'] == 'active' for row in feed['notices']))

    def ingestion_feed(self, now):
        with closing(db.connect(read_only=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            feed = notices.export_feed(conn, now=now)
        return {row['summary'].split('；')[0]: row for row in feed['notices'] if row['category'] == 'ingestion'}

    def test_unobserved_rejections_stop_being_active_but_holds_never_expire(self):
        now, day = 2_000_000_000, 86400
        with db.writer() as conn:
            # Deleted upstream 22 days ago; a superseded content version; a live
            # rejection seen by yesterday's crawl; a month-old hold.
            for identity, content, disposition, reason, age in (
                    ('gone', 'v1', 'rejected', 'missing_endpoint', 22),
                    ('tpl', 'v1', 'rejected', 'needs_configuration:url_template', 10),
                    ('tpl', 'v2', 'rejected', 'needs_configuration:url_template', 1),
                    ('held', 'v1', 'hold', 'new_endpoint_requires_review', 30)):
                conn.execute('INSERT INTO ingestion_issues '
                             '(kind,source,identity_hash,content_hash,disposition,reason,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             ('mcp', 'fixture', identity, content, disposition, reason,
                              now - (age + 1) * day, now - age * day))
        before = self.query('SELECT * FROM ingestion_issues ORDER BY identity_hash,content_hash')
        active = self.ingestion_feed(now)
        self.assertEqual(set(active), {'needs_configuration:url_template', 'new_endpoint_requires_review'})
        self.assertIn('近 3 天观测到来源身份 1，内容版本 1', active['needs_configuration:url_template']['summary'])
        self.assertEqual(active['new_endpoint_requires_review']['severity'], 'error')
        self.assertIn('来源身份 1，内容版本 1', active['new_endpoint_requires_review']['summary'])
        self.assertEqual(self.query('SELECT * FROM ingestion_issues ORDER BY identity_hash,content_hash'), before)

    def test_reobserved_rejection_is_active_again(self):
        row = {'slug': 'fixture-template', 'endpoint_url': 'https://fixture.example/{id}'}
        old, now = 2_000_000_000, 2_000_000_000 + 22 * 86400
        for seen in (old, now):
            with patch.object(ingestion.time, 'time', return_value=seen), db.writer() as conn:
                ingestion.record_issue(conn, 'mcp', 'fixture', row, 'rejected', 'needs_configuration:url_template')
            if seen == old:
                self.assertEqual(self.ingestion_feed(now), {})
        self.assertEqual(self.ingestion_feed(now)['needs_configuration:url_template']['occurrences'], 2)

    def _submission(self, name):
        with db.writer() as conn:
            return db.create_submission(conn, {'name': name, 'url': f'https://{name}.example/api'})

    def test_routine_successes_collapse_to_one_row_per_beijing_day(self):
        # 2_000_044_799 / 2_000_044_800 are 15:59:59 / 16:00:00 UTC: one second either side of Beijing midnight.
        for index, now in enumerate((2_000_044_799, 2_000_044_800, 2_000_044_900)):
            with patch.object(notices.time, 'time', return_value=now):
                sid = self._submission(f'bulk{index}')
                self.assertTrue(mailer.send_admin_notification(
                    'x402 submission', 'Bulk', 'listed', [('Submission', f'#{sid}')]))
                self.assertTrue(notices.publish_mail_result('approval', 'accepted', f'mail-{index}'))
        rows = self.query('SELECT category,submission_id,listing_id,occurrences FROM internal_notices '
                          'ORDER BY category,first_seen')
        self.assertEqual([(r['category'], r['occurrences']) for r in rows],
                         [('outbound_mail', 1), ('outbound_mail', 2), ('submission', 1), ('submission', 2)])
        self.assertTrue(all(r['submission_id'] is None and r['listing_id'] is None for r in rows))
        titles = {row['title'] for row in self.feed()['notices']}
        self.assertEqual(titles, {'x402 提交验证', '上架确认邮件'})
        self.smtp.assert_not_called()

    def test_failures_and_non_routine_events_stay_individual(self):
        for index in range(2):
            sid = self._submission(f'rejected{index}')
            self.assertTrue(mailer.send_admin_notification(
                'x402 submission', 'R', 'rejected', [('Submission', f'#{sid}')]))
        for purpose, state, event in (('approval', 'failed_or_unknown', 'f1'), ('approval', 'failed_or_unknown', 'f2'),
                                      ('external', 'accepted', 'e1'), ('external', 'accepted', 'e2')):
            self.assertTrue(notices.publish_mail_result(purpose, state, event))
        rows = self.query('SELECT submission_id,occurrences FROM internal_notices')
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r['occurrences'] == 1 for r in rows))
        self.assertEqual(sum(r['submission_id'] is not None for r in rows), 2)

    def test_bulk_routine_events_do_not_evict_other_history(self):
        with patch.object(notices, 'MAX_HISTORY', 5), patch.object(notices.time, 'time', return_value=2_000_000_000):
            self.assertTrue(notices.store('system', 'warning', 'Older warning', 'kept', event_key='older'))
            for index in range(20):
                sid = self._submission(f'flood{index}')
                mailer.send_admin_notification('x402 submission', 'Flood', 'listed', [('Submission', f'#{sid}')])
                notices.publish_mail_result('approval', 'accepted', f'flood-{index}')
        titles = [row['title'] for row in self.query('SELECT title FROM internal_notices')]
        self.assertIn('Older warning', titles)
        self.assertEqual(len(titles), 3)

    def test_source_export_aggregates_without_listing_ids_or_endpoint_hashes(self):
        warnings = [{'kind': 'mcp', 'listing_id': 987654321, 'change_type': 'authority',
                     'warning_key': 'b' * 64, 'old_endpoint_hash': 'c' * 64,
                     'current_endpoint_hash': 'd' * 64, 'resolved_at': None,
                     'first_seen': 1, 'last_seen': 2, 'occurrences': 3}]
        with patch.object(db, 'visible_source_endpoint_warnings', return_value=warnings * 2):
            feed = self.feed()
        source = [row for row in feed['notices'] if row['category'] == 'source']
        self.assertEqual(len(source), 1)
        self.assertEqual(source[0]['data'], {'warning_pairs': 2})
        raw = json.dumps(source)
        for reference in ('987654321', 'c' * 12, 'd' * 12, 'b' * 64):
            self.assertNotIn(reference, raw)
