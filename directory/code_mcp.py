"""Code-only MCP servers: repositories and packages with no remote endpoint.

Public sources, merged by GitHub repository before ingestion:

* GitHub repositories tagged ``topic:mcp-server`` (search API, split by
  creation date because one query returns at most 1,000 results);
* npm and PyPI packages with the keyword ``mcp-server`` from ecosyste.ms
  (complete keyword index, with first-release dates), plus npm's own search
  (which stops returning new results after ~5,250 of its ~11,500 matches).

Every row is ``kind='catalog'`` with ``endpoint_url=None``: it belongs to the
code-only sub-directory (/mcp/code), is never probed and never counted in the
remote MCP statistics. A repository that any remote listing already links
(source_code_url or homepage) is skipped -- the remote listing owns it -- and
so is one that another directory already lists as code-only.
"""
from __future__ import annotations

import http.client
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
OWN_SOURCES = ("github", "npm", "pypi")
ECOSYSTEMS_URL = "https://packages.ecosyste.ms/api/v1/keywords/{kw}"
ECOSYSTEMS = {"npm": "npm", "pypi": "pypi"}   # ecosyste.ms ecosystem -> our package_registry
SEARCH_CAP = 1000          # GitHub search returns at most 1,000 results per query
GITHUB_FIRST_DAY = date(2024, 11, 1)   # MCP was announced 2024-11-25
NPM_PAGE = 250
NPM_MAX = 20000


class CodeCrawlError(RuntimeError):
    pass


NET_RETRY_DELAYS = (5, 15, 30, 60, 120)   # a full crawl takes ~1 h; ride out DNS / network blips


def _get(url: str, headers: dict | None = None, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               **(headers or {})})
    for delay in (*NET_RETRY_DELAYS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            # 4xx are answers (callers handle 403/404/429); retry only 5xx.
            if e.code < 500 or delay is None:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                http.client.HTTPException, json.JSONDecodeError):
            if delay is None:
                raise
        log.warning("code mcp: transient error on %s; retrying in %ss",
                    url.split("?", 1)[0], delay)
        time.sleep(delay)


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


def fetch_ecosystems(keyword: str = NPM_KEYWORD, *, max_pages: int = 2000) -> list[dict]:
    """Every npm / PyPI package with the keyword, from ecosyste.ms (no
    deep-paging cap; carries first-release dates). Anonymous limit 5,000
    requests per hour; a full scan is ~320 pages."""
    out: dict[tuple[str, str], dict] = {}
    for page in range(1, max_pages + 1):
        url = ECOSYSTEMS_URL.format(kw=urllib.parse.quote(keyword)) + "?" + urllib.parse.urlencode(
            {"per_page": 100, "page": page})
        for attempt in range(4):
            try:
                data, _ = _get(url, timeout=60)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(60)
                    continue
                raise
        pkgs = data.get("packages") or []
        if not pkgs:
            break
        for p in pkgs:
            reg = ECOSYSTEMS.get(p.get("ecosystem") or "")
            if reg and p.get("name"):
                out.setdefault((reg, p["name"].lower()), p)
    log.info("code mcp: ecosyste.ms keyword:%s npm=%d pypi=%d", keyword,
             sum(1 for r, _ in out if r == "npm"), sum(1 for r, _ in out if r == "pypi"))
    return list(out.values())


