"""Package metadata from the official registries.

ecosyste.ms is only used to *find* code-only MCP packages (its keyword index).
What we show for a package -- description, licence, repository, homepage,
keywords -- is read from the package's own registry (npm, PyPI, crates.io,
NuGet, RubyGems; the Go module proxy and Maven Central only confirm that the
package exists), so it is under that registry's terms, not ecosyste.ms's
CC BY-SA 4.0 data licence. Packages whose registry has not been read yet are
held back (no ecosyste.ms text is ever stored).

A package counts as gone only after two "not found" answers at least
GONE_CONFIRM_SECONDS apart; then its rows (from any source) are dropped.
Failed reads (timeouts, 429, 5xx) are not cached. Results are cached in
``code_pkg_meta`` and re-read after REFRESH_DAYS (+ a per-package spread).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger(__name__)

UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud/bot)"
REFRESH_DAYS = 30
REFRESH_SPREAD_DAYS = 10     # per-package offset so a bulk import does not expire at once
GONE_CONFIRM_SECONDS = 86400
RUN_BUDGET = 4000            # registry reads per crawl run (new packages first)
RATE = {"registry.npmjs.org": 0.25, "pypi.org": 0.1, "crates.io": 1.05,
        "azuresearch-usnc.nuget.org": 0.2, "api.nuget.org": 0.2, "rubygems.org": 0.2,
        "proxy.golang.org": 0.1, "repo1.maven.org": 0.3}
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


def _get(url: str, timeout: int = 30, raw: bool = False) -> tuple[int, dict | str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(6):
        _wait(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, (r.read().decode("utf-8", "replace") if raw else json.load(r))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                ra = (e.headers or {}).get("Retry-After")
                pause = min(120, int(ra)) if ra and ra.isdigit() else 15 * (attempt + 1)
                if e.code == 429:       # the whole host backs off, not just this thread
                    host = urllib.parse.urlsplit(url).hostname or ""
                    with _pace_lock:
                        _pace[host] = max(_pace.get(host, 0.0), time.time() + pause)
                time.sleep(pause)
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
        {"q": f"packageid:{name}", "prerelease": "true", "semVerLevel": "2.0.0", "take": 1}))
    rows = (d or {}).get("data") or []
    if not rows:
        if st != 200:
            return st, None
        # Search hides unlisted / not-yet-indexed packages: the flat container
        # (every published version) is the authority on existence.
        st2, _ = _get("https://api.nuget.org/v3-flatcontainer/"
                      + urllib.parse.quote(name.lower()) + "/index.json")
        return st2, (dict(_EMPTY) if st2 == 200 else None)
    p = rows[0]
    return st, {"description": p.get("description"), "license": None, "repository": None,
                "homepage": p.get("projectUrl"), "keywords": p.get("tags")}


def _rubygems(name):
    st, d = _get("https://rubygems.org/api/v1/gems/" + urllib.parse.quote(name) + ".json")
    if not d:
        return st, None
    lic = ", ".join(x for x in (d.get("licenses") or []) if isinstance(x, str))
    return st, {"description": d.get("info"), "license": lic[:80] or None,
                "repository": d.get("source_code_uri"), "homepage": d.get("homepage_uri"),
                "keywords": None}


_EMPTY = {"description": None, "license": None, "repository": None, "homepage": None, "keywords": None}


def go_escape(path: str) -> str:
    """Module proxy case-encoding: each upper-case letter becomes '!' + lower."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group().lower(), path)


_GO_NOT_A_SERVER = re.compile(r"/(test|tests|testdata|example|examples|_examples|fixtures?)(/|$)", re.I)


def _go(name):
    """The module proxy confirms the module exists; Go modules carry no
    description. The proxy also serves any repository without a go.mod (a
    synthesized one with only the module line): that is not a Go package, and
    neither is a module under a test / example directory -- both count as
    not found."""
    if _GO_NOT_A_SERVER.search(name):
        return 404, None
    base = "https://proxy.golang.org/" + urllib.parse.quote(go_escape(name), safe="/!")
    st, d = _get(base + "/@latest")
    if st != 200 or not isinstance(d, dict) or not d.get("Version"):
        return st if st != 200 else 0, None
    st2, mod = _get(base + "/@v/" + urllib.parse.quote(d["Version"], safe="") + ".mod", raw=True)
    if st2 != 200 or not isinstance(mod, str):
        return st2 if st2 != 200 else 0, None
    if not re.search(r"^\s*(go|require)\s", mod, re.M):
        return 404, None
    return 200, dict(_EMPTY)


