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
            "directory of x402 paid APIs, A2A agents and MCP servers, and "
            "recommends payable endpoints for a given intent."
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
        "auth_schemes": list((card.get("securitySchemes") or {}).keys()) or None,
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
        with db.writer() as c:
            for row in rows:
                is_new, _ = db.upsert_a2a_agent(c, row)
                if is_new:
                    inserted += 1
                else:
                    updated += 1
            c.commit()
    return {
        "seen": len(entries),
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
        "failures": failures,
    }
