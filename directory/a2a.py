"""A2A (Agent-to-Agent) support for the agent-tools directory.

Two roles:
  1. agent-tools.cloud is *itself* an A2A agent. `own_agent_card()` returns the
     Agent Card served at /.well-known/agent-card.json, and `handle_jsonrpc()`
     answers a minimal JSON-RPC `message/send` by routing the user's text to the
     directory search (x402 services + A2A agents).
  2. agent-tools.cloud *indexes* external A2A agents. `fetch_agent_card()` pulls
     a remote Agent Card and `card_to_row()` normalises it into an a2a_agents row.

Streaming, push notifications and the full task lifecycle are intentionally out
of scope for P0 -- we only need synchronous message/send.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from . import db

log = logging.getLogger("directory.a2a")

SITE = "https://agent-tools.cloud"
PROTOCOL_VERSION = "0.3.0"

UA = "agent-tools.cloud-a2a/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

# Card path variants seen across A2A directories / frameworks. Tried in order
# when only a homepage/base URL is known.
CARD_PATHS = (
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
    "/well_known/agent_json",
    "/.well-known/agent-card",
)


# ---------------------------------------------------------------------------
# Role 1: our own Agent Card + JSON-RPC endpoint
# ---------------------------------------------------------------------------

def own_agent_card(base_url: str = SITE) -> dict:
    """The A2A Agent Card describing agent-tools.cloud itself."""
    base = base_url.rstrip("/")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Agent Tools Directory",
        "description": (
            "Discovery agent for the agentic economy. Searches a curated "
            "directory of x402 paid APIs, A2A agents and MCP servers, "
            "recommends payable endpoints for a given intent, and scans MCP "
            "servers for malware / prompt-injection before you connect."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "provider": {
            "organization": "agent-tools.cloud",
            "url": base,
        },
        "version": "0.5.0",
        "documentationUrl": f"{base}/about",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "search_x402_services",
                "name": "Search x402 services",
                "description": "Find x402-payable APIs by intent, category or chain.",
                "tags": ["x402", "payments", "search", "api"],
                "examples": ["find a weather API that accepts x402 on Base"],
            },
            {
                "id": "search_a2a_agents",
                "name": "Search A2A agents",
                "description": "Find A2A agents by skill or capability.",
                "tags": ["a2a", "agents", "search"],
                "examples": ["find an A2A agent that can summarise PDFs"],
            },
            {
                "id": "search_mcp_servers",
                "name": "Search MCP servers",
                "description": "Find MCP servers and tools relevant to a task.",
                "tags": ["mcp", "tools", "search"],
                "examples": ["find an MCP server for GitHub"],
            },
            {
                "id": "get_service",
                "name": "Get service card",
                "description": "Return the full agent-readable card for one listing.",
                "tags": ["x402", "card", "detail"],
                "examples": ["show me the card for slug acme-weather"],
            },
            {
                "id": "get_agent",
                "name": "Get A2A agent",
                "description": "Return the indexed Agent Card for one A2A agent.",
                "tags": ["a2a", "card", "detail"],
                "examples": ["show me the agent card for slug acme-research"],
            },
            {
                "id": "recommend_paid_service",
                "name": "Recommend a paid service",
                "description": "Recommend the best payable endpoint for an intent.",
                "tags": ["x402", "recommendation", "payments"],
                "examples": ["I need to pay for on-demand OCR, what should I use?"],
            },
            {
                "id": "scan_mcp_safety",
                "name": "Scan MCP server safety",
                "description": (
                    "Check an MCP server (by streamable-http endpoint URL) for "
                    "malware / prompt-injection lures. Returns our stored verdict "
                    "if it is already indexed, otherwise probes + statically scans "
                    "it, adds a Qwen3-8B advisory second opinion, and indexes it."
                ),
                "tags": ["mcp", "security", "safety", "malware", "scan"],
                "examples": [
                    "is https://example.com/mcp safe to connect to?",
                    "scan https://foo.bar/mcp for malware",
                ],
            },
        ],
    }


def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _extract_text(message: dict) -> str:
    parts = message.get("parts") or []
    chunks: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("kind") == "text" or "text" in p:
            t = p.get("text")
            if t:
                chunks.append(str(t))
    return " ".join(chunks).strip()


def _agent_message(text: str, data: Any = None) -> dict:
    parts: list[dict] = [{"kind": "text", "text": text}]
    if data is not None:
        parts.append({"kind": "data", "data": data})
    return {
        "role": "agent",
        "parts": parts,
        "messageId": f"atc-{int(time.time() * 1000)}",
        "kind": "message",
    }


# Safety-scan intent: a request to vet an MCP server. Triggered either by an
# explicit structured data part ({skill: scan_mcp_safety, endpoint_url: ...}) or
# by free text that pairs a URL with a security-intent keyword.
_SCAN_URL_RE = re.compile(r"https?://\S+", re.I)
_SCAN_INTENT_RE = re.compile(
    r"\b(scan|safety|safe|malicious|malware|unsafe|phish|audit|vet|"
    r"trustworthy|legit|suspicious)\b",
    re.I,
)


def _safety_scan_request(message: dict, text: str) -> dict | None:
    """Return scan kwargs if this message is a safety-scan request, else None."""
    # 1. explicit structured request via a data part
    for p in (message.get("parts") or []):
        if isinstance(p, dict) and p.get("kind") == "data" and isinstance(p.get("data"), dict):
            d = p["data"]
            skill = str(d.get("skill") or d.get("id") or d.get("intent") or "").lower()
            url = d.get("endpoint_url") or d.get("url")
            if url and ("safety" in skill or "scan" in skill or "malic" in skill):
                return {
                    "endpoint_url": str(url),
                    "name": str(d.get("name") or ""),
                    "description": str(d.get("description") or ""),
                    "tools_text": str(d.get("tools_text") or ""),
                }
    # 2. free text: a URL paired with a security-intent keyword
    if text and _SCAN_INTENT_RE.search(text):
        m = _SCAN_URL_RE.search(text)
        if m:
            return {"endpoint_url": m.group(0).rstrip(".,);]}'\""),
                    "name": "", "description": "", "tools_text": ""}
    return None


def handle_jsonrpc(payload: dict) -> dict:
    """Minimal JSON-RPC handler. Supports `message/send` only."""
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    req_id = payload.get("id")
    method = payload.get("method")
    if method not in ("message/send", "message/stream"):
        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    params = payload.get("params") or {}
    message = params.get("message") or {}
    text = _extract_text(message)

    # Safety-scan skill: vet an MCP server by endpoint URL via the shared core.
    scan_req = _safety_scan_request(message, text)
    if scan_req is not None:
        from . import safety_service
        try:
            verdict = safety_service.scan_endpoint(**scan_req)
        except Exception:
            log.exception("a2a message/send: safety scan failed")
            return _jsonrpc_result(
                req_id, _agent_message("Safety scan failed.", data={"error": "scan_failed"}))
        v = verdict.get("verdict")
        if v:
            summary = (
                f"Safety verdict for {verdict.get('endpoint_url')}: {v} "
                f"(score {verdict.get('score')}; source {verdict.get('source')})."
            )
            adv = verdict.get("advisory")
            if adv:
                summary += " " + adv
        else:
            summary = f"Could not scan: {verdict.get('message') or verdict.get('error')}"
        return _jsonrpc_result(req_id, _agent_message(summary, data=verdict))

    if not text:
        return _jsonrpc_error(req_id, -32602, "params.message must contain a text part")

    services: list[dict] = []
    agents: list[dict] = []
    with db.connect(read_only=True) as c:
        try:
            services = db.search(c, q=text, limit=5)
        except Exception:
            log.exception("a2a message/send: service search failed")
        try:
            agents = db.search_a2a(c, q=text, limit=5)
        except Exception:
            log.exception("a2a message/send: agent search failed")

    svc_brief = [
        {
            "slug": s.get("slug"),
            "name": s.get("name"),
            "category": s.get("category"),
            "chains": s.get("chains"),
            "url": f"{SITE}/api/v1/services/{s.get('slug')}",
        }
        for s in services
    ]
    agent_brief = [
        {
            "slug": a.get("slug"),
            "name": a.get("name"),
            "endpoint_url": a.get("endpoint_url"),
            "url": f"{SITE}/api/v1/a2a/agents/{a.get('slug')}",
        }
        for a in agents
    ]

    if svc_brief or agent_brief:
        summary = (
            f"Found {len(svc_brief)} x402 service(s) and {len(agent_brief)} "
            f"A2A agent(s) for: {text}"
        )
    else:
        summary = f"No directory matches for: {text}"

    result_message = _agent_message(
        summary, data={"services": svc_brief, "agents": agent_brief}
    )
    return _jsonrpc_result(req_id, result_message)


# ---------------------------------------------------------------------------
# Role 2: indexing external A2A agents
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "agent"


def _host_slug(url: str) -> str:
    try:
        host = urlparse(url).hostname or url
    except Exception:
        host = url
    return _slugify((host or "agent").replace(".", "-"))


def fetch_agent_card(url: str, client: httpx.Client | None = None) -> tuple[dict | None, str | None]:
    """Fetch a remote Agent Card.

    `url` may be a direct card URL or a homepage/base URL (we then try the
    well-known card paths). Returns (card_dict, resolved_card_url).
    """
    own = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        candidates: list[str]
        path = urlparse(url).path or "/"
        if path.endswith(".json") or "agent" in path.lower():
            candidates = [url]
        else:
            candidates = [urljoin(url, p) for p in CARD_PATHS]
            candidates.insert(0, url)
        for cand in candidates:
            try:
                r = own.get(cand)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            ctype = r.headers.get("content-type", "")
            try:
                card = r.json()
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(card, dict) and (card.get("name") or card.get("skills")):
                return card, str(r.url)
            if "json" not in ctype:
                continue
        return None, None
    finally:
        if client is None:
            own.close()


def _card_endpoint(card: dict, card_url: str) -> str | None:
    url = card.get("url") or card.get("endpoint") or card.get("endpointUrl")
    if url:
        return str(url)
    interfaces = card.get("additionalInterfaces") or card.get("interfaces")
    if isinstance(interfaces, list):
        for itf in interfaces:
            if isinstance(itf, dict) and itf.get("url"):
                return str(itf["url"])
    return None


def _detect_x402(card: dict) -> tuple[bool, str | None]:
    blob = json.dumps(card, ensure_ascii=False).lower()
    supported = "x402" in blob or "402" in (str(card.get("security") or "")).lower()
    payto = None
    sec = card.get("securitySchemes") or {}
    if isinstance(sec, dict):
        for v in sec.values():
            if isinstance(v, dict):
                addr = v.get("payTo") or v.get("payto") or v.get("address")
                if addr:
                    payto = str(addr)
                    break
    return ("x402" in blob), payto


def _auth_scheme_names(card: dict) -> list[str] | None:
    """Scheme labels from an Agent Card.

    The spec says securitySchemes is a name->scheme object, but some agents
    publish a bare list of scheme objects instead.
    """
    sec = card.get("securitySchemes")
    if isinstance(sec, dict):
        names = [str(k) for k in sec.keys()]
    elif isinstance(sec, list):
        names = []
        for item in sec:
            if isinstance(item, dict):
                label = item.get("type") or item.get("scheme") or item.get("name")
                if label:
                    names.append(str(label))
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
    else:
        return None
    return names or None


def card_to_row(card: dict, card_url: str, source: str = "manual",
                source_id: str | None = None, slug: str | None = None) -> dict:
    """Normalise an A2A Agent Card into an a2a_agents row dict."""
    name = str(card.get("name") or "Unnamed agent").strip()
    endpoint = _card_endpoint(card, card_url)
    provider = card.get("provider") or {}
    if not isinstance(provider, dict):
        provider = {}
    x402_supported, payto = _detect_x402(card)
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    caps = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}

    if not slug:
        slug = _slugify(name)
        if not slug or slug == "agent":
            slug = _host_slug(endpoint or card_url)

    return {
        "slug": slug,
        "name": name,
        "description": (card.get("description") or "").strip() or None,
        "provider_name": provider.get("organization") or provider.get("name"),
        "provider_url": provider.get("url"),
        "card_url": card_url,
        "endpoint_url": endpoint,
        "homepage_url": provider.get("url"),
        "documentation_url": card.get("documentationUrl") or card.get("documentationURL"),
        "protocol_version": card.get("protocolVersion") or card.get("version"),
        "preferred_transport": card.get("preferredTransport"),
        "skills": skills,
        "capabilities": caps,
        "default_input_modes": card.get("defaultInputModes"),
        "default_output_modes": card.get("defaultOutputModes"),
        "auth_schemes": _auth_scheme_names(card),
        "x402_supported": x402_supported,
        "price_hint_usd": None,
        "payto": payto,
        "source": source,
        "source_id": source_id or card_url,
        "confidence": 0.6,
    }


def public_agent(row: dict) -> dict:
    """Public, agent-readable view of an indexed A2A agent."""
    d = dict(row)
    d.pop("source", None)
    d.pop("source_id", None)
    skills = d.get("skills")
    if isinstance(skills, list):
        d["skills"] = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description"),
                "tags": s.get("tags"),
            }
            for s in skills
            if isinstance(s, dict)
        ]
    d["card_view_url"] = f"{SITE}/api/v1/a2a/agents/{d.get('slug')}"
    return d


def crawl_seeds(seed_path: str) -> dict:
    """Fetch every seed card URL and upsert resolvable agents. Returns stats."""
    with open(seed_path, "r", encoding="utf-8") as f:
        seed = json.load(f)
    entries = seed.get("agents") or []
    inserted = updated = failed = 0
    failures: list[str] = []
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA, "Accept": "application/json"})
    rows: list[dict] = []
    try:
        for entry in entries:
            url = entry.get("url")
            if not url:
                continue
            source = entry.get("source") or "manual"
            card, card_url = fetch_agent_card(url, client=client)
            if not card or not card_url:
                failed += 1
                failures.append(url)
                continue
            rows.append(card_to_row(card, card_url, source=source))
    finally:
        client.close()

    if rows:
        def _write_seeds():
            ins = upd = 0
            with db.writer() as c:
                for row in rows:
                    is_new, _ = db.upsert_a2a_agent(c, row)
                    if is_new:
                        ins += 1
                    else:
                        upd += 1
                c.commit()
            return ins, upd
        inserted, updated = db.with_retry(_write_seeds)
    return {
        "seen": len(entries),
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Directory crawlers: discover live Agent Cards from public "awesome-a2a"
# lists. We extract candidate homepages, dedupe by host and probe each for a
# well-known Agent Card. Hit rate is low (most entries are GitHub repos, not
# live endpoints) but every hit is a real, callable A2A agent.
# ---------------------------------------------------------------------------

A2A_DIR_SOURCES = [
    ("awesome-a2a-aiboost",
     "https://raw.githubusercontent.com/ai-boost/awesome-a2a/main/README.md"),
    ("awesome-a2a-pab1it0",
     "https://raw.githubusercontent.com/pab1it0/awesome-a2a/main/README.md"),
]

_MD_LINK_RE = re.compile(r"\((https?://[^)\s]+)\)")
# hosts that never serve an Agent Card -- skip to avoid wasted probes
_SKIP_HOSTS = (
    "github.com", "raw.githubusercontent.com", "gist.github.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "medium.com",
    "linkedin.com", "discord.gg", "discord.com", "t.me", "reddit.com",
    "npmjs.com", "pypi.org", "awesome.re", "img.shields.io", "shields.io",
    "google.com", "docs.google.com", "notion.so", "substack.com",
)


def _extract_candidate_homepages(text: str) -> list[str]:
    """Pull non-repo http(s) homepages from a markdown list, deduped by host."""
    out: list[str] = []
    seen_hosts: set[str] = set()
    for m in _MD_LINK_RE.finditer(text):
        url = m.group(1).rstrip(".,);")
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            continue
        if not host or any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
            continue
        # normalise to scheme://host (probe well-known paths from the root)
        base = f"{urlparse(url).scheme}://{host}"
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        out.append(base)
    return out


def crawl_directories(max_hosts: int = 80) -> dict:
    """Crawl awesome-a2a lists, probe candidate homepages for Agent Cards."""
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA, "Accept": "application/json"})
    candidates: list[str] = []
    seen_hosts: set[str] = set()
    try:
        for tag, url in A2A_DIR_SOURCES:
            try:
                r = client.get(url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("a2a dir source %s failed: %r", tag, e)
                continue
            for base in _extract_candidate_homepages(r.text):
                host = urlparse(base).hostname or base
                if host in seen_hosts:
                    continue
                seen_hosts.add(host)
                candidates.append(base)

        candidates = candidates[:max_hosts]
        log.info("a2a directories: probing %d candidate hosts", len(candidates))
        rows: list[dict] = []
        for base in candidates:
            try:
                card, card_url = fetch_agent_card(base, client=client)
            except Exception:
                card, card_url = None, None
            if card and card_url:
                rows.append(card_to_row(card, card_url, source="awesome-a2a"))
    finally:
        client.close()

    inserted = updated = 0
    if rows:
        def _write_dirs():
            ins = upd = 0
            with db.writer() as c:
                for row in rows:
                    is_new, _ = db.upsert_a2a_agent(c, row)
                    if is_new:
                        ins += 1
                    else:
                        upd += 1
                c.commit()
            return ins, upd
        inserted, updated = db.with_retry(_write_dirs)
    return {
        "candidates": len(candidates),
        "resolved": len(rows),
        "inserted": inserted,
        "updated": updated,
    }


A2AREGISTRY_API = "https://a2aregistry.org/api/agents"


def crawl_a2aregistry(page_size: int = 100, max_pages: int = 20) -> dict:
    """Index a2aregistry.org. Each /api/agents item already IS a full A2A Agent
    Card (plus registry metadata), so we map it directly with card_to_row -- no
    per-agent re-fetch needed."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for page in range(max_pages):
            offset = page * page_size
            try:
                r = c.get(A2AREGISTRY_API, params={"limit": page_size, "offset": offset})
                r.raise_for_status()
                payload = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("a2aregistry: fetch error at offset %d: %r", offset, e)
                break
            agents = payload.get("agents") if isinstance(payload, dict) else None
            if not agents:
                break
            for a in agents:
                if not isinstance(a, dict) or a.get("hidden"):
                    continue
                aid = str(a.get("id") or a.get("url") or "")
                if not aid or aid in seen_ids:
                    continue
                seen_ids.add(aid)
                card_url = a.get("wellKnownURI") or a.get("url")
                if not card_url:
                    continue
                try:
                    row = card_to_row(a, card_url, source="a2aregistry", source_id=aid)
                except Exception as e:
                    log.warning("a2aregistry: card_to_row failed for %s: %r", aid, e)
                    continue
                rows.append(row)
            total = payload.get("total") or 0
            if offset + len(agents) >= total or len(agents) < page_size:
                break
    log.info("a2aregistry: mapped %d agents", len(rows))

    inserted = updated = 0
    if rows:
        def _write():
            ins = upd = 0
            with db.writer() as conn:
                for row in rows:
                    is_new, _ = db.upsert_a2a_agent(conn, row)
                    ins += int(is_new); upd += int(not is_new)
                conn.commit()
            return ins, upd
        inserted, updated = db.with_retry(_write)
    return {"candidates": len(rows), "resolved": len(rows),
            "inserted": inserted, "updated": updated}


