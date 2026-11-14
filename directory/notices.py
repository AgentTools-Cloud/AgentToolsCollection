"""Private, bounded internal notices. No SMTP or public application endpoint.

Only typed summaries and privacy-gated listing references are exported. Request
payloads, contact/client data, credentials and raw crawler errors are excluded.
"""

from collections import Counter
from contextlib import closing
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlsplit

from . import db

log = logging.getLogger(__name__)
SITE = 'agent-tools.cloud'
RETENTION_DAYS = 30
MAX_HISTORY = 1000
SEVERITIES = {'info', 'warning', 'error'}
KINDS = {'x402': 'services', 'mcp': 'mcp_servers', 'a2a': 'a2a_agents'}
# Removed sources keep their history in crawl_runs but are no longer crawled.
RETIRED_CRAWL_SOURCES = {'agenstry:crawl', 'mcp:mcp-catalog', 'mcp:pulsemcp', 'a2a:wellknown'}
# A rejection is only cleared when the same identity later ingests cleanly, so
# rows the upstream deleted, and every superseded content version, would stay
# "unresolved" for good. Rejections need no action: count them as active only
# while crawls keep re-observing them (daily crawls; one missed run tolerated).
# Holds await review and never expire. Audit rows are left untouched.
REJECTED_ACTIVE_DAYS = 3
# Routine successes keep one row per Beijing day (occurrences = count), so a bulk
# submitter cannot push every other event out of the MAX_HISTORY window.
ROUTINE_DAILY = {
    'x402_verified': '事件结果：verified；按北京日汇总，观察次数即当日验证通过的提交数。',
    'mail_accepted': 'SMTP 已接收；对方邮箱是否送达尚未验证。按北京日汇总，观察次数即当日已接收封数。',
}
MAIL_PRIVACY_NOTE = ' 不抄送/密送自己，内部只记录发送结果；不复制收件地址或邮件正文。'


def routine_day_key(name, now=None):
    day = time.strftime('%Y-%m-%d', time.gmtime(int(time.time() if now is None else now) + 8 * 3600))
    return f'daily:{name}:{day}'


def safe_text(value, limit=240):
    text = str(value or '').replace('\r', ' ').replace('\n', ' ')
    # Do not retain even the authority of a URL: it can identify a deleted
    # service, and paths/queries commonly carry user keys.
    text = re.sub(r'https?://[^\s<>"\']+', '[URL omitted]', text, flags=re.I)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[email omitted]', text)
    text = re.sub(r'\b(?:rc-|rcsk_|sk-|atc_live_|wk_live_|gh[pousr]_)[A-Za-z0-9_-]{16,}\b',
                  '[credential omitted]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[credential omitted]', text)
    text = re.sub(r'(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*[:=]\s*\S+',
                  '[credential omitted]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP omitted]', text)
    text = ''.join(char for char in text if ord(char) >= 32)
    return text[:limit]


