"""Unified resource search (P1) and paid-service broker (P2).

Sits on top of the per-protocol stores and presents one normalised shape
across the three agent-capability entry points:

  - x402 service  -> `services` table (payable HTTP API)
  - mcp server    -> `services` rows that carry an `mcp_url` (MCP-callable);
                     a standalone MCP directory (Smithery / MCP.so import) is
                     deferred -- for now MCP discovery extends the x402 data
                     we already have, per doc "MCP search 从已有 discovery 扩展".
  - a2a agent     -> `a2a_agents` table

The broker (P2) does *discovery + scoring only*. It never moves money; it
returns a ranked shortlist of payable endpoints with call/pay hints so an
agent (or a later automated facade) can settle via the facilitator.
"""

from __future__ import annotations

import math
from typing import Any

from . import cards, db

_HEALTH_RANK = {"ok": 0, "degraded": 1, "unknown": 2, "down": 3}


def _price_hint(price_min, price_max, currency: str | None = "USDC") -> dict | None:
    if price_min is None and price_max is None:
        return None
    cur = currency or "USDC"
    if price_min is not None and price_max is not None and price_min != price_max:
        return {"min_usd": price_min, "max_usd": price_max, "currency": cur}
    val = price_min if price_min is not None else price_max
    return {"usd": val, "currency": cur}


def _short_call_hint(template: dict) -> dict:
    """Trim cards.build_call_template down to a compact discovery hint."""
    hint: dict[str, Any] = {}
    mcp = template.get("mcp")
    if isinstance(mcp, dict) and mcp.get("url"):
        hint["mcp"] = {"transport": "streamable-http", "url": mcp["url"]}
    http = template.get("http_x402")
    if isinstance(http, dict) and http.get("url"):
        hint["http_x402"] = {
            "url": http["url"],
            "chains": http.get("chains") or [],
            "facilitator": http.get("facilitator"),
        }
    return hint


# ---------------------------------------------------------------------------
# Normalisers -> unified resource shape
# ---------------------------------------------------------------------------

def normalize_service(row: dict, as_mcp: bool = False) -> dict:
    mcp_url = (row.get("mcp_url") or "").strip()
    protocols = ["x402"]
    if mcp_url:
        protocols.append("mcp")
    return {
        "type": "mcp" if as_mcp else "x402",
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description"),
        "protocols": protocols,
        "endpoint_url": (mcp_url if as_mcp else row.get("url")) or row.get("url"),
        "price_hint": _price_hint(row.get("price_min"), row.get("price_max"),
                                  row.get("currency")),
        "health_status": row.get("health") or "unknown",
        "confidence": row.get("confidence"),
        "call_hint": _short_call_hint(cards.build_call_template(row)),
        "detail_url": f"https://agent-tools.cloud/api/v1/services/{row.get('slug')}",
    }


def normalize_a2a(row: dict) -> dict:
    protocols = ["a2a"]
    if row.get("x402_supported"):
        protocols.append("x402")
    price = row.get("price_hint_usd")
    return {
        "type": "a2a",
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description"),
        "protocols": protocols,
        "endpoint_url": row.get("endpoint_url"),
        "price_hint": ({"usd": price, "currency": "USDC"} if price is not None else None),
        "health_status": row.get("health") or "unknown",
        "confidence": row.get("confidence"),
        "call_hint": {
            "transport": "a2a-jsonrpc",
            "card_url": row.get("card_url"),
            "endpoint": row.get("endpoint_url"),
        },
        "detail_url": f"https://agent-tools.cloud/api/v1/a2a/agents/{row.get('slug')}",
    }


def _sort_key(item: dict) -> tuple:
    health = _HEALTH_RANK.get(item.get("health_status"), 4)
    conf = item.get("confidence")
    return (health, 0 if conf is not None else 1, -(conf or 0.0))


# ---------------------------------------------------------------------------
# P1: unified resource search
# ---------------------------------------------------------------------------

