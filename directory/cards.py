"""Agent-readable service cards for directory rows."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .url_identity import exact_endpoint


def _clean(value: Any) -> Any:
    """Recursively drop empty values while preserving false/0."""
    if isinstance(value, dict):
        out = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v) not in (None, "", [], {})]
    return value


def primary_http_request(row: dict[str, Any]) -> dict[str, Any]:
    info = row.get('call_info') if isinstance(row.get('call_info'), dict) else {}
    samples = row.get('resource_samples') or info.get('resource_samples') or []
    samples = samples if isinstance(samples, list) else []
    url = str(row.get('url') or '').strip()
    matching = [sample for sample in samples if isinstance(sample, dict)
                and exact_endpoint(str(sample.get('url') or sample.get('resource') or '')) == exact_endpoint(url)]
    methods = {str(sample.get('method') or '').upper() for sample in matching} & {'GET', 'POST'}
    known = len(methods) == 1
    method = next(iter(methods)) if known else 'GET'
    info_matches = bool(info.get('primary_url') and exact_endpoint(str(info['primary_url'])) == exact_endpoint(url)
                        and str(info.get('method') or '').upper() in ('GET', 'POST'))
    if info_matches:
        method = str(info['method']).upper()
        known = True
    empty_probe = method == 'POST' and (
        any(str(sample.get('method') or '').upper() == 'POST' and sample.get('probe_body') == {} for sample in matching)
        or info_matches and str(info['method']).upper() == 'POST' and info.get('probe_body') == {})
    return {'url': url, 'method': method, 'method_known': known, 'empty_post_probe': empty_probe}


def _primary_json_body(row: dict[str, Any], url: str) -> str | None:
    info = row.get('call_info') if isinstance(row.get('call_info'), dict) else {}
    samples = row.get('resource_samples') or info.get('resource_samples') or []
    sources = [sample for sample in samples if isinstance(sample, dict)
               and str(sample.get('method') or '').upper() == 'POST'
               and exact_endpoint(str(sample.get('url') or sample.get('resource') or '')) == exact_endpoint(url)] if isinstance(samples, list) else []
    if (str(info.get('method') or '').upper() == 'POST'
            and info.get('primary_url')
            and exact_endpoint(str(info['primary_url'])) == exact_endpoint(url)):
        sources.append(info)
    bodies = set()
    for source in sources:
        for field in ('probe_body', 'input_example'):
            if field not in source:
                continue
            value = source[field]
            if not isinstance(value, (dict, list)):
                return None
            try:
                encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                                     sort_keys=True, separators=(',', ':'))
            except (TypeError, ValueError, RecursionError):
                return None
            if len(encoded) > 64 * 1024:
                return None
            bodies.add(encoded)
    return next(iter(bodies)) if len(bodies) == 1 else None


def build_call_template(row: dict[str, Any]) -> dict[str, Any]:
    """Return ready-to-use call hints for MCP and x402 HTTP modes."""
    out: dict[str, Any] = {}
    mcp_url = (row.get("mcp_url") or "").strip()
    if mcp_url:
        out["mcp"] = {
            "transport": "streamable-http",
            "url": mcp_url,
            "python": (
                "from mcp import ClientSession\n"
                "from mcp.client.streamable_http import streamablehttp_client\n"
                "async with streamablehttp_client(\n"
                f"    {mcp_url!r}\n"
                ") as (r, w, _):\n"
                "    async with ClientSession(r, w) as s:\n"
                "        await s.initialize()\n"
                "        tools = await s.list_tools()\n"
                "        # await s.call_tool('<tool_name>', { ... })"
            ),
            "inspector_cli": (
                f"npx @modelcontextprotocol/inspector --transport http {mcp_url}"
            ),
            "claude_desktop_config": {
                row.get("slug") or "service": {
                    "type": "streamable-http",
                    "url": mcp_url,
                }
            },
        }

    request = primary_http_request(row)
    url, primary_method = request['url'], request['method']
    method_known, empty_probe = request['method_known'], request['empty_post_probe']
    if url:
        json_body = ('{}' if empty_probe else _primary_json_body(row, url)) if primary_method == 'POST' else None
        price_min = row.get("price_min")
        payment = row.get("payment") if isinstance(row.get("payment"), dict) else {}
        currency = row.get("currency") or payment.get("currency")
        price_hint = (
            (f"(about {price_min} {currency}/call)" if currency
             else f"(about ${price_min}/call)") if price_min is not None
            else "(price advertised in the 402 response)"
        )
        out["http_x402"] = {
            "url": url,
            "method": primary_method,
            "method_hint": (f"Use the {primary_method} method advertised for the primary endpoint." if method_known
                            else "No unambiguous primary method is recorded; GET is a fallback. Check the linked request schema."),
            "flow": [
                f"1. Probe {url} and expect HTTP 402 + accepts[] if it is gated.",
                "2. Pick an accepts[] option matching your network and budget.",
                "3. Sign the required x402 payment payload.",
                "4. Retry with header `X-PAYMENT: <base64 payload>`.",
                "5. Parse the service response; if the probe returned docs-only JSON, follow its request schema.",
            ],
            "curl_probe": (
                f'curl -sS -i -X {primary_method} {shlex.quote(url)}'
                + (" -H 'Content-Type: application/json' --data " + shlex.quote(json_body)
                   if json_body is not None else "")
                + "  "
                "# expect HTTP 402 Payment Required " + price_hint
            ),
            "well_known": row.get("well_known_url"),
            "openapi": row.get("openapi_url"),
            "chains": row.get("chains") or payment.get("chains") or [],
            "facilitator": row.get("facilitator") or payment.get("facilitator"),
        }

    if not out:
        out["note"] = "Service has no callable endpoint registered yet."
    return _clean(out)


def public_service_row(row: dict[str, Any]) -> dict[str, Any]:
    base = dict(row)
    base.pop("owner_declaration", None)
    if isinstance(base.get("call_info"), dict):
        base["call_info"] = dict(base["call_info"])
        base["call_info"].pop("owner_declaration_revision", None)
    from . import owner_declarations
    state = owner_declarations.public_state(row)
    if state:
        state.pop("fields", None)
        base["interface_declaration"] = state
    return base


def build_service_card(row: dict[str, Any]) -> dict[str, Any]:
    """Build an agent-readable card while preserving legacy top-level fields."""
    base = public_service_row(row)
    # Provenance is public (2026-08-18): keep the aggregated `sources` list and
    # drop only the internal dedup keys.
    base.pop("source", None)
    base.pop("source_id", None)
    base["sources"] = row.get("sources") or []
    payment = row.get("payment") if isinstance(row.get("payment"), dict) else {}
    call_info = dict(row.get("call_info")) if isinstance(row.get("call_info"), dict) else {}
    call_info.pop("owner_declaration_revision", None)
    if "call_info" in base:
        base["call_info"] = call_info
    quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    resource_samples = row.get("resource_samples") or call_info.get("resource_samples") or []
    accepts = payment.get("accepts") if isinstance(payment.get("accepts"), list) else []

    base["service"] = _clean({
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description"),
        "url": row.get("url"),
        "category": row.get("category"),
        "tags": row.get("tags") or [],
    })
    base["payment"] = _clean({
        **{k: v for k, v in payment.items() if k != "accepts"},
        "currency": row.get("currency") or payment.get("currency"),
        "price_min_usd": row.get("price_min"),
        "price_max_usd": row.get("price_max"),
        "chains": row.get("chains") or payment.get("chains") or [],
        "facilitator": row.get("facilitator") or payment.get("facilitator"),
        "accepts": accepts[:10],
    })
    base["call"] = _clean({
        **{k: v for k, v in call_info.items() if k != "resource_samples"},
        "primary_url": row.get("url"),
        "mcp_url": row.get("mcp_url"),
        "openapi_url": row.get("openapi_url"),
        "well_known_url": row.get("well_known_url"),
        "resource_count": (row.get("resource_count")
                   if row.get("resource_count") is not None
                   else call_info.get("resource_count")),
        # Origin-aggregated listings may publish dozens of canonical resources.
        # The database ingestion cap is 50; expose the same bounded set so an
        # agent can discover every advertised endpoint from the canonical card.
        "resource_samples": resource_samples[:50] if isinstance(resource_samples, list) else [],
        "template": build_call_template(row),
    })
    base["quality"] = _clean({
        "health": row.get("health"),
        "health_checked": row.get("health_checked"),
        "confidence": row.get("confidence"),
        "tx_30d": row.get("tx_30d"),
        **quality,
    })
    return _clean(base)


def brief_for_llm(card: dict[str, Any]) -> dict[str, Any]:
    """Small candidate representation for LLM ranking prompts."""
    return _clean({
        "slug": card.get("slug"),
        "name": card.get("name"),
        "description": card.get("description"),
        "url": card.get("url"),
        "category": card.get("category"),
        "chains": card.get("chains"),
        "price_min_usd": card.get("price_min"),
        "price_max_usd": card.get("price_max"),
        "health": card.get("health"),
        "confidence": card.get("confidence"),
        "tx_30d": card.get("tx_30d"),
        "resource_count": card.get("resource_count"),
        "resources": (card.get("call") or {}).get("resource_samples", [])[:5],
        "payment_accepts": (card.get("payment") or {}).get("accepts", [])[:3],
    })