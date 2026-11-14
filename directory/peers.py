"""Peer directories used as discovery leads (added 2026-11-14).

These directories crawl us and publish data we can read: MCPCensus (Radixia),
the CSOAI remote MCP census (councilof.ai), INITE Atlas, SealKeeper, agentalog,
Assay Observatory, AgentIndex (agents.traderszone.net) and x402lint. A one-off
sample showed each holds hosts we did not have (docs: 38_剩余生态同行反向抓取调研).

Everything taken here is only a pointer: an MCP endpoint URL, an Agent Card URL
or an origin. We store none of the peer's own observations (scores, grades,
uptime, descriptions); every row is then fetched or probed by us like any other
source, and keeps the peer as its ``source`` so the lead can be attributed.

Politeness: one request at a time with a pause between pages, a cap per host and
per run, and full passes over large directories at most once a week (tracked in
``meta`` as ``peer:<name>:last``). Each fetcher honours the peer's published
robots/terms as checked on 2026-11-14:

* MCPCensus     data files CC BY 4.0, robots Allow /
* councilof.ai  Hugging Face dataset csoai/mcp-remote-census, CC BY 4.0
* INITE Atlas   robots explicitly allows /api/atlas
* SealKeeper    terms allow collection through the public API within limits
* agentalog     public API, 1,000 reads/day per crawler; records whose credited
                source carries ODbL are skipped (share-alike)
* Assay         robots Allow /, one JSON
* AgentIndex    /endpoints allowed, Crawl-delay 1, CC BY 4.0
* x402lint      api robots Allow /, 30 requests/minute
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from . import crawlers, db

log = logging.getLogger("directory.peers")

UA = crawlers.UA
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
PER_HOST = 20          # rows one host may contribute per source and run
WEEK_DAYS = 6          # full passes over big peers: about once a week

MCP_CENSUS_INDEX = "https://www.radixia.ai/census/data"
MCP_CENSUS_FILE = "https://www.radixia.ai/census/data/{date}/census.jsonl.gz"
CSOAI_DATASET = "https://huggingface.co/api/datasets/csoai/mcp-remote-census"
CSOAI_FILE = "https://huggingface.co/datasets/csoai/mcp-remote-census/resolve/main/{name}"
CSOAI_LIVE_STATES = {"RESPONDED", "AUTH_REQUIRED", "SSE_ENDPOINT_ONLY"}
INITE_SITEMAP = "https://inite.ai/sitemaps/atlas-mcp-en.xml"
INITE_RECORD = "https://inite.ai/api/atlas/{domain}"
SEALKEEPER_SEARCH = "https://api.sealkeeper.run/v1/agents/search"
AGENTALOG_A2A = "https://api.agentalog.com/api/a2a/agents"
AGENTALOG_PAGE = 50
AGENTALOG_MAX_PAGES = 150   # 6,242 agents / 50 = 125 pages, well under 1,000/day
ASSAY_API = "https://assay-207908449630.us-central1.run.app/api"
AGENTINDEX_ENDPOINTS = "https://agents.traderszone.net/endpoints"
X402LINT_DIRECTORY = "https://api.x402lint.dev/v1/directory"

SOURCE_URLS = {
    "mcp-census": "https://www.radixia.ai/census/",
    "csoai-census": "https://huggingface.co/datasets/csoai/mcp-remote-census",
    "inite": "https://inite.ai/en/atlas",
    "sealkeeper": "https://sealkeeper.run",
    "agentalog": "https://agentalog.com",
    "assay": "https://assay-207908449630.us-central1.run.app",
    "agentindex": "https://agents.traderszone.net",
    "x402lint": "https://x402lint.dev",
}


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": UA, "Accept": "application/json"})


def _get(c: httpx.Client, url: str, *, params: dict | None = None,
         attempts: int = 3) -> httpx.Response:
    """GET with a short retry on 429/5xx (honouring Retry-After up to 60 s)."""
    for attempt in range(attempts):
        try:
            r = c.get(url, params=params)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == attempts - 1:
                r.raise_for_status()
            try:
                wait = min(60.0, float(r.headers.get("retry-after") or 2 ** attempt))
            except ValueError:
                wait = 2.0 ** attempt
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"unreachable: {url}")


def _due(name: str, days: float) -> bool:
    from contextlib import closing
    try:
        with closing(db.connect(read_only=True)) as c:
            last = db.get_meta(c, f"peer:{name}:last")
    except Exception:  # noqa: BLE001 -- no meta yet means due
        return True
    try:
        return time.time() - float(last) >= days * 86400
    except (TypeError, ValueError):
        return True


def _mark(name: str) -> None:
    def op():
        with db.writer() as c:
            db.set_meta(c, f"peer:{name}:last", str(int(time.time())))
    db.with_retry(op)


def _https_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    url = url.strip()
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return None
    if crawlers.is_ephemeral_host(url) or not crawlers._url_acceptable(url):
        return None
    return url.rstrip("/") if p.path not in ("", "/") else f"https://{p.netloc}"


def _origin(host: Any) -> str | None:
    if not isinstance(host, str) or not host.strip():
        return None
    host = host.strip().lower().rstrip(".")
    if "/" in host or any(ch.isspace() for ch in host):
        return None
    return _https_url(f"https://{host}")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _capped(items: Iterable[dict], key: str, per_host: int = PER_HOST) -> list[dict]:
    out, seen, hosts = [], set(), {}
    for it in items:
        k = it[key].lower()
        h = _host(it[key])
        if k in seen or hosts.get(h, 0) >= per_host:
            continue
        seen.add(k)
        hosts[h] = hosts.get(h, 0) + 1
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# row builders
# ---------------------------------------------------------------------------
def mcp_row(endpoint: str, source: str, source_id: str | None = None,
            name: str | None = None) -> dict | None:
    """One mcp_servers row for an endpoint a peer pointed at (probed later)."""
    endpoint = _https_url(endpoint)
    if not endpoint:
        return None
    host = _host(endpoint)
    path = urlparse(endpoint).path.lower()
    transport = "sse" if path.endswith("/sse") else "streamable-http"
    label = name or host
    where = crawlers._slugify(host + path)
    return {
        "slug": (f"{crawlers._slugify(name)}-{where}" if name else where)[:80],
        "name": label,
        "description": None,
        "homepage_url": f"https://{urlparse(endpoint).netloc}",
        "endpoint_url": endpoint,
        "transport": transport,
        "auth_method": None,
        # x402 is proven by reverify_x402 probing, never inferred here.
        "x402_supported": False,
        "source": source,
        "source_id": str(source_id or endpoint),
        "source_url": SOURCE_URLS.get(source),
        "confidence": 0.4,
    }


def x402_row(origin: str, source: str, resource: str | None = None) -> dict | None:
    """Origin-level x402 services row; health/reverify prove the 402 later."""
    origin = _https_url(origin)
    if not origin:
        return None
    o = urlparse(origin)
    origin = f"https://{o.netloc}"
    samples = []
    if resource:
        r = _https_url(resource)
        if r and _host(r) == _host(origin):
            samples.append({"url": r, "kind": "x402-resource"})
    return {
        "slug": f"{crawlers._host_slug(origin)}-{source}"[:80],
        "name": o.hostname,
        "url": origin,
        "description": f"x402 provider {o.hostname} (discovery lead: {source})",
        "category": "general",
        "chains": [],
        "well_known_url": origin + "/.well-known/x402",
        "confidence": None,
        "resource_samples": samples,
        "source": source,
        "source_id": o.hostname,
        "tags": [],
        "region": "global",
    }


def card_lead(url: str, source: str, source_id: str | None = None) -> dict | None:
    url = _https_url(url)
    if not url:
        return None
    return {"card_url": url, "source_id": str(source_id or url),
            "source_url": SOURCE_URLS.get(source)}


# ---------------------------------------------------------------------------
# parsers (pure; unit-tested with fixtures)
# ---------------------------------------------------------------------------
def parse_mcp_census(lines: Iterable[bytes | str]) -> list[dict]:
    """MCPCensus: check D3 found a responding MCP endpoint on the apex."""
    rows = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        for chk in rec.get("checks") or []:
            if not isinstance(chk, dict) or chk.get("id") != "D3" or chk.get("status") != "pass":
                continue
            ep = (chk.get("evidence") or {}).get("endpointUrl")
            row = mcp_row(ep, "mcp-census", rec.get("apex"))
            if row:
                rows.append(row)
    return _capped(rows, "endpoint_url")


def parse_csoai(lines: Iterable[bytes | str]) -> list[dict]:
    rows = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and rec.get("state") in CSOAI_LIVE_STATES:
            row = mcp_row(rec.get("endpoint"), "csoai-census")
            if row:
                rows.append(row)
    return _capped(rows, "endpoint_url")


def parse_inite_record(rec: dict) -> list[str]:
    urls = []
    for m in ((rec.get("interfaces") or {}).get("mcp") or []):
        if not isinstance(m, dict):
            continue
        for r in m.get("remotes") or []:
            urls.append(r if isinstance(r, str) else (r or {}).get("url"))
    server = (rec.get("mcpMeasured") or {}).get("server")
    if server:
        urls.append(server)
    return [u for u in dict.fromkeys(urls) if u]


def sealkeeper_leads(agents: Iterable[dict]) -> list[dict]:
    leads = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        url = a.get("cardUrl") or ""
        # cardUrl often points at SealKeeper's own record; then only the
        # agent's domain is a lead.
        if not url or _host(url).endswith("sealkeeper.run"):
            url = _origin(a.get("domain")) or ""
        lead = card_lead(url, "sealkeeper", a.get("id"))
        if lead:
            leads.append(lead)
    return _capped(leads, "card_url")


def agentalog_leads(items: Iterable[dict]) -> list[dict]:
    leads = []
    for it in items:
        if not isinstance(it, dict):
            continue
        licences = " ".join(str((c or {}).get("licenca") or "") for c in it.get("creditos") or [])
        if "odbl" in licences.lower():
            continue  # share-alike: not merged into our CC BY data
        lead = card_lead(it.get("cartao") or it.get("url"), "agentalog", it.get("chave"))
        if lead:
            leads.append(lead)
    return _capped(leads, "card_url")


def assay_split(board: Iterable[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Assay leaderboard -> (mcp rows, a2a leads, x402 rows)."""
    mcp, a2a_leads, x402 = [], [], []
    for b in board:
        if not isinstance(b, dict):
            continue
        target = b.get("target")
        if isinstance(target, str) and "://" not in target:
            target = "https://" + target
        if (b.get("mcp_tools") or 0) > 0:
            row = mcp_row(target, "assay")
            if row:
                mcp.append(row)
        if b.get("a2a_card"):
            o = _https_url(target)
            lead = card_lead(f"https://{urlparse(o).netloc}" if o else None, "assay")
            if lead:
                a2a_leads.append(lead)
        if b.get("x402_gated"):
            row = x402_row(target, "assay", resource=target)
            if row:
                x402.append(row)
    return (_capped(mcp, "endpoint_url"), _capped(a2a_leads, "card_url"),
            _capped(x402, "url", per_host=1))


