"""Data source crawlers for the agent-tools directory."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone

import yaml
from typing import Any
from urllib.parse import urlparse

import os
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
            "resource_count": 1,
            "resource_samples": [{"url": url, "kind": "mcp" if mcp_url else "homepage"}],
            "call_info": {"resource_samples": [{"url": url, "kind": "mcp" if mcp_url else "homepage"}]},
            "source": "awesome-x402", "source_id": f"{source_tag}:{url}",
            "tags": [current_cat], "region": "global",
        })
    return out


# CDP Bazaar (Coinbase) public x402 discovery feed. The v2 endpoint is fully
# public (no auth / no KYC); the old v1 path returns 401 and the bazaar.coinbase
# subdomain does not resolve. The feed is resource/path-level (~30k rows) while
# our directory is host-level, so we aggregate by host.
CDP_BAZAAR_V2 = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"

# USDC contracts (6 decimals) on supported chains, lowercased. Used to turn a
# bazaar accept ``amount`` (atomic units) into a USD price.
_BAZAAR_USDC = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # base
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # ethereum
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # optimism
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # polygon
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # arbitrum
}


def _bazaar_price_usd(accept: dict):
    """USD price from a bazaar accept entry (USDC atomic amount / 1e6)."""
    amt = accept.get("amount")
    if amt in (None, ""):
        amt = accept.get("maxAmountRequired")
    if amt in (None, ""):
        return None
    try:
        amt = float(amt)
    except (TypeError, ValueError):
        return None
    asset = (accept.get("asset") or "").lower()
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    name = (extra.get("name") or "").lower()
    if asset not in _BAZAAR_USDC and name not in ("usdc", "usd coin"):
        return None
    return (amt / 1_000_000) or None


def _bazaar_host(url: str) -> str:
    if not url or "://" not in url:
        return ""
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _bazaar_fetch_raw(page_sleep: float = 0.3, max_pages: int = 400,
                      since_iso: str | None = None) -> list:
    """Page through the public CDP Bazaar v2 discovery feed.

    The v2 feed has no reliable server-side time filter and its
    ``sortBy=lastUpdated&order=desc`` is NOT monotone across pages (verified
    2026-06-05: deeper offsets can hold newer items), so we cannot early-stop on
    a sorted boundary without skipping recent updates. Instead, when
    ``since_iso`` is given we page through everything in natural order but keep
    only items with ``lastUpdated >= since_iso``. The aggregate/dedup/upsert
    work downstream then only touches the recently-changed tail.
    """
    items: list = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        offset = 0
        for _ in range(max_pages):
            params = {"limit": 100, "offset": offset}
            try:
                r = c.get(CDP_BAZAAR_V2, params=params)
                if r.status_code == 429:
                    time.sleep(2.5)
                    r = c.get(CDP_BAZAAR_V2, params=params)
                if r.status_code != 200:
                    log.warning("cdp-bazaar: stop HTTP %d at offset %d", r.status_code, offset)
                    break
                d = r.json()
            except Exception as e:
                log.warning("cdp-bazaar: fetch error at offset %d: %r", offset, e)
                break
            page = d.get("items") or []
            if not page:
                break
            if since_iso:
                for it in page:
                    if (it.get("lastUpdated") or "") >= since_iso:
                        items.append(it)
            else:
                items.extend(page)
            total = (d.get("pagination") or {}).get("total", 0)
            offset += 100
            if offset >= total:
                break
            time.sleep(page_sleep)
    return items


def fetch_cdp_bazaar() -> list:
    """CDP Bazaar x402 discovery -> host-level service entries.

    Aggregates the resource/path-level feed by host. To keep one entry per
    service, a host already listed by a *different* source is skipped here
    (cross-source dedup); hosts new to the directory -- or previously added by
    this same source -- are emitted and upserted normally.

    Incremental: a ``cdp_bazaar:updated_since`` watermark in the meta table
    bounds each run to resources changed since the last crawl (minus a safety
    overlap), so the routine 6h timer pulls only the recent tail. A cold start
    (no watermark) does a full crawl.
    """
    from . import db

    _OVERLAP_S = 12 * 3600  # re-scan a 12h overlap to absorb in-page jitter
    since_iso = None
    try:
        with db.connect(read_only=True) as conn:
            wm = db.get_meta(conn, "cdp_bazaar:updated_since")
        if wm:
            t = time.strptime(wm[:19], "%Y-%m-%dT%H:%M:%S")
            since_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z",
                time.gmtime(time.mktime(t) - time.timezone - _OVERLAP_S),
            )
    except Exception as e:
        log.warning("cdp-bazaar: watermark read failed, full crawl: %r", e)
        since_iso = None

    raw = _bazaar_fetch_raw(since_iso=since_iso)
    if not raw:
        return []

    # Advance watermark to the newest lastUpdated seen this run.
    max_seen = ""
    for it in raw:
        lu = (it.get("lastUpdated") or "") if isinstance(it, dict) else ""
        if lu > max_seen:
            max_seen = lu
    if max_seen:
        try:
            with db.writer() as conn:
                db.set_meta(conn, "cdp_bazaar:updated_since", max_seen)
        except Exception as e:
            log.warning("cdp-bazaar: watermark write failed: %r", e)

    by_host: dict = {}
    for it in raw:
        if not isinstance(it, dict):
            continue
        host = _bazaar_host(it.get("resource") or "")
        if host:
            by_host.setdefault(host, []).append(it)

    other_hosts: set = set()
    try:
        from . import db
        with db.connect(read_only=True) as conn:
            for r in conn.execute(
                "SELECT url, source FROM services WHERE url IS NOT NULL AND url != ''"
            ):
                if r["source"] == "cdp-bazaar":
                    continue
                h = _bazaar_host(r["url"] or "")
                if h:
                    other_hosts.add(h)
    except Exception as e:
        log.warning("cdp-bazaar: dedup preload failed, may list dups: %r", e)

    out = []
    for host, resources in sorted(by_host.items()):
        if host in other_hosts:
            continue
        origin = "https://" + host
        chains: set = set()
        prices: list = []
        samples: list = []
        descriptions: list = []
        payto = None
        mcp_url = None
        for it in resources:
            res = it.get("resource") or ""
            if it.get("description"):
                descriptions.append(it["description"])
            for a in (it.get("accepts") or []):
                if a.get("network"):
                    chains.update(_normalize_chains(a.get("network")))
                p = _bazaar_price_usd(a)
                if p is not None:
                    prices.append(p)
                if payto is None and a.get("payTo"):
                    payto = a.get("payTo")
            if len(samples) < 12:
                samples.append({"url": res, "kind": it.get("type") or "x402-resource"})
            low = res.lower()
            if mcp_url is None and re.search(r"(^|[./_-])mcp([./_-]|$)|/sse|/streamable", low):
                mcp_url = res
        description = descriptions[0] if descriptions else None
        if description and len(description) > 400:
            description = description[:397] + "..."
        out.append({
            "slug": _host_slug(origin) + "-bazaar",
            "name": host,
            "url": origin,
            "description": description,
            "category": "general",
            "chains": sorted(chains),
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "mcp_url": mcp_url,
            "well_known_url": origin + "/.well-known/x402",
            "resource_count": len(resources),
            "resource_samples": samples,
            "payment": {
                "price_min_usd": min(prices) if prices else None,
                "price_max_usd": max(prices) if prices else None,
                "chains": sorted(chains),
                "pay_to": payto,
            },
            "call_info": {"resource_count": len(resources), "resource_samples": samples},
            "source": "cdp-bazaar",
            "source_id": host,
            "tags": [],
            "region": "global",
        })
    log.info("cdp-bazaar: %d resources -> %d hosts, %d new after cross-source dedup",
             len(raw), len(by_host), len(out))
    return out


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


def _resource_accepts(res: dict[str, Any]) -> list[dict[str, Any]]:
    accepts: list[dict[str, Any]] = []
    direct = res.get("accepts") or []
    if isinstance(direct, list):
        accepts.extend([a for a in direct if isinstance(a, dict)])
    resolved = (res.get("response") or {}).get("response") or {}
    resolved_accepts = resolved.get("accepts") or []
    if isinstance(resolved_accepts, list):
        accepts.extend([a for a in resolved_accepts if isinstance(a, dict)])
    return accepts


def _accept_summary(accept: dict[str, Any], resource_url: str | None = None) -> dict[str, Any]:
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    price_usd = _max_amount_to_usd(accept.get("maxAmountRequired"))
    return {
        "scheme": accept.get("scheme"),
        "network": str(accept.get("network")).lower() if accept.get("network") else None,
        "asset": accept.get("asset"),
        "pay_to": accept.get("payTo"),
        "max_amount_required": accept.get("maxAmountRequired"),
        "estimated_usd": price_usd,
        "resource": accept.get("resource") or resource_url,
        "description": accept.get("description"),
        "mime_type": accept.get("mimeType"),
        "mcp_server": extra.get("mcpServer"),
    }


def _quality_summary(md: dict[str, Any] | None) -> dict[str, Any]:
    if not md:
        return {}
    confidence = md.get("confidence") or {}
    payment = md.get("paymentAnalytics") or {}
    reliability = md.get("reliability") or {}
    performance = md.get("performance") or {}
    return {
        "confidence": confidence.get("overallScore"),
        "payment_analytics": {
            "transactions_month": payment.get("transactionsMonth"),
            "volume_month_usd": payment.get("volumeMonthUsd"),
        },
        "reliability": {
            "success_rate": reliability.get("successRate"),
            "uptime": reliability.get("uptime"),
        },
        "performance": {
            "p50_ms": performance.get("p50"),
            "p95_ms": performance.get("p95"),
        },
    }


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
        resource_samples: list[dict[str, Any]] = []
        accept_samples: list[dict[str, Any]] = []
        quality_samples: list[dict[str, Any]] = []
        mcp_server_urls: set = set()
        mcp_resource_urls: set = set()
        for res in resources:
            if not isinstance(res, dict):
                continue
            res_url = res.get("resource") or ""
            # Heuristic: any resource path ending with /mcp /sse /streamable
            # is almost certainly an MCP transport endpoint. Trim back to
            # that segment so we keep the server root, not a tool-specific
            # sub-path like /mcp/clean-context/__tool__/foo.
            mm = re.search(r"^(.+?/(mcp|sse|streamable))(/|$)", res_url.lower())
            if mm:
                mcp_resource_urls.add(res_url[: mm.end(1)])
            accepts = _resource_accepts(res)
            accept_summaries: list[dict[str, Any]] = []
            local_description = None
            for accept in accepts:
                if not isinstance(accept, dict):
                    continue
                if accept.get("network"):
                    chains.add(str(accept["network"]).lower())
                price = _max_amount_to_usd(accept.get("maxAmountRequired"))
                if price is not None and price > 0:
                    prices.append(price)
                if accept.get("description"):
                    local_description = str(accept["description"]).strip()
                    descriptions.append(local_description)
                summary = _accept_summary(accept, res_url)
                if len(accept_summaries) < 3:
                    accept_summaries.append(summary)
                if len(accept_samples) < 20:
                    accept_samples.append(summary)
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
                if len(quality_samples) < 10:
                    quality_samples.append(_quality_summary(md))
            for tag_link in res.get("tags") or []:
                tag = (tag_link or {}).get("tag") if isinstance(tag_link, dict) else None
                if isinstance(tag, dict) and tag.get("name"):
                    tag_set.add(str(tag["name"]))
            if len(resource_samples) < 20:
                tags = []
                for tag_link in res.get("tags") or []:
                    tag = (tag_link or {}).get("tag") if isinstance(tag_link, dict) else None
                    if isinstance(tag, dict) and tag.get("name"):
                        tags.append(str(tag["name"]))
                resource_samples.append({
                    "url": res_url,
                    "kind": res.get("type") or "x402-resource",
                    "x402_version": res.get("x402Version"),
                    "description": local_description,
                    "tags": tags[:8],
                    "accepts": accept_summaries,
                    "quality": _quality_summary(md),
                })

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
            "resource_count": len(resources),
            "resource_samples": resource_samples,
            "payment": {
                "price_min_usd": min(prices) if prices else None,
                "price_max_usd": max(prices) if prices else None,
                "chains": sorted(chains),
                "accepts": accept_samples,
            },
            "call_info": {
                "resource_count": len(resources),
                "resource_samples": resource_samples,
                "mcp_resource_urls": sorted(mcp_resource_urls),
                "mcp_server_urls": sorted(mcp_server_urls),
            },
            "quality": {
                "confidence_samples": confidences[:10],
                "metadata_samples": quality_samples,
            },
            "source": "x402scan", "source_id": str(origin.get("id") or origin_url),
            "tags": sorted(tag_set), "region": "global",
        })
    return rows


def probe_health(url, well_known=None):
    """Probe an endpoint and return a structured callability signal.

    Returns a dict:
      status      -> "ok" | "degraded" | "down"
      latency_ms  -> int | None   (round-trip of the deciding request)
      http_status -> int | None   (status code that decided the verdict)
      x402        -> bool         (endpoint answered with a proper HTTP 402)
    """
    targets = [well_known] if well_known else []
    targets.append(url)
    last = {"status": "down", "latency_ms": None, "http_status": None, "x402": False}
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                          headers={"User-Agent": UA}) as c:
            for t in targets:
                if not t:
                    continue
                try:
                    t0 = time.monotonic()
                    r = c.head(t)
                    if r.status_code in (405, 501):
                        r = c.get(t)
                    dt = int((time.monotonic() - t0) * 1000)
                    sc = r.status_code
                    if sc < 400 or sc == 402:
                        return {"status": "ok", "latency_ms": dt,
                                "http_status": sc, "x402": sc == 402}
                    if sc in (401, 403, 429):
                        last = {"status": "degraded", "latency_ms": dt,
                                "http_status": sc, "x402": False}
                except Exception:
                    continue
        return last
    except Exception:
        return {"status": "down", "latency_ms": None, "http_status": None, "x402": False}


def check_health(url, well_known=None):
    """Backward-compatible string verdict; see probe_health for full signal."""
    return probe_health(url, well_known)["status"]


# ---------------------------------------------------------------------------
# Signal C: real on-chain demand. For each Base-mainnet USDC payTo address we
# count incoming USDC transfers + unique payers over a 30d window via the free
# Blockscout Base indexer (Etherscan-compatible tokentx). This is the hardest
# signal to fake: spoofing it requires actually paying the service on-chain.
# ---------------------------------------------------------------------------

EVM_USDC_INDEXERS = {
    # chain key -> (Blockscout Etherscan-compatible API base, native USDC).
    # All verified live on 2026-06-05 (getToken -> symbol=USDC, 6 decimals).
    "base":     ("https://base.blockscout.com/api",     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    "ethereum": ("https://eth.blockscout.com/api",      "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    "optimism": ("https://optimism.blockscout.com/api", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
    "polygon":  ("https://polygon.blockscout.com/api",  "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359"),
    "arbitrum": ("https://arbitrum.blockscout.com/api", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "gnosis":   ("https://gnosis.blockscout.com/api",   "0xDDAfbb505ad214D7b80b1f830fcCc89B60fb7A83"),
}

# payment.accepts[].network identifier -> chain key above.
_NETWORK_CHAIN = {
    "base": "base", "eip155:8453": "base", "8453": "base",
    "ethereum": "ethereum", "eth": "ethereum", "eip155:1": "ethereum", "1": "ethereum",
    "optimism": "optimism", "op": "optimism", "eip155:10": "optimism", "10": "optimism",
    "polygon": "polygon", "matic": "polygon", "eip155:137": "polygon", "137": "polygon",
    "arbitrum": "arbitrum", "arbitrum-one": "arbitrum", "eip155:42161": "arbitrum", "42161": "arbitrum",
    "gnosis": "gnosis", "xdai": "gnosis", "eip155:100": "gnosis", "100": "gnosis",
}

# Back-compat alias (kept for any external import).
_BASE_USDC = EVM_USDC_INDEXERS["base"][1]


def network_to_chain(network):
    """Map a payment-network identifier to a supported chain key, or None."""
    if not network:
        return None
    return _NETWORK_CHAIN.get(str(network).strip().lower())


def fetch_payto_activity(payto, chain="base", days=30, max_pages=8):
    """Count incoming USDC transfers to ``payto`` on ``chain`` over the window.

    ``chain`` is a key of EVM_USDC_INDEXERS. Returns
    {"tx": int, "payers": int, "ok": bool, "capped": bool}. ``ok`` is False on a
    query/network failure or unsupported chain, so callers can skip the write
    instead of recording a false zero.
    """
    indexer = EVM_USDC_INDEXERS.get(chain)
    if indexer is None:
        return {"tx": 0, "payers": 0, "ok": False, "capped": False}
    _api_base, contract = indexer
    payto_l = payto.lower()
    cutoff = int(time.time()) - days * 86400
    tx = 0
    payers = set()
    capped = False
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
                          headers={"User-Agent": UA}) as c:
            for page in range(1, max_pages + 1):
                params = {
                    "module": "account", "action": "tokentx",
                    "contractaddress": contract, "address": payto,
                    "page": page, "offset": 100, "sort": "desc",
                }
                r = c.get(_api_base, params=params)
                if r.status_code == 429:
                    time.sleep(1.5)
                    r = c.get(_api_base, params=params)
                data = r.json()
                rows = data.get("result")
                if not isinstance(rows, list):
                    break  # "No transactions found" -> status 0
                stop = False
                for row in rows:
                    try:
                        ts = int(row.get("timeStamp", 0))
                    except (TypeError, ValueError):
                        continue
                    if ts < cutoff:
                        stop = True
                        break
                    if (row.get("to") or "").lower() == payto_l:
                        tx += 1
                        frm = (row.get("from") or "").lower()
                        if frm:
                            payers.add(frm)
                if stop or len(rows) < 100:
                    break
                if page == max_pages:
                    capped = True
        return {"tx": tx, "payers": len(payers), "ok": True, "capped": capped}
    except Exception as e:
        log.debug("payto activity fetch failed for %s: %r", payto, e)
        return {"tx": 0, "payers": 0, "ok": False, "capped": False}


# ---------------------------------------------------------------------------
# x402 verification (automated review)
#
# A submission is a *real* x402 service if we can machine-prove at least one of:
#   1. a /.well-known/x402 descriptor that parses as JSON with x402 markers
#      (accepts / endpoints / x402Version), OR
#   2. a declared endpoint that answers an un-paid request with HTTP 402 and a
#      payment-requirements body (accepts / paymentRequirements / x402Version).
#
# Returns a 3-state verdict so the auto-reviewer never hard-rejects on a flaky
# network: "verified" -> auto-approve, "rejected" -> auto-reject (clearly not
# x402), "uncertain" -> leave pending for a human.
# ---------------------------------------------------------------------------

import ipaddress as _ipaddress
import socket as _socket

_X402_MARKERS = ("accepts", "paymentrequirements", "x402version", "x402_version",
                 "paymentrequired", "maxamountrequired", "payto")
_VERIFY_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_BODY = 256 * 1024  # cap response body we parse


def _host_safety(host: str) -> str:
    """SSRF guard. Returns one of:
      "public"       -> resolves to public addresses, safe to probe
      "private"      -> resolves to loopback/private/link-local (reject: SSRF)
      "unresolvable" -> empty host or DNS resolution failed (uncertain, not reject)
    """
    if not host:
        return "private"
    try:
        infos = _socket.getaddrinfo(host, None)
    except Exception:
        return "unresolvable"
    if not infos:
        return "unresolvable"
    for info in infos:
        ip = info[4][0]
        try:
            addr = _ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return "private"
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            return "private"
    return "public"


def _url_acceptable(url: str) -> bool:
    """Ingestion gate: reject endpoints with no discoverability value.

    Currently drops bare-IP hosts (no domain = unverifiable, unstable, and
    typically dev/demo leftovers like 127.0.0.1 or ephemeral EC2 IPs).
    Domained http(s) URLs are kept.
    """
    try:
        host = urlparse(url if "//" in url else "https://" + url).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    try:
        _ipaddress.ip_address(host.strip("[]").split("%")[0])
        return False  # host is a bare IP literal -> reject
    except ValueError:
        return True   # not an IP -> it's a domain, keep


def _looks_like_x402(obj: Any) -> bool:
    """True if a parsed JSON body carries recognizable x402 markers."""
    try:
        blob = json.dumps(obj).lower()
    except (TypeError, ValueError):
        return False
    return any(m in blob for m in _X402_MARKERS)


def _extract_payment(obj: Any) -> dict[str, Any] | None:
    """Pull asset/network/amount/payTo out of an accepts[] / requirements body."""
    accepts = None
    if isinstance(obj, dict):
        accepts = (obj.get("accepts") or obj.get("paymentRequirements")
                   or obj.get("payment_requirements"))
        if accepts is None and ("payTo" in obj or "maxAmountRequired" in obj):
            accepts = [obj]
        # well-known descriptors nest accepts[] inside endpoints[]
        if accepts is None and isinstance(obj.get("endpoints"), list):
            for ep in obj["endpoints"]:
                if isinstance(ep, dict) and isinstance(ep.get("accepts"), list) and ep["accepts"]:
                    accepts = ep["accepts"]
                    break
    if not isinstance(accepts, list) or not accepts:
        return None
    first = accepts[0] if isinstance(accepts[0], dict) else {}
    # Some descriptors wrap real accept objects: [{resource, accepts:[{scheme,...}]}]
    for _ in range(3):
        if (isinstance(first, dict) and "scheme" not in first
                and isinstance(first.get("accepts"), list) and first["accepts"]):
            first = first["accepts"][0] if isinstance(first["accepts"][0], dict) else {}
        else:
            break
    amount_raw = (first.get("maxAmountRequired") or first.get("amount")
                  or first.get("price"))
    return {
        "scheme": first.get("scheme"),
        "network": first.get("network") or first.get("chain"),
        "asset": first.get("asset") or first.get("currency") or "USDC",
        "max_amount_usdc": _max_amount_to_usd(amount_raw),
        "pay_to": first.get("payTo") or first.get("pay_to"),
        "facilitator": first.get("facilitator") or (obj.get("facilitator") if isinstance(obj, dict) else None),
    }


def verify_x402(url: str, well_known: str | None = None) -> dict[str, Any]:
    """Machine-verify whether `url` exposes a real x402 service.

    Returns {"status": "verified"|"rejected"|"uncertain",
             "evidence": [str, ...], "payment": {...}|None}.
    """
    evidence: list[str] = []
    payment: dict[str, Any] | None = None
    net_error = False

    url = (url or "").strip()
    parsed = urlparse(url if "//" in url else "https://" + url)
    if parsed.scheme not in ("http", "https"):
        return {"status": "rejected", "evidence": ["url scheme not http(s)"],
                "payment": None}
    host = parsed.hostname or ""
    safety = _host_safety(host)
    if safety == "private":
        return {"status": "rejected",
                "evidence": [f"host resolves to a private/loopback address: {host!r}"],
                "payment": None}
    if safety == "unresolvable":
        return {"status": "uncertain",
                "evidence": [f"host did not resolve (transient DNS or dead host): {host!r}"],
                "payment": None}

    origin = f"{parsed.scheme}://{parsed.netloc}"
    wk_candidates = []
    if well_known:
        wk_candidates.append(well_known)
    wk_candidates += [origin + "/.well-known/x402",
                      origin + "/.well-known/x402.json"]

    with httpx.Client(timeout=_VERIFY_TIMEOUT, follow_redirects=True,
                      max_redirects=3,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        # --- 1. well-known descriptor ---
        seen_wk = set()
        for wk in wk_candidates:
            if not wk or wk in seen_wk:
                continue
            seen_wk.add(wk)
            try:
                r = c.get(wk)
            except Exception:
                net_error = True
                continue
            if r.status_code == 200:
                try:
                    obj = json.loads(r.content[:_MAX_BODY])
                except (ValueError, json.JSONDecodeError):
                    continue
                if _looks_like_x402(obj):
                    evidence.append(f"well-known x402 descriptor at {wk} (200, x402 markers)")
                    payment = payment or _extract_payment(obj)

        # --- 2. endpoint 402 challenge ---
        for target in (url, origin):
            if not target:
                continue
            try:
                r = c.get(target)
            except Exception:
                net_error = True
                continue
            if r.status_code == 402:
                body_ok = False
                try:
                    obj = json.loads(r.content[:_MAX_BODY])
                    body_ok = _looks_like_x402(obj)
                    if body_ok:
                        payment = payment or _extract_payment(obj)
                except (ValueError, json.JSONDecodeError):
                    obj = None
                # A bare 402 is suggestive; 402 + payment body is conclusive.
                if body_ok:
                    evidence.append(f"endpoint {target} returns HTTP 402 with payment requirements")
                else:
                    evidence.append(f"endpoint {target} returns HTTP 402 (no parseable accepts body)")
                break

    if evidence:
        # Conclusive only if we saw real x402 markers somewhere.
        conclusive = any("markers" in e or "payment requirements" in e for e in evidence)
        return {"status": "verified" if conclusive else "uncertain",
                "evidence": evidence, "payment": payment}
    if net_error:
        return {"status": "uncertain",
                "evidence": ["no x402 evidence; network errors during probe"],
                "payment": None}
    return {"status": "rejected",
            "evidence": ["no /.well-known/x402 and no 402 challenge from endpoint"],
            "payment": None}


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



PAY_SKILLS_REPO = "solana-foundation/pay-skills"
PAY_SKILLS_API = f"https://api.github.com/repos/{PAY_SKILLS_REPO}/pulls"

# Map common payment network identifiers to friendly chain names.
_PAYSKILL_NETWORKS = {
    "eip155:8453": "base", "8453": "base", "base": "base",
    "eip155:84532": "base-sepolia", "84532": "base-sepolia",
    "eip155:1": "ethereum", "1": "ethereum", "ethereum": "ethereum",
    "eip155:137": "polygon", "137": "polygon", "polygon": "polygon",
    "solana": "solana",
}


def _payskill_chain(network: Any) -> list:
    if not network:
        return []
    key = str(network).strip().lower()
    return [_PAYSKILL_NETWORKS.get(key, key)]


def _parse_pay_md_from_diff(diff_text: str) -> dict | None:
    """Pull the YAML frontmatter of an *added* PAY.md out of a unified diff.

    pay-skills PRs add `providers/<org>/<name>/PAY.md` whose frontmatter carries
    name/title/description/category/service_url. We read only the added (`+`)
    lines between the first and second `---` fence of that file.
    """
    in_paymd = False
    seen_open = False
    collecting = False
    fm_lines: list[str] = []
    for ln in diff_text.splitlines():
        if ln.startswith("+++ ") and ln.rstrip().endswith("PAY.md"):
            in_paymd, seen_open, collecting, fm_lines = True, False, False, []
            continue
        if not in_paymd:
            continue
        if ln.startswith("diff --git") or ln.startswith("--- ") or ln.startswith("+++ "):
            break  # reached the next file in the diff; stop at first PAY.md
        if not ln.startswith("+"):
            continue
        content = ln[1:]
        if content.strip() == "---":
            if not seen_open:
                seen_open = collecting = True
                continue
            break  # closing fence
        if collecting:
            fm_lines.append(content)
    if not fm_lines:
        return None
    try:
        meta = yaml.safe_load("\n".join(fm_lines))
    except Exception:
        return None
    return meta if isinstance(meta, dict) else None


def fetch_pay_skills_prs() -> list:
    """Crawl OPEN pull requests on solana-foundation/pay-skills.

    These are *pre-launch* services (submitted but not yet merged into any live
    registry). Each candidate is machine-verified with verify_x402() and ONLY
    conclusively-verified x402 services are returned for listing; rejected or
    uncertain candidates are skipped (logged, never listed).
    """
    out: list = []
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    prs: list = []
    skipped = 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as c:
        for page in range(1, 4):
            try:
                r = c.get(PAY_SKILLS_API,
                          params={"state": "open", "per_page": 100, "page": page})
            except Exception as e:
                log.warning("pay-skills: list PRs page %d failed: %r", page, e)
                break
            if r.status_code != 200:
                log.warning("pay-skills: list PRs page %d -> HTTP %d",
                            page, r.status_code)
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            prs.extend(batch)
            if len(batch) < 100:
                break

        for pr in prs:
            num = pr.get("number")
            if not num:
                continue
            title = pr.get("title") or ""
            author = (pr.get("user") or {}).get("login")
            html_url = pr.get("html_url")
            created = pr.get("created_at")
            # .diff is served by codeload (no API rate limit)
            try:
                dr = c.get(f"https://github.com/{PAY_SKILLS_REPO}/pull/{num}.diff")
            except Exception as e:
                log.warning("pay-skills PR#%s diff fetch failed: %r", num, e)
                continue
            if dr.status_code != 200:
                continue
            meta = _parse_pay_md_from_diff(dr.text)
            if not meta:
                continue
            service_url = str(meta.get("service_url") or "").strip()
            if not service_url.startswith("http"):
                continue

            # MANDATORY gate: must be a real, reachable x402 service to list.
            try:
                verdict = verify_x402(service_url)
            except Exception as e:
                log.warning("pay-skills PR#%s verify error %r -> skip", num, e)
                continue
            status = verdict.get("status")
            if status != "verified":
                skipped += 1
                log.info("pay-skills PR#%s %s -> %s (not listed)",
                         num, service_url, status)
                continue

            pay = verdict.get("payment") or {}
            price = pay.get("max_amount_usdc")
            name = str(meta.get("title") or meta.get("name")
                       or _host_slug(service_url)).strip()
            category = _slugify(str(meta.get("category") or "uncategorized"))
            tags = ["pre-launch", "pay-skills-pr"]
            if meta.get("category"):
                tags.append(str(meta["category"]))
            out.append({
                "slug": _host_slug(service_url) + "-payskill",
                "name": name,
                "url": service_url,
                "description": (meta.get("description")
                               or meta.get("use_case") or None),
                "category": category,
                "chains": _payskill_chain(pay.get("network")),
                "price_min": price,
                "price_max": price,
                "currency": "USDC",
                "facilitator": pay.get("facilitator"),
                "well_known_url": service_url.rstrip("/") + "/.well-known/x402",
                "source": "pay-skills-pr",
                "source_id": f"pr:{num}",
                "tags": tags,
                "region": "global",
                "resource_count": 1,
                "resource_samples": [{"url": service_url, "kind": "x402-resource"}],
                "call_info": {
                    "resource_samples": [{"url": service_url, "kind": "x402-resource"}],
                    "pr": {"number": num, "title": title, "author": author,
                           "url": html_url, "created_at": created},
                },
                "payment": pay or None,
            })

    log.info("pay-skills: scanned %d open PRs, listed %d verified, skipped %d",
             len(prs), len(out), skipped)
    return out


ALL_CRAWLERS = {
    "awesome-x402": fetch_awesome_x402,
    "cdp-bazaar": fetch_cdp_bazaar,
    "x402scan": fetch_x402scan,
    "pay-skills-pr": fetch_pay_skills_prs,
}


# ---------------------------------------------------------------------------
# MCP server directory sources (standalone MCP directory).
# PulseMCP and the official MCP registry are public JSON APIs that list
# remotely-callable MCP servers (streamable-http / sse endpoints).
# ---------------------------------------------------------------------------

_PULSEMCP_API = "https://api.pulsemcp.com/v0beta/servers"
_MCP_REGISTRY_API = "https://registry.modelcontextprotocol.io/v0/servers"
_MCP_KEEPALIVE_API = "https://holyai.me/mcp-keepalive/api/servers"
_MCP_KEEPALIVE_DETAIL_API = "https://holyai.me/mcp-keepalive/api/server/{safe_name}"


def _mcp_x402(*texts: Any) -> bool:
    # x402 support is never inferred from text. directory.reverify_x402
    # proves it by probing each endpoint for an HTTP 402 / .well-known/x402
    # descriptor and owns the x402_supported flag.
    return False


def fetch_pulsemcp(max_pages: int = 200, per_page: int = 100,
                   remote_only: bool = True, known_ids: set | None = None,
                   stop_after_known: int = 0) -> list:
    """Import remotely-callable MCP servers from the PulseMCP directory.

    PulseMCP lists ~16k servers; we keep the ones that expose a remote
    endpoint (url_direct) so the directory stays a list of callable servers.

    The v0beta API has no sort/updated_since params and no per-row timestamp,
    but its default order is newest-first (offset 0 = most recently released).
    So two modes share one code path:
      * Full crawl (default): page through everything (max_pages*per_page).
      * Incremental "recent" crawl: pass ``known_ids`` (source_ids already in
        the DB) and ``stop_after_known`` > 0; we stop once we have seen that
        many consecutive already-known servers, i.e. we have caught up to the
        existing catalog and everything beyond is older/known.

    The v0beta API is being sunset and randomly rejects ~half of all
    requests with HTTP 410 (code=API_SUNSET). We retry each page with
    exponential backoff + jitter, and a page that still fails is skipped
    (we advance to the next offset) instead of abandoning the whole crawl,
    so one unlucky page can no longer wipe out the entire pulsemcp refresh.
    """
    out: list[dict] = []
    seen: set[str] = set()
    consecutive_fail = 0
    consecutive_known = 0
    known_ids = known_ids or set()
    stopped_early = False
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        offset = 0
        for _ in range(max_pages):
            data = None
            for attempt in range(8):
                try:
                    r = c.get(_PULSEMCP_API,
                              params={"count_per_page": per_page, "offset": offset})
                    if r.status_code == 410:
                        # random sunset failure -> back off and retry
                        time.sleep(min(8.0, 0.5 * (1.6 ** attempt))
                                   + random.uniform(0, 0.4))
                        continue
                    r.raise_for_status()
                    data = r.json()
                    break
                except (httpx.HTTPError, ValueError) as e:
                    if attempt == 7:
                        log.warning("pulsemcp page offset=%d failed: %r", offset, e)
                    else:
                        time.sleep(min(8.0, 0.5 * (1.6 ** attempt))
                                   + random.uniform(0, 0.4))
                    continue
            if data is None:
                # Skip this page instead of dropping the whole crawl; bail
                # only if several consecutive pages are unreachable.
                consecutive_fail += 1
                log.warning("pulsemcp page offset=%d gave up after retries "
                            "(skip; consecutive_fail=%d)", offset, consecutive_fail)
                if consecutive_fail >= 3:
                    log.warning("pulsemcp: too many consecutive failed pages, "
                                "stopping with %d collected", len(out))
                    break
                offset += per_page
                continue
            consecutive_fail = 0
            servers = data.get("servers") or []
            if not servers:
                break
            for s in servers:
                remotes = s.get("remotes") or []
                remote = remotes[0] if isinstance(remotes, list) and remotes else {}
                endpoint = (remote.get("url_direct") or "").strip() or None
                if remote_only and not endpoint:
                    continue
                name = (s.get("name") or "").strip()
                homepage = (s.get("external_url") or "").strip() or None
                source_id = (s.get("url") or "").strip() or (name + ":" + (endpoint or ""))
                if source_id in seen:
                    continue
                seen.add(source_id)
                # Incremental mode: count consecutive already-known servers in
                # newest-first order; once we have seen enough in a row we have
                # caught up to the existing catalog and can stop early. Known is
                # keyed on endpoint_url (stable across cross-source dedup).
                if stop_after_known:
                    if endpoint and endpoint in known_ids:
                        consecutive_known += 1
                        if consecutive_known >= stop_after_known:
                            stopped_early = True
                            break
                    else:
                        consecutive_known = 0
                desc = (s.get("short_description")
                        or s.get("EXPERIMENTAL_ai_generated_description") or "").strip() or None
                slug = _slugify(name) if name else _host_slug(endpoint or homepage or source_id)
                if endpoint:
                    slug = f"{slug}-{_host_slug(endpoint)}"[:80]
                stars = s.get("github_stars")
                conf = 0.5
                if endpoint:
                    conf += 0.1
                if isinstance(stars, int) and stars >= 50:
                    conf += 0.1
                out.append({
                    "slug": slug,
                    "name": name or slug,
                    "description": desc,
                    "homepage_url": homepage,
                    "endpoint_url": endpoint,
                    "transport": remote.get("transport"),
                    "auth_method": remote.get("authentication_method"),
                    "cost_hint": remote.get("cost"),
                    "source_code_url": s.get("source_code_url"),
                    "package_registry": s.get("package_registry"),
                    "package_name": s.get("package_name"),
                    "package_download_count": (
                        s.get("package_download_count")
                        if isinstance(s.get("package_download_count"), int) else None),
                    "github_stars": stars if isinstance(stars, int) else None,
                    "tags": None,
                    "x402_supported": _mcp_x402(desc, name, remote.get("cost")),
                    "source": "pulsemcp",
                    "source_id": source_id,
                    "source_url": (s.get("url") or "").strip() or None,
                    "confidence": round(min(1.0, conf), 3),
                })
            if stopped_early:
                log.info("pulsemcp: caught up (%d consecutive known) at "
                         "offset=%d, stopping incremental crawl", consecutive_known, offset)
                break
            offset += len(servers)
            total = data.get("total_count")
            if isinstance(total, int) and offset >= total:
                break
            if not data.get("next"):
                break
    log.info("pulsemcp: collected %d remote MCP servers", len(out))
    return out


def fetch_mcp_registry(updated_since: str | None = None,
                       max_pages: int = 1000, per_page: int = 100,
                       remote_only: bool = True) -> list:
    """Import servers from the official MCP registry.

    ``updated_since=None`` does a full crawl (pages through everything).
    When ``updated_since`` is an ISO-8601 timestamp the registry returns only
    servers changed since then AND includes deleted ones (status != "active"),
    so removals propagate. Returned items carry private ``_status`` and
    ``_updated_at`` keys for the caller to advance the watermark / drop rows.
    """
    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": per_page}
            if cursor:
                params["cursor"] = cursor
            if updated_since:
                params["updated_since"] = updated_since
            try:
                r = c.get(_MCP_REGISTRY_API, params=params)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("mcp-registry page failed: %r", e)
                break
            servers = data.get("servers") or []
            if not servers:
                break
            for entry in servers:
                srv = entry.get("server") if isinstance(entry, dict) else None
                if not isinstance(srv, dict):
                    continue
                rmeta = ((entry.get("_meta") or {}).get(
                    "io.modelcontextprotocol.registry/official") or {})
                status = (rmeta.get("status") or "active").strip().lower()
                updated_at = rmeta.get("updatedAt") or rmeta.get("publishedAt")
                name = (srv.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                # Deleted servers are emitted (incremental only) so the caller
                # can remove them, even with no endpoint left.
                if status != "active":
                    out.append({
                        "source": "mcp-registry",
                        "source_id": name,
                        "_status": status,
                        "_updated_at": updated_at,
                    })
                    continue
                remotes = srv.get("remotes") or []
                remote = remotes[0] if isinstance(remotes, list) and remotes else {}
                endpoint = (remote.get("url") or "").strip() or None
                if remote_only and not endpoint:
                    continue
                desc = (srv.get("description") or "").strip() or None
                title = (srv.get("title") or "").strip() or None
                slug = _slugify(title or name)
                if endpoint:
                    slug = f"{slug}-{_host_slug(endpoint)}"[:80]
                repo = srv.get("repository")
                out.append({
                    "slug": slug,
                    "name": title or name,
                    "description": desc,
                    "homepage_url": (srv.get("websiteUrl") or "").strip() or None,
                    "endpoint_url": endpoint,
                    "transport": remote.get("type"),
                    "auth_method": None,
                    "cost_hint": None,
                    "source_code_url": repo.get("url") if isinstance(repo, dict) else None,
                    "package_registry": None,
                    "package_name": None,
                    "github_stars": None,
                    "tags": None,
                    "x402_supported": _mcp_x402(desc, name),
                    "source": "mcp-registry",
                    "source_id": name,
                    "source_url": None,
                    "confidence": 0.55 if endpoint else 0.4,
                    "_status": status,
                    "_updated_at": updated_at,
                })
            cursor = (data.get("metadata") or {}).get("nextCursor")
            if not cursor:
                break
    log.info("mcp-registry: collected %d items (updated_since=%s)",
             len(out), updated_since)
    return out


def _mcp_keepalive_ts(raw: Any) -> int | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def _mcp_keepalive_health(score: dict | None, auth_posture: str | None) -> str | None:
    score = score or {}
    status = score.get("last_status")
    uptime = score.get("uptime_pct")
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None
    try:
        uptime_f = float(uptime) if uptime is not None else None
    except (TypeError, ValueError):
        uptime_f = None
    if status_i is not None and status_i >= 500:
        return "down"
    if uptime_f is not None and uptime_f < 50:
        return "down"
    if status_i in (401, 402, 403) or (auth_posture and auth_posture not in ("none", "unknown")):
        return "ok"
    if status_i is not None and 200 <= status_i < 300:
        return "ok"
    if status_i is not None and status_i < 500:
        return "degraded"
    if uptime_f is not None and uptime_f >= 90:
        return "ok"
    return None


def fetch_mcp_keepalive(max_pages: int = 20, per_page: int = 100,
                        detail_limit: int | None = None,
                        page_sleep: float = 0.2,
                        detail_sleep: float = 0.03) -> list:
    """Import high-quality live MCP endpoints from holyai.me/mcp-keepalive.

    mcp-keepalive mirrors the official MCP registry and probes every remote
    endpoint every 15 minutes, publishing health, auth posture, latency, and
    per-remote detail JSON. The list endpoint does not include endpoint URLs,
    so we fetch detail records for a bounded number of alive, grade-sorted
    entries. The default is intentionally conservative; set
    MCP_KEEPALIVE_DETAIL_LIMIT to widen the reverse crawl.
    """
    if detail_limit is None:
        try:
            detail_limit = int(os.environ.get("MCP_KEEPALIVE_DETAIL_LIMIT", "500"))
        except ValueError:
            detail_limit = 500
    detail_limit = max(0, detail_limit)
    out: list[dict] = []
    seen_ep: set[str] = set()
    seen_detail: set[str] = set()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as c:
        offset = 0
        while offset < max_pages * per_page and len(seen_detail) < detail_limit:
            try:
                r = c.get(_MCP_KEEPALIVE_API, params={
                    "limit": per_page,
                    "offset": offset,
                    "status": "alive",
                    "sort": "grade",
                    "dir": "desc",
                })
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("mcp-keepalive page offset=%d failed: %r", offset, e)
                break
            servers = data.get("servers") if isinstance(data, dict) else None
            if not servers:
                break
            for summary in servers:
                if len(seen_detail) >= detail_limit:
                    break
                if not isinstance(summary, dict):
                    continue
                safe_name = (summary.get("safe_name") or "").strip()
                if not safe_name or safe_name in seen_detail:
                    continue
                seen_detail.add(safe_name)
                try:
                    detail_url = _MCP_KEEPALIVE_DETAIL_API.format(safe_name=safe_name)
                    dr = c.get(detail_url)
                    dr.raise_for_status()
                    detail = dr.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("mcp-keepalive detail %s failed: %r", safe_name, e)
                    continue
                name = (detail.get("name") or summary.get("name") or safe_name).strip()
                title = (detail.get("title") or summary.get("title") or name).strip()
                desc = (detail.get("description") or summary.get("description") or "").strip()
                repo = detail.get("repository_url") or summary.get("repository_url")
                source_url = "https://holyai.me" + (detail.get("permalink") or summary.get("detail_url") or "")
                remotes = detail.get("remotes") or []
                if not isinstance(remotes, list):
                    continue
                for remote in remotes:
                    if not isinstance(remote, dict):
                        continue
                    endpoint = (remote.get("url") or "").strip()
                    if not endpoint or not endpoint.lower().startswith("http"):
                        continue
                    ep_key = endpoint.lower().rstrip("/")
                    if ep_key in seen_ep:
                        continue
                    seen_ep.add(ep_key)
                    score = remote.get("score") if isinstance(remote.get("score"), dict) else {}
                    auth = (score.get("auth_posture") or summary.get("auth_posture") or "").strip() or None
                    health = _mcp_keepalive_health(score, auth)
                    checked = _mcp_keepalive_ts(score.get("last_probe_at") or summary.get("last_probe_at"))
                    http_status = score.get("last_status") or summary.get("last_status")
                    latency = score.get("p50_ms") or summary.get("p50_ms")
                    confidence = 0.5
                    try:
                        confidence = max(0.0, min(1.0, float(score.get("score", summary.get("score", 50))) / 100.0))
                    except (TypeError, ValueError):
                        pass
                    bits = []
                    if desc:
                        bits.append(desc)
                    grade = score.get("grade") or summary.get("grade")
                    uptime = score.get("uptime_pct") or summary.get("uptime_pct")
                    if grade:
                        bits.append(f"mcp-keepalive grade {grade}")
                    if uptime is not None:
                        bits.append(f"uptime {uptime}%")
                    if latency is not None:
                        bits.append(f"p50 {latency}ms")
                    if auth:
                        bits.append(f"auth {auth}")
                    protocol = score.get("mcp_protocol") or summary.get("mcp_protocol")
                    tags = ["mcp-keepalive"]
                    if grade:
                        tags.append(f"grade:{grade}")
                    if auth:
                        tags.append(f"auth:{auth}")
                    if protocol:
                        tags.append(f"protocol:{protocol}")
                    out.append({
                        "slug": f"{_slugify(title or name)}-{_host_slug(endpoint)}"[:80],
                        "name": title or name,
                        "description": "; ".join(bits) or None,
                        "homepage_url": repo or source_url,
                        "endpoint_url": endpoint,
                        "transport": remote.get("transport") or summary.get("transport_declared"),
                        "auth_method": None if auth in (None, "none", "unknown") else auth,
                        "cost_hint": None,
                        "source_code_url": repo,
                        "package_registry": "mcp-registry",
                        "package_name": name,
                        "github_stars": None,
                        "tags": tags,
                        "x402_supported": _mcp_x402(desc, title, name),
                        "source": "mcp-keepalive",
                        "source_id": f"{name}:{endpoint}",
                        "source_url": source_url,
                        "confidence": round(confidence, 3),
                        "health": health,
                        "health_checked": checked,
                        "latency_ms": latency if isinstance(latency, int) else None,
                        "http_status": http_status if isinstance(http_status, int) else None,
                        "last_success_at": checked if health == "ok" else None,
                    })
                if detail_sleep:
                    time.sleep(detail_sleep)
            offset += len(servers)
            total = data.get("total") if isinstance(data, dict) else None
            if isinstance(total, int) and offset >= total:
                break
            if len(servers) < per_page:
                break
            if page_sleep:
                time.sleep(page_sleep)
    log.info("mcp-keepalive: collected %d remotes from %d server details",
             len(out), len(seen_detail))
    return out


MCP_CRAWLERS = {
    "pulsemcp": fetch_pulsemcp,
    "mcp-registry": fetch_mcp_registry,
    "mcp-keepalive": fetch_mcp_keepalive,
}


def _mcp_parse_response(r):
    """Parse an MCP streamable-http response body (JSON or SSE) to a dict."""
    ct = r.headers.get("content-type", "")
    if "event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
        return None
    try:
        return r.json()
    except Exception:
        return None


# Per-server caps so a padded multi-tool server can't bloat our row/index.
_MAX_TOOLS = 60
_MAX_TOOL_DESC = 160


def _summarize_tools(tools: list) -> list:
    """Reduce a tools/list array to [{name, description}], capped.

    We keep every tool a server advertises (up to a sane cap) because each is
    an independently callable capability the directory should index — a server
    that exposes 24 tools is 24 discoverable capabilities, not one.
    """
    out = []
    for t in tools[:_MAX_TOOLS]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        desc = str(t.get("description") or "").strip().replace("\n", " ")
        if len(desc) > _MAX_TOOL_DESC:
            desc = desc[:_MAX_TOOL_DESC].rstrip() + "…"
        out.append({"name": name, "description": desc})
    return out


def probe_mcp_health(endpoint: str) -> dict:
    """Probe an MCP streamable-http endpoint.

    Does the `initialize` handshake and, when it succeeds, a follow-up
    `tools/list` (Tier-2 conformance: the server really exposes the tools it
    advertises). Returns {status, latency_ms, http_status, conformance,
    tool_count}. ``conformance`` is 'pass' (tools/list returned a tools array),
    'partial' (initialize ok but tools/list failed/errored), 'fail' (initialize
    reachable but not a clean 2xx) or None (not probed). Auth challenges
    (401/402/403) mean the remote MCP server is reachable and intentionally
    credential-gated, so health is 'ok' while conformance remains 'fail'.
    Other reachable non-5xx protocol/method errors are 'degraded'.
    """
    base = {"status": "unknown", "latency_ms": None, "http_status": None,
            "conformance": None, "tool_count": None, "tools": None}
    if not endpoint:
        return base
    # Smithery-hosted endpoints require the caller's own Smithery api_key.
    if ("server.smithery.ai" in endpoint or ".run.tools" in endpoint):
        _sk = (os.environ.get("SMITHERY_API_KEY") or "").strip()
        if _sk and "api_key=" not in endpoint:
            sep = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{sep}api_key={_sk}"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agent-tools.cloud", "version": "0.1"},
        },
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                          follow_redirects=True) as c:
            t0 = time.monotonic()
            r = c.post(endpoint, json=init, headers=headers)
            dt = int((time.monotonic() - t0) * 1000)
            sc = r.status_code
            if sc >= 500:
                return {**base, "status": "down", "latency_ms": dt, "http_status": sc}
            if sc in (401, 402, 403):
                return {**base, "status": "ok", "latency_ms": dt,
                        "http_status": sc, "conformance": "fail"}
            if sc >= 300:
                return {**base, "status": "degraded", "latency_ms": dt,
                        "http_status": sc, "conformance": "fail"}
            # initialize ok -> Tier-2: confirm tools/list
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            h2 = dict(headers)
            if sid:
                h2["Mcp-Session-Id"] = sid
            conformance = "partial"
            tool_count = None
            try:
                c.post(endpoint, json={"jsonrpc": "2.0",
                                       "method": "notifications/initialized"},
                       headers=h2)
            except Exception:
                pass
            tool_list = None
            try:
                rt = c.post(endpoint, json={"jsonrpc": "2.0", "id": 2,
                                            "method": "tools/list", "params": {}},
                            headers=h2)
                if rt.status_code < 300:
                    parsed = _mcp_parse_response(rt)
                    if isinstance(parsed, dict) and not parsed.get("error"):
                        tools = (parsed.get("result") or {}).get("tools")
                        if isinstance(tools, list):
                            conformance = "pass"
                            tool_count = len(tools)
                            tool_list = _summarize_tools(tools)
            except Exception:
                pass
            return {"status": "ok", "latency_ms": dt, "http_status": sc,
                    "conformance": conformance, "tool_count": tool_count,
                    "tools": tool_list}
    except Exception:
        return {**base, "status": "down"}


# ---------------------------------------------------------------------------
# Additional MCP directory sources (added 2026-06-04).
#   - Smithery: public registry (no auth), ~6k servers, deployed ones expose a
#     callable streamable-http endpoint at server.smithery.ai/{name}/mcp.
#   - Glama: public cursor-paginated catalog (repo + metadata).
# Both append themselves to MCP_CRAWLERS at import time (see bottom).
# ---------------------------------------------------------------------------

_SMITHERY_API = "https://registry.smithery.ai/servers"
_SMITHERY_SEED = 1325  # stable seed -> deterministic deep pagination past the 500 rerank cap
_SMITHERY_REMOTE = "https://server.smithery.ai/{name}/mcp"


def fetch_smithery(max_pages: int = 80, per_page: int = 100,
                   remote_only: bool = False) -> list:
    """Import MCP servers from the Smithery registry.

    Smithery hosts ~5.9k servers. With a ``SMITHERY_API_KEY`` in the env the
    full registry is paginated (``seed`` gives stable deep pagination past the
    500 rerank cap). Remote/deployed servers are callable over streamable-http
    at ``server.smithery.ai/{qualifiedName}/mcp`` (clients bring their own
    ``api_key``) and stored with an endpoint; the rest (local/stdio configs)
    are kept as catalog entries. With ``remote_only`` we keep just the callable
    ones.
    """
    out: list[dict] = []
    seen: set[str] = set()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    api_key = (os.environ.get("SMITHERY_API_KEY") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers=headers) as c:
        page = 1
        total_pages = None
        for _ in range(max_pages):
            try:
                params = {"seed": _SMITHERY_SEED,
                          "page": page, "pageSize": per_page}
                if remote_only:
                    params["remote"] = "1"
                r = c.get(_SMITHERY_API, params=params)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("smithery page %d failed: %r", page, e)
                break
            servers = data.get("servers") or []
            if not servers:
                break
            for s in servers:
                qn = (s.get("qualifiedName") or "").strip()
                if not qn or qn in seen:
                    continue
                remote = bool(s.get("remote")) and bool(s.get("isDeployed"))
                endpoint = _SMITHERY_REMOTE.format(name=qn) if remote else None
                if remote_only and not endpoint:
                    continue
                seen.add(qn)
                name = (s.get("displayName") or qn).strip()
                desc = (s.get("description") or "").strip() or None
                homepage = (s.get("homepage") or "").strip() or None
                use = s.get("useCount")
                conf = 0.55
                if s.get("verified"):
                    conf += 0.15
                if isinstance(use, int) and use >= 100:
                    conf += 0.1
                slug = _slugify(qn)
                if endpoint:
                    slug = f"{slug}-smithery"[:80]
                out.append({
                    "slug": slug,
                    "name": name,
                    "description": desc,
                    "homepage_url": homepage,
                    "endpoint_url": endpoint,
                    "transport": "streamable-http" if endpoint else None,
                    "auth_method": "smithery_api_key" if endpoint else None,
                    "cost_hint": None,
                    "source_code_url": None,
                    "package_registry": None,
                    "package_name": qn,
                    "github_stars": None,
                    "tags": None,
                    "x402_supported": _mcp_x402(desc, name),
                    "source": "smithery",
                    "source_id": qn,
                    "source_url": homepage,
                    "confidence": round(min(1.0, conf), 3),
                })
            pg = data.get("pagination") or {}
            total_pages = pg.get("totalPages")
            page += 1
            if isinstance(total_pages, int) and page > total_pages:
                break
    log.info("smithery: collected %d MCP servers", len(out))
    return out


_GLAMA_API = "https://glama.ai/api/mcp/v1/servers"


def fetch_glama(max_pages: int = 60, per_page: int = 100,
                remote_only: bool = False) -> list:
    """Import MCP servers from the Glama catalog (public, cursor paginated).

    Glama exposes rich metadata (repo, hosting attributes) but no single
    callable URL in the list response, so these are stored as catalog
    entries (source_code_url + description). With ``remote_only`` only
    remote-capable servers are kept.
    """
    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"first": per_page}
            if cursor:
                params["after"] = cursor
            try:
                r = c.get(_GLAMA_API, params=params)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("glama page failed: %r", e)
                break
            servers = data.get("servers") or []
            if not servers:
                break
            for s in servers:
                sid = (s.get("id") or "").strip()
                if not sid or sid in seen:
                    continue
                attrs = s.get("attributes") or []
                remote_capable = any("remote" in str(a).lower() for a in attrs)
                if remote_only and not remote_capable:
                    continue
                seen.add(sid)
                name = (s.get("name") or s.get("slug") or sid).strip()
                desc = (s.get("description") or "").strip() or None
                repo = s.get("repository")
                repo_url = repo.get("url") if isinstance(repo, dict) else None
                page_url = (s.get("url") or "").strip() or None
                base = f"{s.get('namespace') or ''}-{s.get('slug') or name}"
                slug = _slugify(base)[:80]
                tags = ",".join(str(a) for a in attrs) or None
                conf = 0.4
                if remote_capable:
                    conf += 0.1
                out.append({
                    "slug": slug,
                    "name": name,
                    "description": desc,
                    "homepage_url": page_url,
                    "endpoint_url": None,
                    "transport": None,
                    "auth_method": None,
                    "cost_hint": None,
                    "source_code_url": repo_url,
                    "package_registry": None,
                    "package_name": None,
                    "github_stars": None,
                    "tags": tags,
                    "x402_supported": _mcp_x402(desc, name, tags),
                    "source": "glama",
                    "source_id": sid,
                    "source_url": page_url,
                    "confidence": round(min(1.0, conf), 3),
                })
            pi = data.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
            if not cursor:
                break
    log.info("glama: collected %d MCP servers", len(out))
    return out


MCP_CRAWLERS["smithery"] = fetch_smithery

# ---------------------------------------------------------------------------
# chiark.ai - "Agent Quality Index". Crawls 9 MCP/A2A registries, dedupes by
# endpoint, and scores each agent 0..100 (uptime/conformance/latency). Public
# paginated REST at /api/v1/agents (limit<=100). We ingest as MCP servers; the
# endpoint-level dedup in upsert_mcp_server collapses overlaps with our other
# sources. chiark's operational_score (/100) maps to our confidence (0..1).
# ---------------------------------------------------------------------------
CHIARK_API = "https://chiark.ai/api/v1/agents"


def fetch_chiark(max_pages: int = 80, per_page: int = 100, page_sleep: float = 0.3) -> list:
    """Pull chiark.ai's quality-indexed agents as MCP server rows."""
    out: list = []
    seen_ep: set = set()
    # /agents is a mixed A2A+MCP feed with no protocol field; pull the A2A ids
    # from /discover so we don't dump A2A agents into the MCP table.
    a2a_skip = _chiark_a2a_id_set()
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for page in range(max_pages):
            offset = page * per_page
            try:
                r = c.get(CHIARK_API, params={"limit": per_page, "offset": offset})
                if r.status_code == 429:
                    time.sleep(2.0)
                    r = c.get(CHIARK_API, params={"limit": per_page, "offset": offset})
                if r.status_code != 200:
                    log.warning("chiark: stop HTTP %d at offset %d", r.status_code, offset)
                    break
                rows = r.json()
            except Exception as e:
                log.warning("chiark: fetch error at offset %d: %r", offset, e)
                break
            if not isinstance(rows, list) or not rows:
                break
            for it in rows:
                if not isinstance(it, dict):
                    continue
                _cid = it.get("id")
                if _cid is not None and str(_cid) in a2a_skip:
                    continue
                ep = (it.get("endpoint_url") or "").strip()
                if not ep or not ep.lower().startswith("http"):
                    continue
                ep_key = ep.lower().rstrip("/")
                if ep_key in seen_ep:
                    continue
                seen_ep.add(ep_key)
                cid = it.get("id") or ep_key
                name = it.get("name") or it.get("provider") or _host_slug(ep)
                low = ep.lower()
                transport = "streamable-http" if "/mcp" in low or low.rstrip("/").endswith("mcp") else None
                # operational_score is 0..max_possible_score (100 or 45 for
                # auth-gated). Normalise to 0..1 against its own max.
                score = it.get("operational_score")
                maxp = it.get("max_possible_score") or 100.0
                conf = None
                try:
                    if score is not None and maxp:
                        conf = max(0.0, min(1.0, float(score) / float(maxp)))
                except (TypeError, ValueError):
                    conf = None
                skills = it.get("skills") or []
                desc_bits = []
                if skills:
                    desc_bits.append("skills: " + ", ".join(str(s) for s in skills[:8]))
                up = it.get("uptime_30d")
                if up is not None:
                    desc_bits.append(f"uptime_30d {up}%")
                p95 = it.get("p95_latency_ms")
                if p95 is not None:
                    desc_bits.append(f"p95 {p95}ms")
                conf_label = it.get("conformance")
                if conf_label:
                    desc_bits.append(f"conformance: {conf_label}")
                out.append({
                    "slug": _host_slug(ep) + "-chiark",
                    "name": name,
                    "description": "; ".join(desc_bits) or None,
                    "endpoint_url": ep,
                    "homepage_url": ep,
                    "transport": transport,
                    "auth_method": "required" if it.get("auth_required") else None,
                    "tags": [str(s) for s in skills[:8]],
                    "confidence": conf,
                    "source": "chiark",
                    "source_id": str(cid),
                    "source_url": f"https://chiark.ai/agents/{cid}",
                })
            if len(rows) < per_page:
                break
            time.sleep(page_sleep)
    log.info("chiark: collected %d unique-endpoint agents", len(out))
    return out


