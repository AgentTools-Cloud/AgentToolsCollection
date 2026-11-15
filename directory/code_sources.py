"""Extra discovery sources for code-only MCP servers.

Directories that mostly point at GitHub repositories or packages, used to
find servers the GitHub topic / package keyword crawls miss:

* the official MCP Registry -- servers that publish packages but no remote;
* the Cline MCP marketplace (GitHub URLs);
* ModelScope MCP Plaza listings without a public remote (GitHub link and/or
  an ``npx`` / ``uvx`` start command);
* Docker's MCP catalog -- local servers shipped as ``mcp/<name>`` images;
* Baidu MCP World (www.mcpworld.com), ~62k mirrored GitHub repositories. Its
  search returns at most 100 hits per query, so it is walked with a keyword
  list and only on full runs and once a week.

Each source yields *repository hints* (owner/repo that some directory lists
as an MCP server) and *package rows*. Repositories we do not hold yet are
looked up on GitHub (``repo:a repo:b ...`` search, many per request) so they
become ordinary GitHub rows dated by repository creation.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import time
import urllib.error
import urllib.parse

from . import db

log = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
CLINE_URL = "https://api.cline.bot/v1/mcp/marketplace"
BAIDU_URL = "https://www.mcpworld.com/api/mcp-market/servers"
BAIDU_PAGE = 25               # pn * pl may not exceed 100
BAIDU_MAX_REQUESTS = 8000
BAIDU_WORKERS = 4
LOOKUP_BATCH = 30             # repo: qualifiers per GitHub search request
LOOKUP_RECHECK_DAYS = 30      # repositories not found / not kept are retried after this
LOOKUP_MAX = 60000

# Registry package types we can list (registryType -> our package_registry).
REGISTRY_TYPES = {"npm": "npm", "pypi": "pypi", "oci": "oci", "nuget": "nuget",
                  "mcpb": "mcpb", "cargo": "cargo"}

_MCP_WORD = re.compile(r"mcp|model[\s_-]*context[\s_-]*protocol", re.I)


def mentions_mcp(*texts) -> bool:
    return any(_MCP_WORD.search(t or "") for t in texts)


def _cm():
    from . import code_mcp
    return code_mcp


# ---------------------------------------------------------------------------
# Official MCP Registry
# ---------------------------------------------------------------------------

def _registry_identifier(reg: str, ident: str) -> str:
    ident = (ident or "").strip()
    if reg == "oci":
        # docker.io/acme/server:1.2 -> docker.io/acme/server (tags change per release)
        head, _, tail = ident.rpartition("/")
        if ":" in tail:
            tail = tail.split(":", 1)[0]
        ident = f"{head}/{tail}" if head else tail
        ident = ident.split("@", 1)[0]
    return ident


def registry_rows(servers: list[dict]) -> tuple[dict, list[dict]]:
    """Latest version of every package-only server -> (repo hints, package rows).

    `servers` are /v0/servers entries (``{"server": ..., "_meta": ...}``); when
    several versions are given, the first publish of any version dates the row."""
    cm = _cm()
    first: dict[str, str] = {}
    latest: dict[str, dict] = {}
    for e in servers:
        s = e.get("server") or {}
        meta = (e.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
        name = s.get("name")
        if not name:
            continue
        pub = meta.get("publishedAt")
        if pub and (name not in first or pub < first[name]):
            first[name] = pub
        if meta.get("isLatest") and (meta.get("status") or "active") == "active":
            latest[name] = s
    hints: dict[str, str] = {}
    rows: list[dict] = []
    for name, s in latest.items():
        if s.get("remotes"):
            continue        # remote servers are listed by the remote crawler
        repo_url = ((s.get("repository") or {}).get("url") or "").strip() or None
        repo = cm.github_repo(repo_url)
        if repo and not (s.get("repository") or {}).get("subfolder"):
            hints.setdefault(repo, "mcp-registry")
        for p in s.get("packages") or []:
            reg = REGISTRY_TYPES.get((p.get("registryType") or "").lower())
            ident = _registry_identifier(reg or "", p.get("identifier") or "")
            if not reg or not ident:
                continue
            page = f"https://registry.modelcontextprotocol.io/v0/servers?search={urllib.parse.quote(name)}"
            row = {
                "slug": cm._slug(name.split("/")[-1], name.split("/")[0]),
                "name": s.get("title") or name,
                "description": (s.get("description") or "").strip()[:1000] or None,
                "homepage_url": (s.get("websiteUrl") or "").strip() or repo_url or page,
                "endpoint_url": None,
                "kind": "catalog",
                "source_code_url": f"https://github.com/{repo}" if repo else repo_url,
                "package_registry": reg,
                "package_name": ident,
                "tags": None,
                "source": reg,
                "source_id": ident.lower(),
                "source_url": cm.package_page(reg, ident) or page,
                "confidence": 0.6,
            }
            created = cm._epoch(first.get(name))
            if created:
                row["created_at"] = created
            rows.append(row)
            break           # one package per server: the first is the primary one
    return hints, rows


def fetch_registry(max_pages: int = 1500) -> list[dict]:
    """Latest version of every server (all versions are 200k+ entries).
    Rows without a GitHub repository are dated by that version's publish
    time; repository rows by the repository's creation."""
    cm = _cm()
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        q = {"limit": 100, "version": "latest"}
        if cursor:
            q["cursor"] = cursor
        data, _h = cm._get(REGISTRY_URL + "?" + urllib.parse.urlencode(q), timeout=60)
        for e in data.get("servers") or []:
            s = e.get("server") or {}
            out.append({"server": {k: s.get(k) for k in (
                "name", "title", "description", "repository", "websiteUrl", "packages", "remotes")},
                "_meta": e.get("_meta") or {}})
        cursor = (data.get("metadata") or {}).get("nextCursor")
        if not cursor:
            break
        time.sleep(0.2)
    log.info("code mcp: registry entries=%d", len(out))
    return out