def store(category, severity, title, summary, *, event_key=None, listing_kind=None,
          listing_id=None, submission_id=None, verdict=None, data=None):
    """Durable local acceptance is the notification acknowledgement, not mail."""
    if severity not in SEVERITIES:
        raise ValueError('Invalid notice severity')
    if not re.fullmatch(r'[a-z_]{1,40}', category):
        raise ValueError('Invalid notice category')
    now = int(time.time())
    title, summary = safe_text(title), safe_text(summary, 600)
    typed = {key: value for key, value in (data or {}).items()
             if re.fullmatch(r'[a-z_]{1,40}', key)
             and isinstance(value, (int, bool)) and 0 <= value <= 10 ** 9}
    payload = json.dumps(typed, sort_keys=True, separators=(',', ':'))
    identity = event_key or json.dumps([category, title, summary, listing_kind, listing_id,
                                       submission_id, verdict, payload], ensure_ascii=False)
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()

    def write():
        with db.writer(immediate=True) as conn:
            if listing_kind in KINDS and listing_id is not None:
                target = conn.execute(f'SELECT * FROM {KINDS[listing_kind]} WHERE id=?', (listing_id,)).fetchone()
                if target is None:
                    return False
                db.assert_listing_not_retired(conn, listing_kind, dict(target))
            if submission_id is not None:
                submitted = conn.execute('SELECT payload FROM submissions WHERE id=?', (submission_id,)).fetchone()
                if submitted is None:
                    return False
                db.assert_listing_not_retired(conn, 'x402', json.loads(submitted[0]))
            conn.execute(
                'INSERT INTO internal_notices '
                '(event_key,category,severity,title,summary,listing_kind,listing_id,submission_id,'
                'verdict,data_json,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                # Concurrent writers can commit out of order; never move last_seen backwards.
                'ON CONFLICT(event_key) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen),'
                'occurrences=occurrences+1,'
                # A disk report is a rolling status: a later run the same day
                # replaces its numbers and severity instead of only bumping the count.
                "severity=CASE WHEN category='disk' AND excluded.last_seen>=last_seen "
                'THEN excluded.severity ELSE severity END,'
                "summary=CASE WHEN category='disk' AND excluded.last_seen>=last_seen "
                'THEN excluded.summary ELSE summary END,'
                "data_json=CASE WHEN category='disk' AND excluded.last_seen>=last_seen "
                'THEN excluded.data_json ELSE data_json END',
                (key, category, severity, title, summary, listing_kind, listing_id, submission_id,
                 verdict, payload, now, now))
            conn.execute('DELETE FROM internal_notices WHERE last_seen<?',
                         (now - RETENTION_DAYS * 86400,))
            conn.execute('DELETE FROM internal_notices WHERE id NOT IN '
                         '(SELECT id FROM internal_notices ORDER BY last_seen DESC,id DESC LIMIT ?)',
                         (MAX_HISTORY,))
        return True

    try:
        return bool(db.with_retry(write))
    except db.RetiredListingError:
        return False
    except Exception as exc:
        log.warning('internal notice storage failed: %s', type(exc).__name__)
        return False  # Never fall back to email on a storage failure.


def publish_admin(kind, title, verdict, fields):
    mapping = {'x402 submission': ('x402', 'x402 提交验证'),
               'MCP submission': ('mcp', 'MCP 提交'),
               'A2A submission': ('a2a', 'A2A 提交')}
    listing_kind, label = mapping.get(kind, (None, '内部通知'))
    # The x402 call currently precedes admission: accurately say verified,
    # never claim that a tentative verifier verdict was committed as listed.
    state = 'verified' if kind == 'x402 submission' and verdict == 'listed' else verdict
    if state not in ('verified', 'listed', 'updated', 'pending', 'uncertain', 'rejected', 'error'):
        state = 'recorded'
    if listing_kind == 'x402' and state == 'verified':
        return store('submission', 'info', label, ROUTINE_DAILY['x402_verified'],
                     event_key=routine_day_key('x402_verified'), listing_kind='x402', verdict=state)
    values = dict(fields)
    submission_id = None
    match = re.fullmatch(r'#?(\d{1,12})', str(values.get('Submission') or '').strip())
    if match:
        submission_id = int(match[1])
    listing_id = None
    view = str(values.get('View') or '')
    parsed = urlsplit(view)
    prefixes = {'mcp': '/mcp/servers/', 'a2a': '/a2a/agents/', 'x402': '/services/'}
    if listing_kind and parsed.netloc == SITE and not parsed.query and not parsed.fragment:
        prefix = prefixes[listing_kind]
        if parsed.path.startswith(prefix):
            slug = parsed.path[len(prefix):]
            with closing(db.connect(read_only=True)) as conn:
                row = conn.execute(f'SELECT id FROM {KINDS[listing_kind]} WHERE slug=?', (slug,)).fetchone()
                listing_id = int(row[0]) if row else None
    health = str(values.get('Health') or '')
    summary = f'事件结果：{state}' + (f'；健康状态：{health}' if health in ('ok', 'down', 'degraded', 'unknown') else '')
    reference = f'提交 #{submission_id}' if submission_id is not None else (
        f'条目 #{listing_id}' if listing_id is not None else '未关联条目')
    # A service name can contain a domain, client detail or credential. Fixed
    # labels plus internal IDs are sufficient for this operational channel.
    return store('submission', 'warning' if state in ('rejected', 'uncertain', 'error') else 'info',
                 label + ' · ' + reference, summary,
                 listing_kind=listing_kind, listing_id=listing_id, submission_id=submission_id,
                 verdict=state)


