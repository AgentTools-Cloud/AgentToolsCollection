"""Unified resource search (P1) and paid-service broker (P2).

Sits on top of the per-protocol stores and presents one normalised shape
across the three agent-capability entry points:

  - x402 service  -> `services` table (payable HTTP API)
  - mcp server    -> standalone `mcp_servers` table (PulseMCP / official MCP
                     registry import), unioned with `services` rows that also
                     carry an `mcp_url` (an x402 API that is MCP-callable).
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


# Raw auth strings that don't actually gate access.
_OPEN_AUTH = {"", "none", "open", "public", "public-discovery", "[]", "null"}
# cost_hint values that mean "no charge".
_FREE_COST = {"free", "free_tier", "free_trial"}
_PAID_COST = {"paid", "pay-per-event", "pay_per_event", "subscription", "per-event"}


def _access_model(x402, auth=None, cost_hint=None, price_hint: dict | None = None) -> dict:
    """Who provisions access + whether/how it costs — the discovery facts an
    agent needs before calling. Mirrors db._access_case() for the tier.

      model: 'open' | 'human_key' | 'agent_pays'
      registration_required: must a human sign up / provision a credential?
      agent_can_self_serve: can an agent use it with no human in the loop?
      paid: True / False / None(unknown)
      billing: 'x402-per-call' | 'per-event' | 'subscription' | 'paid'
               | 'per-call-usd' | 'free' | None(unknown)
    """
    _a = str(auth).lower() if auth else ""
    has_x402 = bool(x402) or ("x402" in _a)
    key_gated = bool(auth) and _a.strip() not in _OPEN_AUTH
    cost = str(cost_hint).strip().lower() if cost_hint else ""

    if has_x402:
        model, label = "agent_pays", "Agent-pays — settles each call on-chain (x402); no human account needed"
    elif key_gated:
        model, label = "human_key", "Human key — a person must provision an API key / OAuth first"
    else:
        model, label = "open", "Open — no credentials, an agent can call directly"

    if has_x402:
        paid = True
    elif cost in _FREE_COST:
        paid = False
    elif cost in _PAID_COST or price_hint:
        paid = True
    else:
        paid = None

    if has_x402:
        billing = "x402-per-call"
    elif cost in ("pay-per-event", "pay_per_event", "per-event"):
        billing = "per-event"
    elif cost == "subscription":
        billing = "subscription"
    elif cost == "paid":
        billing = "paid"
    elif cost in _FREE_COST:
        billing = "free"
    elif price_hint:
        billing = "per-call-usd"
    else:
        billing = None

    return {
        "model": model,
        "label": label,
        "registration_required": model == "human_key",
        "agent_can_self_serve": model in ("open", "agent_pays"),
        "paid": paid,
        "billing": billing,
        "auth": auth if auth else None,
        "price_hint": price_hint,
    }


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
        "access": _access_model(
            True, auth=None, cost_hint=None,
            price_hint=_price_hint(row.get("price_min"), row.get("price_max"),
                                   row.get("currency"))),
        "health_status": row.get("health") or "unknown",
        "confidence": row.get("confidence"),
        "call_hint": _short_call_hint(cards.build_call_template(row)),
        "detail_url": f"https://agent-tools.cloud/api/v1/services/{row.get('slug')}",
    }


def normalize_mcp_server(row: dict) -> dict:
    """Normalise a standalone mcp_servers row into the unified shape."""
    protocols = ["mcp"]
    if row.get("x402_supported"):
        protocols.append("x402")
    endpoint = row.get("endpoint_url")
    call_hint: dict[str, Any] = {}
    if endpoint:
        call_hint["mcp"] = {
            "transport": row.get("transport") or "streamable-http",
            "url": endpoint,
            "auth": row.get("auth_method"),
            "cost": row.get("cost_hint"),
            # Revision the server settled on when we offered it the newest one
            # we know; null when we have not completed a handshake with it.
            "protocol_version": row.get("protocol_version"),
        }
    return {
        "type": "mcp",
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description"),
        "protocols": protocols,
        "endpoint_url": endpoint,
        "price_hint": None,
        "access": _access_model(row.get("x402_supported"),
                                auth=row.get("auth_method"),
                                cost_hint=row.get("cost_hint")),
        "kind": row.get("kind") or ("callable" if endpoint else "catalog"),
        "health_status": row.get("health") or "unknown",
        "confidence": row.get("confidence"),
        "call_hint": call_hint,
        "detail_url": f"https://agent-tools.cloud/api/v1/mcp/servers/{row.get('slug')}",
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
        "access": _access_model(
            row.get("x402_supported"), auth=row.get("auth_schemes"),
            cost_hint=None,
            price_hint=({"usd": price, "currency": "USDC"} if price is not None else None)),
        "health_status": row.get("health") or "unknown",
        "confidence": row.get("confidence"),
        "call_hint": {
            "transport": "a2a-jsonrpc",
            "card_url": row.get("card_url"),
            "endpoint": row.get("endpoint_url"),
        },
        "detail_url": f"https://agent-tools.cloud/api/v1/a2a/agents/{row.get('slug')}",
    }


def _exact_name_rows(conn, names) -> dict[str, list[dict]]:
    candidates: dict[str, str] = {}
    for value in names:
        name = str(value or "").strip()
        if name:
            candidates.setdefault(name.lower(), name)
    if not candidates:
        return {}

    placeholders = ",".join("?" for _ in candidates)
    params = tuple(candidates)
    grouped: dict[str, list[dict]] = {key: [] for key in candidates}

    for row in conn.execute(
            "SELECT slug,name,description,url,mcp_url,health FROM services "
            "WHERE lower(trim(name)) IN (%s)" % placeholders, params):
        grouped[row["name"].strip().lower()].append({
            "name": row["name"], "description": row["description"],
            "endpoint_url": row["url"], "card_url": None,
            "protocols": ["x402", "mcp"] if row["mcp_url"] else ["x402"],
            "health_status": row["health"] or "unknown",
            "listing": {
                "type": "x402", "slug": row["slug"],
                "detail_url": "https://agent-tools.cloud/services/%s" % row["slug"],
            },
        })
    for row in conn.execute(
            "SELECT slug,name,description,endpoint_url,health,x402_supported "
            "FROM mcp_servers WHERE lower(trim(name)) IN (%s)"
            % placeholders, params):
        grouped[row["name"].strip().lower()].append({
            "name": row["name"], "description": row["description"],
            "endpoint_url": row["endpoint_url"], "card_url": None,
            "protocols": ["mcp", "x402"] if row["x402_supported"] else ["mcp"],
            "health_status": row["health"] or "unknown",
            "listing": {
                "type": "mcp", "slug": row["slug"],
                "detail_url": "https://agent-tools.cloud/mcp/servers/%s" % row["slug"],
            },
        })
    for row in conn.execute(
            "SELECT slug,name,description,endpoint_url,card_url,health,x402_supported "
            "FROM a2a_agents WHERE lower(trim(name)) IN (%s)"
            % placeholders, params):
        grouped[row["name"].strip().lower()].append({
            "name": row["name"], "description": row["description"],
            "endpoint_url": row["endpoint_url"], "card_url": row["card_url"],
            "protocols": ["a2a", "x402"] if row["x402_supported"] else ["a2a"],
            "health_status": row["health"] or "unknown",
            "listing": {
                "type": "a2a", "slug": row["slug"],
                "detail_url": "https://agent-tools.cloud/a2a/agents/%s" % row["slug"],
            },
        })
    return grouped


def _exact_name_group(name: str, raw: list[dict]) -> dict | None:
    products: dict[str, dict] = {}
    for item in raw:
        product_url = (item.get("endpoint_url") or item.get("card_url") or "").strip()
        key = db._norm_endpoint(product_url) if product_url else (
            "%s:%s" % (item["listing"]["type"], item["listing"]["slug"]))
        existing = products.get(key)
        if existing is None:
            host, _ = db._host_and_path(product_url)
            products[key] = {
                "name": item.get("name"),
                "url": product_url or None,
                "host": host or None,
                "endpoint_url": item.get("endpoint_url"),
                "card_url": item.get("card_url"),
                "protocols": sorted(set(item.get("protocols") or [])),
                "description": item.get("description"),
                "health_status": item.get("health_status") or "unknown",
                "listings": [item["listing"]],
            }
            continue
        existing["protocols"] = sorted(
            set(existing["protocols"]) | set(item.get("protocols") or []))
        if item["listing"] not in existing["listings"]:
            existing["listings"].append(item["listing"])
        if not existing.get("endpoint_url") and item.get("endpoint_url"):
            existing["endpoint_url"] = item["endpoint_url"]
        if not existing.get("card_url") and item.get("card_url"):
            existing["card_url"] = item["card_url"]
        if _HEALTH_RANK.get(item.get("health_status"), 4) < _HEALTH_RANK.get(
                existing.get("health_status"), 4):
            existing["health_status"] = item["health_status"]

    if len(products) < 2:
        return None
    matches = sorted(products.values(), key=lambda item: (
        (item.get("host") or "").lower(), (item.get("url") or "").lower()))
    return {
        "name": matches[0].get("name") or name,
        "count": len(matches),
        "listing_count": len(raw),
        "complete": True,
        "deduplicated_by": "normalized_url",
        "scope": "all_protocols",
        "matches": matches,
    }


def exact_name_matches(conn, query: str | None) -> dict | None:
    """Return every distinct URL whose complete display name equals `query`."""
    name = (query or "").strip()
    if not name:
        return None
    grouped = _exact_name_rows(conn, (name,))
    return _exact_name_group(name, grouped.get(name.lower(), []))


def exact_name_groups(conn, query: str | None,
                      result_names) -> list[dict]:
    """Complete exact-name groups touched by a ranked search window."""
    query_name = (query or "").strip()
    candidates: dict[str, str] = {}
    for value in (query_name, *tuple(result_names)):
        name = str(value or "").strip()
        if name:
            candidates.setdefault(name.lower(), name)
    grouped = _exact_name_rows(conn, candidates.values())
    groups: list[dict] = []
    for key, name in candidates.items():
        group = _exact_name_group(name, grouped.get(key, []))
        if group is None:
            continue
        group["query_exact"] = key == query_name.lower()
        groups.append(group)
    groups.sort(key=lambda group: (
        not group["query_exact"], (group.get("name") or "").casefold()))
    return groups


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
        for r in db.search_mcp(conn, q=q, health=health, limit=pull):
            items.append(normalize_mcp_server(r))
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
    exact_groups = exact_name_groups(
        conn, q, (item.get("name") for item in window))
    return {
        "query": q,
        "protocol": protocol,
        "count": len(window),
        "total_matched": len(deduped),
        "resources": window,
        "exact_name_matches": next(
            (group for group in exact_groups if group["query_exact"]), None),
        "exact_name_groups": exact_groups,
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