# ---------------------------------------------------------------------------
# Cline marketplace
# ---------------------------------------------------------------------------

def fetch_cline() -> dict[str, str]:
    cm = _cm()
    data, _ = cm._get(CLINE_URL, timeout=60)
    hints = {}
    for item in data if isinstance(data, list) else []:
        repo = cm.github_repo(item.get("githubUrl"))
        if repo:
            hints.setdefault(repo, "cline")
    log.info("code mcp: cline repos=%d", len(hints))
    return hints


# ---------------------------------------------------------------------------
# ModelScope (stdio / GitHub listings)
# ---------------------------------------------------------------------------

def _config_list(value):
    if isinstance(value, (list, dict)):
        return value
    if not value or not isinstance(value, str):
        return []
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(value)
        except (ValueError, SyntaxError):
            continue
    return []


def stdio_package(config) -> tuple[str, str] | None:
    """('npm'|'pypi', package) from an mcpServers config's npx / uvx command."""
    blocks = config if isinstance(config, list) else [config]
    for block in blocks:
        servers = (block or {}).get("mcpServers") if isinstance(block, dict) else None
        for spec in (servers or {}).values() if isinstance(servers, dict) else []:
            if not isinstance(spec, dict):
                continue
            cmd = str(spec.get("command") or "").strip().lower()
            args = [str(a) for a in (spec.get("args") or []) if isinstance(a, (str, int))]
            reg = {"npx": "npm", "bunx": "npm", "pnpx": "npm", "uvx": "pypi", "pipx": "pypi"}.get(
                cmd.rsplit("/", 1)[-1])
            if not reg:
                continue
            rest = [a for a in args if not a.startswith("-") and a not in ("run",)]
            if not rest:
                continue
            pkg = rest[0]
            if reg == "npm":
                pkg = re.sub(r"(?<=.)@[^/@]*$", "", pkg)          # name@latest
            else:
                pkg = re.split(r"[=<>@\[]", pkg, 1)[0]            # name==1.0 / name[extra]
            if re.fullmatch(r"(@[a-z0-9._~-]+/)?[a-zA-Z0-9._~-]+", pkg):
                return reg, pkg
    return None


