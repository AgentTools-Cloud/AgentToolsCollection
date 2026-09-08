#!/usr/bin/env python3
"""Daily watchdog for the agent-tools background jobs.

The jobs swallow per-stage exceptions into WARNING logs and still return 0, so
systemd reports success while a whole pass silently did nothing (see doc §4.25:
the MCP pass aborted on 10 of 64 runs for a week without any visible signal).
This reads one day of journal in a single pass and emails the admin when
something is wrong. It stays silent when everything is healthy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/mcpserver")

from directory import db, mailer  # noqa: E402

STATE = Path("/var/lib/agent-tools-watchdog/state.json")
WINDOW = "24 hours ago"
WINDOW_SECONDS = 24 * 3600
# Every table is written by at least one job per 6h; twice that is clearly stuck.
STALE_HOURS = 12
FRESH_TABLES = ("mcp_servers", "services", "a2a_agents")

# A stage that aborts logs "<stage> failed: <exc>" and is otherwise invisible.
ABORT_RE = re.compile(r"directory\.jobs ((?:mcp|a2a) health) failed: (.+)")
# Completion lines, one per finished pass.
DONE_RE = {
    "services health": re.compile(r"directory\.jobs health: ok="),
    "mcp health": re.compile(r"directory\.jobs mcp health: ok="),
    "a2a health": re.compile(r"directory\.jobs a2a health: ok="),
}
# The health timer fires every 4h (6/day). Allow slack for deploys and reboots.
MIN_PASSES = 4
CRAWL_FAIL_RE = re.compile(r"(?:mcp |a2a )?crawl ([a-z0-9][a-z0-9-]*) (?:fetch )?failed")
# Every crawl source runs on the same ~6h timer, so four rounds is a full day.
PARTIAL_STREAK = 4


def journal() -> list[str]:
    out = subprocess.run(
        ["journalctl", "-u", "agent-tools-*", "--since", WINDOW, "--no-pager", "-o", "cat"],
        capture_output=True, text=True, timeout=600,
    )
    return out.stdout.splitlines()


def unit_results() -> list[tuple[str, str]]:
    """Units whose last run did not end cleanly."""
    bad = []
    for unit in ("crawl", "health", "reverify", "review", "onchain", "quarantine"):
        name = f"agent-tools-{unit}.service"
        r = subprocess.run(
            ["systemctl", "show", name, "-p", "Result", "--value"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if r and r != "success":
            bad.append((unit, r))
    return bad


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
    with db.connect(read_only=True) as c:
        # status=error means the fetch itself failed; the failing-source
        # baseline above already owns those, and repeating them here would put
        # the chronically broken sources into a daily mail.
        runs = c.execute(
            "SELECT source, added, updated, errors FROM crawl_runs "
            "WHERE COALESCE(finished_at, started_at) >= ? AND status <> ?",
            (now - WINDOW_SECONDS, "error")).fetchall()
        for source, added, updated, errors in runs:
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
    return problems, detail


def stuck_sources() -> list[tuple[str, int, str]]:
    """Sources whose last PARTIAL_STREAK runs were every one of them partial.

    A source that limps every round hides from all three checks above: its
    status is partial rather than error so the failing-source set never moves,
    its writes still outnumber its errors, and the tables stay fresh because
    other sources are writing to them. agenstry read as healthy by all three
    for 21 days (doc §4.49).
    """
    now = time.time()
    with db.connect(read_only=True) as c:
        rows = c.execute(
            "SELECT source, status, errors, COALESCE(finished_at, started_at) ts "
            "FROM crawl_runs "
            "WHERE COALESCE(finished_at, started_at) BETWEEN ? AND ? "
            "ORDER BY source, ts", (now - 14 * 86400, now)).fetchall()

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
        if streak >= PARTIAL_STREAK:
            first = (seq[-streak][1] or "").splitlines()
            out.append((source, streak, first[0][:120] if first else ""))
    return sorted(out)


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

    db_problems, db_detail = db_checks()
    problems += db_problems
    detail += db_detail

    # Known-broken crawl sources fail every run; alerting on them daily would be
    # pure noise. Track the set instead and speak up only when it changes.
    now_failing = sorted(set(CRAWL_FAIL_RE.findall("\n".join(lines))))
    state = load_state()
    baseline = state.get("failing_sources")
    if baseline is None:
        state["failing_sources"] = now_failing
        state["baseline_set_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)
    elif now_failing != baseline:
        added = [s for s in now_failing if s not in baseline]
        gone = [s for s in baseline if s not in now_failing]
        if added:
            problems.append(f"新增失败爬源: {', '.join(added)}")
        if gone:
            detail.append(f"  已恢复的爬源: {', '.join(gone)}（基线已更新）")
        state["failing_sources"] = now_failing
        state["changed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)

    # Same treatment as the failing-source set: a source stuck on partial stays
    # stuck until someone fixes it, so report the set changing rather than its
    # contents, or this becomes a daily mail nobody reads.
    stuck = stuck_sources()
    stuck_names = sorted(s for s, _, _ in stuck)
    stuck_base = state.get("stuck_sources")
    if stuck_base is None:
        state["stuck_sources"] = stuck_names
        save_state(state)
    elif stuck_names != stuck_base:
        for source, streak, first in stuck:
            if source not in stuck_base:
                problems.append(
                    f"{source} 连续 {streak} 轮 partial（约 {streak * 6}h 没跑全）")
                if first:
                    detail.append(f"  {source} 首条报错: {first}")
        recovered = [s for s in stuck_base if s not in stuck_names]
        if recovered:
            detail.append(f"  已恢复的 partial 源: {', '.join(recovered)}（基线已更新）")
        state["stuck_sources"] = stuck_names
        save_state(state)

    if not problems:
        print(f"ok - {counts}, failing sources: {now_failing or 'none'}")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = [f"agent-tools 后台任务异常（{stamp}，过去 24 小时）", ""]
    body += [f"- {p}" for p in problems]
    if detail:
        body += ["", "细节:"] + detail
    body += [
        "",
        f"完成轮次: {counts}",
        f"当前失败爬源: {', '.join(now_failing) or '无'}",
        "",
        "排查: journalctl -u 'agent-tools-*' --since '24 hours ago' | grep -E 'failed'",
        "背景: 任务把分段异常吞成 WARNING 且仍返回 0，systemd 看不出问题（文档 §4.25）。",
    ]
    text = "\n".join(body)
    html = "<pre style='font:13px/1.6 ui-monospace,monospace'>" + \
        text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    subject = f"[agent-tools] 后台任务异常: {problems[0]}"
    sent = mailer._send(mailer.ADMIN_EMAIL, subject, text, html)
    print(("alert sent" if sent else "ALERT NOT SENT (smtp failed)") + "\n" + text)
    return 1


if __name__ == "__main__":
    sys.exit(main())