def agentindex_split(results: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """AgentIndex domains -> (x402 rows, a2a origin leads). MCP-only domains
    carry no endpoint path and are left to the sources that publish one."""
    x402, a2a_leads = [], []
    for r in results:
        if not isinstance(r, dict):
            continue
        protos = {str(p).lower() for p in r.get("protocols") or []}
        origin = _origin(r.get("domain"))
        if not origin:
            continue
        if "x402" in protos:
            row = x402_row(origin, "agentindex")
            if row:
                x402.append(row)
        if "a2a" in protos:
            lead = card_lead(origin, "agentindex", r.get("domain"))
            if lead:
                a2a_leads.append(lead)
    return _capped(x402, "url", per_host=1), _capped(a2a_leads, "card_url", per_host=1)


def x402lint_rows(projects: Iterable[dict]) -> list[dict]:
    rows = []
    for p in projects:
        if isinstance(p, dict):
            row = x402_row(_origin(p.get("host")) or "", "x402lint")
            if row:
                rows.append(row)
    return _capped(rows, "url", per_host=1)


# ---------------------------------------------------------------------------
# fetchers (network). MCP fetchers follow the MCP_CRAWLERS contract and x402
# fetchers the ALL_CRAWLERS one: return a list of rows; [] when not due.
# ---------------------------------------------------------------------------
def fetch_mcp_census() -> list[dict]:
    if not _due("mcp-census", WEEK_DAYS):
        return []
    with _client() as c:
        page = _get(c, MCP_CENSUS_INDEX).text
        dates = sorted(set(re.findall(r"/census/data/(\d{4}-\d{2}-\d{2})/census\.jsonl\.gz", page)))
        if not dates:
            raise RuntimeError("mcp-census: no data file linked from the data page")
        raw = gzip.decompress(_get(c, MCP_CENSUS_FILE.format(date=dates[-1])).content)
    rows = parse_mcp_census(raw.splitlines())
    _mark("mcp-census")
    log.info("mcp-census %s: %d endpoint leads", dates[-1], len(rows))
    return rows


def fetch_csoai_census() -> list[dict]:
    if not _due("csoai-census", WEEK_DAYS):
        return []
    with _client() as c:
        meta = _get(c, CSOAI_DATASET).json()
        files = sorted(s.get("rfilename", "") for s in meta.get("siblings") or []
                       if re.search(r"results\.public\.\d{4}-\d{2}-\d{2}\.jsonl\.gz$",
                                    s.get("rfilename", "")))
        if not files:
            raise RuntimeError("csoai-census: no results.public file in the dataset")
        raw = gzip.decompress(_get(c, CSOAI_FILE.format(name=files[-1])).content)
    rows = parse_csoai(raw.splitlines())
    _mark("csoai-census")
    log.info("csoai-census %s: %d endpoint leads", files[-1], len(rows))
    return rows


def fetch_inite_mcp(pause: float = 1.0) -> list[dict]:
    if not _due("inite", WEEK_DAYS):
        return []
    rows: list[dict] = []
    with _client() as c:
        sm = _get(c, INITE_SITEMAP).content
        locs = [e.text or "" for e in ElementTree.fromstring(sm).iter() if e.tag.endswith("loc")]
        domains = []
        for loc in locs:
            parts = loc.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-1] == "mcp":
                domains.append(parts[-2])
        for dom in dict.fromkeys(domains):
            try:
                rec = _get(c, INITE_RECORD.format(domain=dom)).json()
            except Exception as e:  # noqa: BLE001 -- one record is not fatal
                log.info("inite: %s failed: %r", dom, e)
                continue
            for u in parse_inite_record(rec):
                row = mcp_row(u, "inite", dom)
                if row:
                    rows.append(row)
            time.sleep(pause)
    _mark("inite")
    rows = _capped(rows, "endpoint_url")
    log.info("inite: %d MCP domains -> %d endpoint leads", len(domains), len(rows))
    return rows


