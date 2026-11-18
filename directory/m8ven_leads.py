"""Leads from the M8ven MCP index (m8ven.ai): which repositories and endpoints exist, nothing more.

Each day the listings in their sitemaps that we have not seen are re-read from their own source:
GitHub for owner/repo listings (the code crawl's filters, classifier and import), the endpoint
itself for remote ones (our MCP handshake; listed only when it answers with a serverInfo).
Their terms forbid redistributing their trust data and scraping beyond rate limits, so we keep
only listing URLs, read one page at a time 3 s apart and stop at the first 429.
Off unless AGENT_TOOLS_M8VEN_LEADS=1.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
from contextlib import closing
from urllib.parse import urlsplit

import httpx

from . import code_classify, code_mcp, crawlers, db, public_http
from .code_sources import mentions_mcp
from .popularity import _github_auth, github_repo

log = logging.getLogger(__name__)

SITEMAP_INDEX = "https://m8ven.ai/sitemap-index.xml"
HOST = "m8ven.ai"
RUN = "mcp:leads"
GAP = 3.0
MAX_PAGES = 40            # listing pages per run: theirs can take ~20 s each
MAX_REPOS = 5000
MAX_ENDPOINTS = 100
MAX_ATTEMPTS = 3
MAX_NEW = 20000           # more new listings than this at once means their URLs changed
# Endpoints from registries we crawl ourselves come through that crawl, with its metadata.
OWN_REGISTRIES = frozenset({"official", "smithery", "modelscope", "pulsemcp"})
MCP_TOPICS = frozenset(code_mcp.GITHUB_TOPICS) - frozenset(code_mcp.NOISY)
GENERIC_NAMES = frozenset({"mcp", "server", "mcp-server", "mcp server", "mcpserver", "fastmcp",
                           "my-mcp-server", "my mcp server", "example", "demo", "test",
                           "remote-mcp", "remote mcp", "app", "api"})
_REPO = re.compile(r"^[a-z0-9-]{1,39}/[a-z0-9._-]{1,100}$")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


class LeadsError(RuntimeError):
    pass


class Throttled(LeadsError):
    pass


def enabled() -> bool:
    return os.getenv("AGENT_TOOLS_M8VEN_LEADS", "0") == "1"


# ---------------------------------------------------------------------------
# State: listing URLs seen, and the repositories / endpoints they led to
# ---------------------------------------------------------------------------

def _ensure_table(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS m8ven_leads ("
              " kind TEXT NOT NULL, key TEXT NOT NULL, first_seen INTEGER NOT NULL,"
              " done_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,"
              " PRIMARY KEY (kind, key))")


def _queue(kind: str, keys, now: int, *, done: bool = False) -> int:
    rows = [(kind, k, now, now if done else None) for k in sorted(set(keys))]
    added = 0
    for i in range(0, len(rows), 2000):
        chunk = rows[i:i + 2000]

        def op(chunk=chunk):
            with db.writer() as c:
                _ensure_table(c)
                before = c.total_changes
                c.executemany("INSERT OR IGNORE INTO m8ven_leads(kind, key, first_seen, done_at) "
                              "VALUES (?,?,?,?)", chunk)
                return c.total_changes - before
        added += db.with_retry(op)
    return added


def _settle(kind: str, now: int, done=(), failed=()) -> None:
    """Mark leads handled; a failed one is retried until MAX_ATTEMPTS, then dropped."""
    done, failed = list(done), list(failed)
    if not done and not failed:
        return

    def op():
        with db.writer() as c:
            c.executemany("UPDATE m8ven_leads SET done_at=? WHERE kind=? AND key=?",
                          [(now, kind, k) for k in done])
            c.executemany("UPDATE m8ven_leads SET attempts=attempts+1, "
                          "done_at=CASE WHEN attempts+1>=? THEN ? END WHERE kind=? AND key=?",
                          [(MAX_ATTEMPTS, now, kind, k) for k in failed])
    db.with_retry(op)


def _pending(kind: str) -> list[str]:
    with closing(db.connect(read_only=True)) as c:
        return [k for (k,) in c.execute(
            "SELECT key FROM m8ven_leads WHERE kind=? AND done_at IS NULL ORDER BY first_seen, key",
            (kind,))]


# ---------------------------------------------------------------------------
# m8ven.ai
# ---------------------------------------------------------------------------

def paced(client: httpx.Client, gap: float = GAP, sleep=None):
    """GET one URL at a time, `gap` seconds apart; Throttled on 429."""
    last = [float("-inf")]

    def get(url: str) -> httpx.Response:
        wait = last[0] + gap - time.monotonic()
        if wait > 0:
            (sleep or time.sleep)(wait)
        last[0] = time.monotonic()
        r = client.get(url)
        if r.status_code == 429:
            raise Throttled("HTTP 429 (retry-after %s)" % r.headers.get("retry-after"))
        return r
    return get


def _fetch(get, url: str, *, tries: int = 3, sleep=None) -> str:
    for attempt in range(1, tries + 1):
        try:
            r = get(url)
        except httpx.TransportError:
            if attempt == tries:
                raise
        else:
            if r.status_code == 200:
                return r.text
            if r.status_code < 500 or attempt == tries:
                raise LeadsError("%s: HTTP %d" % (urlsplit(url).path, r.status_code))
        (sleep or time.sleep)(10 * attempt)
    raise AssertionError("unreachable")


def _listing_parts(url: str) -> list[str] | None:
    """["owner", "repo"] or ["slug"] for an M8ven MCP listing URL, else None."""
    p = urlsplit(url)
    parts = p.path.strip("/").split("/")
    if p.scheme != "https" or p.hostname != HOST or parts[0] != "mcp" \
            or len(parts) not in (2, 3) or not all(parts[1:]):
        return None
    return parts[1:]


def _locs(text: str) -> list[str]:
    return [html.unescape(u) for u in _LOC.findall(text)]


def listing_urls(get, *, sleep=None) -> set[str]:
    maps = [u for u in _locs(_fetch(get, SITEMAP_INDEX, sleep=sleep))
            if urlsplit(u).hostname == HOST and urlsplit(u).path.startswith("/mcp-sitemap")]
    if not maps:
        raise LeadsError("the sitemap index lists no MCP sitemaps")
    urls = set()
    for url in maps:
        urls.update(u for u in _locs(_fetch(get, url, sleep=sleep)) if _listing_parts(u))
    if not urls:
        raise LeadsError("the MCP sitemaps list no listings")
    return urls


def _record_at(text: str, at: int, decoder: json.JSONDecoder, reach: int = 200_000) -> dict | None:
    """The JSON object whose own key sits at `at`: nearest enclosing '{' that parses past it."""
    start = text.rfind("{", max(0, at - reach), at)
    while start >= 0:
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            obj, end = None, start
        if end > at and isinstance(obj, dict) and "source_registry" in obj and "remote_url" in obj:
            return obj
        start = text.rfind("{", max(0, at - reach), start)
    return None


def page_lead(page: str) -> dict | None:
    """Registry, remote endpoint and repository of the page's own listing (React flight data).

    Flight rows are not one JSON value per line (text rows carry raw newlines and run straight
    into the next row), so the record is found by its key, not by splitting lines."""
    try:
        flight = "".join(json.loads('"' + chunk + '"') for chunk in _CHUNK.findall(page))
    except ValueError:
        return None
    decoder = json.JSONDecoder()
    at = flight.find('"source_registry":')
    while at >= 0:
        record = _record_at(flight, at, decoder)
        if record is not None:
            return {"registry": record.get("source_registry"),
                    "remote_url": record.get("remote_url"),
                    "github_url": record.get("github_url")}
        at = flight.find('"source_registry":', at + 1)
    return None


def _endpoint_lead(lead: dict) -> str | None:
    url = lead.get("remote_url")
    if not isinstance(url, str) or not url.strip() or lead.get("registry") in OWN_REGISTRIES:
        return None
    url = url.strip()
    p = urlsplit(url)
    # A query string may carry someone's key; .run.tools is Smithery's runtime.
    if p.scheme not in ("https", "http") or not p.hostname or p.query \
            or p.hostname.endswith(".run.tools") or not db._exact_endpoint(url):
        return None
    return url


# ---------------------------------------------------------------------------
# Repositories: read from GitHub, then the code crawl's import
# ---------------------------------------------------------------------------

def _listed_repos(c) -> set[str]:
    repos = set()
    for (value,) in c.execute(
            "SELECT source_code_url FROM mcp_servers WHERE source_code_url LIKE '%github.com/%' "
            "UNION SELECT homepage_url FROM mcp_servers WHERE homepage_url LIKE '%github.com/%'"):
        r = github_repo(value)
        if r:
            repos.add(r)
    return repos


def github_items(names: list[str], auth: str | None) -> list[dict]:
    """GitHub repository objects for owner/repo names, 100 per search request."""
    items = []
    for i in range(0, len(names), 100):
        part = names[i:i + 100]
        try:
            data, _ = code_mcp._github_search(" ".join("repo:" + n for n in part), 1, auth)
        except urllib.error.HTTPError as e:
            if e.code != 422:
                raise
            # 422: none of these can be searched (deleted, private, renamed); halve to find any that can.
            if len(part) > 1:
                half = len(part) // 2
                items += github_items(part[:half], auth) + github_items(part[half:], auth)
            continue
        items += data.get("items") or []
    return items


def repo_rows(items: list[dict]) -> list[dict]:
    """The code crawl's rows for repositories that say MCP themselves; no forks or archives."""
    rows = {}
    for item in items:
        if item.get("fork") or item.get("archived") or item.get("disabled"):
            continue
        if not (mentions_mcp(item.get("name"), item.get("description"))
                or MCP_TOPICS & set(item.get("topics") or ())):
            continue
        row = code_mcp.github_row(code_mcp.slim_github(item))
        rows[row["source_id"]] = row
    return list(rows.values())


