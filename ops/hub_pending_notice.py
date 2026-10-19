"""Active Notice row for hub.agent-tools.cloud submissions awaiting review.

Counts only: slugs, upstream URLs, wallets and contacts never leave the hub DB.
"""

from contextlib import closing
import hashlib
import json
import sqlite3

HUB_DB = '/var/lib/agent-tools-hub/hub.db'


def pending_row(path=HUB_DB):
    try:
        with closing(sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)) as conn:
            count, first, last, signed_in = conn.execute(
                "SELECT COUNT(*), MIN(s.created_at), MAX(s.created_at), "
                "SUM(se.account_id IS NOT NULL) FROM services s "
                "JOIN sellers se ON se.id=s.seller_id WHERE s.status='pending'").fetchone()
    except sqlite3.Error:
        return [_unreadable()]
    if not count:
        return []
    signed_in = int(signed_in or 0)
    return [{
        'id': hashlib.sha256(json.dumps(['hub', 'pending_review']).encode()).hexdigest(),
        'category': 'hub', 'severity': 'warning', 'status': 'active',
        'title': 'Hub · 待审核提交',
        'summary': f'hub.agent-tools.cloud 有 {count} 个托管服务待审核（已登录账户提交 {signed_in} 个）。'
                   '服务名、上游地址、钱包与联系方式不导出。',
        'first_seen': int(first), 'last_seen': int(last), 'occurrences': int(count),
        'data': {'pending': int(count), 'signed_in': signed_in}}]


def _unreadable():
    import time
    now = int(time.time())
    return {
        'id': hashlib.sha256(json.dumps(['hub', 'pending_unreadable']).encode()).hexdigest(),
        'category': 'hub', 'severity': 'error', 'status': 'active',
        'title': 'Hub · 待审核状态无法读取',
        'summary': 'Hub 数据库读取失败；无法判断是否有待审核提交。',
        'first_seen': now, 'last_seen': now, 'occurrences': 1, 'data': {}}


def merge(feed, rows):
    if not rows:
        return feed
    active = rows + [r for r in feed['notices'] if r['status'] == 'active']
    history = [r for r in feed['notices'] if r['status'] != 'active']
    active.sort(key=lambda item: (item['last_seen'], item['id']), reverse=True)
    notices = active + history
    feed['active_count'] = len(active)
    feed['truncated_count'] = feed.get('truncated_count', 0) + max(0, len(notices) - 1200)
    feed['notices'] = notices[:1200]
    return feed
