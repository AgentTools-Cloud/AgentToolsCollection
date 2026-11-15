"""Code-only MCP servers: repositories and packages with no remote endpoint.

Two public sources, merged by GitHub repository before ingestion:

* GitHub repositories tagged ``topic:mcp-server`` (search API, split by
  creation date because one query returns at most 1,000 results);
* npm packages with the keyword ``mcp-server``.

Every row is ``kind='catalog'`` with ``endpoint_url=None``: it belongs to the
code-only sub-directory (/mcp/code), is never probed and never counted in the
remote MCP statistics. A repository that any remote listing already links
(source_code_url or homepage) is skipped -- the remote listing owns it -- and
so is one that another directory already lists as code-only.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import db
from .popularity import _github_auth, github_repo as _github_repo

log = logging.getLogger(__name__)

UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
GITHUB_TOPIC = "mcp-server"
NPM_KEYWORD = "mcp-server"
OWN_SOURCES = ("github", "npm")
SEARCH_CAP = 1000          # GitHub search returns at most 1,000 results per query
GITHUB_FIRST_DAY = date(2024, 11, 1)   # MCP was announced 2024-11-25
NPM_PAGE = 250
NPM_MAX = 20000


class CodeCrawlError(RuntimeError):
    pass


def _get(url: str, headers: dict | None = None, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), dict(r.headers)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github_search(q: str, page: int, auth: str | None) -> tuple[dict, dict]:
    url = ("https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page}))
    headers = {"Accept": "application/vnd.github+json"}
    if auth:
        headers["Authorization"] = auth
    for attempt in range(4):
        try:
            data, h = _get(url, headers)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 3:
                reset = int((e.headers or {}).get("X-RateLimit-Reset") or 0)
                time.sleep(min(90, max(10, reset - time.time() + 2)))
                continue
            raise
        # Search allows 30 (authenticated) or 10 requests per minute.
        if int(h.get("X-RateLimit-Remaining", "1") or 1) <= 1:
            reset = int(h.get("X-RateLimit-Reset") or 0)
            time.sleep(min(90, max(2, reset - time.time() + 2)))
        else:
            time.sleep(2.2 if auth else 6.5)
        return data, h
    raise CodeCrawlError("github search kept rate-limiting")


def _date_ranges(first: date, last: date, count, cap=SEARCH_CAP):
    """Split [first, last] into creation-date ranges with < cap results each."""
    stack = [(first, last)]
    while stack:
        a, b = stack.pop()
        n = count(a, b)
        if n <= cap or a == b:
            if n:
                yield a, b, n
            continue
        mid = a + (b - a) // 2
        stack.append((mid + timedelta(days=1), b))
        stack.append((a, mid))


def fetch_github(topic: str = GITHUB_TOPIC, *, today: date | None = None,
                 max_repos: int | None = None, min_stars: int = 0,
                 pushed_since: date | None = None) -> list[dict]:
    """All repositories with the topic, or (incremental) only those pushed
    on or after `pushed_since` -- new repositories are always pushed."""
    auth = _github_auth()
    today = today or date.today()
    base = f"topic:{topic} fork:false archived:false"
    if min_stars > 0:
        base += f" stars:>={int(min_stars)}"
    if pushed_since:
        base += f" pushed:>={pushed_since.isoformat()}"

    def count(a, b):
        data, _ = _github_search(f"{base} created:{a.isoformat()}..{b.isoformat()}", 1, auth)
        return int(data.get("total_count") or 0)

    out: dict[str, dict] = {}
    for a, b, n in _date_ranges(GITHUB_FIRST_DAY, today, count):
        q = f"{base} created:{a.isoformat()}..{b.isoformat()}"
        for page in range(1, min(10, (n + 99) // 100) + 1):
            data, _ = _github_search(q, page, auth)
            for item in data.get("items") or []:
                repo = (item.get("full_name") or "").lower()
                if repo and repo not in out:
                    out[repo] = item
            if len(data.get("items") or []) < 100:
                break
        if max_repos and len(out) >= max_repos:
            break
    log.info("code mcp: github topic:%s repos=%d", topic, len(out))
    return list(out.values())


def github_row(item: dict) -> dict:
    full = item["full_name"]
    owner, name = full.split("/", 1)
    url = item.get("html_url") or f"https://github.com/{full}"
    homepage = (item.get("homepage") or "").strip()
    if not homepage.startswith(("https://", "http://")):
        homepage = url
    row = {
        "slug": _slug(name, owner),
        "name": name,
        "description": (item.get("description") or "").strip()[:1000] or None,
        "homepage_url": homepage,
        "endpoint_url": None,
        "kind": "catalog",
        "source_code_url": url,
        "github_stars": item.get("stargazers_count"),
        "license": ((item.get("license") or {}).get("spdx_id") or None),
        "repo_pushed_at": item.get("pushed_at"),
        "tags": [t for t in (item.get("topics") or [])][:12] or None,
        "source": "github",
        "source_id": full.lower(),
        "source_url": url,
        "confidence": 0.5,
    }
    # First seen = when the repository was created, not when we found it, so
    # importing the existing backlog does not read as thousands of new servers.
    created = _epoch(item.get("created_at"))
    if created:
        row["created_at"] = created
    return row


# ---------------------------------------------------------------------------
# npm
# ---------------------------------------------------------------------------

def fetch_npm(keyword: str = NPM_KEYWORD, *, max_packages: int = NPM_MAX) -> list[dict]:
    out: dict[str, dict] = {}
    offset = 0
    while offset < max_packages:
        url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode(
            {"text": f"keywords:{keyword}", "size": NPM_PAGE, "from": offset})
        try:
            data, _ = _get(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30)
                continue
            raise
        objs = data.get("objects") or []
        for o in objs:
            p = o.get("package") or {}
            if p.get("name"):
                out.setdefault(p["name"].lower(), o)
        offset += len(objs)
        if len(objs) < NPM_PAGE or offset >= int(data.get("total") or 0):
            break
        time.sleep(1.0)
    log.info("code mcp: npm keywords:%s packages=%d", keyword, len(out))
    return list(out.values())


def npm_row(obj: dict) -> dict:
    p = obj.get("package") or {}
    links = p.get("links") or {}
    name = p["name"]
    repo = github_repo(links.get("repository"))
    repo_url = f"https://github.com/{repo}" if repo else (links.get("repository") or None)
    homepage = links.get("homepage") or repo_url or links.get("npm")
    return {
        "slug": _slug(name.split("/")[-1], name.split("/")[0].lstrip("@") if "/" in name else "npm"),
        "name": name,
        "description": (p.get("description") or "").strip()[:1000] or None,
        "homepage_url": homepage,
        "endpoint_url": None,
        "kind": "catalog",
        "source_code_url": repo_url,
        "package_registry": "npm",
        "package_name": name,
        "license": p.get("license") if isinstance(p.get("license"), str) else None,
        "repo_pushed_at": p.get("date"),
        "tags": [k for k in (p.get("keywords") or []) if isinstance(k, str)][:12] or None,
        "source": "npm",
        "source_id": name.lower(),
        "source_url": links.get("npm") or f"https://www.npmjs.com/package/{name}",
        "confidence": 0.5,
    }


# ---------------------------------------------------------------------------
# Merge + de-duplicate
# ---------------------------------------------------------------------------

def github_repo(url: str | None) -> str | None:
    """owner/repo (lowercase) also for npm-style `git+https://...git` and
    `git://` repository links."""
    u = (url or "").strip()
    if u.startswith("git+"):
        u = u[4:]
    if u.startswith(("git://", "ssh://git@")):
        u = "https://" + u.split("://", 1)[1].replace("git@", "", 1)
    u = re.sub(r"\.git(?=$|[/#?])", "", u)
    return _github_repo(u)


def _epoch(value) -> int | None:
    """ISO-8601 timestamp (GitHub / npm) -> unix seconds."""
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def npm_created(names: list[str], workers: int = 6) -> dict[str, int]:
    """First-publish time of npm packages (`time.created` in the packument)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(name):
        url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")
        for attempt in range(3):
            try:
                # The abbreviated install document has no `time`; the full one does.
                data, _ = _get(url, {"Accept": "application/json"}, timeout=60)
                created = (data.get("time") or {}).get("created")
                return name, _epoch(created)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(20)
                    continue
                return name, None
            except Exception:  # noqa: BLE001 - one package must not stop the run
                return name, None
        return name, None

    out: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, ts in pool.map(one, names):
            if ts:
                out[name] = ts
    return out