def import_repos(rows: list[dict]) -> dict:
    """classify -> upsert -> remember -> backdate, as jobs.cmd_crawl_code does for GitHub."""
    from . import jobs
    classes = code_classify.classify(rows)
    keep = [r for r in rows if classes.get((r["source"], r["source_id"])) in code_classify.KEEP]
    same = code_mcp.unchanged_rows(keep)
    part, originals = [], []
    for r in keep:
        if (r["source"], r["source_id"]) in same:
            continue
        originals.append(r)
        r = dict(r)
        if not r.get("created_at"):
            r.pop("created_at", None)
        part.append(r)
    result = jobs._ingest_rows("mcp", "github", part, db.upsert_mcp_server)
    if not result["errors"]:
        code_mcp.remember_rows(originals)
    jobs._code_backdate(keep)
    result["servers"] = len(keep)
    result["unanswered"] = {r["source_id"] for r in rows
                            if (r["source"], r["source_id"]) not in classes}
    return result


def _process_repos(out: dict, now: int) -> None:
    pending = _pending("repo")[:MAX_REPOS]
    if not pending:
        return
    with closing(db.connect(read_only=True)) as c:
        listed = _listed_repos(c)
    names = [n for n in pending if n not in listed]
    _settle("repo", now, done=[n for n in pending if n in listed])
    if not names:
        return
    if not code_classify.configured():
        out["errors"].append("repositories: no classifier model configured")
        return
    try:
        items = github_items(names, _github_auth())
    except (code_mcp.CodeCrawlError, urllib.error.URLError, OSError) as e:
        out["errors"].append(("github: %r" % e)[:200])
        _settle("repo", now, failed=names)
        return
    result = import_repos(repo_rows(items))
    out["repos_read"] += len(names)
    out["repos_listed"] += result["servers"]
    out["added"] += result["added"]
    out["updated"] += result["updated"]
    if result["errors"]:
        out["errors"].extend(result["errors"][:3])
        failed = names
    else:
        failed = [n for n in names if n in result["unanswered"]]
    _settle("repo", now, done=[n for n in names if n not in failed], failed=failed)