def _maven(name):
    # search.maven.org's index stopped following new releases; the repository's
    # own maven-metadata.xml is the authority on existence.
    parts = name.split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return 0, None              # not group:artifact -- never call it gone
    g, a = parts[0], parts[1]
    st, _ = _get("https://repo1.maven.org/maven2/" + g.replace(".", "/") + "/"
                 + urllib.parse.quote(a) + "/maven-metadata.xml", raw=True)
    return st, (dict(_EMPTY) if st == 200 else None)


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
        db.with_retry(op, attempts=10, base_delay=1.0)


def is_gone(entry) -> bool:
    """Two "not found" answers at least GONE_CONFIRM_SECONDS apart."""
    return bool(entry and entry[0] in (404, 410) and (entry[1] or {}).get("confirmed"))


def _due(key: tuple[str, str], entry, now: float) -> float | None:
    """When a cached entry should be read again (None: not cached)."""
    if entry is None:
        return None
    st, meta, ts = entry
    if st in (404, 410) and not (meta or {}).get("confirmed"):
        return ts + GONE_CONFIRM_SECONDS
    spread = zlib.crc32(f"{key[0]}:{key[1]}".encode()) % (REFRESH_SPREAD_DAYS * 86400)
    return ts + REFRESH_DAYS * 86400 + spread


def _record(key, st, meta, prev, now):
    """The cache entry for a read, or None when the read failed (not cached)."""
    if st == 200 and meta is not None:
        return 200, meta
    if st in (404, 410):
        first = now
        if prev and prev[0] in (404, 410):
            first = (prev[1] or {}).get("first_404") or prev[2]
        return st, {"first_404": first, "confirmed": now - first >= GONE_CONFIRM_SECONDS}
    return None


def refresh(keys: list[tuple[str, str]], have: dict, *, budget: int = RUN_BUDGET,
            workers: int = 6) -> int:
    """Read packages missing from the cache (first), then due re-checks of
    "not found" answers, then entries past their refresh date; at most
    `budget` reads. Failed reads are not cached (retried next run)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    now = time.time()
    seen, missing, due = set(), [], []
    for k in keys:
        kk = (k[0], k[1].lower())
        if kk in seen:
            continue
        seen.add(kk)
        when = _due(kk, have.get(kk), now)
        if when is None:
            missing.append(k)
        elif when <= now:
            due.append((when, k))
    todo = (missing + [k for _w, k in sorted(due, key=lambda x: x[0])])[:budget]
    if not todo:
        return 0

    def one(k):
        try:
            st, meta = fetch(k[0], k[1])
        except Exception as e:  # noqa: BLE001 - one odd answer must not sink the batch
            log.warning("code meta: %s %s: %r", k[0], k[1], e)
            st, meta = 0, None
        return k, st, meta

    batch: list = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(one, k) for k in todo]):
            (reg, name), st, meta = fut.result()
            kk = (reg, name.lower())
            t = int(time.time())
            rec = _record(kk, st, meta, have.get(kk), t)
            if rec is None:
                continue
            have[kk] = (rec[0], rec[1], t)
            batch.append((reg, name, rec[0], rec[1], t))
            if len(batch) >= 200:
                store(batch)
                batch = []
    if batch:
        store(batch)
    return len(todo)


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
        key = ((r.get("package_registry") or r.get("source") or "").lower(),
               (r.get("package_name") or "").lower())
        entry = have.get(key) if r.get("source") in REGISTRIES and key[1] else None
        if is_gone(entry):          # from any directory: the package is gone
            counts["gone"] += 1
            continue
        if not from_ecosystems(r):
            out.append(r)
            continue
        st, meta, _ts = entry or (0, None, 0)
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
        if not repo and r["package_registry"] == "go":
            repo = code_mcp.github_repo("https://" + r["package_name"])   # github.com/o/r[/...]
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
    """Delete stored package rows whose package is confirmed gone (two "not
    found" answers a day apart). Owner-claimed and remote rows are kept."""
    with db.connect(read_only=True) as c:
        try:
            gone = {(r, n) for r, n, m in c.execute(
                "SELECT registry, name, meta FROM code_pkg_meta WHERE status IN (404, 410)")
                if m and json.loads(m).get("confirmed")}
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
                c.executemany("DELETE FROM mcp_servers WHERE id=? AND COALESCE(owner_verified,0)=0 "
                              "AND COALESCE(endpoint_url,'')=''", chunk)
        db.with_retry(op, attempts=10, base_delay=1.0)
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