def publish_watchdog(title, problems, *, advisory_only=False):
    """Store a typed summary; never persist raw journal exceptions or bodies."""
    allowed = {'后台任务异常', '摄入数据质量提醒', '来源端点失配警告'}
    title = title if title in allowed else '后台巡检通知'
    pair_count = sum(problem.startswith('来源端点失配警告:') for problem in problems)
    quality_count = sum(problem.startswith('新增数据质量拒绝组:') for problem in problems)
    total = len(problems)
    data = {'problem_count': total, 'source_warning_count': pair_count,
            'quality_group_count': quality_count,
            'failure_count': total - pair_count - quality_count}
    # Classify known producer shapes into fixed labels, never copy a raw
    # source name, unit result, exception or request into the remote history.
    labels = []
    patterns = (
        ('stage_abort', r'^(?:services|mcp|a2a) health 整轮中断 ', '健康检查阶段中断'),
        ('stage_missing', r'^(?:services|mcp|a2a) health 24h 内只完成 ', '健康检查轮次不足'),
        ('unit_failure', r'^agent-tools-(?:crawl|health|reverify|review|onchain|quarantine|backup)\.service 上次结束状态 = ', '后台任务退出异常'),
        ('backup_unavailable', r'^加密异地备份最近完成已超过 30h', '异地备份过期或记录无效'),
        ('table_stale', r'^(?:mcp_servers|services|a2a_agents) (?:没有任何 updated_at|最新写入已是 )', '主表写入过期'),
        ('source_failure', r'^新增失败爬源:', '新增抓取失败来源'),
        ('source_partial', r'^\S+ 连续 \d+ 轮已完成运行均为 partial$', '来源持续部分失败'),
        ('source_stale', r'^\S+ (?:最近完成|首次出现后尚无已完成轮次)已过 ', '子来源完成记录过期'),
        ('write_failure', r'^\S+ (?:跑完但一条未写入，报错 |报错 \d+ 条多于写入 )', '抓取写入异常'),
        ('binding_hold', r'^新增摄入待审原因组:', '新增摄入绑定待审'),
    )
    for key, pattern, label in patterns:
        count = sum(bool(re.search(pattern, problem)) for problem in problems)
        if count:
            data[key] = count
            labels.append(f'{label} {count} 项')
    summary = (f'巡检记录 {total} 项：来源端点失配 {pair_count}，数据质量组 {quality_count}，'
               f'任务/绑定异常 {data["failure_count"]}。'
               + ('分类：' + '；'.join(labels) + '。' if labels else '')
               + '这是巡检时的历史观察，当前摄入与来源状态另列；原始日志仅留来源主机。')
    return store('watchdog', 'warning' if advisory_only else 'error', title, summary, data=data,
                 event_key=json.dumps(['watchdog', title, data, int(time.time()) // 86400], sort_keys=True))


def publish_mail_result(purpose, state, event_id):
    """Anonymous fixed-label receipt, not an email-body archive or delivery proof."""
    labels = {'approval': '上架确认邮件', 'rejection': '提交拒绝邮件', 'external': '外部业务邮件',
              'claim_invitation': '认领邀请邮件'}
    states = {
        'accepted': ('info', 'SMTP 已接收；对方邮箱是否送达尚未验证。'),
        'failed_or_unknown': ('warning', '发送失败或接收状态未知；未自动重发。'),
        'skipped': ('warning', 'SMTP 配置不可用；本次未发送。'),
    }
    if purpose not in labels or state not in states:
        raise ValueError('Invalid external mail result')
    severity, summary = states[state]
    event_key = 'mail-result:' + event_id
    if state == 'accepted' and purpose != 'external':
        summary, event_key = ROUTINE_DAILY['mail_accepted'], routine_day_key('mail_accepted_' + purpose)
    return store('outbound_mail', severity, labels[purpose], summary + MAIL_PRIVACY_NOTE,
                 event_key=event_key,
                 data={'smtp_accepted': state == 'accepted', 'not_sent': state == 'skipped'})


def record_lifecycle(conn, action, request_id):
    labels = {'retire': ('Listing withdrawn', 'A verified operator withdrew one listing.'),
              'restore': ('Listing restored', 'An operator restored one listing after current control and service verification.'),
              'merge': ('Listings merged', 'Both owned hosts were rechecked; one listing was retired in favor of its verified primary. No reputation was transferred.'),
              'migration': ('Listing migration requested', 'One migration request awaits staff review. No listings or reputation were merged.')}
    title, summary = labels[action]
    now = int(time.time())
    key = hashlib.sha256(('owner-lifecycle:' + request_id).encode()).hexdigest()
    conn.execute('INSERT OR IGNORE INTO internal_notices(event_key,category,severity,title,summary,'
                 'data_json,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)',
                 (key, 'owner_lifecycle', 'info', title, summary, '{"operation_count":1}', now, now))
    conn.execute('DELETE FROM internal_notices WHERE last_seen<?', (now - RETENTION_DAYS * 86400,))
    conn.execute('DELETE FROM internal_notices WHERE id NOT IN '
                 '(SELECT id FROM internal_notices ORDER BY last_seen DESC,id DESC LIMIT ?)', (MAX_HISTORY,))


DISK_REPORT_ACTIVE_DAYS = 8


def publish_disk_report(used_pct, free_gb, candidate_gb, candidate_items, tmp_copies,
                        backup_age_hours, *, action_needed):
    """Daily host disk report (ops/disk_cleanup_reminder.py). Counts only.

    The reminder never deletes anything; this row keeps the latest report
    visible on the Notice page until the listed items are dealt with.
    """
    vals = {'used_pct': int(used_pct), 'free_gb': int(free_gb),
            'candidate_gb': int(round(candidate_gb)), 'candidate_items': int(candidate_items),
            'tmp_copies': int(tmp_copies), 'action_needed': bool(action_needed)}
    if backup_age_hours is not None:
        vals['backup_age_hours'] = int(backup_age_hours)
    warn = vals['used_pct'] >= 80 or vals['tmp_copies'] > 0 or (
        backup_age_hours is not None and backup_age_hours > 30)
    backup = ('读不到' if backup_age_hours is None
              else f'{int(backup_age_hours)} 小时前' + ('（超过 30 小时）' if backup_age_hours > 30 else ''))
    summary = (f'磁盘已用 {vals["used_pct"]}%，剩余 {vals["free_gb"]} GB；'
               f'可清理候选 {vals["candidate_items"]} 项约 {vals["candidate_gb"]} GB'
               f'（其中测试遗留库副本 {vals["tmp_copies"]} 份）；异地日备最近成功 {backup}。'
               '清单见 latex-tools 的 /var/lib/agent-tools-disk-reminder/latest.txt；'
               '只提醒不自动删除，旧快照先归档 NFS 并核对哈希再删。')
    return store('disk', 'warning' if warn else 'info', 'latex-tools 磁盘清理提醒', summary,
                 event_key=routine_day_key('disk-report'), data=vals)


def capture_legacy_admin(subject, text):
    # Defensive interception of old direct _send(ADMIN_EMAIL, ...). Do not
    # carry arbitrary email bodies into the private feed or back to SMTP.
    return store('system', 'info', '内部通知（旧发送入口）',
                 '内部邮件发送已停用；本事件已记录到 notice。原始邮件内容不复制。')


def _history(conn, now):
    result = []
    # Disk reports are a rolling status, not events: only the latest one is
    # shown, from _active_rows.
    for row in conn.execute("SELECT * FROM internal_notices WHERE last_seen>=? AND category<>'disk' "
                            'ORDER BY last_seen DESC,id DESC LIMIT ?',
                            (now - RETENTION_DAYS * 86400, MAX_HISTORY)):
        target = None
        try:
            if row['listing_kind'] in KINDS and row['listing_id'] is not None:
                target = conn.execute(f'SELECT * FROM {KINDS[row["listing_kind"]]} WHERE id=?',
                                      (row['listing_id'],)).fetchone()
                if target is None:
                    continue
                db.assert_listing_not_retired(conn, row['listing_kind'], dict(target))
            if row['submission_id'] is not None:
                submitted = conn.execute('SELECT * FROM submissions WHERE id=?', (row['submission_id'],)).fetchone()
                if submitted is None:
                    continue
                db.assert_listing_not_retired(conn, 'x402', json.loads(submitted['payload']))
        except db.RetiredListingError:
            continue
        item = {'id': row['event_key'], 'category': row['category'], 'severity': row['severity'],
                'status': 'recorded', 'title': safe_text(row['title']), 'summary': safe_text(row['summary'], 600),
                'first_seen': row['first_seen'], 'last_seen': row['last_seen'], 'occurrences': row['occurrences'],
                'data': json.loads(row['data_json'] or '{}')}
        # Listing/submission IDs stay on this host. A remote static snapshot
        # cannot synchronously erase those references following a local purge.
        if row['category'] == 'submission':
            item['title'] = {'x402': 'x402 提交验证', 'mcp': 'MCP 提交',
                             'a2a': 'A2A 提交'}.get(row['listing_kind'], '提交事件')
        if row['submission_id'] is not None:
            state = submitted['status']
            if state not in ('pending', 'approved', 'rejected', 'uncertain', 'error'):
                state = 'recorded'
            item['summary'] += f' 原提交当前状态：{state}。'
        result.append(item)
    return result


def _reviewed_binding_partial(conn, run, now):
    scopes = {'mcp:wellknown': ('mcp', 'source binding held: '),
              'a2a:crawl': ('a2a', 'wellknown: source binding held: ')}
    if run['status'] != 'partial' or run['source'] not in scopes or not conn.in_transaction:
        return False
    started, finished = run['started_at'], run['finished_at']
    if type(started) is not int or type(finished) is not int or not 0 < started <= finished <= now:
        return False
    kind, prefix = scopes[run['source']]
    reasons = {'wellknown_binding_target_changed', 'wellknown_new_endpoint_requires_review',
               'wellknown_synthetic_card_collision', 'wellknown_owner_endpoint_changed',
               'wellknown_endpoint_mismatch'}
    errors = run['errors']
    if not isinstance(errors, str) or not errors:
        return False
    expected = Counter()
    for line in errors.splitlines():
        if not line.startswith(prefix) or line[len(prefix):] not in reasons:
            return False
        expected[line[len(prefix):]] += 1
    if conn.execute('SELECT 1 FROM ingestion_issues WHERE kind=? AND source=? '
                    'AND disposition=? AND resolved_at IS NULL LIMIT 1',
                    (kind, 'wellknown', 'hold')).fetchone() is not None:
        return False
    observed = list(conn.execute(
        'SELECT reason,COUNT(*) versions,'
        'SUM(CASE WHEN resolved_at>? AND resolved_at<=? THEN 1 ELSE 0 END) reviewed '
        'FROM ingestion_issues WHERE kind=? AND source=? AND disposition=? '
        'AND last_seen BETWEEN ? AND ? GROUP BY reason',
        (finished, now, kind, 'wellknown', 'hold', started, finished)))
    if Counter({row['reason']: row['versions'] for row in observed}) != expected:
        return False
    return all(row['versions'] == row['reviewed'] for row in observed)


def _active_rows(conn, now):
    result = []
    for row in conn.execute(
            'SELECT kind,source,disposition,reason,COUNT(*) versions,COUNT(DISTINCT identity_hash) identities,'
            'MIN(first_seen) first_seen,MAX(last_seen) last_seen,SUM(occurrences) occurrences '
            "FROM ingestion_issues WHERE resolved_at IS NULL AND (disposition='hold' OR last_seen>=?) "
            'GROUP BY kind,source,disposition,reason', (now - REJECTED_ACTIVE_DAYS * 86400,)):
        held = row['disposition'] == 'hold'
        key = json.dumps(['ingestion', row['kind'], row['source'], row['disposition'], row['reason']])
        result.append({'id': hashlib.sha256(key.encode()).hexdigest(), 'category': 'ingestion',
                       'severity': 'error' if held else 'warning', 'status': 'active',
                       'title': f'{row["kind"]}:{safe_text(row["source"], 50)} · ' + ('摄入待审' if held else '数据质量拒绝'),
                       'summary': safe_text(row['reason'], 100) + ('；' if held else f'；近 {REJECTED_ACTIVE_DAYS} 天观测到')
                                  + f'来源身份 {row["identities"]}，内容版本 {row["versions"]}。',
                       'first_seen': row['first_seen'], 'last_seen': row['last_seen'], 'occurrences': row['occurrences'],
                       'data': {'source_identities': row['identities'], 'content_versions': row['versions']}})
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_endpoint_warnings'").fetchone():
        grouped = {}
        for row in db.visible_source_endpoint_warnings(conn):
            current_context = row.get('current_context', True)
            status = 'active' if row['resolved_at'] is None else 'resolved'
            group = (row['kind'], row['change_type'], status, current_context)
            if group not in grouped:
                grouped[group] = {'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
                                  'occurrences': 0, 'pairs': 0}
            counts = grouped[group]
            counts['first_seen'] = min(counts['first_seen'], row['first_seen'])
            counts['last_seen'] = max(counts['last_seen'], row['last_seen'])
            counts['occurrences'] += row['occurrences']
            counts['pairs'] += 1
        for (kind, change, status, current_context), counts in grouped.items():
            key = json.dumps(['source', kind, change, status, current_context])
            result.append({'id': hashlib.sha256(key.encode()).hexdigest(),
                           'category': 'source', 'severity': 'warning', 'status': status,
                           'title': f'{kind} · 来源端点失配',
                           'summary': ('Wellknown 旧关联保留历史；' if current_context else
                                       '历史端点差异记录；当前主条目后续另有变化；')
                                      + f'差异类型 {safe_text(change, 60)}；关联对 {counts["pairs"]}。'
                                      '不代表发布者主动迁移，也不证明旧地址已下线。'
                                      '条目编号与端点指纹仅保留在来源主机。',
                           'first_seen': counts['first_seen'], 'last_seen': counts['last_seen'],
                           'occurrences': counts['occurrences'], 'data': {'warning_pairs': counts['pairs']}})
    disk = conn.execute("SELECT * FROM internal_notices WHERE category='disk' "
                        'ORDER BY last_seen DESC,id DESC LIMIT 1').fetchone()
    if disk is not None and disk['last_seen'] >= now - DISK_REPORT_ACTIVE_DAYS * 86400:
        data = json.loads(disk['data_json'] or '{}')
        result.append({'id': disk['event_key'], 'category': 'disk', 'severity': disk['severity'],
                       'status': 'active' if data.get('action_needed') else 'resolved',
                       'title': safe_text(disk['title']), 'summary': safe_text(disk['summary'], 600),
                       'first_seen': disk['first_seen'], 'last_seen': disk['last_seen'],
                       'occurrences': disk['occurrences'], 'data': data})
    latest = conn.execute(
        "SELECT * FROM crawl_runs WHERE status<>'running' ORDER BY COALESCE(finished_at,started_at) DESC,id DESC")
    seen = set()
    for row in latest:
        if row['source'] in seen or row['source'] in RETIRED_CRAWL_SOURCES:
            continue
        seen.add(row['source'])
        if row['status'] not in ('error', 'partial'):
            continue
        reviewed = _reviewed_binding_partial(conn, row, now)
        key = json.dumps(['crawl', row['source']])
        result.append({'id': hashlib.sha256(key.encode()).hexdigest(), 'category': 'crawl',
                       'severity': 'error' if row['status'] == 'error' else 'warning',
                       'status': 'resolved' if reviewed else 'active',
                       'title': '抓取来源 · ' + safe_text(row['source'], 60),
                       'summary': f'最近完成状态 {row["status"]}；新增 {row["added"]}，返回更新 {row["updated"]}，'
                                  f'分类错误 {len((row["errors"] or "").splitlines())} 条。原始异常/请求不导出。'
                                  + (' 该轮绑定待审已逐版处置，仅保留历史；下轮仍重新判断。' if reviewed else ''),
                       'first_seen': row['started_at'], 'last_seen': row['finished_at'] or row['started_at'],
                       'occurrences': 1, 'data': {'added': row['added'], 'updated': row['updated']}})
    return result


def export_feed(conn, now=None):
    now = int(time.time()) if now is None else int(now)
    active = _active_rows(conn, now)
    history = _history(conn, now)
    active.sort(key=lambda item: (item['last_seen'], item['id']), reverse=True)
    # Current failures must not disappear behind a busy submission history.
    notices = active + history
    return {'version': 1, 'site': SITE, 'generated_at': now, 'retention_days': RETENTION_DAYS,
            'history_limit': MAX_HISTORY, 'active_count': len(active),
            'truncated_count': max(0, len(notices) - 1200), 'notices': notices[:1200]}