_assay_cache: tuple[float, list[dict]] | None = None


def _assay_board() -> list[dict]:
    global _assay_cache
    if _assay_cache and time.time() - _assay_cache[0] < 3600:
        return _assay_cache[1]
    with _client() as c:
        board = _get(c, ASSAY_API).json().get("leaderboard") or []
    _assay_cache = (time.time(), board)
    return board


def fetch_assay_mcp() -> list[dict]:
    return assay_split(_assay_board())[0]


def fetch_assay_x402() -> list[dict]:
    return assay_split(_assay_board())[2]


_agentindex_cache: tuple[float, list[dict]] | None = None


def _agentindex_results(pause: float = 1.2) -> list[dict]:
    """All AgentIndex domains, at most once a week (Crawl-delay: 1)."""
    global _agentindex_cache
    if _agentindex_cache and time.time() - _agentindex_cache[0] < 3600:
        return _agentindex_cache[1]
    if not _due("agentindex", WEEK_DAYS):
        return []
    out: list[dict] = []
    with _client() as c:
        offset = 0
        while offset < 100_000:
            d = _get(c, AGENTINDEX_ENDPOINTS, params={"limit": 100, "offset": offset}).json()
            out.extend(d.get("results") or [])
            if not d.get("hasMore"):
                break
            offset += 100
            time.sleep(pause)
    _agentindex_cache = (time.time(), out)
    _mark("agentindex")
    return out


