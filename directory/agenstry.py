"""Agenstry.com reverse-crawl source.

agenstry.com is a competing A2A/MCP directory. It advertises ~80k "indexed"
MCP servers but its own /api/stats shows ~97% are metadata-only GitHub/registry
rows with no live endpoint; only ~2.3k are actually alive. We therefore pull
its inventory through its JSON API rather than its (top-5000-truncated) sitemap:

  GET /api/mcp-servers?alive=true&limit=100&offset=N
       -> live, responding MCP servers (name/title/description/primary_url/...)
  /agents/<domain>  (sitemap)
       -> a live A2A agent (slug == its domain); we fetch its own Agent Card.

We treat agenstry as a *discovery* source only: we take the endpoint it lists,
then verify/probe it ourselves. x402 support is never inferred from text — it is
proven by directory.reverify_x402 probing each endpoint for an HTTP 402 / a
/.well-known/x402 descriptor.

skills/* are agenstry's auto-generated taxonomy (e.g. payments.crypto.*), not
URLs, so they do not map onto our concrete x402 `services` table.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from . import a2a as a2a_mod
from . import db

log = logging.getLogger("directory.agenstry")

SITEMAP = "https://agenstry.com/sitemap.xml"
API_BASE = "https://agenstry.com/api"
UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

# Anonymous clients are limited to 50 rows; larger pages require an API key.
_API_PAGE = 50


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "item"


def _host_slug(url: str) -> str:
    host = urlparse(url).netloc.lower() if "://" in (url or "") else (url or "").lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "host"


def _fetch_sitemap_paths() -> list[str]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(SITEMAP)
        r.raise_for_status()
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    return [u.strip() for u in locs]


# ---------------------------------------------------------------------------
# A2A agents
# ---------------------------------------------------------------------------
def crawl_agenstry_a2a(max_hosts: int = 4000, workers: int = 12) -> dict:
    """Discover A2A agents via agenstry, fetch each domain's live Agent Card."""
    try:
        paths = _fetch_sitemap_paths()
    except httpx.HTTPError as e:
        log.warning("agenstry sitemap fetch failed: %r", e)
        return {"candidates": 0, "resolved": 0, "inserted": 0, "updated": 0}

    domains: list[str] = []
    seen: set[str] = set()
    for u in paths:
        path = urlparse(u).path
        if not path.startswith("/agents/"):
            continue
        dom = path[len("/agents/"):].strip("/").lower()
        if not dom or "/" in dom or "." not in dom or dom in seen:
            continue
        seen.add(dom)
        domains.append(dom)
    domains = domains[:max_hosts]
    log.info("agenstry a2a: %d candidate domains", len(domains))

    rows: list[dict] = []

    def _probe(dom: str):
        base = f"https://{dom}"
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                              headers={"User-Agent": UA, "Accept": "application/json"}) as c:
                card, card_url = a2a_mod.fetch_agent_card(base, client=c)
        except Exception:
            return None
        if card and card_url:
            return a2a_mod.card_to_row(card, card_url, source="agenstry", source_id=dom)
        return None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for row in ex.map(_probe, domains):
            if row:
                rows.append(row)

    log.info("agenstry a2a: resolved %d live cards / %d domains", len(rows), len(domains))

    inserted = updated = 0
    if rows:
        def _write():
            ins = upd = 0
            with db.writer() as c:
                for row in rows:
                    is_new, _ = db.upsert_a2a_agent(c, row)
                    ins += int(is_new)
                    upd += int(not is_new)
                c.commit()
            return ins, upd
        inserted, updated = db.with_retry(_write)
    return {"candidates": len(domains), "resolved": len(rows),
            "inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# MCP servers  (registered into crawlers.MCP_CRAWLERS as "agenstry")
# ---------------------------------------------------------------------------
def _api_row_to_mcp(rec: dict) -> dict | None:
    """Map one /api/mcp-servers record to our mcp_servers upsert row."""
    endpoint = (rec.get("primary_url") or "").strip()
    if not endpoint:
        return None
    endpoint = endpoint.rstrip("/")
    host = urlparse(endpoint).netloc.lower()
    if not host or "agenstry.com" in host:
        return None
    aid = rec.get("name") or endpoint                 # e.g. "gvzq/flight-mcp"
    name = rec.get("title") or rec.get("name") or _host_slug(endpoint)
    transport = (rec.get("transport") or "").lower() or None
    if transport == "http" and endpoint.lower().endswith("/mcp"):
        transport = "streamable-http"
    out_slug = f"{_slugify(str(aid).replace('/', '-'))}-{_host_slug(endpoint)}"[:80]
    return {
        "slug": out_slug,
        "name": name,
        "description": (rec.get("description") or "").strip() or None,
        "homepage_url": f"https://{urlparse(endpoint).netloc}",
        "endpoint_url": endpoint,
        "transport": transport,
        "auth_method": None,
        # x402 is proven by reverify_x402 probing, never inferred here.
        "x402_supported": False,
        "source": "agenstry",
        "source_id": str(aid),
        "source_url": f"https://agenstry.com/mcp/{aid}",
        "confidence": 0.5,
    }


def fetch_agenstry_mcp(max_pages: int = 6000, workers: int = 12) -> list:
    """MCP crawler: pull every *alive* agenstry MCP server via its JSON API.

    Matches the MCP_CRAWLERS contract (returns a list of row dicts; jobs
    upserts them via db.upsert_mcp_server, which dedups by endpoint across
    sources so overlap with existing servers just refreshes them).

    `max_pages` caps the number of records (not HTTP pages) for trials.
    """
    out: list[dict] = []
    seen_eps: set[str] = set()
    offset = 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        while offset < max_pages:
            for attempt in range(2):
                try:
                    r = c.get(f"{API_BASE}/mcp-servers",
                              params={"alive": "true", "limit": _API_PAGE, "offset": offset})
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:
                    if attempt == 0:
                        log.warning("agenstry mcp api offset=%d failed, retrying: %r",
                                    offset, e)
                        time.sleep(1.0)
                        continue
                    raise RuntimeError(
                        f"agenstry mcp api offset={offset} failed after retry: {e!r}"
                    ) from e
            results = data.get("results") or []
            if not results:
                break
            for rec in results:
                row = _api_row_to_mcp(rec)
                if not row:
                    continue
                ep = row["endpoint_url"].lower()
                if ep in seen_eps:
                    continue
                seen_eps.add(ep)
                out.append(row)
            if len(results) < _API_PAGE:
                break
            offset += _API_PAGE
    log.info("agenstry mcp: pulled %d alive servers via api", len(out))
    return out