GITHUB_TOPIC_SEARCH = "https://api.github.com/search/repositories"


def crawl_github_topic(topic: str = "a2a-protocol", max_repos: int = 100,
                       max_hosts: int = 120) -> dict:
    """Discover A2A agents from GitHub repos tagged with an A2A topic: take each
    repo's homepage and probe it for a live Agent Card. Uses GITHUB_TOKEN from
    the environment if present (raises the search rate limit), else unauth."""
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    homepages: list[str] = []
    seen_hosts: set[str] = set()
    per_page = min(100, max_repos)
    pages = max(1, (max_repos + per_page - 1) // per_page)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as c:
        for page in range(1, pages + 1):
            try:
                r = c.get(GITHUB_TOPIC_SEARCH, params={
                    "q": f"topic:{topic}", "per_page": per_page,
                    "page": page, "sort": "stars", "order": "desc"})
                if r.status_code == 403:
                    log.warning("github topic: rate-limited (no token); stopping")
                    break
                r.raise_for_status()
                items = r.json().get("items", [])
            except (httpx.HTTPError, ValueError) as e:
                log.warning("github topic: search error page %d: %r", page, e)
                break
            if not items:
                break
            for it in items:
                hp = (it.get("homepage") or "").strip()
                if not hp or not hp.lower().startswith("http"):
                    continue
                host = urlparse(hp).hostname or hp
                if not host or host in seen_hosts:
                    continue
                # skip docs/spec hubs that are not themselves agents
                if host in ("a2a-protocol.org", "github.com", "github.io"):
                    continue
                seen_hosts.add(host)
                homepages.append(hp)
            if len(items) < per_page:
                break

    homepages = homepages[:max_hosts]
    log.info("github topic %s: probing %d candidate homepages", topic, len(homepages))

    rows: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for hp in homepages:
            try:
                card, card_url = fetch_agent_card(hp, client=c)
            except Exception:
                card, card_url = None, None
            if card and card_url:
                rows.append(card_to_row(card, card_url, source="github-topic"))
    log.info("github topic %s: resolved %d live cards", topic, len(rows))

    inserted = updated = 0
    if rows:
        def _write():
            ins = upd = 0
            with db.writer() as conn:
                for row in rows:
                    is_new, _ = db.upsert_a2a_agent(conn, row)
                    ins += int(is_new); upd += int(not is_new)
                conn.commit()
            return ins, upd
        inserted, updated = db.with_retry(_write)
    return {"candidates": len(homepages), "resolved": len(rows),
            "inserted": inserted, "updated": updated}


def probe_a2a_health(card_url: str | None, endpoint_url: str | None = None) -> dict:
    """Liveness probe for an indexed A2A agent.

    A reachable Agent Card (valid JSON) means the agent is published and
    discoverable -> 'ok'. A reachable-but-not-a-card response -> 'degraded'.
    Unreachable card with a reachable endpoint -> 'degraded'. Otherwise down.
    """
    targets = [t for t in (card_url, endpoint_url) if t]
    if not targets:
        return {"status": "unknown", "latency_ms": None, "http_status": None,
                "conformance": None}
    last = {"status": "down", "latency_ms": None, "http_status": None,
            "conformance": None}
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                          follow_redirects=True,
                          headers={"User-Agent": UA, "Accept": "application/json"}) as c:
            for i, t in enumerate(targets):
                try:
                    t0 = time.monotonic()
                    r = c.get(t)
                    dt = int((time.monotonic() - t0) * 1000)
                    sc = r.status_code
                    if i == 0 and sc == 200:
                        try:
                            card = r.json()
                            if isinstance(card, dict) and (card.get("name") or card.get("skills")):
                                # Tier-2 conformance: a proper Agent Card declares
                                # both a name and a non-empty skills array.
                                skills = card.get("skills")
                                conf = ("pass" if (card.get("name") and isinstance(skills, list) and skills)
                                        else "partial")
                                return {"status": "ok", "latency_ms": dt,
                                        "http_status": sc, "conformance": conf}
                        except (ValueError, json.JSONDecodeError):
                            pass
                    if sc < 500:
                        last = {"status": "degraded", "latency_ms": dt,
                                "http_status": sc, "conformance": "fail"}
                except Exception:
                    continue
        return last
    except Exception:
        return {"status": "down", "latency_ms": None, "http_status": None,
                "conformance": None}
