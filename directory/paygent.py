"""Paygent Discover reverse-crawl source.

discover.paygent.net ("Paygent Discover") is a competing reliability index for
the agent-payment web. It probes x402 endpoints / MCP servers / A2A agents and
scores them on uptime, latency and security, exposing a public JSON API:

  GET https://discover-api.paygent.net/v1/services?limit=100&offset=N
      -> {count, results:[{id, kind, resource, operator, category,
                           category_slug, currencies, networks,
                           reputation{composite, confidence, availability,
                           latency, security, ...}, status, ...}]}

Paygent's catalogue is ~4000+ entries and is mostly MCP (~2.5k) + A2A (~0.5k),
which we already cover richly via smithery/pulsemcp/agenstry/etc. and which
belong in the mcp_servers / a2a_agents tables. So this source ingests ONLY the
*payment* kinds (x402 / mpp / l402, ~900) into the x402 `services` table -- the
one thing paygent is uniquely authoritative on.

Discovery-only: we take the endpoint it lists (`resource`), mirror it, and
health-probe / reverify_x402 it ourselves. Paygent's reputation.confidence is
carried forward as our `confidence`; x402 support is still proven by
directory.reverify_x402 (never inferred from text).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from .crawlers import UA, TIMEOUT, _host_slug, _normalize_chains

log = logging.getLogger("directory.paygent")

_API = "https://discover-api.paygent.net/v1/services"
_PAGE = 100
_MAX_PAGES = 60  # safety cap (60*100 = 6000)
_PAYMENT_KINDS = {"x402", "mpp", "l402"}


def _map(item: dict, seen: set) -> dict | None:
    kind = (item.get("kind") or "x402").strip().lower()
    if kind not in _PAYMENT_KINDS:
        return None
    resource = (item.get("resource") or "").strip()
    sid = (item.get("id") or "").strip()
    if not resource or not sid or sid in seen:
        return None
    try:
        p = urlparse(resource)
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    seen.add(sid)
    origin = f"{p.scheme}://{p.netloc}"
    host = p.hostname
    rep = item.get("reputation") or {}
    conf = rep.get("confidence")
    composite = rep.get("composite")
    currencies = item.get("currencies") or []
    desc = (item.get("description") or "").strip()
    score_txt = (
        f"reliability {composite}/100" if isinstance(composite, (int, float))
        else "reliability-scored"
    )
    name = (item.get("title") or item.get("operator") or host or "").strip() or host
    tags = ["paygent", "reputation-scored", kind]
    if item.get("category_slug"):
        tags.append(str(item["category_slug"]))
    if item.get("status"):
        tags.append(str(item["status"]))
    return {
        "slug": f"{_host_slug(origin)}-pg-{sid[:8]}",
        "name": name,
        "url": origin,
        "description": (desc + (" -- " if desc else "") + f"{score_txt} per Paygent Discover")[:500],
        "category": "general",
        "chains": _normalize_chains(item.get("networks")),
        "currency": (currencies[0] if currencies else None),
        "well_known_url": origin + "/.well-known/x402",
        "confidence": conf if isinstance(conf, (int, float)) else None,
        "resource_samples": [{"url": resource, "kind": f"{kind}-resource"}],
        "source": "paygent-discover",
        "source_id": sid,
        "tags": tags,
        "region": "global",
    }


def fetch_paygent_discover() -> list:
    out: list = []
    seen: set = set()
    total_seen = 0
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            for page in range(_MAX_PAGES):
                r = c.get(_API, params={"limit": _PAGE, "offset": page * _PAGE})
                r.raise_for_status()
                results = r.json().get("results") or []
                if not results:
                    break
                total_seen += len(results)
                for item in results:
                    row = _map(item, seen)
                    if row:
                        out.append(row)
                if len(results) < _PAGE:
                    break
    except Exception as e:  # noqa: BLE001
        log.warning("paygent-discover fetch failed: %r", e)
        return out
    log.info("paygent-discover: scanned %d catalogue entries -> %d payment services", total_seen, len(out))
    return out
