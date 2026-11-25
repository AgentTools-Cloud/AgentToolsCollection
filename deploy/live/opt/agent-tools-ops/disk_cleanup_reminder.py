#!/usr/bin/env python3
"""Daily disk report for the directory host. Never deletes anything.

Runs from agent-tools-disk-reminder.timer (daily 09:15). Writes the full list to
REPORT and publishes counts to the Agent Tools Notice feed (category ``disk``).
The Notice row stays active while there is something to clean, the disk is at
least ALERT_PCT full, or the off-site backup is older than 30 hours.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ALERT_PCT = int(os.getenv("DISK_REMINDER_ALERT_PCT", "80"))
OLD_DAYS = int(os.getenv("DISK_REMINDER_OLD_DAYS", "14"))
BIG = 200 * 1024 * 1024
GB = 1024 ** 3
REPORT = Path("/var/lib/agent-tools-disk-reminder/latest.txt")
LAST_SUCCESS = Path("/var/backups/agent-tools/last-success.json")
# (directory, glob patterns, label). Only one-off snapshots and scratch copies;
# source checkouts are not cleanup candidates.
SCAN = [
    ("/tmp", ["*.db"], "测试/脚本遗留的线上库副本"),
    ("/opt/mcpserver/backups", ["*"], "部署前快照"),
    ("/opt/mcpserver/data/backups", ["*.db", "*.db.gz", "*.jsonl.gz", "*.sql"], "部署前快照"),
    ("/root", ["*before*", "*deploy*", "*.db"], "整库副本和部署目录"),
]


def size_of(p: Path) -> int:
    if p.is_symlink():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def collect():
    now = time.time()
    du = shutil.disk_usage("/")
    pct = round(du.used * 100 / du.total)
    try:
        ls = json.loads(LAST_SUCCESS.read_text())
        backup_age = (now - float(ls["finished_at"])) / 3600
        archive = ls.get("archive", "?")
    except (OSError, ValueError, KeyError, TypeError):
        backup_age, archive = None, None

    sections, items, tmp_copies, total = [], 0, 0, 0
    for base, patterns, label in SCAN:
        root = Path(base)
        if not root.is_dir():
            continue
        found = {}
        for pattern in patterns:
            for p in root.glob(pattern):
                found[p] = None
        rows = []
        for p in found:
            try:
                st = p.lstat()
            except OSError:
                continue
            age_d = (now - st.st_mtime) / 86400
            if base == "/tmp":
                tail = p.stem.rsplit("-", 1)[-1]
                if tail.isdigit():
                    if alive(tail):
                        continue  # a test run still owns it
                elif age_d < 1:
                    continue  # no owner pid in the name: only list copies older than a day
            elif age_d < OLD_DAYS:
                continue
            size = size_of(p)
            if size < BIG:
                continue
            rows.append((size, age_d, p))
        if rows:
            rows.sort(reverse=True)
            sections.append((base, label, rows))
            items += len(rows)
            total += sum(r[0] for r in rows)
            if base == "/tmp":
                tmp_copies = len(rows)
    return du, pct, backup_age, archive, sections, items, tmp_copies, total


def render(du, pct, backup_age, archive, sections, total) -> str:
    out = [f"磁盘：已用 {pct}%（{human(du.used)} / {human(du.total)}），剩余 {human(du.free)}。",
           "本报告只列清单，不删除任何文件。删除前逐项确认用途；旧快照先归档到 NFS 并核对哈希，再删本地。", ""]
    if backup_age is None:
        out.append("异地日备：读不到 last-success.json。")
    else:
        out.append(f"异地日备：最近成功 {backup_age:.0f} 小时前（{archive}）"
                   + ("，已超过 30 小时" if backup_age > 30 else "") + "。")
    out.append("")
    for base, label, rows in sections:
        cond = "进程已结束或超过 1 天" if base == "/tmp" else f"超过 {OLD_DAYS} 天"
        out.append(f"## {base}（{label}，≥200 MB，{cond}）")
        out += [f"- {human(s):>9}  {a:5.0f} 天  {p}" for s, a, p in rows]
        out.append(f"  小计 {human(sum(r[0] for r in rows))}")
        out.append("")
    out.append(f"候选合计 {human(total)}。" if sections else "没有需要清理的候选项。")
    return "\n".join(out) + "\n"


def main() -> int:
    du, pct, backup_age, archive, sections, items, tmp_copies, total = collect()
    body = render(du, pct, backup_age, archive, sections, total)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(body)
    print(body)
    action = bool(items) or pct >= ALERT_PCT or backup_age is None or backup_age > 30
    sys.path.insert(0, "/opt/mcpserver")
    from directory import notices
    ok = notices.publish_disk_report(pct, du.free // GB, total / GB, items, tmp_copies,
                                     backup_age, action_needed=action)
    print("notice stored" if ok else "notice NOT stored")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
