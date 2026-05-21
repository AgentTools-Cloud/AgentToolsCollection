"""Data source crawlers for the agent-tools directory."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("directory.crawlers")

UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unnamed"


def _host_slug(url: str) -> str:
    try:
        host = urlparse(url).hostname or url
    except Exception:
        host = url
    return _slugify(host)


def _normalize_chains(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,/|\s]+", raw)
        return [p.lower() for p in parts if p]
    if isinstance(raw, list):
        return [str(c).lower() for c in raw if c]
    return []


AWESOME_SOURCES = [
    ("merit-systems",
     "https://raw.githubusercontent.com/Merit-Systems/awesome-x402/master/README.md"),
    ("xpaysh",
     "https://raw.githubusercontent.com/xpaysh/awesome-x402/main/README.md"),
]

_LINE_RE = re.compile(
    r"^\s*[-*]\s+\[(?P<name>[^\]]+)\]\((?P<url>https?://[^)]+)\)"
    r"\s*(?:[-\u2013\u2014:]\s*(?P<desc>.+))?$"
)
_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(?P<heading>.+?)\s*$")


def _parse_awesome_md(text, source_tag):
    out = []
    current_cat = None
    for raw_line in text.splitlines():
        m_h = _HEADING_RE.match(raw_line)
        if m_h:
            heading = m_h.group("heading").strip()
            if any(k in heading.lower() for k in (
                "contents", "contributing", "license", "table of", "resources", "awesome"
            )):
                current_cat = None
            else:
                current_cat = heading
            continue
        m = _LINE_RE.match(raw_line)
        if not m or current_cat is None:
            continue
        name = m.group("name").strip()
        url = m.group("url").strip()
        desc = (m.group("desc") or "").strip()
        if not url.startswith("http"):
            continue
        out.append({
            "slug": _host_slug(url) + "-" + _slugify(name)[:32],
            "name": name, "url": url,
            "description": desc or None,
            "category": _slugify(current_cat),
            "source": "awesome-x402", "source_id": f"{source_tag}:{url}",
            "tags": [current_cat], "region": "global",
        })
    return out


CDP_BAZAAR_CANDIDATES = [
    "https://api.cdp.coinbase.com/platform/v1/x402/discovery/resources",
    "https://bazaar.coinbase.com/api/services",
]


def fetch_cdp_bazaar() -> list:
    payload = None
    last_err = None
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for url in CDP_BAZAAR_CANDIDATES:
            try:
                r = c.get(url)
                if r.status_code // 100 == 2 and "json" in r.headers.get("content-type", ""):
                    payload = r.json()
                    break
                last_err = f"{url} -> HTTP {r.status_code}"
            except Exception as e:
                last_err = f"{url} -> {e!r}"
    if payload is None:
        log.warning("cdp-bazaar: no endpoint reachable (last=%s)", last_err)
        return []

    items = []
    raw_list = (payload.get("resources") or payload.get("services") or payload.get("data")
                or (payload if isinstance(payload, list) else []))
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("endpoint") or item.get("resource")
        if not url:
            continue
        name = item.get("name") or item.get("title") or url
        price = item.get("price") or {}
        price_amount = None
        if isinstance(price, dict):
            try:
                price_amount = float(price.get("amount") or price.get("value") or 0) or None
            except (TypeError, ValueError):
                price_amount = None
        chains = _normalize_chains(item.get("chains") or item.get("network"))
        items.append({
            "slug": _host_slug(url) + "-bazaar",
            "name": name, "url": url,
            "description": item.get("description"),
            "category": _slugify(item.get("category") or "general"),
            "chains": chains,
            "price_min": price_amount, "price_max": price_amount,
            "facilitator": item.get("facilitator"),
            "openapi_url": item.get("openapi"),
            "mcp_url": item.get("mcp"),
            "well_known_url": item.get("well_known"),
            "source": "cdp-bazaar", "source_id": str(item.get("id") or url),
            "tags": item.get("tags") or [], "region": "global",
        })
    return items


X402SCAN_API_CANDIDATES = [
    "https://www.x402scan.com/api/resources",
    "https://x402scan.com/api/resources",
]


def fetch_x402scan() -> list:
    payload = None
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for url in X402SCAN_API_CANDIDATES:
            try:
                r = c.get(url)
                if r.status_code // 100 == 2 and "json" in r.headers.get("content-type", ""):
                    payload = r.json()
                    break
            except Exception:
                continue
    if payload is None:
        log.info("x402scan: JSON endpoint not reachable; skipping")
        return []

    rows = []
    raw = payload.get("resources") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("endpoint")
        if not url:
            continue
        name = item.get("name") or url
        rows.append({
            "slug": _host_slug(url) + "-scan",
            "name": name, "url": url,
            "description": item.get("description"),
            "category": _slugify(item.get("category") or "general"),
            "chains": _normalize_chains(item.get("chain") or item.get("chains")),
            "price_min": item.get("priceMin") or item.get("price"),
            "price_max": item.get("priceMax") or item.get("price"),
            "facilitator": item.get("facilitator"),
            "source": "x402scan", "source_id": str(item.get("id") or url),
            "tags": item.get("tags") or [], "region": "global",
        })
    return rows


def check_health(url, well_known=None):
    targets = [well_known] if well_known else []
    targets.append(url)
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                          headers={"User-Agent": UA}) as c:
            for t in targets:
                if not t:
                    continue
                try:
                    r = c.head(t)
                    if r.status_code in (405, 501):
                        r = c.get(t)
                    if r.status_code < 400 or r.status_code == 402:
                        return "ok"
                    if r.status_code in (401, 403, 429):
                        return "degraded"
                except Exception:
                    continue
        return "down"
    except Exception:
        return "down"


def fetch_awesome_x402() -> list:
    out = []
    for tag, url in AWESOME_SOURCES:
        try:
            with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
                r = c.get(url)
                r.raise_for_status()
                out.extend(_parse_awesome_md(r.text, tag))
        except Exception as e:
            log.warning("awesome %s fetch failed: %r", tag, e)
    return out


ALL_CRAWLERS = {
    "awesome-x402": fetch_awesome_x402,
    "cdp-bazaar": fetch_cdp_bazaar,
    "x402scan": fetch_x402scan,
}