def unified_search(conn, q: str | None = None, protocol: str | None = None,
                   chain: str | None = None, health: str | None = None,
                   limit: int = 20, offset: int = 0) -> dict:
    """Union search across x402 / mcp / a2a, returning normalised rows."""
    protocol = (protocol or "").lower() or None
    pull = limit + offset
    items: list[dict] = []

    if protocol in (None, "x402"):
        for r in db.search(conn, q=q, chain=chain, health=health, limit=pull):
            items.append(normalize_service(r, as_mcp=False))
    if protocol in (None, "mcp"):
        for r in db.search(conn, q=q, chain=chain, health=health,
                           has_mcp=True, limit=pull):
            items.append(normalize_service(r, as_mcp=True))
    if protocol in (None, "a2a"):
        for r in db.search_a2a(conn, q=q, health=health, limit=pull):
            items.append(normalize_a2a(r))

    # When unfiltered, a service that is both x402 and mcp would surface twice
    # (once per protocol pass). Collapse by (type, slug) keeping first seen.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for it in items:
        key = (it["type"], it["slug"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    deduped.sort(key=_sort_key)
    window = deduped[offset:offset + limit]
    return {
        "query": q,
        "protocol": protocol,
        "count": len(window),
        "total_matched": len(deduped),
        "resources": window,
    }


# ---------------------------------------------------------------------------
# P2: paid-service broker (discovery + scoring, no settlement)
# ---------------------------------------------------------------------------

def _score(row: dict, max_price_usd: float | None) -> float:
    """Heuristic 0..1 fitness score for a payable x402 service."""
    score = 0.0
    health = row.get("health")
    score += {"ok": 0.40, "degraded": 0.20, "unknown": 0.10}.get(health, 0.0)
    conf = row.get("confidence")
    if conf is not None:
        score += 0.25 * max(0.0, min(1.0, float(conf)))
    tx = row.get("tx_30d") or 0
    if tx > 0:
        # log-scaled demand signal, saturates ~ tx=10k
        score += 0.20 * min(1.0, math.log10(tx + 1) / 4.0)
    if (row.get("mcp_url") or "").strip():
        score += 0.05
    price = row.get("price_min")
    if max_price_usd is not None and price is not None:
        if price <= max_price_usd:
            score += 0.10
    elif price is not None:
        score += 0.05
    return round(min(1.0, score), 4)


def broker_recommend(conn, q: str, max_price_usd: float | None = None,
                     chain: str | None = None, require_healthy: bool = True,
                     limit: int = 5) -> dict:
    """Recommend payable x402 endpoints for an intent, ranked by fitness."""
    candidates = db.search(
        conn, q=q, chain=chain,
        health="ok" if require_healthy else None,
        limit=max(limit * 4, 20),
    )
    picks: list[dict] = []
    for row in candidates:
        price = row.get("price_min")
        if max_price_usd is not None and price is not None and price > max_price_usd:
            continue
        template = cards.build_call_template(row)
        picks.append({
            "slug": row.get("slug"),
            "name": row.get("name"),
            "description": row.get("description"),
            "score": _score(row, max_price_usd),
            "price_hint": _price_hint(row.get("price_min"), row.get("price_max"),
                                      row.get("currency")),
            "health_status": row.get("health") or "unknown",
            "confidence": row.get("confidence"),
            "tx_30d": row.get("tx_30d"),
            "chains": row.get("chains") or [],
            "call_hint": _short_call_hint(template),
            "pay_hint": {
                "scheme": "x402",
                "facilitator": row.get("facilitator"),
                "chains": row.get("chains") or [],
                "price_usd": price,
                "flow": (
                    "Probe endpoint -> expect HTTP 402 + accepts[] -> pick an "
                    "accepts option for your chain/budget -> sign payload -> "
                    "retry with header X-PAYMENT -> parse response."
                ),
            },
            "detail_url": f"https://agent-tools.cloud/api/v1/services/{row.get('slug')}",
        })
    picks.sort(key=lambda p: p["score"], reverse=True)
    picks = picks[:limit]
    return {
        "query": q,
        "max_price_usd": max_price_usd,
        "count": len(picks),
        "settlement": "not_automated",
        "note": (
            "Broker is discovery-and-scoring only. It does not move funds; "
            "use pay_hint + the facilitator to settle x402 payments yourself."
        ),
        "recommendations": picks,
    }