def fetch_agentindex_x402() -> list[dict]:
    return agentindex_split(_agentindex_results())[0]


def fetch_x402lint(pause: float = 2.2, max_pages: int = 400) -> list[dict]:
    if not _due("x402lint", WEEK_DAYS):
        return []
    projects: list[dict] = []
    with _client() as c:
        for page in range(max_pages):
            d = _get(c, X402LINT_DIRECTORY, params={"page": page}).json()
            batch = d.get("projects") or []
            projects.extend(batch)
            if not batch or len(projects) >= int(d.get("count") or 0):
                break
            time.sleep(pause)
    _mark("x402lint")
    rows = x402lint_rows(projects)
    log.info("x402lint: %d hosts -> %d origin leads", len(projects), len(rows))
    return rows


# ---------------------------------------------------------------------------
# A2A leads (fetched and ingested by a2a.crawl_card_leads)
# ---------------------------------------------------------------------------
def _drop_known(leads: list[dict], known_urls: set[str], known_hosts: set[str]) -> list[dict]:
    out = []
    for lead in leads:
        url = lead["card_url"]
        if urlparse(url).path in ("", "/"):
            if _host(url) in known_hosts:
                continue
        elif url.rstrip("/").lower() in known_urls:
            continue
        out.append(lead)
    return out