def modelscope_rows(items: list[dict]) -> tuple[dict, list[dict]]:
    from . import crawlers
    cm = _cm()
    hints: dict[str, str] = {}
    rows: list[dict] = []
    for s in items:
        if crawlers._modelscope_row(s):
            continue        # has a public remote: the remote crawler lists it
        repo = cm.github_repo(s.get("FromSiteUrl"))
        if repo:
            hints.setdefault(repo, "modelscope")
            continue
        pkg = stdio_package(_config_list(s.get("ServerConfig")))
        if not pkg:
            continue
        reg, name = pkg
        page = crawlers.MODELSCOPE_MCP_PAGE.format(path=(s.get("Path") or "").strip("/"))
        rows.append({
            "slug": cm._slug(name.split("/")[-1], name.split("/")[0].lstrip("@") if "/" in name else reg),
            "name": name,
            "description": (s.get("Abstract") or s.get("AbstractCN") or "").strip()[:1000] or None,
            "homepage_url": cm.package_page(reg, name),
            "endpoint_url": None,
            "kind": "catalog",
            "source_code_url": None,
            "package_registry": reg,
            "package_name": name,
            "tags": None,
            "source": reg,
            "source_id": name.lower(),
            "source_url": cm.package_page(reg, name),
            "confidence": 0.5,
        })
    return hints, rows


def fetch_modelscope() -> list[dict]:
    from . import crawlers
    items = crawlers.fetch_modelscope_mcp(raw=True)
    log.info("code mcp: modelscope listings=%d", len(items))
    return items


# ---------------------------------------------------------------------------
# Docker MCP catalog (local image servers)
# ---------------------------------------------------------------------------

def docker_rows(docs: list[dict]) -> tuple[dict, list[dict]]:
    cm = _cm()
    hints: dict[str, str] = {}
    rows: list[dict] = []
    for d in docs:
        if not isinstance(d, dict) or d.get("type") != "server":
            continue
        name = str(d.get("name") or "").strip()
        image = str(d.get("image") or "").strip()
        if not name or not image:
            continue
        image = _registry_identifier("oci", image)
        about = d.get("about") or {}
        src = d.get("source") or {}
        project = src.get("project") if isinstance(src, dict) else None
        repo = cm.github_repo(project)
        if repo:
            hints.setdefault(repo, "docker-mcp")
        tags = [str(t) for t in ((d.get("meta") or {}).get("tags") or []) if t][:12]
        page = f"https://hub.docker.com/mcp/server/{name}/overview"
        rows.append({
            "slug": cm._slug(name, "docker"),
            "name": str(about.get("title") or name),
            "description": re.sub(r"\s+", " ", str(about.get("description") or "")).strip()[:1000] or None,
            "homepage_url": project or page,
            "endpoint_url": None,
            "kind": "catalog",
            "source_code_url": f"https://github.com/{repo}" if repo else project,
            "package_registry": "oci",
            "package_name": image,
            "tags": tags or None,
            "source": "oci",
            "source_id": image.lower(),
            "source_url": page,
            "confidence": 0.7,
        })
    return hints, rows