def eco_row(p: dict) -> dict:
    reg = ECOSYSTEMS[p["ecosystem"]]
    name = p["name"]
    repo = github_repo(p.get("repository_url"))
    repo_url = f"https://github.com/{repo}" if repo else ((p.get("repository_url") or "").strip() or None)
    page = (f"https://www.npmjs.com/package/{name}" if reg == "npm"
            else f"https://pypi.org/project/{name}/")
    lic = p.get("licenses") if isinstance(p.get("licenses"), str) else None
    owner = (name.split("/")[0].lstrip("@") if "/" in name else reg)
    row = {
        "slug": _slug(name.split("/")[-1], owner),
        "name": name,
        "description": (p.get("description") or "").strip()[:1000] or None,
        "homepage_url": (p.get("homepage") or "").strip() or repo_url or page,
        "endpoint_url": None,
        "kind": "catalog",
        "source_code_url": repo_url,
        "package_registry": reg,
        "package_name": name,
        "package_download_count": p.get("downloads") if isinstance(p.get("downloads"), int) else None,
        "license": (lic or "")[:80] or None,
        "repo_pushed_at": p.get("latest_release_published_at"),
        "tags": [k for k in (p.get("keywords_array") or []) if isinstance(k, str)][:12] or None,
        "source": reg,
        "source_id": name.lower(),
        "source_url": page,
        "confidence": 0.5,
    }
    created = _epoch(p.get("first_release_published_at"))
    if created:
        row["created_at"] = created
    return row


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
    """(repos owned by another listing, (registry, package) owned by another listing).

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
        if pkg and (reg or "").lower() in ECOSYSTEMS.values():
            packages.add(((reg or "").lower(), pkg.lower()))
    return repos, packages


def merge(github_items: list[dict], package_rows: list[dict],
          known: tuple[set[str], set]) -> tuple[list[dict], dict]:
    """One row per repository (GitHub first, enriched with its package);
    packages without a GitHub repository stay as npm / PyPI rows.

    `package_rows` are npm_row() / eco_row() rows (npm search objects are
    accepted too, for the fallback path)."""
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
    for obj in package_rows:
        row = npm_row(obj) if "package" in obj else obj
        pkg = row["package_name"].lower()
        repo = github_repo(row.get("source_code_url"))
        if (row["package_registry"], pkg) in known_pkg or (repo and repo in known_repo):
            skipped["linked_elsewhere"] += 1
            continue
        if repo and repo in rows:
            gh = rows[repo]
            if not gh.get("package_name"):
                gh["package_registry"], gh["package_name"] = row["package_registry"], row["package_name"]
            else:
                skipped["duplicate_package"] += 1
            continue
        if repo and repo in npm_only:
            skipped["duplicate_package"] += 1   # monorepo: first package wins
            continue
        npm_only[repo or (row["package_registry"], pkg)] = row
    out = list(rows.values()) + list(npm_only.values())
    return out, skipped


def fetch_all(*, github_limit: int | None = None, npm_limit: int = NPM_MAX,
              min_stars: int = 0, pushed_since: date | None = None) -> tuple[list[dict], dict]:
    gh = fetch_github(max_repos=github_limit, min_stars=min_stars, pushed_since=pushed_since)
    try:
        eco = [eco_row(p) for p in fetch_ecosystems()]
    except Exception as e:  # noqa: BLE001 - fall back to npm search alone
        log.warning("code mcp: ecosyste.ms failed (%r); npm search only", e)
        eco = []
    have = {(r["package_registry"], r["source_id"]) for r in eco}
    npm = [npm_row(o) for o in fetch_npm(max_packages=npm_limit)]
    packages = eco + [r for r in npm if ("npm", r["source_id"]) not in have]
    with db.connect(read_only=True) as c:
        known = known_repos(c)
        have_npm = {r[0] for r in c.execute(
            "SELECT source_id FROM mcp_servers WHERE source='npm'")}
    rows, skipped = merge(gh, packages, known)
    # npm search has no creation date: look it up only for search-only packages
    # we do not have yet (ecosyste.ms rows carry their first-release date).
    need = [r["package_name"] for r in rows
            if r["source"] == "npm" and not r.get("created_at") and r["source_id"] not in have_npm]
    created = npm_created(need) if need else {}
    for r in rows:
        if r["source"] == "npm" and not r.get("created_at"):
            r["created_at"] = created.get(r["package_name"])
    info = {"github": len(gh), "ecosystems": len(eco), "npm_search": len(npm),
            "npm_search_only": len(packages) - len(eco), "rows": len(rows), "min_stars": min_stars,
            "pushed_since": pushed_since.isoformat() if pushed_since else None,
            "npm_created_lookups": len(need), **skipped}
    log.info("code mcp merged: %s", info)
    return rows, info
