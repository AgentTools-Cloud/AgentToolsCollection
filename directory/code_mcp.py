"""Code-only MCP servers: repositories and packages with no remote endpoint.

Public sources, merged by GitHub repository before ingestion:

* GitHub repositories tagged ``mcp-server``, ``model-context-protocol``,
  ``modelcontextprotocol``, ``mcp-tools`` and (when the name or description
  says MCP) ``mcp`` (search API, split by creation date because one query
  returns at most 1,000 results);
* npm, PyPI, Cargo, Go, Maven, NuGet and RubyGems packages with the keywords
  ``mcp-server``, ``model-context-protocol``, ``modelcontextprotocol`` and
  (filtered the same way) ``mcp`` from ecosyste.ms (complete keyword index,
  with first-release dates), plus npm's own search (which stops returning
  new results after ~5,250 of its ~11,500 matches);
* directories that point at repositories / packages (official MCP Registry,
  Cline, ModelScope, Docker catalog, Baidu MCP World): see code_sources.

Every row is ``kind='catalog'`` with ``endpoint_url=None``: it belongs to the
code-only sub-directory (/mcp/code), is never probed and never counted in the
remote MCP statistics. A server that also has a hosted endpoint keeps both
listings: the remote one for the endpoint, this one for the code. Repositories
that remote listings link are looked up too (HOSTED_LOOKUPS a run). Only a
repository or package another directory already lists as code-only is skipped.
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
GITHUB_TOPICS = ("mcp-server", "model-context-protocol", "modelcontextprotocol",
                 "mcp-tools", "mcp")
NPM_KEYWORD = "mcp-server"
ECO_KEYWORDS = ("mcp-server", "model-context-protocol", "modelcontextprotocol", "mcp")
# Generic tags: kept only when the name or description itself says MCP.
NOISY = {"mcp"}
ECOSYSTEMS_URL = "https://packages.ecosyste.ms/api/v1/keywords/{kw}"
# ecosyste.ms ecosystem -> our package_registry
ECOSYSTEMS = {"npm": "npm", "pypi": "pypi", "cargo": "cargo", "go": "go",
              "maven": "maven", "nuget": "nuget", "rubygems": "rubygems"}
PACKAGE_SOURCES = tuple(dict.fromkeys([*ECOSYSTEMS.values(), "oci", "mcpb"]))
OWN_SOURCES = ("github", *PACKAGE_SOURCES)
PACKAGE_PAGES = {
    "npm": "https://www.npmjs.com/package/{pkg}",
    "pypi": "https://pypi.org/project/{pkg}/",
    "cargo": "https://crates.io/crates/{pkg}",
    "go": "https://pkg.go.dev/{pkg}",
    "nuget": "https://www.nuget.org/packages/{pkg}",
    "rubygems": "https://rubygems.org/gems/{pkg}",
}
SEARCH_CAP = 1000          # GitHub search returns at most 1,000 results per query
GITHUB_FIRST_DAY = date(2024, 11, 1)   # MCP was announced 2024-11-25
NPM_PAGE = 250
NPM_MAX = 20000
# Repositories linked by hosted listings, looked up per run (30 per GitHub
# search request): the ~16,500 at the switch take three nights, then a few a day.
HOSTED_LOOKUPS = 6000


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

# Search allows 30 (authenticated) or 10 requests per minute. Pace by request
# start, not by sleeping after each answer: a 100-item page takes 1-2 s to
# arrive, and sleeping 2.2 s afterwards used only ~13 of the 30 (11-16).
SEARCH_INTERVAL = {True: 2.05, False: 6.1}
_search_last = [0.0]


def _github_search(q: str, page: int, auth: str | None) -> tuple[dict, dict]:
    url = ("https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page}))
    headers = {"Accept": "application/vnd.github+json"}
    if auth:
        headers["Authorization"] = auth
    for attempt in range(4):
        wait = _search_last[0] + SEARCH_INTERVAL[bool(auth)] - time.time()
        if wait > 0:
            time.sleep(wait)
        _search_last[0] = time.time()
        try:
            data, h = _get(url, headers)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 3:
                reset = int((e.headers or {}).get("X-RateLimit-Reset") or 0)
                time.sleep(min(90, max(10, reset - time.time() + 2)))
                continue
            raise
        # Quota shared with other jobs: wait for the window to reset when spent.
        if int(h.get("X-RateLimit-Remaining", "1") or 1) <= 1:
            reset = int(h.get("X-RateLimit-Reset") or 0)
            time.sleep(min(90, max(2, reset - time.time() + 2)))
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
                    out[repo] = slim_github(item)
            if len(data.get("items") or []) < 100:
                break
        if max_repos and len(out) >= max_repos:
            break
    log.info("code mcp: github topic:%s repos=%d", topic, len(out))
    return list(out.values())


def fetch_github_topics(topics=GITHUB_TOPICS, **kw) -> list[dict]:
    """Union of the topic crawls; generic topics only where the repository
    name or description says MCP."""
    from .code_sources import mentions_mcp
    out: dict[str, dict] = {}
    for topic in topics:
        for item in fetch_github(topic, **kw):
            repo = (item.get("full_name") or "").lower()
            if not repo or repo in out:
                continue
            if topic in NOISY and not mentions_mcp(item.get("name"), item.get("description")):
                continue
            out[repo] = item
    return list(out.values())


# Only what github_row() / the filters read: a full search item is ~100 keys,
# and a full crawl holds 200k+ of them.
GITHUB_FIELDS = ("full_name", "name", "html_url", "homepage", "description",
                 "stargazers_count", "topics", "pushed_at", "created_at")
ECO_FIELDS = ("ecosystem", "name", "description", "repository_url", "homepage",
              "registry_url", "licenses", "downloads", "keywords_array",
              "latest_release_published_at", "first_release_published_at")


def slim_github(item: dict) -> dict:
    out = {k: item.get(k) for k in GITHUB_FIELDS}
    out["license"] = {"spdx_id": (item.get("license") or {}).get("spdx_id")}
    return out


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
                out.setdefault(p["name"].lower(), {"package": {
                    k: p.get(k) for k in ("name", "description", "links", "keywords",
                                          "license", "date")}})
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
                out.setdefault((reg, p["name"].lower()), {k: p.get(k) for k in ECO_FIELDS})
    from collections import Counter
    log.info("code mcp: ecosyste.ms keyword:%s %s", keyword,
             dict(Counter(r for r, _ in out)))
    return list(out.values())


def fetch_ecosystems_all(keywords=ECO_KEYWORDS) -> list[dict]:
    """eco_row() rows for every keyword; generic keywords only where the
    package name or description says MCP."""
    from .code_sources import mentions_mcp
    out: dict[tuple[str, str], dict] = {}
    for kw in keywords:
        for p in fetch_ecosystems(kw):
            row = eco_row(p)
            key = (row["package_registry"], row["source_id"])
            if key in out:
                continue
            if kw in NOISY and not mentions_mcp(row["name"], row.get("description")):
                continue
            out[key] = row
    return list(out.values())


def eco_row(p: dict) -> dict:
    reg = ECOSYSTEMS[p["ecosystem"]]
    name = p["name"]
    repo = github_repo(p.get("repository_url"))
    repo_url = f"https://github.com/{repo}" if repo else ((p.get("repository_url") or "").strip() or None)
    page = (package_page(reg, name) or (p.get("registry_url") or "").strip()
            or repo_url or f"https://packages.ecosyste.ms/registries")
    lic = p.get("licenses") if isinstance(p.get("licenses"), str) else None
    owner, short = _owner_name(reg, name)
    row = {
        "slug": _slug(short, owner),
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


def package_page(reg: str, pkg: str) -> str | None:
    if reg == "maven" and ":" in pkg:
        group, artifact = pkg.split(":", 1)
        return f"https://central.sonatype.com/artifact/{group}/{artifact}"
    if reg == "oci":
        ref = pkg.split("/", 1)[1] if pkg.startswith("docker.io/") else pkg
        return f"https://hub.docker.com/r/{ref}" if "." not in ref.split("/")[0] else None
    template = PACKAGE_PAGES.get(reg)
    return template.format(pkg=pkg) if template else None


def _owner_name(reg: str, name: str) -> tuple[str, str]:
    """(owner, short name) for slugs: @scope/pkg, group:artifact, host/owner/mod."""
    if reg == "maven" and ":" in name:
        group, artifact = name.split(":", 1)
        return group.split(".")[-1], artifact
    parts = [p for p in name.split("/") if p]
    if len(parts) >= 2:
        return parts[-2].lstrip("@"), parts[-1]
    return reg, name


# ---------------------------------------------------------------------------
# Skip unchanged rows: a daily run re-reads ~70k rows and upserting each one
# costs ~60 ms (an hour a day). code_row_digest remembers what was written.
# ---------------------------------------------------------------------------

def row_digest(row: dict) -> str:
    import hashlib
    body = {k: v for k, v in row.items() if k not in ("created_at", "updated_at", "last_seen")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:20]


def _ensure_digest_table(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS code_row_digest ("
              "source TEXT NOT NULL, source_id TEXT NOT NULL, digest TEXT NOT NULL, "
              "PRIMARY KEY (source, source_id))")


def unchanged_rows(rows: list[dict]) -> set[tuple[str, str]]:
    """(source, source_id) of rows stored with exactly this content."""
    with db.connect(read_only=True) as c:
        try:
            have = {(s, sid): d for s, sid, d in c.execute(
                "SELECT source, source_id, digest FROM code_row_digest")}
        except Exception:  # noqa: BLE001 - table not created yet
            return set()
        stored = {(s, sid) for s, sid in c.execute(
            f"SELECT source, source_id FROM mcp_servers WHERE source IN "
            f"({','.join('?' * len(OWN_SOURCES))}) AND COALESCE(endpoint_url,'')=''",
            OWN_SOURCES)}
    out = set()
    for r in rows:
        k = (r["source"], r["source_id"])
        if k in stored and have.get(k) == row_digest(r):
            out.add(k)
    return out


def remember_rows(rows: list[dict]) -> None:
    items = [(r["source"], r["source_id"], row_digest(r)) for r in rows]
    for i in range(0, len(items), 2000):
        chunk = items[i:i + 2000]

        def op(chunk=chunk):
            with db.writer() as c:
                _ensure_digest_table(c)
                c.executemany(
                    "INSERT INTO code_row_digest(source, source_id, digest) VALUES (?,?,?) "
                    "ON CONFLICT(source, source_id) DO UPDATE SET digest=excluded.digest", chunk)
        db.with_retry(op)


def known_repos(conn) -> tuple[set[str], set[str]]:
    """(repos another directory lists as code-only, (registry, package) another
    directory lists as code-only).

    Rows from our own code crawlers are excluded so their re-crawl updates
    them instead of skipping them. Hosted listings are excluded too: a server
    with both an endpoint and code is listed in both sub-directories."""
    repos, packages = set(), set()
    placeholders = ",".join("?" * len(OWN_SOURCES))
    for hp, sc, reg, pkg in conn.execute(
            "SELECT homepage_url, source_code_url, package_registry, package_name "
            f"FROM mcp_servers WHERE COALESCE(source,'') NOT IN ({placeholders}) "
            "AND COALESCE(endpoint_url,'')=''", OWN_SOURCES):
        for u in (sc, hp):
            r = github_repo(u)
            if r:
                repos.add(r)
        if pkg and (reg or "").lower() in PACKAGE_SOURCES:
            packages.add(((reg or "").lower(), pkg.lower()))
    return repos, packages


def hosted_repos(conn) -> set[str]:
    """GitHub repositories that hosted (remote) listings link."""
    repos = set()
    for urls in conn.execute("SELECT source_code_url, homepage_url FROM mcp_servers "
                             "WHERE COALESCE(endpoint_url,'')!=''"):
        repos.update(r for r in map(github_repo, urls) if r)
    return repos


_PLATFORM_PKG = re.compile(r"(^|[-_.])(linux|darwin|macos|win32|windows|arm64|x64|amd64|aarch64|musl|gnu)([-_.]|$)", re.I)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _core(s: str) -> str:
    s = re.sub(r"^mcp-", "", s)
    return re.sub(r"(-mcp-server|-mcp|-server)$", "", s) or s


def package_matches_repo(registry: str | None, pkg: str | None, repo: str | None) -> bool:
    """Is `pkg` this repository's own package, not a mirror that copied its
    repository URL? Re-published packages keep the original URL but use
    another scope (@mseep/..., @iflow-mcp/...) or a prefixed name, and some
    packages are per-platform binaries. Go modules must be the repository
    root (or a /vN major version)."""
    if not pkg or not repo or "/" not in repo:
        return False
    reg = (registry or "").lower()
    if reg == "go":
        p, root = pkg.lower().rstrip("/"), "github.com/" + repo.lower()
        return p == root or re.fullmatch(re.escape(root) + r"/v\d+", p) is not None
    owner, name = (_norm(x) for x in repo.split("/", 1))
    scope, base = "", pkg
    if pkg.startswith("@") and "/" in pkg:
        scope, base = pkg[1:].split("/", 1)
    elif reg == "maven" and ":" in pkg:
        scope, base = pkg.split(":", 1)[0], pkg.split(":")[1]
    if _PLATFORM_PKG.search(base):
        return False
    base_n, scope_n = _norm(base), _norm(scope)
    if reg == "maven":
        return owner in scope_n.split("-") or _core(base_n) == _core(name)
    if scope_n:
        return (scope_n == owner or _core(f"{scope_n}-{base_n}") == _core(name)
                or _core(scope_n) == _core(name))
    if _core(base_n) == _core(name) or base_n == f"{owner}-{name}":
        return True
    # Same words in another order / with filler ("blender-mcp" for
    # "mcp-for-blender", "headroom-ai" for "headroom"); an extra word such as a
    # mirror's prefix ("mseep-...", "hk-...") or "-dev" / "-lite" is not.
    filler = {"mcp", "server", "ai", "py", "python", "cli", "js", "ts", "node", "for", "the", "of"}
    p_words = set(base_n.split("-")) - filler
    r_words = set(name.split("-")) - filler
    return bool(p_words) and p_words <= r_words | set(owner.split("-")) and r_words <= p_words


def stored_repos(conn) -> set[str]:
    """Repositories we already hold as our own GitHub rows."""
    return {r[0] for r in conn.execute("SELECT source_id FROM mcp_servers WHERE source='github'")}


def merge(github_items: list[dict], package_rows: list[dict],
          known: tuple[set[str], set], stored: set[str] = frozenset(),
          attach: list | None = None) -> tuple[list[dict], dict]:
    """One row per repository (GitHub first, enriched with its package);
    packages without a GitHub repository stay as package rows.

    `package_rows` are npm_row() / eco_row() rows (npm search objects are
    accepted too, for the fallback path). `stored` are repositories we hold
    as GitHub rows already but did not re-crawl this run (incremental): their
    packages are not listed again, only offered to `attach` as
    (repo, registry, package)."""
    known_repo, known_pkg = known
    rows: dict[str, dict] = {}
    skipped = {"linked_elsewhere": 0, "duplicate_package": 0, "stored_repo": 0,
               "foreign_package": 0}
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
            if not package_matches_repo(row["package_registry"], row["package_name"], repo):
                skipped["foreign_package"] += 1     # a mirror of this repository
            elif not gh.get("package_name"):
                gh["package_registry"], gh["package_name"] = row["package_registry"], row["package_name"]
            else:
                skipped["duplicate_package"] += 1
            continue
        if repo and repo in stored:
            skipped["stored_repo"] += 1
            if attach is not None and package_matches_repo(row["package_registry"], row["package_name"], repo):
                attach.append((repo, row["package_registry"], row["package_name"]))
            continue
        key = repo or (row["package_registry"], pkg)
        if key in npm_only:
            skipped["duplicate_package"] += 1   # monorepo: first package wins
            continue
        npm_only[key] = row
    out = list(rows.values()) + list(npm_only.values())
    return out, skipped


def fetch_dir() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(db.DEFAULT_DB_PATH)), "code-crawl")


def save_fetch(rows: list[dict], info: dict, meta: dict, keep: int = 3) -> str:
    """Write a fetch as gzip JSON lines (meta + info first), keep the last few."""
    import glob
    import gzip
    import os
    d = fetch_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("fetch-%Y%m%d-%H%M%S.jsonl.gz"))
    tmp = path + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"meta": meta, "info": info}, ensure_ascii=False, default=list) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    for old in sorted(glob.glob(os.path.join(d, "fetch-*.jsonl.gz")))[:-keep]:
        os.remove(old)
    return path


def load_fetch(path: str) -> tuple[list[dict], dict, dict]:
    import gzip
    with gzip.open(path, "rt", encoding="utf-8") as f:
        head = json.loads(f.readline())
        rows = [json.loads(line) for line in f if line.strip()]
    info = head.get("info") or {}
    info["attach"] = [tuple(a) for a in info.get("attach") or []]
    return rows, info, head.get("meta") or {}


# ---------------------------------------------------------------------------
# Parts fetched on another machine (only public, unauthenticated APIs: GitHub
# search at 10/min, ecosyste.ms, npm, Registry, Cline, ModelScope, Docker,
# Baidu). The helper writes parts/<name>.json.gz; the main run waits for it
# after its own GitHub topics and falls back to fetching it itself.
# ---------------------------------------------------------------------------

PART_WAIT_SECONDS = 6 * 3600
PART_MAX_AGE = 6 * 3600     # a part may be started up to 6 h before the main run


def parts_dir() -> str:
    import os
    return os.path.join(fetch_dir(), "parts")


def part_name(part: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "_", part.lower())


def fetch_part(part: str, *, baidu: bool = False, words: list[str] | None = None,
               keywords: tuple[str, ...] = ECO_KEYWORDS):
    """`github:<topic>`, `packages` or `extra`."""
    from . import code_sources
    if part.startswith("github:"):
        return fetch_github_topics(topics=(part.split(":", 1)[1],))
    if part == "packages":
        try:
            eco = fetch_ecosystems_all(keywords)
        except Exception as e:  # noqa: BLE001 - npm search alone
            log.warning("code mcp: ecosyste.ms failed (%r); npm search only", e)
            eco = []
        return {"eco": eco, "npm": [npm_row(o) for o in fetch_npm()]}
    if part == "extra":
        hints, rows, info = code_sources.fetch_extra(baidu=baidu, words=words)
        return {"hints": hints, "rows": rows, "info": info}
    raise ValueError(f"unknown part {part!r}")


def write_part(part: str, data, out_dir: str | None = None) -> str:
    import gzip
    import os
    d = out_dir or parts_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, part_name(part) + ".json.gz")
    with gzip.open(path + ".part", "wt", encoding="utf-8") as f:
        json.dump({"part": part, "written_at": int(time.time()), "data": data}, f,
                  ensure_ascii=False)
    os.replace(path + ".part", path)
    return path


def wait_part(part: str, since: float, timeout: float | None = None):
    """Data of a part written after `since`, or None after `timeout`."""
    import gzip
    import os
    path = os.path.join(parts_dir(), part_name(part) + ".json.gz")
    # One deadline for all parts, counted from the run's start (not per part).
    deadline = (since + PART_WAIT_SECONDS) if timeout is None else time.time() + timeout
    while True:
        if os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    doc = json.load(f)
                if doc.get("written_at", 0) >= since - PART_MAX_AGE:
                    log.info("code mcp: part %s loaded from %s", part, path)
                    return doc["data"]
            except (OSError, ValueError) as e:
                log.warning("code mcp: part %s unreadable (%r)", part, e)
        if time.time() >= deadline:
            log.warning("code mcp: part %s not delivered; fetching it here", part)
            return None
        time.sleep(60)


def fetch_all(*, github_limit: int | None = None, npm_limit: int = NPM_MAX,
              min_stars: int = 0, pushed_since: date | None = None,
              baidu: bool = False, remote_parts: tuple[str, ...] = ()) -> tuple[list[dict], dict]:
    from . import code_sources
    started = time.time()
    remote = set(remote_parts)
    local_topics = tuple(t for t in GITHUB_TOPICS if f"github:{t}" not in remote)
    gh = fetch_github_topics(local_topics, max_repos=github_limit, min_stars=min_stars,
                             pushed_since=pushed_since)
    seen_gh = {(i.get("full_name") or "").lower() for i in gh}
    for t in GITHUB_TOPICS:
        if f"github:{t}" not in remote:
            continue
        got = wait_part(f"github:{t}", started)
        if got is None:
            got = fetch_part(f"github:{t}")
        for i in got:
            repo = (i.get("full_name") or "").lower()
            if repo and repo not in seen_gh:
                seen_gh.add(repo)
                gh.append(i)
    pk = wait_part("packages", started) if "packages" in remote else None
    if pk is None:
        # The generic `mcp` keyword is ~140k packages and 45 min of paging;
        # daily runs read the specific keywords, weekly / full runs all of them.
        pk = fetch_part("packages", keywords=ECO_KEYWORDS if baidu else
                        tuple(k for k in ECO_KEYWORDS if k not in NOISY))
    eco = pk["eco"]
    have = {(r["package_registry"], r["source_id"]) for r in eco}
    eco_keys = set(have)
    npm = pk["npm"]
    packages = eco + [r for r in npm if ("npm", r["source_id"]) not in have]
    ex = wait_part("extra", started) if "extra" in remote else None
    if ex is None:
        ex = fetch_part("extra", baidu=baidu)
    hints, extra_rows, extra_info = ex["hints"], ex["rows"], ex["info"]
    with db.connect(read_only=True) as c:
        known = known_repos(c)
        stored = stored_repos(c)
        hosted = hosted_repos(c)
        have_npm = {r[0] for r in c.execute(
            "SELECT source_id FROM mcp_servers WHERE source='npm'")}
    # Repositories other directories list as MCP servers that we do not hold:
    # fetch them from GitHub (a few hundred a day once the backlog is done).
    # Repositories hosted listings link go the same way, HOSTED_LOOKUPS a run;
    # the classifier keeps the ones that are a server's code.
    crawled = {(i.get("full_name") or "").lower() for i in gh}
    todo = {r: v for r, v in hints.items()
            if r not in crawled and r not in stored and r not in known[0]}
    hosted -= crawled | stored | known[0] | set(hints)
    hosted_todo = 0
    if todo or hosted:
        checked = code_sources.recently_checked()   # once: it reads the whole table
        todo = {r: v for r, v in todo.items() if r not in checked}
        more = sorted(hosted - checked)[:HOSTED_LOOKUPS]
        todo.update(dict.fromkeys(more, "hosted-listing"))
        hosted_todo = len(more)
    looked = code_sources.lookup_repos(todo) if todo else []
    gh = gh + [i for i in looked if (i.get("full_name") or "").lower() not in crawled]
    have.update((r["package_registry"], r["source_id"]) for r in packages)
    packages += [r for r in extra_rows if (r["package_registry"], r["source_id"]) not in have]
    attach: list = []
    rows, skipped = merge(gh, packages, known, stored - crawled, attach)
    # npm search has no creation date: look it up only for search-only packages
    # we do not have yet (ecosyste.ms rows carry their first-release date).
    need = [r["package_name"] for r in rows
            if r["source"] == "npm" and not r.get("created_at") and r["source_id"] not in have_npm]
    created = npm_created(need) if need else {}
    for r in rows:
        if r["source"] == "npm" and not r.get("created_at"):
            r["created_at"] = created.get(r["package_name"])
    info = {"github": len(gh), "ecosystems": len(eco), "npm_search": len(npm),
            "npm_search_only": sum(1 for r in npm if ("npm", r["source_id"]) not in eco_keys),
            "rows": len(rows), "min_stars": min_stars,
            "pushed_since": pushed_since.isoformat() if pushed_since else None,
            "npm_created_lookups": len(need), "hinted_repos": len(hints),
            "hint_lookups": len(todo), "hosted_lookups": hosted_todo, "hint_found": len(looked),
            **extra_info, **skipped}
    log.info("code mcp merged: %s", info)
    info["attach"] = attach
    return rows, info
