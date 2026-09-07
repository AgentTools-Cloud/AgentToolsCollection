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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/mcpserver")

from directory import mailer  # noqa: E402

STATE = Path("/var/lib/agent-tools-watchdog/state.json")
WINDOW = "24 hours ago"

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
