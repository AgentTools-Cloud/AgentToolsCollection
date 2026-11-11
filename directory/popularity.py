"""External adoption signals: GitHub stars, npm downloads, Tranco site rank.

Collected from free public sources into three cache tables keyed by the
external identity (repo, package, domain), then joined to listings when a page
or the daily /stats snapshot needs them. Display only: search order and
leaderboards keep using the quality score.

- GitHub: REST `GET /repos/{owner}/{repo}` authenticated with the site's GitHub
  OAuth App client credentials (5,000 requests/hour for public data). Paced to
  stay well under that, refreshed weekly.
- npm: the public downloads API (last 30 days), bulk for unscoped names.
- Tranco: the daily research top-1M list (https://tranco-list.eu), stored only
  for domains we index. A domain fronting many listings (a hosting platform or
  a gateway) is counted but never attributed to one listing.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from typing import Callable, Iterable

log = logging.getLogger("directory.popularity")

UA = "agent-tools.cloud popularity (+https://agent-tools.cloud/about)"
STALE_SECONDS = 7 * 86400
GITHUB_PER_HOUR = 3000
SHARED_DOMAIN_LISTINGS = 20   # a domain behind more listings is a platform
TRANCO_LATEST = "https://tranco-list.eu/api/lists/date/latest"

_GH_RE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})",
                    re.I)
_GH_RESERVED = {"orgs", "sponsors", "topics", "marketplace", "apps", "features", "settings",
                "users", "about", "collections", "enterprise", "search", "login", "explore"}
_NPM_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_github_repos (
  repo        TEXT PRIMARY KEY,     -- lowercase owner/name
  full_name   TEXT,
  stars       INTEGER,
  forks       INTEGER,
  pushed_at   TEXT,
  archived    INTEGER,
  status      TEXT NOT NULL,        -- ok | not_found | error
  fetched_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ext_npm_packages (
  package       TEXT PRIMARY KEY,
  downloads_30d INTEGER,
  status        TEXT NOT NULL,
  fetched_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ext_tranco (
  domain    TEXT PRIMARY KEY,       -- registrable domain
  rank      INTEGER NOT NULL,
  listings  INTEGER NOT NULL,       -- how many of our listings sit on it
  list_id   TEXT NOT NULL,
  list_date TEXT NOT NULL
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def github_repo(url: str | None) -> str | None:
    """`owner/repo` (lowercase) for a github.com URL, else None."""
    m = _GH_RE.match((url or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in _GH_RESERVED:
        return None
    repo = re.sub(r"\.git$", "", repo)
    if not repo or repo in (".", ".."):
        return None
    return f"{owner}/{repo}".lower()


def npm_package(registry: str | None, name: str | None) -> str | None:
    name = (name or "").strip()
    if (registry or "").lower() != "npm" or not _NPM_RE.match(name):
        return None
    return name


_SITES = None


def site(url_or_host: str | None) -> str | None:
    """Registrable domain (private PSL suffixes included, offline list)."""
    global _SITES
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return None
    host = urllib.parse.urlsplit(raw).hostname if "//" in raw else raw.split("/")[0]
    if not host or re.fullmatch(r"[\d.]+|\[.*\]", host) or host == "localhost":
        return None
    if _SITES is None:
        import tldextract
        _SITES = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(),
                                       include_psl_private_domains=True)
    return _SITES(host).top_domain_under_public_suffix or None


# The site a listing is served from: the URL an agent calls. MCP homepages are
# left out because they often point at the product being wrapped (a "YouTube
# transcript" server with youtube.com as homepage is not YouTube's).
LISTING_SITE_SQL = {
    "x402": "SELECT id, url FROM services",
    "mcp": "SELECT id, endpoint_url FROM mcp_servers",
    "a2a": "SELECT id, COALESCE(NULLIF(endpoint_url,''), card_url) FROM a2a_agents",
}
# Platforms that host third-party content or code under their own domain; a
# listing there says nothing about the platform's traffic.
DENY_SITES = frozenset({
    "youtube.com", "youtu.be", "twitter.com", "x.com", "github.com", "githubusercontent.com",
    "gitlab.com", "googleusercontent.com", "medium.com", "npmjs.com", "pypi.org",
    "linkedin.com", "reddit.com", "notion.site", "substack.com", "huggingface.co",
    "amazonaws.com", "cloudfront.net", "azure.com", "azurewebsites.net", "apify.com",
    "smithery.ai", "railway.app", "chatgpt.site", "sslip.io", "nip.io", "base44.app",
})


def listing_url(kind: str, row: dict) -> str | None:
    if kind == "x402":
        return row.get("url")
    if kind == "mcp":
        return row.get("endpoint_url")
    return row.get("endpoint_url") or row.get("card_url")


def attributable(domain: str | None, listings: int) -> bool:
    return bool(domain) and domain not in DENY_SITES and listings <= SHARED_DOMAIN_LISTINGS


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http(url: str, headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""


def _github_auth() -> str | None:
    cid = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip()
    sec = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        return None
    return "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def wanted_repos(conn) -> set[str]:
    repos = set()
    for src, home in conn.execute("SELECT source_code_url, homepage_url FROM mcp_servers"):
        for u in (src, home):
            r = github_repo(u)
            if r:
                repos.add(r)
                break
    return repos


def refresh_github(conn_factory, writer_factory, *, budget: int = GITHUB_PER_HOUR,
                   per_hour: int = GITHUB_PER_HOUR, now: int | None = None,
                   http: Callable = _http, sleep: Callable = time.sleep) -> dict:
    """Fetch stale or missing repos, oldest first, at most `budget` requests."""
    now = int(now or time.time())
    auth = _github_auth()
    if not auth:
        return {"skipped": "GITHUB_OAUTH_CLIENT_ID/SECRET not set"}
    with writer_factory() as w:
        ensure_schema(w)
    with closing(conn_factory()) as c:
        repos = wanted_repos(c)
        fetched = dict(c.execute("SELECT repo, fetched_at FROM ext_github_repos").fetchall())
    todo = sorted((r for r in repos if now - fetched.get(r, 0) >= STALE_SECONDS),
                  key=lambda r: fetched.get(r, 0))[:budget]
    gap = 3600.0 / max(per_hour, 1)
    done = {"ok": 0, "not_found": 0, "error": 0}
    rows = []
    for i, repo in enumerate(todo):
        status, headers, body = http(f"https://api.github.com/repos/{repo}",
                                     {"Authorization": auth, "Accept": "application/vnd.github+json"})
        if status == 200:
            d = json.loads(body or b"{}")
            rows.append((repo, d.get("full_name"), d.get("stargazers_count"), d.get("forks_count"),
                         d.get("pushed_at"), int(bool(d.get("archived"))), "ok", now))
            done["ok"] += 1
        elif status in (404, 451):
            rows.append((repo, None, None, None, None, None, "not_found", now))
            done["not_found"] += 1
        else:
            done["error"] += 1
            if status in (401, 403, 429):
                log.warning("github stopped at %s: HTTP %s", repo, status)
                break
        remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
        if remaining is not None and str(remaining).isdigit() and int(remaining) < 300:
            log.warning("github rate limit low (%s); stopping", remaining)
            break
        if len(rows) >= 200:
            _write_github(writer_factory, rows)
            rows = []
        if i + 1 < len(todo):
            sleep(gap)
    if rows:
        _write_github(writer_factory, rows)
    done["wanted"], done["queued"] = len(repos), len(todo)
    return done


def _write_github(writer_factory, rows):
    with writer_factory() as w:
        ensure_schema(w)
        w.executemany(
            "INSERT INTO ext_github_repos(repo, full_name, stars, forks, pushed_at, archived, status, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET full_name=excluded.full_name, "
            "stars=excluded.stars, forks=excluded.forks, pushed_at=excluded.pushed_at, "
            "archived=excluded.archived, status=excluded.status, fetched_at=excluded.fetched_at", rows)


# ---------------------------------------------------------------------------
# npm
# ---------------------------------------------------------------------------

def wanted_packages(conn) -> set[str]:
    out = set()
    for reg, name in conn.execute("SELECT package_registry, package_name FROM mcp_servers "
                                  "WHERE COALESCE(package_name,'')!=''"):
        p = npm_package(reg, name)
        if p:
            out.add(p)
    return out


def refresh_npm(conn_factory, writer_factory, *, now: int | None = None,
                http: Callable = _http, sleep: Callable = time.sleep) -> dict:
    now = int(now or time.time())
    with writer_factory() as w:
        ensure_schema(w)
    with closing(conn_factory()) as c:
        pkgs = wanted_packages(c)
        fetched = dict(c.execute("SELECT package, fetched_at FROM ext_npm_packages").fetchall())
    todo = sorted(p for p in pkgs if now - fetched.get(p, 0) >= STALE_SECONDS)
    unscoped = [p for p in todo if not p.startswith("@")]
    scoped = [p for p in todo if p.startswith("@")]
    rows, done = [], {"ok": 0, "not_found": 0, "error": 0}

    def record(pkg, dl):
        if isinstance(dl, int):
            rows.append((pkg, dl, "ok", now)); done["ok"] += 1
        else:
            rows.append((pkg, None, "not_found", now)); done["not_found"] += 1

    for i in range(0, len(unscoped), 100):
        batch = unscoped[i:i + 100]
        status, _h, body = http("https://api.npmjs.org/downloads/point/last-month/"
                                + ",".join(urllib.parse.quote(p, safe="") for p in batch))
        if status != 200:
            done["error"] += len(batch)
            continue
        data = json.loads(body or b"{}")
        if len(batch) == 1:
            data = {batch[0]: data}
        for p in batch:
            d = data.get(p)
            record(p, d.get("downloads") if isinstance(d, dict) else None)
        sleep(1.0)
    for p in scoped:
        url = "https://api.npmjs.org/downloads/point/last-month/" + urllib.parse.quote(p, safe="@")
        for attempt in range(3):
            status, _h, body = http(url)
            if status != 429:
                break
            sleep(15 * (attempt + 1))     # the downloads API throttles bursts of scoped lookups
        if status == 200:
            record(p, json.loads(body or b"{}").get("downloads"))
        elif status == 404:
            record(p, None)
        else:
            done["error"] += 1
        sleep(0.5)
    if rows:
        with writer_factory() as w:
            ensure_schema(w)
            w.executemany(
                "INSERT INTO ext_npm_packages(package, downloads_30d, status, fetched_at) VALUES (?,?,?,?) "
                "ON CONFLICT(package) DO UPDATE SET downloads_30d=excluded.downloads_30d, "
                "status=excluded.status, fetched_at=excluded.fetched_at", rows)
    done["wanted"], done["queued"] = len(pkgs), len(todo)
    return done


# ---------------------------------------------------------------------------
# Tranco
# ---------------------------------------------------------------------------

def our_sites(conn) -> dict[str, int]:
    """Registrable domain -> number of listings (all kinds) on it."""
    counts: dict[str, int] = {}
    for sql in LISTING_SITE_SQL.values():
        for _id, url in conn.execute(sql):
            s = site(url)
            if s:
                counts[s] = counts.get(s, 0) + 1
    return counts


def parse_tranco(lines: Iterable[str], wanted: dict[str, int]) -> list[tuple[str, int, int]]:
    out = []
    for row in csv.reader(lines):
        if len(row) < 2 or not row[0].isdigit():
            continue
        dom = row[1].strip().lower()
        if dom in wanted:
            out.append((dom, int(row[0]), wanted[dom]))
    return out


def refresh_tranco(conn_factory, writer_factory, *, http: Callable = _http) -> dict:
    status, _h, body = http(TRANCO_LATEST)
    if status != 200:
        return {"error": f"latest list metadata HTTP {status}"}
    meta = json.loads(body or b"{}")
    list_id = meta.get("list_id")
    list_date = (meta.get("created_on") or "")[:10]
    with writer_factory() as w:
        ensure_schema(w)
    with closing(conn_factory()) as c:
        have = c.execute("SELECT list_id FROM ext_tranco LIMIT 1").fetchone()
        wanted = our_sites(c)
    if have and have[0] == list_id:
        return {"unchanged": list_id}
    url = meta.get("download") or f"https://tranco-list.eu/download/{list_id}/1000000"
    status, headers, body = http(url, timeout=120)
    if status != 200 or not body:
        return {"error": f"download HTTP {status}"}
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    else:
        text = body.decode("utf-8", "replace")
    if len(text) < 1_000_000:          # a full list is ~20 MB; refuse a truncated one
        return {"error": f"list too small ({len(text)} bytes)"}
    rows = parse_tranco(io.StringIO(text), wanted)
    with writer_factory() as w:
        ensure_schema(w)
        w.execute("DELETE FROM ext_tranco")
        w.executemany("INSERT INTO ext_tranco(domain, rank, listings, list_id, list_date) VALUES (?,?,?,?,?)",
                      [(d, r, n, list_id, list_date) for d, r, n in rows])
    return {"list_id": list_id, "list_date": list_date, "our_sites": len(wanted), "ranked": len(rows)}


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def for_listing(conn, kind: str, row: dict) -> dict | None:
    """Signals for one listing, or None when there are none to show."""
    out: dict = {}
    if kind == "mcp" and _table_exists(conn, "ext_github_repos"):
        repo = github_repo(row.get("source_code_url")) or github_repo(row.get("homepage_url"))
        if repo:
            g = conn.execute("SELECT full_name, stars, forks, pushed_at, archived, status, fetched_at "
                             "FROM ext_github_repos WHERE repo=?", (repo,)).fetchone()
            if g and g[5] == "ok":
                out["github"] = {"repo": g[0] or repo, "url": f"https://github.com/{g[0] or repo}",
                                 "stars": g[1], "forks": g[2], "pushed_at": g[3],
                                 "archived": bool(g[4]), "fetched_at": g[6]}
        pkg = npm_package(row.get("package_registry"), row.get("package_name"))
        if pkg and _table_exists(conn, "ext_npm_packages"):
            n = conn.execute("SELECT downloads_30d, status, fetched_at FROM ext_npm_packages WHERE package=?",
                             (pkg,)).fetchone()
            if n and n[1] == "ok":
                out["npm"] = {"package": pkg, "url": f"https://www.npmjs.com/package/{pkg}",
                              "downloads_30d": n[0], "fetched_at": n[2]}
    s = site(listing_url(kind, row))
    if s and _table_exists(conn, "ext_tranco"):
        t = conn.execute("SELECT rank, listings, list_date FROM ext_tranco WHERE domain=?", (s,)).fetchone()
        if t and attributable(s, t[1]):
            out["tranco"] = {"domain": s, "rank": t[0], "list_date": t[2]}
    return out or None


STAR_BANDS = ((0, "0"), (9, "1–9"), (99, "10–99"), (999, "100–999"), (9999, "1k–9.9k"), (None, "10k+"))
RANK_BANDS = ((10_000, "Top 10k"), (100_000, "10k–100k"), (1_000_000, "100k–1M"))


def adoption_stats(conn) -> dict:
    """Aggregates for the /stats snapshot (reachable listings only)."""
    if not _table_exists(conn, "ext_github_repos"):
        return {"available": False}
    # MCP listings joined to their repo's stars, one row per listing.
    stars_by_repo = {r: (s, fn) for r, s, fn in conn.execute(
        "SELECT repo, stars, full_name FROM ext_github_repos WHERE status='ok'")}
    listing_stars, top = [], {}
    with_link = 0
    for _id, slug, name, src, home in conn.execute(
            "SELECT id, slug, name, source_code_url, homepage_url FROM mcp_servers WHERE health='ok'"):
        repo = github_repo(src) or github_repo(home)
        with_link += bool(repo)
        if repo in stars_by_repo and stars_by_repo[repo][0] is not None:
            stars = stars_by_repo[repo][0]
            listing_stars.append(stars)
            if repo not in top or top[repo]["stars"] < stars:
                top[repo] = {"repo": stars_by_repo[repo][1] or repo, "stars": stars, "slug": slug, "name": name}
    bands = {label: 0 for _hi, label in STAR_BANDS}
    for s in listing_stars:
        for hi, label in STAR_BANDS:
            if hi is None or s <= hi:
                bands[label] += 1
                break
    total = len(listing_stars) or 1
    tranco = {}
    if _table_exists(conn, "ext_tranco"):
        ranks = {d: (r, n) for d, r, n in conn.execute("SELECT domain, rank, listings FROM ext_tranco")}
        meta = conn.execute("SELECT list_date FROM ext_tranco LIMIT 1").fetchone()
        for kind, sql in LISTING_SITE_SQL.items():
            counts = {label: 0 for _hi, label in RANK_BANDS}
            n_sites = 0
            for _id, url in conn.execute(sql + " WHERE health='ok'"):
                s = site(url)
                if not s:
                    continue
                n_sites += 1
                hit = ranks.get(s)
                if hit and attributable(s, hit[1]):
                    for hi, label in RANK_BANDS:
                        if hit[0] <= hi:
                            counts[label] += 1
                            break
            tranco[kind] = {"reachable_with_site": n_sites, "bands": counts,
                            "ranked": sum(counts.values())}
        tranco["list_date"] = meta[0] if meta else None
    npm_top = []
    if _table_exists(conn, "ext_npm_packages"):
        npm_top = [{"package": p, "downloads_30d": d} for p, d in conn.execute(
            "SELECT package, downloads_30d FROM ext_npm_packages WHERE status='ok' "
            "ORDER BY downloads_30d DESC LIMIT 5")]
    return {
        "available": True,
        "mcp_with_github": len(listing_stars),
        "mcp_with_repo_link": with_link,
        "star_bands": [{"label": k, "count": v, "pct": round(100.0 * v / total, 1)} for k, v in bands.items()],
        "top_repos": sorted(top.values(), key=lambda r: -r["stars"])[:10],
        "npm_top": npm_top,
        "tranco": tranco,
    }
