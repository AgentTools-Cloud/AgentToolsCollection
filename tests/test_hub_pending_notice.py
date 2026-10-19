"""Hub pending-review Notice row: counts only, validator-safe, merged as active."""

from contextlib import closing
import os
import re
import sqlite3
import tempfile
import unittest

from ops import hub_pending_notice as hub


def make_hub(rows):
    path = tempfile.mktemp(suffix='.db')
    with closing(sqlite3.connect(path)) as c:
        c.executescript("""
        CREATE TABLE sellers (id INTEGER PRIMARY KEY, slug TEXT, contact_email TEXT,
          payout_address TEXT, account_id INTEGER);
        CREATE TABLE services (id INTEGER PRIMARY KEY, seller_id INTEGER, slug TEXT,
          upstream_base_url TEXT, created_at INTEGER, status TEXT);
        """)
        for i, (status, account, created) in enumerate(rows, 1):
            c.execute("INSERT INTO sellers VALUES (?,?,?,?,?)",
                      (i, 'secret-slug-%d' % i, 'owner%d@private.example' % i,
                       '0x' + '9' * 40, account))
            c.execute("INSERT INTO services VALUES (?,?,?,?,?,?)",
                      (i, i, 'secret-slug-%d' % i, 'https://private.example/%d' % i,
                       created, status))
        c.commit()
    return path


class HubPendingNoticeTests(unittest.TestCase):
    def test_no_pending_means_no_row(self):
        path = make_hub([('rejected', None, 10), ('live', 1, 20)])
        self.assertEqual(hub.pending_row(path), [])
        os.unlink(path)

    def test_pending_row_counts_without_identifying_details(self):
        path = make_hub([('pending', None, 100), ('pending', 7, 300), ('rejected', 2, 50)])
        [row] = hub.pending_row(path)
        os.unlink(path)
        self.assertEqual((row['category'], row['severity'], row['status']),
                         ('hub', 'warning', 'active'))
        self.assertEqual(row['data'], {'pending': 2, 'signed_in': 1})
        self.assertEqual((row['first_seen'], row['last_seen'], row['occurrences']), (100, 300, 2))
        text = row['title'] + row['summary']
        for leak in ('secret-slug', 'private.example', '0x999', '@'):
            self.assertNotIn(leak, text)
        self.assertRegex(row['id'], r'^[a-f0-9]{64}$')

    def test_unreadable_hub_is_an_error_not_silence(self):
        [row] = hub.pending_row('/nonexistent/hub.db')
        self.assertEqual((row['severity'], row['status']), ('error', 'active'))

    def test_merge_puts_row_with_active_and_updates_counts(self):
        feed = {'active_count': 1, 'truncated_count': 0, 'notices': [
            {'id': 'a' * 64, 'status': 'active', 'last_seen': 50},
            {'id': 'b' * 64, 'status': 'recorded', 'last_seen': 900}]}
        path = make_hub([('pending', None, 10), ('pending', None, 60)])
        merged = hub.merge(feed, hub.pending_row(path))
        os.unlink(path)
        self.assertEqual([r['status'] for r in merged['notices']], ['active', 'active', 'recorded'])
        self.assertEqual(merged['notices'][0]['category'], 'hub')
        self.assertEqual(merged['active_count'], 2)

    def test_merge_without_rows_is_identity(self):
        feed = {'active_count': 0, 'truncated_count': 0, 'notices': []}
        self.assertIs(hub.merge(feed, []), feed)

    def test_row_satisfies_notice_validator_constraints(self):
        path = make_hub([('pending', None, 10)])
        [row] = hub.pending_row(path)
        os.unlink(path)
        self.assertTrue(re.fullmatch(r'[a-z_]{1,40}', row['category']))
        self.assertTrue(all(re.fullmatch(r'[a-z_]{1,40}', k) and type(v) is int
                            for k, v in row['data'].items()))
        self.assertLessEqual(row['first_seen'], row['last_seen'])


if __name__ == '__main__':
    unittest.main()
