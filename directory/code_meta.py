"""Package metadata from the official registries.

ecosyste.ms is only used to *find* code-only MCP packages (its keyword index).
What we show for a package -- description, licence, repository, homepage,
keywords -- is read from the package's own registry (npm, PyPI, crates.io,
NuGet, RubyGems; the Go module proxy and Maven Central only confirm that the
package exists), so it is under that registry's terms, not ecosyste.ms's
CC BY-SA 4.0 data licence. Packages the registry no longer has are dropped;
packages whose registry could not be read keep the ecosyste.ms data (and the
CC BY-SA credit on /mcp/code, /about and /terms).

Results are cached in ``code_pkg_meta`` and re-read after REFRESH_DAYS.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger(__name__)

UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
REFRESH_DAYS = 30
RUN_BUDGET = 4000            # registry reads per crawl run (new packages first)
RATE = {"registry.npmjs.org": 0.25, "pypi.org": 0.1, "crates.io": 1.05,
        "azuresearch-usnc.nuget.org": 0.2, "rubygems.org": 0.2,
        "proxy.golang.org": 0.1, "search.maven.org": 0.5}
REGISTRIES = ("npm", "pypi", "cargo", "go", "maven", "nuget", "rubygems")

_pace: dict[str, float] = {}
_pace_lock = threading.Lock()


def _wait(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    gap = RATE.get(host)
    if not gap:
        return
    with _pace_lock:
        now = time.time()
        nxt = max(now, _pace.get(host, 0.0))
        _pace[host] = nxt + gap
    if nxt > now:
        time.sleep(nxt - now)


def _get(url: str, timeout: int = 30) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(6):
        _wait(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                ra = (e.headers or {}).get("Retry-After")
                time.sleep(min(120, int(ra)) if ra and ra.isdigit() else 15 * (attempt + 1))
                continue
            return e.code, None
        except Exception:  # noqa: BLE001 - network: retry, then give up
            if attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            return 0, None
    return 0, None


def _lic(v):
    if isinstance(v, dict):
        v = v.get("type")
    return v[:80] if isinstance(v, str) else None


def _npm(name):
    st, d = _get("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@") + "/latest")
    if not d:
        return st, None
    repo = d.get("repository")
    repo = repo.get("url") if isinstance(repo, dict) else repo
    kw = d.get("keywords")
    return st, {"description": d.get("description"), "license": _lic(d.get("license")),
                "repository": repo if isinstance(repo, str) else None,
                "homepage": d.get("homepage") if isinstance(d.get("homepage"), str) else None,
                "keywords": [k for k in kw if isinstance(k, str)] if isinstance(kw, list) else None}


def _pypi(name):
    st, d = _get("https://pypi.org/pypi/" + urllib.parse.quote(name) + "/json")
    if not d:
        return st, None
    i = d.get("info") or {}
    urls = i.get("project_urls") or {}
    repo = next((u for k, u in urls.items()
                 if k.lower() in ("source", "repository", "code", "github", "source code")), None)
    kw = i.get("keywords")
    return st, {"description": i.get("summary"),
                "license": i.get("license_expression") or _lic(i.get("license")),
                "repository": repo, "homepage": i.get("home_page") or urls.get("Homepage"),
                "keywords": [k.strip() for k in kw.replace(";", ",").split(",") if k.strip()]
                if isinstance(kw, str) else None}


def _cargo(name):
    st, d = _get("https://crates.io/api/v1/crates/" + urllib.parse.quote(name))
    if not d:
        return st, None
    c = d.get("crate") or {}
    v = (d.get("versions") or [{}])[0]
    return st, {"description": c.get("description"), "license": _lic(v.get("license")),
                "repository": c.get("repository"), "homepage": c.get("homepage"),
                "keywords": c.get("keywords")}


def _nuget(name):
    st, d = _get("https://azuresearch-usnc.nuget.org/query?" + urllib.parse.urlencode(
        {"q": f"packageid:{name}", "prerelease": "true", "take": 1}))
    rows = (d or {}).get("data") or []
    if not rows:
        return (404 if st == 200 else st), None
    p = rows[0]
    return st, {"description": p.get("description"), "license": None, "repository": None,
                "homepage": p.get("projectUrl"), "keywords": p.get("tags")}


def _rubygems(name):
    st, d = _get("https://rubygems.org/api/v1/gems/" + urllib.parse.quote(name) + ".json")
    if not d:
        return st, None
    return st, {"description": d.get("info"), "license": ", ".join(d.get("licenses") or [])[:80] or None,
                "repository": d.get("source_code_uri"), "homepage": d.get("homepage_uri"),
                "keywords": None}


_EMPTY = {"description": None, "license": None, "repository": None, "homepage": None, "keywords": None}


def _go(name):
    # The module proxy confirms the module exists; Go modules carry no description.
    st, d = _get("https://proxy.golang.org/" + urllib.parse.quote(name.lower()) + "/@latest")
    return st, (dict(_EMPTY) if d else None)


def _maven(name):
    g, _, a = name.partition(":")
    st, d = _get("https://search.maven.org/solrsearch/select?" + urllib.parse.urlencode(
        {"q": f'g:"{g}" AND a:"{a}"', "rows": 1, "wt": "json"}))
    docs = ((d or {}).get("response") or {}).get("docs") or []
    return (st if docs else (404 if st == 200 else st)), (dict(_EMPTY) if docs else None)


FETCH = {"npm": _npm, "pypi": _pypi, "cargo": _cargo, "nuget": _nuget,
         "rubygems": _rubygems, "go": _go, "maven": _maven}


def fetch(registry: str, name: str) -> tuple[int, dict | None]:
    return FETCH[registry](name)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _ensure(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS code_pkg_meta ("
              "registry TEXT NOT NULL, name TEXT NOT NULL, status INTEGER NOT NULL, "
              "meta TEXT, checked_at INTEGER NOT NULL, PRIMARY KEY (registry, name))")


def cached() -> dict[tuple[str, str], tuple[int, dict | None, int]]:
    with db.connect(read_only=True) as c:
        try:
            return {(r, n): (st, json.loads(m) if m else None, ts) for r, n, st, m, ts in c.execute(
                "SELECT registry, name, status, meta, checked_at FROM code_pkg_meta")}
        except Exception:  # noqa: BLE001 - table not created yet
            return {}


def store(results: list[tuple[str, str, int, dict | None, int]]) -> None:
    for i in range(0, len(results), 1000):
        chunk = [(r, n.lower(), st, json.dumps(m, ensure_ascii=False) if m else None, ts)
                 for r, n, st, m, ts in results[i:i + 1000]]

        def op(chunk=chunk):
            with db.writer() as c:
                _ensure(c)
                c.executemany(
                    "INSERT INTO code_pkg_meta(registry, name, status, meta, checked_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(registry, name) DO UPDATE SET "
                    "status=excluded.status, meta=excluded.meta, checked_at=excluded.checked_at",
                    chunk)
        db.with_retry(op)


def refresh(keys: list[tuple[str, str]], have: dict, *, budget: int = RUN_BUDGET,
            workers: int = 6) -> int:
    """Read packages missing from the cache (first) or older than
    REFRESH_DAYS, at most `budget` of them."""
    from concurrent.futures import ThreadPoolExecutor
    stale_before = time.time() - REFRESH_DAYS * 86400
    missing = [k for k in keys if (k[0], k[1].lower()) not in have]
    stale = sorted((k for k in keys if (k[0], k[1].lower()) in have
                    and have[(k[0], k[1].lower())][2] < stale_before),
                   key=lambda k: have[(k[0], k[1].lower())][2])
    todo = (missing + stale)[:budget]
    if not todo:
        return 0

    def one(k):
        st, meta = fetch(k[0], k[1])
        return k[0], k[1], st, meta, int(time.time())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        res = list(pool.map(one, todo))
    store(res)
    for r, n, st, m, ts in res:
        have[(r, n.lower())] = (st, m, ts)
    return len(res)


# ---------------------------------------------------------------------------
# Apply to crawl rows
# ---------------------------------------------------------------------------

def from_ecosystems(row: dict) -> bool:
    """eco_row() rows carry package_download_count; no other package source does."""
    return row.get("source") in REGISTRIES and "package_download_count" in row


def apply_official(rows: list[dict], *, budget: int = RUN_BUDGET,
                   keep_unread: bool = True) -> tuple[list[dict], dict]:
    """Replace ecosyste.ms metadata with the official registry's; drop packages
    the registry no longer has. With keep_unread=False, packages whose registry
    has not been read yet are held back (retried next run), so no ecosyste.ms
    text is ever stored. Returns (rows, counts)."""
    from . import code_mcp
    eco = [r for r in rows if from_ecosystems(r)]
    have = cached()
    fetched = refresh([(r["package_registry"], r["package_name"]) for r in eco], have,
                      budget=budget) if budget > 0 else 0
    out, counts = [], {"official": 0, "gone": 0, "unread": 0, "fetched": fetched}
    for r in rows:
        if not from_ecosystems(r):
            out.append(r)
            continue
        st, meta, _ts = have.get((r["package_registry"], r["package_name"].lower()), (0, None, 0))
        if st == 404 or st == 410:
            counts["gone"] += 1
            continue
        if st != 200 or meta is None:
            counts["unread"] += 1
            if keep_unread:            # e.g. for classification only
                out.append(r)
            continue
        counts["official"] += 1
        repo_url = meta.get("repository")
        repo = code_mcp.github_repo(repo_url)
        page = code_mcp.package_page(r["package_registry"], r["package_name"])
        r = dict(r)
        r["description"] = (meta.get("description") or "").strip()[:1000] or None
        r["license"] = meta.get("license")
        r["source_code_url"] = f"https://github.com/{repo}" if repo else (
            repo_url.strip() if isinstance(repo_url, str) and repo_url.startswith("http") else None)
        r["homepage_url"] = (meta.get("homepage") or "").strip() or r["source_code_url"] or page or r.get("homepage_url")
        kw = meta.get("keywords")
        r["tags"] = [k for k in kw if isinstance(k, str)][:12] or None if isinstance(kw, list) else None
        r["package_download_count"] = None     # ecosyste.ms figure; npm downloads come from npm itself
        out.append(r)
    return out, counts


def drop_gone() -> int:
    """Delete stored package rows whose registry answered 404 (owner-claimed
    rows are kept)."""
    with db.connect(read_only=True) as c:
        try:
            gone = {(r, n) for r, n in c.execute(
                "SELECT registry, name FROM code_pkg_meta WHERE status IN (404, 410)")}
        except Exception:  # noqa: BLE001
            return 0
        ids = [rid for rid, s, pkg in c.execute(
            f"SELECT id, source, package_name FROM mcp_servers WHERE source IN "
            f"({','.join('?' * len(REGISTRIES))}) AND COALESCE(endpoint_url,'')='' "
            "AND COALESCE(owner_verified,0)=0", REGISTRIES)
            if (s, (pkg or "").lower()) in gone]
    for i in range(0, len(ids), 500):
        chunk = [(x,) for x in ids[i:i + 500]]

        def op(chunk=chunk):
            with db.writer() as c:
                c.executemany("DELETE FROM listing_sources WHERE kind='mcp' AND listing_id=?", chunk)
                c.executemany("DELETE FROM mcp_servers WHERE id=? AND COALESCE(owner_verified,0)=0", chunk)
        db.with_retry(op)
    return len(ids)


def clear_ecosystems_downloads(keys: list[tuple[str, str]]) -> int:
    """One-off: blank the ecosyste.ms download figures already stored."""
    n = 0
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                before = c.total_changes
                c.executemany("UPDATE mcp_servers SET package_download_count=NULL "
                              "WHERE source=? AND source_id=?", chunk)
                return c.total_changes - before
        n += db.with_retry(op)
    return n
