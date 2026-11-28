"""Daily check of our GitHub repositories' issues and pull requests -> Notice.

Each issue / PR touched in the last RETENTION_DAYS becomes one rolling notice
row (category ``github``): active while open, resolved once closed or merged.
Only the number, title, author and counts are kept; bodies are never copied.
Uses the public REST API without credentials (one request per repo per run).
"""

import logging
import os
import time

import httpx

from . import notices

log = logging.getLogger(__name__)
DEFAULT_REPOS = ("AgentTools-Cloud/AgentToolsCollection",)
API = "https://api.github.com/repos/{repo}/issues"
UA = "agent-tools.cloud-github-watch/1.0 (+https://agent-tools.cloud/about)"
MAX_PAGES = 5


def repos() -> tuple[str, ...]:
    value = os.getenv("AGENT_TOOLS_GITHUB_WATCH_REPOS", "").strip()
    return tuple(r.strip() for r in value.split(",") if r.strip()) or DEFAULT_REPOS


def fetch(repo: str, client: httpx.Client, now: int) -> list[dict]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(now - notices.RETENTION_DAYS * 86400))
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        r = client.get(API.format(repo=repo), params={
            "state": "all", "since": since, "per_page": 100, "page": page,
            "sort": "updated", "direction": "desc"})
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            raise ValueError("unexpected GitHub response")
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def notice_for(repo: str, item: dict) -> dict:
    is_pr = "pull_request" in item
    merged = bool(is_pr and (item.get("pull_request") or {}).get("merged_at"))
    is_open = item.get("state") == "open"
    state = "打开" if is_open else ("已合并" if merged else "已关闭")
    kind = "PR" if is_pr else "issue"
    created = str(item.get("created_at") or "")[:10]
    author = (item.get("user") or {}).get("login") or "?"
    number = int(item["number"])
    summary = (f"{repo} #{number}：{item.get('title') or ''}；作者 {author}；"
               f"{state}；创建 {created}，评论 {int(item.get('comments') or 0)}。"
               + ("需要处理：请到 GitHub 查看并回复。" if is_open else ""))
    return {
        "severity": "warning" if is_open else "info",
        "title": f"GitHub 新 {kind} · {repo.split('/')[-1]}",
        "summary": summary,
        "event_key": f"github:{repo}:{number}",
        "data": {"number": number, "pull_request": is_pr, "open": is_open,
                 "merged": merged, "comments": int(item.get("comments") or 0)},
    }


def run(now: int | None = None, client: httpx.Client | None = None) -> dict:
    now = int(time.time() if now is None else now)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(20.0), headers={
        "User-Agent": UA, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    stats = {"repos": 0, "items": 0, "open": 0, "stored": 0, "errors": 0}
    try:
        for repo in repos():
            try:
                items = fetch(repo, client, now)
            except Exception as exc:  # noqa: BLE001 -- one repo must not stop the rest
                stats["errors"] += 1
                log.warning("github watch %s failed: %s", repo, type(exc).__name__)
                notices.store("github", "warning", "GitHub 检查失败",
                              f"{repo}：{type(exc).__name__}；PR / issue 未能刷新。",
                              event_key=notices.routine_day_key(f"github-fail:{repo}", now))
                continue
            stats["repos"] += 1
            for item in items:
                if not isinstance(item, dict) or "number" not in item:
                    continue
                n = notice_for(repo, item)
                stats["items"] += 1
                stats["open"] += n["data"]["open"]
                stats["stored"] += bool(notices.store(
                    "github", n["severity"], n["title"], n["summary"],
                    event_key=n["event_key"], data=n["data"]))
    finally:
        if own:
            client.close()
    log.info("github watch: %s", stats)
    return stats