# ---------------------------------------------------------------------------
# Endpoints: our own MCP handshake
# ---------------------------------------------------------------------------

def read_endpoint(url: str) -> dict | None:
    """serverInfo of an endpoint that answers initialize anonymously (SSRF-safe client), else None."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": crawlers.MCP_LATEST_REVISION, "capabilities": {},
                       "clientInfo": {"name": "agent-tools.cloud", "version": "0.1"}}}
    headers = {"User-Agent": crawlers.UA, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    try:
        with public_http.client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                                follow_redirects=True) as c:
            r = c.post(url, json=init, headers=headers)
            if r.status_code >= 300:
                return None
            parsed = crawlers._mcp_parse_response(r)
    except (public_http.UnsafeURL, httpx.HTTPError, OSError, ValueError):
        return None
    result = parsed.get("result") if isinstance(parsed, dict) else None
    info = result.get("serverInfo") if isinstance(result, dict) else None
    return info if isinstance(info, dict) else None


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def endpoint_row(url: str, info: dict) -> dict:
    host = (urlsplit(url).hostname or "").lower()
    name = _text(info.get("title"), 120) or _text(info.get("name"), 120)
    if not name or name.lower() in GENERIC_NAMES:
        name = host
    site = _text(info.get("websiteUrl"), 500)
    if not (site or "").startswith(("https://", "http://")):
        site = "https://%s/" % host
    return {"slug": crawlers._slugify(name if name == host else "%s %s" % (name, host))[:80],
            "name": name, "description": _text(info.get("description"), 1000),
            "endpoint_url": url, "transport": "streamable-http", "homepage_url": site,
            "source": "manual", "source_id": url, "source_url": None, "confidence": 0.55}


def _process_endpoints(out: dict, now: int) -> None:
    pending = _pending("endpoint")[:MAX_ENDPOINTS]
    if not pending:
        return
    with closing(db.connect(read_only=True)) as c:
        hosts = {(urlsplit(u).hostname or "").lower() for (u,) in c.execute(
            "SELECT endpoint_url FROM mcp_servers WHERE COALESCE(endpoint_url,'')<>''")}
        known = {u for u in pending
                 if (urlsplit(u).hostname or "").lower() in hosts or db.find_mcp_by_endpoint(c, u)}
    rows = []
    for url in pending:
        if url in known:
            continue
        out["endpoints_probed"] += 1
        info = read_endpoint(url)
        if info is not None:
            rows.append(endpoint_row(url, info))
    failed = []
    if rows:
        from . import jobs
        result = jobs._ingest_rows("mcp", "manual", rows, db.upsert_mcp_server)
        out["endpoints_listed"] += result["added"]
        out["added"] += result["added"]
        out["updated"] += result["updated"]
        if result["errors"]:
            out["errors"].extend(result["errors"][:3])
            failed = [r["endpoint_url"] for r in rows]
    # One look per endpoint: one that does not answer now is not listed.
    _settle("endpoint", now, done=[u for u in pending if u not in failed], failed=failed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def record_baseline(urls) -> int:
    """Mark listings as seen without reading them (they were handled another way)."""
    keys = [u for u in urls if _listing_parts(u)]
    if not keys:
        raise LeadsError("baseline: no M8ven listing URLs")
    return _queue("listing", keys, int(time.time()), done=True)


def run(get, *, max_pages: int = MAX_PAGES, sleep=None) -> dict:
    out = {"new_listings": 0, "pages_read": 0, "repos_read": 0, "repos_listed": 0,
           "endpoints_probed": 0, "endpoints_listed": 0, "added": 0, "updated": 0, "errors": []}
    now = int(time.time())
    with db.writer() as c:
        _ensure_table(c)
    with closing(db.connect(read_only=True)) as c:
        seen = {k for (k,) in c.execute("SELECT key FROM m8ven_leads WHERE kind='listing'")}
    if not seen:
        raise LeadsError("no baseline yet: run `m8ven-leads --baseline` first")
    new = listing_urls(get, sleep=sleep) - seen
    if len(new) > max(MAX_NEW, len(seen) // 10):
        raise LeadsError("%d new listings at once: check their sitemap before a new baseline" % len(new))
    out["new_listings"] = _queue("listing", new, now)

    pending = _pending("listing")
    # owner/repo listings name their repository: no page to read.
    named = [u for u in pending if len(_listing_parts(u) or ()) == 2]
    _queue("repo", (n for n in ("/".join(_listing_parts(u)).lower() for u in named)
                    if _REPO.match(n)), now)
    _settle("listing", now, done=named)

    done, failed, repos, endpoints = [], [], set(), set()
    for url in [u for u in pending if len(_listing_parts(u) or ()) == 1][:max_pages]:
        try:
            r = get(url)
        except Throttled as e:
            out["errors"].append("listing pages: %s" % e)
            break
        except httpx.RequestError:
            failed.append(url)
            continue
        if r.status_code in (404, 410):
            done.append(url)
            continue
        lead = page_lead(r.text) if r.status_code == 200 else None
        if lead is None:
            failed.append(url)
            continue
        done.append(url)
        github_url = lead.get("github_url")
        repo = github_repo(github_url) if isinstance(github_url, str) else None
        if repo and _REPO.match(repo):
            repos.add(repo)
        endpoint = _endpoint_lead(lead)
        if endpoint:
            endpoints.add(endpoint)
    out["pages_read"] = len(done) + len(failed)
    _queue("repo", repos, now)
    _queue("endpoint", endpoints, now)
    _settle("listing", now, done=done, failed=failed)
    if failed:
        out["errors"].append("%d listing pages unread or without a listing record" % len(failed))

    _process_repos(out, now)
    _process_endpoints(out, now)
    return out


def add_cli(sub) -> None:
    p = sub.add_parser("m8ven-leads", help="re-read new M8ven listings from GitHub or the endpoint "
                                           "(only with AGENT_TOOLS_M8VEN_LEADS=1)")
    p.add_argument("--baseline", nargs="?", const="", default=None, metavar="FILE",
                   help="mark the current listings (or the JSON list in FILE) as seen, unread")
    p.add_argument("--dry-run", action="store_true", help="count new listings, read nothing else")
    p.add_argument("--max-pages", type=int, default=MAX_PAGES)


def run_cli(args) -> int:
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            print(json.dumps({"baseline_added": record_baseline(json.load(f))}))
        return 0
    if not enabled():
        log.info("m8ven leads: off (AGENT_TOOLS_M8VEN_LEADS is not 1)")
        return 0
    with httpx.Client(headers={"User-Agent": crawlers.UA}, timeout=60, follow_redirects=True) as client:
        get = paced(client)
        if args.baseline is not None:
            print(json.dumps({"baseline_added": record_baseline(listing_urls(get))}))
            return 0
        if args.dry_run:
            with closing(db.connect(read_only=True)) as c:
                try:
                    seen = {k for (k,) in c.execute("SELECT key FROM m8ven_leads WHERE kind='listing'")}
                except sqlite3.OperationalError:
                    seen = set()
            new = listing_urls(get) - seen
            print(json.dumps({"seen": len(seen), "new": len(new),
                              "new_repos": sum(1 for u in new if len(_listing_parts(u)) == 2),
                              "new_pages": sum(1 for u in new if len(_listing_parts(u)) == 1)}))
            return 0
        from . import jobs
        run_id = jobs._start_run(RUN)
        try:
            out = run(get, max_pages=args.max_pages)
        except Exception as e:  # noqa: BLE001 - recorded on the run; the watchdog reads it
            jobs._finish_run(run_id, 0, 0, [("%s: %s" % (type(e).__name__, e))[:300]], status="error")
            log.warning("m8ven leads failed: %r", e)
            return 1
    jobs._finish_run(run_id, out["added"], out["updated"], out["errors"],
                     status="ok" if not out["errors"] else "partial")
    log.info("m8ven leads: %s", json.dumps(out, sort_keys=True))
    return 0
