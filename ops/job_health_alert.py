#!/usr/bin/env python3
"""Daily watchdog for the agent-tools background jobs.

The jobs swallow per-stage exceptions into WARNING logs and still return 0, so
systemd reports success while a whole pass silently did nothing (see doc §4.25:
the MCP pass aborted on 10 of 64 runs for a week without any visible signal).
This reads the latest health-service invocation and distinguishes stage failures
and binding holds from isolated data-quality warnings. It stays silent when no
new problem group is observed.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/mcpserver")

from directory import db, mailer  # noqa: E402

STATE = Path("/var/lib/agent-tools-watchdog/state.json")
BACKUP_SUCCESS = Path("/var/backups/agent-tools/last-success.json")
WINDOW = "24 hours ago"
WINDOW_SECONDS = 24 * 3600
HEALTH_UNIT = "agent-tools-health.service"
JOURNAL_GREP = (
    r"directory\.jobs (?:(?:mcp|a2a) health failed:|"
    r"(?:health|mcp health|a2a health): ok=)"
)
# Every table is written by at least one job per 6h; twice that is clearly stuck.
# health writes each table once a day now, and the a2a stage finishes early
# in a round that runs over three hours, so a healthy table can legitimately
# be a full day old. 30h flags a round that never ran without firing on the
# normal daily rhythm.
STALE_HOURS = 30
FRESH_TABLES = ("mcp_servers", "services", "a2a_agents")
# Child runs must not be hidden by successful writes from their parents.
# Only start enforcing freshness after the child has appeared in crawl_runs.
FRESH_SOURCES = {"a2a:agenstry": ("a2a:crawl", "agenstry:crawl")}

# A stage that aborts logs "<stage> failed: <exc>" and is otherwise invisible.
ABORT_RE = re.compile(r"directory\.jobs ((?:mcp|a2a) health) failed: (.+)")
# Completion lines, one per finished pass.
DONE_RE = {
    "services health": re.compile(r"directory\.jobs health: ok="),
    "mcp health": re.compile(r"directory\.jobs mcp health: ok="),
    "a2a health": re.compile(r"directory\.jobs a2a health: ok="),
}
MIN_PASSES = 1
# health and crawl both moved to a single daily round on 2026-09-10, so one
# pass per stage is now a full day and anything less means a stage aborted.
PARTIAL_STREAK = 4


def journal() -> list[str]:
    invocation = subprocess.run(
        ["systemctl", "show", HEALTH_UNIT, "-p", "InvocationID", "--value"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    if not invocation:
        return []
    out = subprocess.run(
        ["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}",
         "--since", WINDOW, "--no-pager", "-o", "cat",
         "--grep", JOURNAL_GREP],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout.splitlines()


def unit_results() -> list[tuple[str, str]]:
    """Units whose last run did not end cleanly."""
    bad = []
    for unit in ("crawl", "health", "reverify", "review", "onchain", "quarantine", "backup"):
        name = f"agent-tools-{unit}.service"
        r = subprocess.run(
            ["systemctl", "show", name, "-p", "Result", "--value"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if r and r != "success":
            bad.append((unit, r))
    return bad


def backup_is_fresh() -> bool:
    """Read the durable offsite receipt, not unloadable systemd timestamps."""
    try:
        if BACKUP_SUCCESS.resolve() != BACKUP_SUCCESS.absolute():
            return False
        fd = os.open(BACKUP_SUCCESS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                return False
            raw = stream.read(4097)
            if len(raw) > 4096:
                return False
            state = json.loads(raw)
        if (not isinstance(state, dict) or type(state.get("version")) is not int
                or state["version"] != 1 or state.get("offsite") is not True):
            return False
        epoch, archive = state.get("finished_at"), state.get("archive")
        return (type(epoch) in (int, float) and epoch > 0
                and 0 <= time.time() - epoch <= 30 * 3600
                and isinstance(archive, str)
                and re.fullmatch(r"agent-tools-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}\.tar\.gz\.age", archive) is not None)
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


def db_checks() -> tuple[list[str], list[str]]:
    """Two failure shapes the journal checks above cannot see.

    A crawl whose every item raises still finishes, exits 0 and leaves the
    failing-source set untouched, so nothing else here notices — that is how the
    MCP table went six days without a single write (doc §4.41).
    """
    problems: list[str] = []
    detail: list[str] = []
    now = time.time()
    latest = latest_completed_runs()
    with closing(db.connect(read_only=True)) as c:
        # status=error means the fetch itself failed; the failing-source
        # baseline above already owns those, and repeating them here would put
        # the chronically broken sources into a daily mail.
        runs = c.execute(
            "SELECT source, added, updated, errors FROM crawl_runs "
            "WHERE COALESCE(finished_at, started_at) BETWEEN ? AND ? "
            "AND status NOT IN ('error', 'running') "
            "ORDER BY COALESCE(finished_at, started_at), id",
            (now - WINDOW_SECONDS, now)).fetchall()
        for source, added, updated, errors in runs:
            # These children alert on their very first partial via stuck_sources;
            # repeating each bad run here would bypass the known-failure baseline.
            if source in FRESH_SOURCES:
                continue
            n_err = len(errors.splitlines()) if errors else 0
            wrote = (added or 0) + (updated or 0)
            if not n_err:
                continue
            if wrote == 0:
                problems.append(f"{source} 跑完但一条未写入，报错 {n_err} 条")
            elif n_err > wrote:
                problems.append(f"{source} 报错 {n_err} 条多于写入 {wrote} 条")

        for table in FRESH_TABLES:
            newest = c.execute(f"SELECT MAX(updated_at) FROM {table}").fetchone()[0]
            if not newest:
                problems.append(f"{table} 没有任何 updated_at")
                continue
            hours = (now - newest) / 3600
            if hours > STALE_HOURS:
                problems.append(
                    f"{table} 最新写入已是 {hours:.1f} 小时前（阈值 {STALE_HOURS}）")
            else:
                detail.append(f"  {table} 最新写入 {hours:.1f} 小时前")

        for source in FRESH_SOURCES:
            first_seen = c.execute(
                "SELECT MIN(started_at) FROM crawl_runs WHERE source=? AND started_at<=?",
                (source, now)).fetchone()[0]
            if first_seen is None:
                continue
            completed = latest.get(source)
            ts = completed[2] if completed else first_seen
            hours = (now - ts) / 3600
            label = "最近完成" if completed else "首次出现后尚无已完成轮次"
            if hours > STALE_HOURS:
                problems.append(f"{source} {label}已过 {hours:.1f} 小时（阈值 {STALE_HOURS}）")
            else:
                detail.append(f"  {source} {label} {hours:.1f} 小时")
    return problems, detail


def latest_completed_runs() -> dict[str, tuple[str, str | None, float]]:
    """All-time completed status, including sources that aged out of the window.

    Missing/running rows do not prove recovery. IDs break same-second ties;
    future timestamps cannot hide the latest observable completion.
    """
    with closing(db.connect(read_only=True)) as c:
        rows = c.execute(
            "SELECT source, status, errors, COALESCE(finished_at, started_at) ts "
            "FROM crawl_runs WHERE status != 'running' "
            "AND COALESCE(finished_at, started_at) <= ? ORDER BY ts, id",
            (time.time(),)).fetchall()
    return {source: (status, errors, ts) for source, status, errors, ts in rows}


def failing_sources() -> list[str]:
    """Sources whose most recent run in the window ended in status=error.

    This was read out of the journal, but the crawler logs
    "<source>: fetch failed: ..." while the pattern wanted
    "crawl <source> failed", so the set had been empty for as long as it
    existed -- and db_checks skips status=error on the grounds that this
    owns it. x402scan failed every round for 48 days without a word.
    """
    now = time.time()
    return sorted(source for source, (status, _, ts) in latest_completed_runs().items()
                  if status == "error" and ts >= now - WINDOW_SECONDS)


def stuck_sources() -> list[tuple[str, int, str]]:
    """Consecutive completed partial runs and the latest run's first error.

    Ordinary sources require PARTIAL_STREAK; monitored children alert on their
    first partial, independently of parent status and total table writes.

    A source that limps every round hides from all three checks above: its
    status is partial rather than error so the failing-source set never moves,
    its writes still outnumber its errors, and the tables stay fresh because
    other sources are writing to them. agenstry read as healthy by all three
    for 21 days (doc §4.49).
    """
    now = time.time()
    with closing(db.connect(read_only=True)) as c:
        rows = c.execute(
            "SELECT source, status, errors, COALESCE(finished_at, started_at) ts "
            "FROM crawl_runs "
            "WHERE status != 'running' "
            "AND COALESCE(finished_at, started_at) BETWEEN ? AND ? "
            "ORDER BY source, ts, id", (now - 14 * 86400, now)).fetchall()

    seqs: dict[str, list] = {}
    for source, status, errors, ts in rows:
        seqs.setdefault(source, []).append((status, errors, ts))

    out: list[tuple[str, int, str]] = []
    for source, seq in seqs.items():
        # A source that stopped running altogether is a different failure and
        # belongs to the staleness check, not here.
        if seq[-1][2] < now - WINDOW_SECONDS:
            continue
        streak = 0
        for status, _, _ in reversed(seq):
            if status != "partial":
                break
            streak += 1
        threshold = 1 if source in FRESH_SOURCES else PARTIAL_STREAK
        if streak >= threshold:
            latest_error = (seq[-1][1] or "").splitlines()
            out.append((source, streak, latest_error[0][:120] if latest_error else ""))
    return sorted(out)


def ingestion_checks(state: dict) -> tuple[list[str], list[str], set[str]]:
    """Summarize active holds without payloads or per-identity mail noise.

    Observe reason groups in watchdog state, never write to the crawl database.
    MIN(first_seen) includes resolved history: another identity/content hash in
    an existing group is not a new reason. Missing groups are not recoveries.
    """
    now = time.time()
    with closing(db.connect(read_only=True)) as c:
        if not c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_issues'"
        ).fetchone():
            return [], [], set()
        groups = c.execute(
            "SELECT kind, source, disposition, reason, "
            "SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) active, "
            "COUNT(DISTINCT CASE WHEN resolved_at IS NULL THEN identity_hash END), "
            "SUM(CASE WHEN resolved_at IS NULL THEN occurrences ELSE 0 END), "
            "MIN(first_seen), MAX(CASE WHEN resolved_at IS NULL THEN last_seen END) "
            "FROM ingestion_issues GROUP BY kind, source, disposition, reason "
            "HAVING active > 0 ORDER BY kind, source, disposition, reason"
        ).fetchall()
        totals = c.execute(
            "SELECT disposition, COUNT(DISTINCT kind || char(0) || source || char(0) || identity_hash), "
            "COUNT(*) FROM ingestion_issues WHERE resolved_at IS NULL GROUP BY disposition"
        ).fetchall()

    problems: list[str] = []
    detail: list[str] = []
    holds: set[str] = set()
    seen = set(state.get("ingestion_reason_groups") or [])
    pending = set(state.get("pending_ingestion_reason_groups") or [])
    active_keys: set[str] = set()
    for kind, source, disposition, reason, versions, count, occurrences, first_seen, last_seen in groups:
        key = json.dumps([kind, source, disposition, reason], ensure_ascii=True)
        active_keys.add(key)
        label = f"{kind}:{source} {disposition}/{reason}"
        stamp = datetime.fromtimestamp(last_seen, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        held = disposition == "hold"
        category = "摄入待审" if held else "数据质量拒绝"
        detail.append(f"  活跃{category}: {label}; 来源身份 {count}, 内容版本 {versions}, "
                      f"累计出现 {occurrences}; 最新出现 {stamp}")
        if key in pending or (key not in seen and now - WINDOW_SECONDS <= first_seen <= now):
            prefix = "新增摄入待审原因组" if held else "新增数据质量拒绝组"
            problems.append(f"{prefix}: {label}（来源身份 {count}，内容版本 {versions}）")
            pending.add(key)
        seen.add(key)
        # Rejected metadata is isolated data quality, not a failed crawl. Only
        # unresolved binding holds prevent an explicit completed ok recovery.
        if not held:
            continue
        qualified = f"{kind}:{source}"
        holds.update((source, qualified))
        if kind == "a2a":
            holds.add("a2a:crawl")
        holds.update(FRESH_SOURCES.get(qualified, ()))
    # Observation is not a delivery acknowledgement. Retry an unaccepted
    # notification even beyond the first-seen window or after the issue clears.
    for key in sorted(pending - active_keys):
        kind, source, disposition, reason = json.loads(key)
        prefix = "新增摄入待审原因组" if disposition == "hold" else "新增数据质量拒绝组"
        problems.append(f"{prefix}: {kind}:{source} {disposition}/{reason}"
                        "（首次提醒待送达；当前无活跃记录）")
    for disposition, count, versions in totals:
        category = "摄入待审" if disposition == "hold" else "数据质量拒绝"
        detail.append(f"  活跃{category}合计（跨原因组去重）: 来源身份 {count}, 内容版本 {versions}")
    if seen:
        state["ingestion_reason_groups"] = sorted(seen)
    if pending:
        state["pending_ingestion_reason_groups"] = sorted(pending)
    return problems, detail, holds


def source_warning_checks(state: dict) -> tuple[list[str], list[str]]:
    """Warn per old/current endpoint pair without blocking normal recovery.

    Keep opaque dedup keys only; deleted/purged records cancel even pending
    delivery. Renderer data is read afresh through privacy/retirement guards.
    """
    with closing(db.connect(read_only=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='source_endpoint_warnings'").fetchone():
            return [], []
        rows = db.visible_source_endpoint_warnings(conn)
    available = {row['warning_key'] for row in rows}
    seen = set(state.get('source_endpoint_warning_seen') or []) & available
    pending = set(state.get('pending_source_endpoint_warnings') or []) & available
    warnings, detail = [], []
    labels = {'authority': '主机/协议', 'path': '路径', 'query': '查询参数',
              'normalized_endpoint': '规范化端点'}
    for row in rows:
        key = row['warning_key']
        if key not in seen or key in pending:
            change = '、'.join(labels.get(part, '端点') for part in row['change_type'].split('+'))
            outcome = ('旧关联标记stale，当前有效主记录保留'
                       if row['resolved_at'] is None and row.get('current_context', True) else
                       '历史失配已解除' if row['resolved_at'] is not None else
                       '已记录历史端点变化，当前条目后续另有变化')
            warnings.append(f"来源端点失配警告: {row['kind']} 条目 #{row['listing_id']} "
                            f"（Wellknown；差异：{change}；{outcome}）")
            detail.append(f"  条目: https://agent-tools.cloud{row['detail_path']}；"
                          f"旧/当前端点指纹 {row['old_endpoint_hash'][:12]} → "
                          f"{row['current_endpoint_hash'][:12]}；累计观察 {row['occurrences']} 次。"
                          "不代表发布者主动迁移或旧地址已下线。")
            pending.add(key)
        seen.add(key)
    if seen or 'source_endpoint_warning_seen' in state:
        state['source_endpoint_warning_seen'] = sorted(seen)
    if pending:
        state['pending_source_endpoint_warnings'] = sorted(pending)
    else:
        state.pop('pending_source_endpoint_warnings', None)
    return warnings, detail


def main() -> int:
    lines = journal()
    problems: list[str] = []
    detail: list[str] = []

    aborts = Counter()
    for line in lines:
        m = ABORT_RE.search(line)
        if m:
            aborts[(m.group(1), m.group(2)[:120])] += 1
    for (stage, exc), n in sorted(aborts.items()):
        problems.append(f"{stage} 整轮中断 {n} 次")
        detail.append(f"  {stage}: {n}x  {exc}")

    counts = {k: sum(bool(rx.search(l)) for l in lines) for k, rx in DONE_RE.items()}
    for stage, n in counts.items():
        if n < MIN_PASSES:
            problems.append(f"{stage} 24h 内只完成 {n} 轮（预期 ≥{MIN_PASSES}）")

    for unit, result in unit_results():
        problems.append(f"agent-tools-{unit}.service 上次结束状态 = {result}")

    # This receipt survives reboot and oneshot unloading. Exact unit Result
    # above still reports a newer failed run even while the receipt is fresh.
    if not backup_is_fresh():
        problems.append("加密异地备份最近完成已超过 30h，或成功记录缺失/无效")

    db_problems, db_detail = db_checks()
    problems += db_problems
    detail += db_detail

    # Known-broken crawl sources fail every run; alerting on them daily would be
    # pure noise. Track the set instead and speak up only when it changes.
    now_failing = failing_sources()
    state = load_state()
    previous_state = state.copy()
    issue_problems, issue_detail, holds = ingestion_checks(state)
    problems += issue_problems
    detail += issue_detail
    source_warnings, source_detail = source_warning_checks(state)
    problems += source_warnings
    detail += source_detail
    latest = latest_completed_runs()

    def recovered(source):
        return source in latest and latest[source][0] == "ok" and source not in holds

    baseline = state.get("failing_sources")
    known_failing = set(baseline or [])
    added = sorted(set(now_failing) - known_failing)
    gone = sorted(s for s in known_failing if recovered(s))
    if added:
        problems.append(f"新增失败爬源: {', '.join(added)}")
    if gone:
        detail.append(f"  已恢复的爬源: {', '.join(gone)}（仅抓取状态：最新完成 ok；数据质量另列）")
    state["failing_sources"] = sorted((known_failing | set(now_failing)) - set(gone))
    if baseline is None:
        state["baseline_set_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elif state["failing_sources"] != baseline:
        state["changed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Same treatment as the failing-source set: a source stuck on partial stays
    # stuck until someone fixes it, so report the set changing rather than its
    # contents, or this becomes a daily mail nobody reads.
    stuck = stuck_sources()
    stuck_names = {s for s, _, _ in stuck}
    stuck_base = set(state.get("stuck_sources") or [])
    for source, streak, latest_error in stuck:
        if source not in stuck_base:
            problems.append(f"{source} 连续 {streak} 轮已完成运行均为 partial")
        if latest_error:
            detail.append(f"  {source} 最新 partial 轮次的首条报错: {latest_error}")
    gone_partial = sorted(s for s in stuck_base if recovered(s))
    if gone_partial:
        detail.append(f"  已恢复的 partial 源: {', '.join(gone_partial)}（仅抓取状态：最新完成 ok；数据质量另列）")
    state["stuck_sources"] = sorted((stuck_base | stuck_names) - set(gone_partial))
    if state != previous_state:
        save_state(state)

    if not problems:
        print(f"ok - {counts}, failing sources: {now_failing or 'none'}")
        for line in detail:
            if "已恢复" in line:
                print(line)
        return 0

    advisory = lambda problem: problem.startswith(("新增数据质量拒绝组:", "来源端点失配警告:"))
    advisory_only = all(advisory(problem) for problem in problems)
    title = ("来源端点失配警告" if advisory_only and source_warnings else
             "摄入数据质量提醒" if advisory_only else "后台任务异常")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = [f"agent-tools {title}（{stamp}，过去 24 小时）", ""]
    body += [f"- {p}" for p in problems]
    if detail:
        body += ["", "细节:"] + detail
    body += [
        "",
        f"完成轮次: {counts}",
        f"当前失败爬源: {', '.join(now_failing) or '无'}",
        "",
        "说明: 数据质量拒绝已逐条隔离，不代表整轮失败；绑定待审仍保留保护。",
        "来源失配: 当前有效资料优先，旧关联保留历史；同一端点对首次提醒，新变化另行提醒。",
        "排查: journalctl -u 'agent-tools-*' --since '24 hours ago' | grep -E 'failed'",
        "背景: 任务把分段异常吞成 WARNING 且仍返回 0，systemd 看不出问题（文档 §4.25）。",
    ]
    text = "\n".join(body)
    html = "<pre style='font:13px/1.6 ui-monospace,monospace'>" + \
        text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    primary = (source_warnings[0] if advisory_only and source_warnings else
               problems[0] if advisory_only else next(problem for problem in problems if not advisory(problem)))
    subject = f"[agent-tools] {title}: {primary}"
    recorded = mailer.send_internal_notice(subject, text, html)
    if recorded:
        acknowledged = False
        for key in ('pending_ingestion_reason_groups', 'pending_source_endpoint_warnings'):
            if state.pop(key, None):
                acknowledged = True
        if acknowledged:
            save_state(state)
    print(("notice recorded" if recorded else "NOTICE NOT RECORDED (local storage failed)")
          + "\n" + text)
    return 0 if advisory_only and recorded else 1


if __name__ == "__main__":
    sys.exit(main())