def _slug(name: str, owner: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", f"{name}-{owner}".lower()).strip("-")
    return s[:80] or "mcp-code"


def known_repos(conn) -> tuple[set[str], set[str]]:
    """(repos owned by another listing, npm packages owned by another listing).

    Rows from our own code crawlers are excluded so their re-crawl updates
    them instead of skipping them."""
    repos, packages = set(), set()
    placeholders = ",".join("?" * len(OWN_SOURCES))
    for hp, sc, reg, pkg in conn.execute(
            "SELECT homepage_url, source_code_url, package_registry, package_name "
            f"FROM mcp_servers WHERE COALESCE(source,'') NOT IN ({placeholders})", OWN_SOURCES):
        for u in (sc, hp):
            r = github_repo(u)
            if r:
                repos.add(r)
        if pkg and (reg or "").lower() == "npm":
            packages.add(pkg.lower())
    return repos, packages


def merge(github_items: list[dict], npm_objs: list[dict],
          known: tuple[set[str], set[str]]) -> tuple[list[dict], dict]:
    """One row per repository (GitHub first, enriched with its npm package);
    npm packages without a GitHub repository stay as npm rows."""
    known_repo, known_pkg = known
    rows: dict[str, dict] = {}
    skipped = {"linked_elsewhere": 0, "duplicate_package": 0}
    for item in github_items:
        row = github_row(item)
        repo = row["source_id"]
        if repo in known_repo:
            skipped["linked_elsewhere"] += 1
            continue
        rows[repo] = row
    npm_only: dict[str, dict] = {}
    for obj in npm_objs:
        row = npm_row(obj)
        pkg = row["package_name"].lower()
        repo = github_repo(row.get("source_code_url"))
        if pkg in known_pkg or (repo and repo in known_repo):
            skipped["linked_elsewhere"] += 1
            continue
        if repo and repo in rows:
            gh = rows[repo]
            if not gh.get("package_name"):
                gh["package_registry"], gh["package_name"] = "npm", row["package_name"]
            else:
                skipped["duplicate_package"] += 1
            continue
        if repo and repo in npm_only:
            skipped["duplicate_package"] += 1   # monorepo: first package wins
            continue
        npm_only[repo or pkg] = row
    out = list(rows.values()) + list(npm_only.values())
    return out, skipped


def fetch_all(*, github_limit: int | None = None, npm_limit: int = NPM_MAX,
              min_stars: int = 0, pushed_since: date | None = None) -> tuple[list[dict], dict]:
    gh = fetch_github(max_repos=github_limit, min_stars=min_stars, pushed_since=pushed_since)
    npm = fetch_npm(max_packages=npm_limit)
    with db.connect(read_only=True) as c:
        known = known_repos(c)
        have_npm = {r[0] for r in c.execute(
            "SELECT source_id FROM mcp_servers WHERE source='npm'")}
    rows, skipped = merge(gh, npm, known)
    # npm search has no creation date: look it up only for packages we do not
    # have yet (the incremental runs stay cheap).
    need = [r["package_name"] for r in rows
            if r["source"] == "npm" and r["source_id"] not in have_npm]
    created = npm_created(need) if need else {}
    for r in rows:
        if r["source"] == "npm":
            r["created_at"] = created.get(r["package_name"])
    info = {"github": len(gh), "npm": len(npm), "rows": len(rows), "min_stars": min_stars,
            "pushed_since": pushed_since.isoformat() if pushed_since else None,
            "npm_created_lookups": len(need), **skipped}
    log.info("code mcp merged: %s", info)
    return rows, info