def sealkeeper_fetch_leads(pause: float = 1.5, max_pages: int = 600) -> list[dict]:
    if not _due("sealkeeper", WEEK_DAYS):
        return []
    agents: list[dict] = []
    with _client() as c:
        cursor = None
        for _ in range(max_pages):
            d = _get(c, SEALKEEPER_SEARCH, params={"cursor": cursor} if cursor else None).json()
            agents.extend(d.get("agents") or [])
            cursor = d.get("nextCursor")
            if not cursor:
                break
            time.sleep(pause)
    _mark("sealkeeper")
    return sealkeeper_leads(agents)


def agentalog_fetch_leads(pause: float = 1.5) -> list[dict]:
    if not _due("agentalog", WEEK_DAYS):
        return []
    items: list[dict] = []
    with _client() as c:
        for page in range(AGENTALOG_MAX_PAGES):
            d = _get(c, AGENTALOG_A2A, params={"offset": page * AGENTALOG_PAGE,
                                              "limit": AGENTALOG_PAGE}).json()
            batch = d.get("items") or []
            items.extend(batch)
            if not batch or len(items) >= int(d.get("total") or 0):
                break
            time.sleep(pause)
    _mark("agentalog")
    return agentalog_leads(items)


A2A_LEAD_SOURCES: dict[str, Callable[[], list[dict]]] = {
    "sealkeeper": sealkeeper_fetch_leads,
    "agentalog": agentalog_fetch_leads,
    "assay": lambda: assay_split(_assay_board())[1],
    "agentindex": lambda: agentindex_split(_agentindex_results())[1],
}

MCP_FETCHERS = {
    "mcp-census": fetch_mcp_census,
    "csoai-census": fetch_csoai_census,
    "inite": fetch_inite_mcp,
    "assay": fetch_assay_mcp,
}

X402_FETCHERS = {
    "x402lint": fetch_x402lint,
    "agentindex": fetch_agentindex_x402,
    "assay": fetch_assay_x402,
}


def crawl_a2a_peers(max_new: int = 400) -> dict:
    """Run every A2A lead source; returns per-source results and totals."""
    from . import a2a as a2a_mod
    from contextlib import closing
    with closing(db.connect(read_only=True)) as c:
        rows = c.execute("SELECT card_url, endpoint_url FROM a2a_agents").fetchall()
    known_urls = {(u or "").rstrip("/").lower() for r in rows for u in r if u}
    known_hosts = {_host(u) for r in rows for u in r if u}
    out: dict = {"inserted": 0, "updated": 0, "errors": [], "sources": {}}
    for name, fn in A2A_LEAD_SOURCES.items():
        try:
            leads = _drop_known(fn(), known_urls, known_hosts)[:max_new]
            res = a2a_mod.crawl_card_leads(name, leads) if leads else {
                "candidates": 0, "resolved": 0, "inserted": 0, "updated": 0, "rejected": 0}
        except Exception as e:  # noqa: BLE001 -- one peer must not stop the rest
            out["errors"].append(f"{name}: {e!r}")
            log.warning("a2a peer %s failed: %r", name, e)
            continue
        out["sources"][name] = res
        out["inserted"] += res["inserted"]; out["updated"] += res["updated"]
        log.info("a2a peer %s: candidates=%d resolved=%d inserted=%d",
                 name, res["candidates"], res["resolved"], res["inserted"])
    return out
