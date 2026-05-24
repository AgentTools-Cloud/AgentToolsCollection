"""Data source crawlers for the agent-tools directory."""

from __future__ import annotations

import json
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
        # MCP-aware mapping: any entry under a heading mentioning MCP /
        # "model context protocol" is itself an MCP server URL.
        cat_lower = current_cat.lower()
        is_mcp_section = ("mcp" in cat_lower) or ("model context protocol" in cat_lower)
        mcp_url = url if is_mcp_section else None
        out.append({
            "slug": _host_slug(url) + "-" + _slugify(name)[:32],
            "name": name, "url": url,
            "description": desc or None,
            "category": _slugify(current_cat),
            "mcp_url": mcp_url,
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


# x402scan exposes its data via a public tRPC API.
# `public.origins.list.withResources` returns every origin (one row per
# x402-enabled site) with its resources, accepts, response metadata and tags.
# Same feed x402scan.com itself consumes; free + unauthenticated.
# tRPC GET convention: ?input={"json":<payload>} URL-encoded.
X402SCAN_TRPC_URL = (
    "https://www.x402scan.com/api/trpc/public.origins.list.withResources"
)

# USDC and most x402 stablecoins have 6 decimals.
_X402_TOKEN_DECIMALS = 6


def _max_amount_to_usd(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        amount = float(str(raw))
    except (TypeError, ValueError):
        return None
    return amount / (10**_X402_TOKEN_DECIMALS)


def fetch_x402scan() -> list:
    """Fetch the full x402scan origin list via its public tRPC endpoint.

    One service row per origin; resource accepts aggregated into the row.
    """
    params = {"input": json.dumps({"json": {}}, separators=(",", ":"))}
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, "Accept": "application/json"}) as c:
            r = c.get(X402SCAN_TRPC_URL, params=params)
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        log.warning("x402scan: tRPC fetch failed: %r", e)
        return []

    origins = (
        payload.get("result", {}).get("data", {}).get("json")
        if isinstance(payload, dict) else None
    )
    if not isinstance(origins, list):
        log.warning("x402scan: unexpected payload shape")
        return []

    rows = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        origin_url = origin.get("origin")
        if not origin_url:
            continue
        resources = origin.get("resources") or []
        if not resources:
            continue

        chains: set = set()
        prices: list = []
        tag_set: set = set()
        descriptions: list = []
        confidences: list = []
        tx_30d_total = 0
        mcp_server_urls: set = set()
        mcp_resource_urls: set = set()
        for res in resources:
            if not isinstance(res, dict):
                continue
            res_url = res.get("resource") or ""
            # Heuristic: any resource path ending with /mcp /sse /streamable
            # is almost certainly an MCP transport endpoint.
            if re.search(r"/(mcp|sse|streamable)(/|$)", res_url.lower()):
                mcp_resource_urls.add(res_url)
            for accept in res.get("accepts") or []:
                if not isinstance(accept, dict):
                    continue
                if accept.get("network"):
                    chains.add(str(accept["network"]).lower())
                price = _max_amount_to_usd(accept.get("maxAmountRequired"))
                if price is not None and price > 0:
                    prices.append(price)
                if accept.get("description"):
                    descriptions.append(str(accept["description"]).strip())
            # Higher-fidelity MCP signal: x402scan's resolved response object
            # at response.response.accepts[].extra.mcpServer (explicitly
            # declared by the service in its .well-known).
            rresp = (res.get("response") or {}).get("response") or {}
            for a in rresp.get("accepts") or []:
                if not isinstance(a, dict):
                    continue
                extra = a.get("extra")
                if isinstance(extra, dict) and extra.get("mcpServer"):
                    mcp_server_urls.add(str(extra["mcpServer"]))
            md = res.get("metadata") if isinstance(res.get("metadata"), dict) else None
            if md:
                conf = (md.get("confidence") or {}).get("overallScore")
                if isinstance(conf, (int, float)):
                    confidences.append(float(conf))
                pa = md.get("paymentAnalytics") or {}
                tx30 = pa.get("transactionsMonth")
                if isinstance(tx30, int):
                    tx_30d_total += tx30
            for tag_link in res.get("tags") or []:
                tag = (tag_link or {}).get("tag") if isinstance(tag_link, dict) else None
                if isinstance(tag, dict) and tag.get("name"):
                    tag_set.add(str(tag["name"]))

        name = origin.get("title") or urlparse(origin_url).hostname or origin_url
        description = origin.get("description") or (descriptions[0] if descriptions else None)
        if description and len(description) > 400:
            description = description[:397] + "..."
        category = _slugify(sorted(tag_set)[0]) if tag_set else "general"

        # mcp_url priority:
        #   1. explicit mcpServer from response.response.accepts.extra
        #   2. a resource whose path ends in /mcp /sse /streamable
        #   3. origin hostname contains 'mcp' (weak signal — but x402scan
        #      shows this is the most reliable one for actually-callable
        #      MCP services like mcp.cryptoiz.org, mcp.swissdeals.app)
        mcp_url = None
        if mcp_server_urls:
            mcp_url = sorted(mcp_server_urls)[0]
        elif mcp_resource_urls:
            mcp_url = sorted(mcp_resource_urls)[0]
        elif re.search(r"(^|[./_-])mcp[./_-]|//mcp\.", origin_url.lower()):
            mcp_url = origin_url

        rows.append({
            "slug": _host_slug(origin_url) + "-scan",
            "name": name, "url": origin_url,
            "description": description,
            "category": category,
            "chains": sorted(chains),
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "mcp_url": mcp_url,
            "well_known_url": origin_url.rstrip("/") + "/.well-known/x402",
            "confidence": max(confidences) if confidences else None,
            "tx_30d": tx_30d_total if tx_30d_total > 0 else None,
            "source": "x402scan", "source_id": str(origin.get("id") or origin_url),
            "tags": sorted(tag_set), "region": "global",
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
