"""Peer directories used as discovery leads (added 2026-11-14).

These directories crawl us and publish data we can read: MCPCensus (Radixia),
the CSOAI remote MCP census (councilof.ai), agentalog, Assay Observatory,
AgentIndex (agents.traderszone.net) and x402lint. A one-off sample showed each
holds hosts we did not have (docs: 38_剩余生态同行反向抓取调研). INITE Atlas and
SealKeeper were dropped (INITE terms forbid competing products; SealKeeper's API
host robots.txt is Disallow: /).

Everything taken here is only a pointer: an MCP endpoint URL, an Agent Card URL
or an origin. We store none of the peer's own observations (scores, grades,
uptime, descriptions); every row is then fetched or probed by us like any other
source, and keeps the peer as its ``source`` so the lead can be attributed.

Politeness: one request at a time with a pause between pages, a cap per host and
per run, and full passes over large directories at most once a week (tracked in
``meta`` as ``peer:<name>:last``, written only after the rows were ingested, so a
failed run is retried the next day).

Hosts whose owner (or staff on the owner's request) took a listing down are never
re-added from a peer, even under another URL (``_owner_removed_hosts``).
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from contextlib import closing
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

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
AGENTALOG_A2A = "https://api.agentalog.com/api/a2a/agents"
AGENTALOG_PAGE = 50
AGENTALOG_MAX_PAGES = 150   # 6,242 agents / 50 = 125 pages, well under 1,000/day
ASSAY_API = "https://assay-207908449630.us-central1.run.app/api"
AGENTINDEX_ENDPOINTS = "https://agents.traderszone.net/endpoints"
X402LINT_DIRECTORY = "https://api.x402lint.dev/v1/directory"

SOURCE_URLS = {
    "mcp-census": "https://www.radixia.ai/census/",
    "csoai-census": "https://huggingface.co/datasets/csoai/mcp-remote-census",
    "agentalog": "https://agentalog.com",
    "assay": "https://assay-207908449630.us-central1.run.app",
    "agentindex": "https://agents.traderszone.net",
    "x402lint": "https://x402lint.dev",
}

# Hosts we never take from a peer. Free tunnels change name on restart, and
# these resolve to private machines or arbitrary IPs. Kept local to the peers:
# the site-wide EPHEMERAL_SUFFIXES list drives auto-retirement of existing rows.
PEER_SKIP_SUFFIXES = (".pinggy.link", ".pinggy-free.link", ".pinggy.net", ".pinggy.io",
                      ".ts.net", ".sslip.io", ".nip.io")

# Removals that came from the owner (directly, or via staff on their request).
_OWNER_REMOVAL = ("owner:%", "staff:removal%", "staff-email%", "operator-approved%")


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
                wait = max(0.0, min(60.0, float(r.headers.get("retry-after") or 2 ** attempt)))
            except ValueError:
                wait = 2.0 ** attempt
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"unreachable: {url}")


def _due(name: str, days: float) -> bool:
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


# Fetchers register the meta key they would advance; jobs calls settle() once
# the rows are ingested, so a run that fails to ingest is retried next day.
_PENDING: dict[str, str] = {}


def _pending(crawler: str, key: str) -> None:
    _PENDING[crawler] = key


def settle(crawler: str, ok: bool) -> None:
    key = _PENDING.pop(crawler, None)
    if key and ok:
        _mark(key)


def _clean(url: Any) -> tuple[str, bool] | None:
    """https URL a peer pointed at -> (url, needs_key), or None.

    Uses the shared directory rules (no localhost/intranet names, placeholders
    or pasted credentials: a secret query is dropped and the endpoint marked
    keyed). The path is kept as given, trailing slash included, since some
    servers answer only one form.
    """
    if not isinstance(url, str):
        return None
    try:
        res = crawlers._remote_endpoint(url.strip())
        if not res:
            return None
        ep, needs_key = res
        p = urlparse(ep)
        host = (p.hostname or "").lower()
    except ValueError:
        return None
    if p.scheme != "https" or not host or ("." + host).endswith(PEER_SKIP_SUFFIXES):
        return None
    if crawlers.is_ephemeral_host(ep):
        return None
    if p.path in ("", "/") and not p.query:
        ep = f"https://{p.netloc}"
    return ep, needs_key


def _https_url(url: Any) -> str | None:
    res = _clean(url)
    return res[0] if res else None


def _origin(host: Any) -> str | None:
    if not isinstance(host, str) or not host.strip():
        return None
    host = host.strip().lower().rstrip(".")
    if "/" in host or "." not in host or any(ch.isspace() for ch in host):
        return None
    return _https_url(f"https://{host}")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _key(url: str) -> str:
    """Dedup key: host lowercased, path as given minus a trailing slash."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    return f"{(p.hostname or '').lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def _capped(items: Iterable[dict], key: str, per_host: int = PER_HOST) -> list[dict]:
    out, seen, hosts = [], set(), {}
    for it in items:
        k = _key(it[key])
        h = _host(it[key])
        if k in seen or hosts.get(h, 0) >= per_host:
            continue
        seen.add(k)
        hosts[h] = hosts.get(h, 0) + 1
        out.append(it)
    return out


def _owner_removed_hosts() -> set[str]:
    """Hosts of listings taken down by their owner (or staff on request)."""
    where = " OR ".join("r.retired_by LIKE ?" for _ in _OWNER_REMOVAL)
    with closing(db.connect(read_only=True)) as c:
        rows = c.execute(
            "SELECT r.original_url, u.original_url FROM retired_listings r "
            "LEFT JOIN retired_listing_urls u ON u.retirement_id=r.id "
            "WHERE r.status='active' AND (%s)" % where, _OWNER_REMOVAL).fetchall()
    return {h for row in rows for u in row if u and (h := _host(u))}


def _drop_removed(items: list[dict], key: str) -> list[dict]:
    if not items:
        return items
    try:
        removed = _owner_removed_hosts()
    except Exception as e:  # noqa: BLE001 -- refuse rather than risk re-adding
        raise RuntimeError(f"owner-removal check failed: {e!r}") from e
    kept = [it for it in items if _host(it[key]) not in removed]
    if len(kept) != len(items):
        log.info("peers: skipped %d rows on owner-removed hosts", len(items) - len(kept))
    return kept


def _each(records: Iterable[Any], fn: Callable[[Any], Any]) -> list:
    """Map fn over records; a malformed record is skipped, not fatal."""
    out = []
    for rec in records:
        try:
            got = fn(rec)
        except (AttributeError, TypeError, ValueError, KeyError) as e:
            log.debug("peers: skipped malformed record: %r", e)
            continue
        if got is None:
            continue
        out.extend(got if isinstance(got, list) else [got])
    return out


# ---------------------------------------------------------------------------
# row builders
# ---------------------------------------------------------------------------
def mcp_row(endpoint: str, source: str, source_id: str | None = None,
            name: str | None = None) -> dict | None:
    """One mcp_servers row for an endpoint a peer pointed at (probed later)."""
    res = _clean(endpoint)
    if not res:
        return None
    endpoint, needs_key = res
    host = _host(endpoint)
    path = urlparse(endpoint).path.lower()
    transport = "sse" if path.rstrip("/").endswith("/sse") else "streamable-http"
    label = name or host
    where = crawlers._slugify(host + path)
    return {
        "slug": (f"{crawlers._slugify(name)}-{where}" if name else where)[:80],
        "name": label,
        "description": None,
        "homepage_url": f"https://{urlparse(endpoint).netloc}",
        "endpoint_url": endpoint,
        "transport": transport,
        "auth_method": "api-key" if needs_key else None,
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
        "source_url": SOURCE_URLS.get(source),
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
def _json_lines(lines: Iterable[bytes | str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _mcp_census_rec(rec: dict) -> list[dict]:
    rows = []
    for chk in rec.get("checks") or []:
        if not isinstance(chk, dict) or chk.get("id") != "D3" or chk.get("status") != "pass":
            continue
        ev = chk.get("evidence")
        row = mcp_row(ev.get("endpointUrl") if isinstance(ev, dict) else None, "mcp-census")
        if row:
            rows.append(row)
    return rows


def parse_mcp_census(lines: Iterable[bytes | str]) -> list[dict]:
    """MCPCensus: check D3 found a responding MCP endpoint on the apex.
    source_id is the endpoint itself, so two endpoints on one apex never
    overwrite each other."""
    return _capped(_each(_json_lines(lines), _mcp_census_rec), "endpoint_url")


def parse_csoai(lines: Iterable[bytes | str]) -> list[dict]:
    return _capped(_each(
        _json_lines(lines),
        lambda rec: mcp_row(rec.get("endpoint"), "csoai-census")
        if rec.get("state") in CSOAI_LIVE_STATES else None), "endpoint_url")


def _agentalog_lead(it: dict) -> dict | None:
    card = it.get("cartao")
    if not isinstance(card, str) or not card:
        # "url" is the A2A endpoint, not the card: discover from its origin.
        h = _host(it.get("url") or "") if isinstance(it.get("url"), str) else ""
        card = f"https://{h}" if h else None
    return card_lead(card, "agentalog", it.get("chave"))


def agentalog_leads(items: Iterable[dict]) -> list[dict]:
    """Every agentalog agent with a card (or an origin to discover one at)."""
    return _each(items, _agentalog_lead)


def _num(x: Any) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _assay_rec(b: dict) -> list:
    target = b.get("target")
    if not isinstance(target, str):
        return []
    if "://" not in target:
        target = "https://" + target
    out: list = []
    if _num(b.get("mcp_tools")) > 0:
        row = mcp_row(target, "assay")
        if row:
            out.append(("mcp", row))
    if b.get("a2a_card") is True:
        o = _https_url(target)
        lead = card_lead(f"https://{urlparse(o).netloc}" if o else None, "assay")
        if lead:
            out.append(("a2a", lead))
    if b.get("x402_gated") is True:
        row = x402_row(target, "assay", resource=target)
        if row:
            out.append(("x402", row))
    return out


def assay_split(board: Iterable[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Assay leaderboard -> (mcp rows, a2a leads, x402 rows)."""
    got = _each(board, _assay_rec)
    pick = lambda k: [r for kind, r in got if kind == k]  # noqa: E731
    return (_capped(pick("mcp"), "endpoint_url"), pick("a2a"),
            _capped(pick("x402"), "url", per_host=1))


def _protocols(r: dict) -> set[str]:
    p = r.get("protocols")
    if isinstance(p, str):
        p = p.split(",")
    return {str(x).strip().lower() for x in p or [] if x}


def agentindex_split(results: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """AgentIndex domains -> (x402 rows, a2a origin leads). MCP-only domains
    carry no endpoint path and are left to the sources that publish one."""
    def one(r: dict) -> list:
        origin = _origin(r.get("domain"))
        if not origin:
            return []
        protos, out = _protocols(r), []
        if "x402" in protos and (row := x402_row(origin, "agentindex")):
            out.append(("x402", row))
        if "a2a" in protos and (lead := card_lead(origin, "agentindex", r.get("domain"))):
            out.append(("a2a", lead))
        return out
    got = _each(results, one)
    return (_capped([r for k, r in got if k == "x402"], "url", per_host=1),
            [r for k, r in got if k == "a2a"])


def x402lint_rows(projects: Iterable[dict]) -> list[dict]:
    return _capped(_each(projects, lambda p: x402_row(_origin(p.get("host")) or "", "x402lint")),
                   "url", per_host=1)


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
    rows = _drop_removed(parse_mcp_census(raw.splitlines()), "endpoint_url")
    _pending("mcp-census", "mcp-census")
    log.info("mcp-census %s: %d endpoint leads", dates[-1], len(rows))
    return rows


def fetch_csoai_census() -> list[dict]:
    if not _due("csoai-census", WEEK_DAYS):
        return []
    with _client() as c:
        meta = _get(c, CSOAI_DATASET).json()
        files = sorted(s.get("rfilename", "") for s in meta.get("siblings") or []
                       if isinstance(s, dict)
                       and re.search(r"results\.public\.\d{4}-\d{2}-\d{2}\.jsonl\.gz$",
                                     s.get("rfilename", "")))
        if not files:
            raise RuntimeError("csoai-census: no results.public file in the dataset")
        raw = gzip.decompress(_get(c, CSOAI_FILE.format(name=files[-1])).content)
    rows = _drop_removed(parse_csoai(raw.splitlines()), "endpoint_url")
    _pending("csoai-census", "csoai-census")
    log.info("csoai-census %s: %d endpoint leads", files[-1], len(rows))
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
    return _drop_removed(assay_split(_assay_board())[0], "endpoint_url")


def fetch_assay_x402() -> list[dict]:
    return _drop_removed(assay_split(_assay_board())[2], "url")


def _agentindex_results(pause: float = 1.2) -> list[dict]:
    """All AgentIndex domains (Crawl-delay: 1)."""
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
    return out


def fetch_agentindex_x402() -> list[dict]:
    # x402 and A2A keep separate weekly keys: the A2A crawl runs hours later.
    if not _due("agentindex-x402", WEEK_DAYS):
        return []
    rows = _drop_removed(agentindex_split(_agentindex_results())[0], "url")
    _pending("agentindex", "agentindex-x402")
    return rows


def agentindex_fetch_leads() -> list[dict]:
    if not _due("agentindex-a2a", WEEK_DAYS):
        return []
    leads = agentindex_split(_agentindex_results())[1]
    _pending("a2a:agentindex", "agentindex-a2a")
    return leads


def fetch_x402lint(pause: float = 2.2, max_pages: int = 400) -> list[dict]:
    if not _due("x402lint", WEEK_DAYS):
        return []
    projects: list[dict] = []
    complete = False
    with _client() as c:
        for page in range(max_pages):
            d = _get(c, X402LINT_DIRECTORY, params={"page": page}).json()
            batch = d.get("projects") or []
            projects.extend(batch)
            if not batch or len(projects) >= int(d.get("count") or 0):
                complete = True
                break
            time.sleep(pause)
    rows = _drop_removed(x402lint_rows(projects), "url")
    if not complete:
        raise crawlers.PartialCrawl(rows, f"stopped after {max_pages} pages")
    _pending("x402lint", "x402lint")
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
        elif _key(url).lower() in known_urls:
            continue
        out.append(lead)
    return out


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
    leads = agentalog_leads(items)
    _pending("a2a:agentalog", "agentalog")
    return leads


A2A_LEAD_SOURCES: dict[str, Callable[[], list[dict]]] = {
    "agentalog": agentalog_fetch_leads,
    "assay": lambda: assay_split(_assay_board())[1],
    "agentindex": agentindex_fetch_leads,
}

MCP_FETCHERS = {
    "mcp-census": fetch_mcp_census,
    "csoai-census": fetch_csoai_census,
    "assay": fetch_assay_mcp,
}

X402_FETCHERS = {
    "x402lint": fetch_x402lint,
    "agentindex": fetch_agentindex_x402,
    "assay": fetch_assay_x402,
}


def crawl_a2a_peers(max_new: int = 400) -> dict:
    """Run every A2A lead source; returns per-source results and totals.

    Known cards/hosts are dropped *before* the per-host cap, so a big host
    whose first agents are already listed still yields new ones.
    """
    from . import a2a as a2a_mod
    with closing(db.connect(read_only=True)) as c:
        rows = c.execute("SELECT card_url, endpoint_url FROM a2a_agents").fetchall()
    known_urls = {_key(u).lower() for r in rows for u in r if u}
    known_hosts = {_host(u) for r in rows for u in r if u}
    out: dict = {"inserted": 0, "updated": 0, "errors": [], "sources": {}}
    for name, fn in A2A_LEAD_SOURCES.items():
        ok = False
        try:
            leads = _drop_known(fn(), known_urls, known_hosts)
            leads = _drop_removed(_capped(leads, "card_url"), "card_url")[:max_new]
            res = a2a_mod.crawl_card_leads(name, leads) if leads else {
                "candidates": 0, "resolved": 0, "inserted": 0, "updated": 0, "rejected": 0}
            ok = True
        except Exception as e:  # noqa: BLE001 -- one peer must not stop the rest
            out["errors"].append(f"{name}: {e!r}")
            log.warning("a2a peer %s failed: %r", name, e)
            continue
        finally:
            settle(f"a2a:{name}", ok)
        for lead in leads:  # later sources skip what this one already tried
            u = lead["card_url"]
            known_urls.add(_key(u).lower())
            if urlparse(u).path in ("", "/"):
                known_hosts.add(_host(u))
        out["sources"][name] = res
        out["inserted"] += res["inserted"]; out["updated"] += res["updated"]
        log.info("a2a peer %s: candidates=%d resolved=%d inserted=%d",
                 name, res["candidates"], res["resolved"], res["inserted"])
    return out
