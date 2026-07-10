"""Reverse-discovery sources observed crawling agent-tools.cloud.

These sources are metadata-only: public well-known documents, OpenAPI
metadata, A2A Agent Cards, and MCP endpoints that the normal health job probes
with initialize/tools-list. They never call business tools or paid endpoints.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from . import a2a as a2a_mod
from . import db
from .crawlers import TIMEOUT, UA, _host_slug, network_to_chain

log = logging.getLogger("directory.reverse_sources")

SOURCE = "reverse-discovery"

X402_TARGETS = [
    {
        "source_id": "x402-fuchss-app",
        "base_url": "https://x402.fuchss.app",
        "well_known_url": "https://x402.fuchss.app/.well-known/x402",
        "openapi_url": "https://x402.fuchss.app/openapi.json",
        "name": "x402.fuchss.app Trust API",
        "description": "Trust and reliability data for x402 endpoints.",
        "tags": ["x402", "trust", "reliability", "score"],
        "confidence": 0.8,
    },
    {
        "source_id": "agenstry-x402",
        "base_url": "https://agenstry.com",
        "well_known_url": "https://agenstry.com/.well-known/x402",
        "openapi_url": None,
        "name": "Agenstry x402 Agents",
        "description": (
            "Agenstry task-planning and orchestration skills exposed through "
            "x402 payment metadata."
        ),
        "tags": ["x402", "a2a", "agents", "orchestration"],
        "confidence": 0.75,
    },
]

MCP_TARGETS = [
    {
        "source_id": "glimind-mcp",
        "slug": "reverse-glimind-mcp",
        "name": "Glimind MCP",
        "description": (
            "Reliability layer for checking whether AI tools, MCP servers, "
            "and APIs are working before calling them."
        ),
        "homepage_url": "https://glimind.com",
        "endpoint_url": "https://glimind.com/mcp",
        "auth_method": "open",
        "tags": ["reliability", "mcp", "tool-status"],
        "confidence": 0.85,
    },
    {
        "source_id": "semrush-mcp",
        "slug": "reverse-semrush-mcp",
        "name": "Semrush MCP",
        "description": (
            "OAuth-protected Semrush MCP server for SEO and marketing "
            "intelligence workflows."
        ),
        "homepage_url": "https://mcp.semrush.com/",
        "endpoint_url": "https://mcp.semrush.com/v1/mcp",
        "auth_method": "oauth",
        "tags": ["seo", "marketing", "analytics", "mcp"],
        "confidence": 0.7,
    },
]

A2A_CARDS = [
    ("glimind", "https://glimind.com/.well-known/agent-card.json", "reverse-glimind"),
]


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _price_from_accept(accept: dict) -> float | None:
    raw = accept.get("amount")
    if raw is None:
        return None
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    decimals = extra.get("decimals", 6)
    try:
        return float(raw) / (10 ** int(decimals))
    except (TypeError, ValueError, OverflowError):
        return None


def _sample_from_accept(base_url: str, accept: dict) -> dict:
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    resource = (
        accept.get("resource")
        or accept.get("url")
        or accept.get("endpoint")
        or accept.get("path")
        or base_url
    )
    if isinstance(resource, str) and resource.startswith("/"):
        resource = urljoin(base_url, resource)
    sample = {
        "kind": "x402-accept",
        "url": resource,
        "scheme": accept.get("scheme"),
        "network": accept.get("network"),
        "amount": accept.get("amount"),
        "asset": accept.get("asset"),
        "payTo": accept.get("payTo"),
    }
    if extra.get("skill"):
        sample["skill"] = extra.get("skill")
    if extra.get("description"):
        sample["description"] = extra.get("description")
    return {k: v for k, v in sample.items() if v not in (None, "")}


def _payment_from_accept(accept: dict) -> dict:
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    payment = {
        "scheme": accept.get("scheme"),
        "network": accept.get("network"),
        "amount": accept.get("amount"),
        "asset": accept.get("asset"),
        "payTo": accept.get("payTo"),
        "currency": extra.get("name"),
        "decimals": extra.get("decimals"),
        "skill": extra.get("skill"),
    }
    price = _price_from_accept(accept)
    if price is not None:
        payment["price"] = price
    return {k: v for k, v in payment.items() if v not in (None, "")}


def _openapi_info(
    client: httpx.Client, url: str | None
) -> tuple[str | None, str | None, dict | None]:
    if not url:
        return None, None, None
    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = _json_or_empty(resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("reverse x402 openapi %s failed: %r", url, exc)
        return None, None, None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    title = (info.get("title") or "").strip() or None
    desc = (info.get("description") or "").strip() or None
    path_count = len(data.get("paths") or {}) if isinstance(data.get("paths"), dict) else None
    return title, desc, {"openapi": data.get("openapi"), "path_count": path_count}


def _row_for_x402(target: dict, client: httpx.Client) -> dict | None:
    base_url = target["base_url"]
    well_known_url = target["well_known_url"]
    try:
        resp = client.get(well_known_url)
        resp.raise_for_status()
        data = _json_or_empty(resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("reverse x402 well-known %s failed: %r", well_known_url, exc)
        return None

    resources = [r for r in (data.get("resources") or []) if isinstance(r, str)]
    accepts = [a for a in (data.get("accepts") or []) if isinstance(a, dict)]
    title, openapi_desc, openapi_meta = _openapi_info(client, target.get("openapi_url"))

    samples = [{"kind": "x402-resource", "url": r} for r in resources]
    samples.extend(_sample_from_accept(base_url, a) for a in accepts)
    samples = samples[:20]

    payments = [_payment_from_accept(a) for a in accepts]
    payments = [p for p in payments if p]
    prices = [p["price"] for p in payments if isinstance(p.get("price"), (int, float))]
    chains = sorted({
        chain for chain in (network_to_chain(a.get("network")) for a in accepts) if chain
    })

    description = (openapi_desc or target["description"]).strip()
    if len(description) > 650:
        description = description[:647].rstrip() + "..."

    quality: dict[str, Any] = {"well_known_http_status": resp.status_code}
    if openapi_meta:
        quality["openapi"] = openapi_meta

    return {
        "slug": f"reverse-{_host_slug(base_url)}"[:80],
        "name": (title or target["name"])[:200],
        "url": base_url,
        "description": description,
        "category": "general",
        "chains": chains,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "currency": "USDC" if payments else None,
        "openapi_url": target.get("openapi_url"),
        "well_known_url": well_known_url,
        "source": "reverse-x402",
        "source_id": target["source_id"],
        "tags": target["tags"],
        "region": "global",
        "confidence": target.get("confidence"),
        "resource_count": len(resources) + len(accepts),
        "resource_samples": samples,
        "payment": {"accepts": payments[:20]} if payments else None,
        "call_info": {
            "metadata_urls": [u for u in (well_known_url, target.get("openapi_url")) if u]
        },
        "quality": quality,
    }


def fetch_reverse_x402() -> list[dict]:
    rows: list[dict] = []
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as client:
        for target in X402_TARGETS:
            row = _row_for_x402(target, client)
            if row:
                rows.append(row)
    log.info("reverse-x402: %d metadata services", len(rows))
    return rows


def fetch_reverse_mcp() -> list[dict]:
    rows: list[dict] = []
    for target in MCP_TARGETS:
        rows.append({
            "slug": target["slug"],
            "name": target["name"],
            "description": target["description"],
            "homepage_url": target["homepage_url"],
            "endpoint_url": target["endpoint_url"],
            "transport": "streamable-http",
            "auth_method": target["auth_method"],
            "tags": target["tags"],
            "x402_supported": False,
            "source": SOURCE,
            "source_id": target["source_id"],
            "source_url": target["homepage_url"],
            "confidence": target["confidence"],
        })
    log.info("reverse-discovery mcp: %d metadata endpoints", len(rows))
    return rows


def fetch_reverse_a2a() -> list[dict]:
    rows: list[dict] = []
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as client:
        for source_id, card_url, slug in A2A_CARDS:
            try:
                card, resolved_url = a2a_mod.fetch_agent_card(card_url, client=client)
            except Exception as exc:  # noqa: BLE001
                log.warning("reverse a2a %s failed: %r", card_url, exc)
                continue
            if not card or not resolved_url:
                continue
            row = a2a_mod.card_to_row(
                card,
                resolved_url,
                source=SOURCE,
                source_id=source_id,
                slug=slug,
            )
            if not (row.get("endpoint_url") or "").strip():
                log.info("reverse a2a %s skipped: no callable endpoint", resolved_url)
                continue
            row["confidence"] = max(float(row.get("confidence") or 0), 0.85)
            rows.append(row)
    log.info("reverse-discovery a2a: %d agent cards", len(rows))
    return rows


def crawl_reverse_a2a() -> dict:
    rows = fetch_reverse_a2a()
    inserted = updated = skipped = 0

    def _write() -> None:
        nonlocal inserted, updated, skipped
        with db.writer() as conn:
            known_other = {
                (r[0] or "").lower().rstrip("/")
                for r in conn.execute(
                    "SELECT endpoint_url FROM a2a_agents WHERE source!=?",
                    (SOURCE,),
                ).fetchall()
            }
            for row in rows:
                endpoint = (row.get("endpoint_url") or "").lower().rstrip("/")
                has_same_source = conn.execute(
                    "SELECT 1 FROM a2a_agents WHERE source=? AND source_id=? LIMIT 1",
                    (SOURCE, row.get("source_id")),
                ).fetchone()
                if endpoint and endpoint in known_other and not has_same_source:
                    skipped += 1
                    continue
                created, _ = db.upsert_a2a_agent(conn, row)
                inserted += int(created)
                updated += int(not created)

    if rows:
        db.with_retry(_write)
    return {
        "candidates": len(A2A_CARDS),
        "resolved": len(rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "failed": 0,
    }