def fetch_docker() -> list[dict]:
    from . import crawlers
    docs = crawlers.fetch_docker_mcp(raw=True)
    log.info("code mcp: docker catalog entries=%d", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Baidu MCP World
# ---------------------------------------------------------------------------

BAIDU_SEED_WORDS = (
    "mcp", "server", "github", "api", "ai", "agent", "tool", "data", "search", "file",
    "browser", "database", "sql", "web", "code", "git", "docker", "kubernetes", "cloud",
    "aws", "azure", "google", "notion", "slack", "discord", "telegram", "email", "calendar",
    "weather", "map", "finance", "stock", "crypto", "blockchain", "image", "video", "audio",
    "pdf", "excel", "markdown", "memory", "rag", "llm", "openai", "claude", "python", "node",
    "java", "rust", "go", "typescript", "test", "security", "monitor", "log", "devops",
    "搜索", "浏览器", "数据库", "文件", "开发", "代码", "地图", "天气", "金融", "股票", "新闻",
    "图片", "视频", "音频", "翻译", "文档", "邮件", "日程", "笔记", "知识库", "爬虫", "自动化",
)


def baidu_words(limit: int = 1500) -> list[str]:
    """Seed words plus the most frequent name tokens of the code-only rows we
    hold: Baidu mostly mirrors the same GitHub repositories."""
    from collections import Counter
    counts: Counter = Counter()
    with db.connect(read_only=True) as c:
        for (name,) in c.execute(
                "SELECT name FROM mcp_servers WHERE COALESCE(endpoint_url,'')='' "
                "AND source='github'"):
            for tok in re.split(r"[^a-z0-9]+", (name or "").lower()):
                if len(tok) >= 3 and not tok.isdigit() and tok not in ("mcp", "server"):
                    counts[tok] += 1
    words = list(dict.fromkeys(BAIDU_SEED_WORDS))
    for tok, _n in counts.most_common(limit):
        if tok not in words:
            words.append(tok)
    return words[:limit]


def fetch_baidu(words: list[str], max_requests: int = BAIDU_MAX_REQUESTS,
                sleep: float = 0.3, workers: int = BAIDU_WORKERS) -> dict[str, str]:
    """Walk the keyword list (each answer takes ~2.3 s from the US, so a few
    keywords run at once)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    cm = _cm()
    hints: dict[str, str] = {}
    state = {"requests": 0, "exhausted": False}
    lock = threading.Lock()

    def word(wd):
        for pn in range(1, 100 // BAIDU_PAGE + 1):
            with lock:
                if state["requests"] >= max_requests:
                    state["exhausted"] = True
                    return
                state["requests"] += 1
            url = BAIDU_URL + "?" + urllib.parse.urlencode(
                {"lg": "zh", "wd": wd, "pn": pn, "pl": BAIDU_PAGE})
            try:
                data, _ = cm._get(url, {"User-Agent": "Mozilla/5.0 (compatible; agent-tools.cloud)"},
                                  timeout=30)
            except Exception as e:  # noqa: BLE001 - one keyword must not stop the walk
                log.warning("code mcp: baidu %r page %d failed: %r", wd, pn, e)
                return
            servers = []
            for grp in ((data or {}).get("data") or {}).get("mcpList") or []:
                servers.extend(grp.get("servers") or [])
            with lock:
                for s in servers:
                    repo = cm.github_repo(s.get("serverUrl"))
                    if repo:
                        hints.setdefault(repo, "baidu-mcpworld")
            time.sleep(sleep)
            if len(servers) < BAIDU_PAGE:
                return

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(word, words))
    if state["exhausted"]:
        log.warning("code mcp: baidu request budget exhausted")
    log.info("code mcp: baidu repos=%d requests=%d", len(hints), state["requests"])
    return hints


# ---------------------------------------------------------------------------
# All directories
# ---------------------------------------------------------------------------

def fetch_extra(baidu: bool = False) -> tuple[dict[str, str], list[dict], dict]:
    """(repo hints, package rows, counts) from every directory; one failing
    directory only loses its own share."""
    hints: dict[str, str] = {}
    rows: list[dict] = []
    info: dict[str, int | str] = {}

    def take(name, fn):
        try:
            h, r = fn()
        except Exception as e:  # noqa: BLE001 - network / format change
            log.warning("code mcp: %s failed: %r", name, e)
            info[f"{name}_error"] = type(e).__name__
            return
        for repo, via in h.items():
            hints.setdefault(repo, via)
        rows.extend(r)
        info[f"{name}_repos"] = len(h)
        info[f"{name}_packages"] = len(r)

    take("registry", lambda: registry_rows(fetch_registry()))
    take("cline", lambda: (fetch_cline(), []))
    take("modelscope", lambda: modelscope_rows(fetch_modelscope()))
    take("docker", lambda: docker_rows(fetch_docker()))
    if baidu:
        take("baidu", lambda: (fetch_baidu(baidu_words()), []))
    return hints, rows, info


def attach_packages(attach: list[tuple[str, str, str]]) -> int:
    """Give stored GitHub rows without a package the package found for them."""
    if not attach:
        return 0
    first: dict[str, tuple[str, str]] = {}
    for repo, reg, pkg in attach:
        first.setdefault(repo, (reg, pkg))
    items = [(reg, pkg, repo) for repo, (reg, pkg) in first.items()]
    n = 0
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                before = c.total_changes
                c.executemany(
                    "UPDATE mcp_servers SET package_registry=?, package_name=? "
                    "WHERE source='github' AND source_id=? AND COALESCE(package_name,'')=''",
                    chunk)
                return c.total_changes - before
        n += db.with_retry(op)
    return n


def drop_duplicate_packages() -> dict[str, int]:
    """Delete our own package rows whose repository we also list as a GitHub
    row (left by earlier incremental runs); the GitHub row takes the package
    if it has none. Owner-claimed rows are kept."""
    cm = _cm()
    with db.connect(read_only=True) as c:
        gh = {sid: pkg for sid, pkg in c.execute(
            "SELECT source_id, package_name FROM mcp_servers WHERE source='github'")}
        pkgs = list(c.execute(
            "SELECT id, source, package_registry, package_name, source_code_url FROM mcp_servers "
            f"WHERE source IN ({','.join('?' * len(cm.PACKAGE_SOURCES))}) "
            "AND COALESCE(endpoint_url,'')='' AND COALESCE(owner_verified,0)=0",
            cm.PACKAGE_SOURCES))
    drop, attach = [], []
    for rid, src, reg, pkg, url in pkgs:
        repo = cm.github_repo(url)
        if not repo or repo not in gh:
            continue
        drop.append((rid, src))
        if not gh[repo] and pkg:
            attach.append((repo, reg or src, pkg))
            gh[repo] = pkg
    attach_packages(attach)
    removed: dict[str, int] = {}
    for i in range(0, len(drop), 500):
        chunk = drop[i:i + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                ids = [(rid,) for rid, _s in chunk]
                c.executemany("DELETE FROM listing_sources WHERE kind='mcp' AND listing_id=?", ids)
                c.executemany("DELETE FROM mcp_servers WHERE id=? AND COALESCE(owner_verified,0)=0",
                              ids)
        db.with_retry(op)
        for _rid, src in chunk:
            removed[src] = removed.get(src, 0) + 1
    return removed


# ---------------------------------------------------------------------------
# GitHub lookup of hinted repositories
# ---------------------------------------------------------------------------

def _ensure_lookup_table():
    def op():
        with db.writer() as c:
            c.execute("CREATE TABLE IF NOT EXISTS code_repo_lookup ("
                      "repo TEXT PRIMARY KEY, via TEXT, checked_at INTEGER NOT NULL, "
                      "found INTEGER NOT NULL DEFAULT 0)")
    db.with_retry(op)


def recently_checked(days: int = LOOKUP_RECHECK_DAYS) -> set[str]:
    _ensure_lookup_table()
    cutoff = int(time.time()) - days * 86400
    with db.connect(read_only=True) as c:
        return {r[0] for r in c.execute(
            "SELECT repo FROM code_repo_lookup WHERE checked_at >= ?", (cutoff,))}


def _record_lookups(results: list[tuple[str, str, int]]):
    now = int(time.time())
    for i in range(0, len(results), 500):
        chunk = [(r, v, now, f) for r, v, f in results[i:i + 500]]

        def op(chunk=chunk):
            with db.writer() as c:
                c.executemany(
                    "INSERT INTO code_repo_lookup(repo, via, checked_at, found) VALUES (?,?,?,?) "
                    "ON CONFLICT(repo) DO UPDATE SET via=excluded.via, "
                    "checked_at=excluded.checked_at, found=excluded.found", chunk)
        db.with_retry(op)


def lookup_repos(hints: dict[str, str], max_repos: int = LOOKUP_MAX) -> list[dict]:
    """GitHub search items for the hinted repositories (forks and archived
    repositories excluded, renamed ones are found under their new name)."""
    cm = _cm()
    _ensure_lookup_table()
    auth = cm._github_auth()
    repos = sorted(hints)[:max_repos]
    found: dict[str, dict] = {}
    done: list[tuple[str, str, int]] = []
    batch = LOOKUP_BATCH
    i = 0
    while i < len(repos):
        part = repos[i:i + batch]
        q = " ".join(f"repo:{r}" for r in part) + " fork:false archived:false"
        try:
            data, _ = cm._github_search(q, 1, auth)
        except urllib.error.HTTPError as e:
            if e.code == 422 and batch > 1:
                batch = max(1, batch // 2)      # too long, or one name is invalid
                continue
            if e.code == 422:
                done.append((part[0], hints[part[0]], 0))
                i += 1
                batch = LOOKUP_BATCH
                continue
            raise
        for item in data.get("items") or []:
            full = (item.get("full_name") or "").lower()
            if full:
                found[full] = cm.slim_github(item)
        for r in part:
            done.append((r, hints[r], int(r in found)))
        i += len(part)
        if len(done) >= 2000:
            _record_lookups(done)
            done = []
    if done:
        _record_lookups(done)
    log.info("code mcp: looked up %d hinted repositories, %d on GitHub", len(repos), len(found))
    return list(found.values())