CHIARK_DISCOVER = "https://chiark.ai/api/v1/discover"


def _chiark_discover(protocol: str, page_size: int = 100, max_pages: int = 40,
                     page_sleep: float = 0.3) -> list:
    """Paginate chiark's /discover endpoint for one protocol (a2a|mcp).

    The plain /agents list neither returns the `protocol` field nor filters
    by it; /discover does both, so it is the only way to isolate the A2A set.
    """
    out: list = []
    with httpx.Client(timeout=TIMEOUT,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for page in range(1, max_pages + 1):
            params = {"protocol": protocol, "page": page, "page_size": page_size}
            try:
                r = c.get(CHIARK_DISCOVER, params=params)
                if r.status_code == 429:
                    time.sleep(2.0)
                    r = c.get(CHIARK_DISCOVER, params=params)
                if r.status_code != 200:
                    log.warning("chiark discover: stop HTTP %d at page %d",
                                r.status_code, page)
                    break
                payload = r.json()
            except Exception as e:
                log.warning("chiark discover: error at page %d: %r", page, e)
                break
            agents = payload.get("agents") if isinstance(payload, dict) else None
            if not agents:
                break
            out.extend(a for a in agents if isinstance(a, dict))
            total = payload.get("total") or 0
            if len(out) >= total or len(agents) < page_size:
                break
            time.sleep(page_sleep)
    return out


def _chiark_a2a_id_set() -> set:
    """Source ids of chiark agents whose protocol is A2A, so the MCP crawler can
    skip them (the /agents list it reads cannot tell A2A and MCP apart)."""
    try:
        return {str(a.get("id")) for a in _chiark_discover("a2a") if a.get("id")}
    except Exception as e:
        log.warning("chiark a2a id set failed: %r", e)
        return set()


def fetch_chiark_a2a() -> list:
    """Pull chiark.ai's A2A agents (the protocol the /agents list hides) as
    a2a_agents row dicts."""
    out: list = []
    seen_slug: set = set()
    for it in _chiark_discover("a2a"):
        ep = (it.get("endpoint_url") or "").strip()
        if not ep or not ep.lower().startswith("http"):
            continue
        cid = it.get("id")
        name = (it.get("name") or _host_slug(ep)).strip()
        # operational_score is 0..max_score; normalise to a 0..1 confidence.
        score = it.get("operational_score")
        maxs = it.get("max_score") or 100.0
        conf = None
        try:
            if score is not None and maxs:
                conf = max(0.0, min(1.0, float(score) / float(maxs)))
        except (TypeError, ValueError):
            conf = None
        # discover gives skills as plain strings; wrap as dicts so the public
        # card view (which expects skill objects) renders them.
        skills = [{"name": str(s)} for s in (it.get("skills") or []) if s]
        cats = it.get("categories") or []
        desc_bits = []
        if cats:
            desc_bits.append(", ".join(str(c) for c in cats[:4]))
        up = it.get("uptime_30d")
        if up is not None:
            desc_bits.append(f"uptime_30d {up}")
        p95 = it.get("p95_latency_ms")
        if p95 is not None:
            desc_bits.append(f"p95 {p95}ms")
        # slug must be unique within a2a_agents (vs agenstry and within batch),
        # since upsert merges same-slug rows across sources.
        base = (_slugify(name) or _host_slug(ep)) + "-chiark"
        slug = base if base not in seen_slug else f"{base}-{str(cid)[:8]}"
        seen_slug.add(base)
        seen_slug.add(slug)
        out.append({
            "slug": slug,
            "name": name,
            "description": "; ".join(desc_bits) or None,
            "provider_name": it.get("provider"),
            "provider_url": it.get("provider_url"),
            "card_url": None,
            "endpoint_url": ep,
            "homepage_url": it.get("provider_url") or ep,
            "skills": skills,
            "auth_schemes": ["required"] if it.get("auth_gated") else None,
            "x402_supported": bool(it.get("payment_enabled")),
            "confidence": conf,
            "source": "chiark",
            "source_id": str(cid),
        })
    log.info("chiark a2a: collected %d agents", len(out))
    return out


MCP_CRAWLERS["chiark"] = fetch_chiark

# glama removed: catalog-only, all endpoint_url=null, not callable by agents (2026-06-04)
# MCP_CRAWLERS["glama"] = fetch_glama


# --- mcp-catalog.com (community-curated, Supabase-backed) -----------------
# A small but high-signal directory: every entry has a real callable endpoint
# (/mcp or /sse), maintained mostly by the official vendors (Cloudflare, GitHub,
# Notion, Stripe, ...). Data is served from a public Supabase PostgREST table;
# the anon key below is the site's own public client key (embedded in its JS),
# read-only. We re-derive it at runtime so a key rotation doesn't break us.
MCP_CATALOG_PAGE = "https://mcp-catalog.com/catalog"
MCP_CATALOG_SUPABASE = "https://zscusvbjclywtnfilbne.supabase.co"


def _mcp_catalog_anon_key() -> str | None:
    """Re-derive the public Supabase anon key from the site's JS bundle so a
    key rotation is picked up automatically (falls back to None on failure)."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            html = c.get(MCP_CATALOG_PAGE).text
            blob = html
            for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
                u = src if src.startswith("http") else "https://mcp-catalog.com" + src
                try:
                    blob += c.get(u).text
                except Exception:
                    continue
        keys = re.findall(
            r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}", blob)
        return max(keys, key=len) if keys else None
    except Exception as e:
        log.warning("mcp-catalog: anon key derive failed: %r", e)
        return None


def fetch_mcp_catalog() -> list:
    """Pull mcp-catalog.com's curated MCP servers (Supabase REST) as rows."""
    key = _mcp_catalog_anon_key()
    if not key:
        log.warning("mcp-catalog: no anon key, skipping")
        return []
    out: list = []
    seen_ep: set = set()
    hdr = {"User-Agent": UA, "Accept": "application/json",
           "apikey": key, "Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=TIMEOUT, headers=hdr) as c:
            r = c.get(f"{MCP_CATALOG_SUPABASE}/rest/v1/servers",
                      params={"select": "*", "order": "created_at.desc"})
            if r.status_code != 200:
                log.warning("mcp-catalog: HTTP %d", r.status_code)
                return []
            rows = r.json()
    except Exception as e:
        log.warning("mcp-catalog: fetch error: %r", e)
        return []
    if not isinstance(rows, list):
        return []
    for it in rows:
        if not isinstance(it, dict):
            continue
        ep = (it.get("url") or "").strip()
        if not ep or not ep.lower().startswith("http"):
            continue
        ep_key = ep.lower().rstrip("/")
        if ep_key in seen_ep:
            continue
        seen_ep.add(ep_key)
        name = it.get("name") or _host_slug(ep)
        low = ep.lower()
        transport = "sse" if low.endswith("/sse") else (
            "streamable-http" if "/mcp" in low or low.rstrip("/").endswith("mcp") else None)
        auth_raw = (it.get("authentication") or "").strip()
        auth_method = None
        if auth_raw and auth_raw.lower() not in ("open", "none", "public"):
            auth_method = "required"
        cat = (it.get("category") or "").strip()
        maint = (it.get("maintainer") or "").strip()
        tools = it.get("tools") if isinstance(it.get("tools"), list) else []
        tool_names = [t.get("name") for t in tools
                      if isinstance(t, dict) and t.get("name")]
        desc_bits = []
        if cat:
            desc_bits.append(cat)
        if maint:
            desc_bits.append(f"maintained by {maint}")
        if tool_names:
            desc_bits.append(f"{len(tool_names)} tools: " + ", ".join(tool_names[:8]))
        tags = [t for t in [cat] if t]
        out.append({
            "slug": _host_slug(ep) + "-mcpcatalog",
            "name": name,
            "description": " · ".join(desc_bits) or None,
            "endpoint_url": ep,
            "homepage_url": it.get("website") or ep,
            "transport": transport,
            "auth_method": auth_method,
            "tags": tags,
            "source": "mcp-catalog",
            "source_id": str(it.get("id") or ep_key),
            "source_url": MCP_CATALOG_PAGE,
        })
    log.info("mcp-catalog: collected %d unique-endpoint servers", len(out))
    return out


MCP_CRAWLERS["mcp-catalog"] = fetch_mcp_catalog
