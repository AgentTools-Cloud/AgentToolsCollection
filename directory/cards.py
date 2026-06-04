"""Agent-readable service cards for directory rows."""

from __future__ import annotations

from typing import Any


def _clean(value: Any) -> Any:
    """Recursively drop empty values while preserving false/0."""
    if isinstance(value, dict):
        out = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v) not in (None, "", [], {})]
    return value


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

    call_info = row.get("call_info") if isinstance(row.get("call_info"), dict) else {}
    resource_samples = row.get("resource_samples") or call_info.get("resource_samples") or []
    primary_resource = None
    if resource_samples and isinstance(resource_samples[0], dict):
        primary_resource = resource_samples[0].get("url") or resource_samples[0].get("resource")
    url = (primary_resource or row.get("url") or "").strip()
    if url:
        price_min = row.get("price_min")
        price_hint = (
            f"(about ${price_min}/call in USDC)" if price_min is not None
            else "(price advertised in the 402 response)"
        )
        out["http_x402"] = {
            "url": url,
            "method_hint": "Try the method advertised by the service card; POST is common for paid x402 APIs.",
            "flow": [
                f"1. Probe {url} and expect HTTP 402 + accepts[] if it is gated.",
                "2. Pick an accepts[] option matching your network and budget.",
                "3. Sign the required x402 payment payload.",
                "4. Retry with header `X-PAYMENT: <base64 payload>`.",
                "5. Parse the service response; if the probe returned docs-only JSON, follow its request schema.",
            ],
            "curl_probe": (
                f'curl -sS -i -X POST "{url}"  '
                "# expect HTTP 402 Payment Required " + price_hint
            ),
            "well_known": row.get("well_known_url"),
            "openapi": row.get("openapi_url"),
            "chains": row.get("chains") or [],
            "facilitator": row.get("facilitator"),
        }

    if not out:
        out["note"] = "Service has no callable endpoint registered yet."
    return _clean(out)


def build_service_card(row: dict[str, Any]) -> dict[str, Any]:
    """Build an agent-readable card while preserving legacy top-level fields."""
    base = dict(row)
    base.pop("source", None)
    base.pop("source_id", None)
    payment = row.get("payment") if isinstance(row.get("payment"), dict) else {}
    call_info = row.get("call_info") if isinstance(row.get("call_info"), dict) else {}
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
        "currency": row.get("currency") or "USDC",
        "price_min_usd": row.get("price_min"),
        "price_max_usd": row.get("price_max"),
        "chains": row.get("chains") or [],
        "facilitator": row.get("facilitator"),
        "accepts": accepts[:10],
        **{k: v for k, v in payment.items() if k != "accepts"},
    })
    base["call"] = _clean({
        "primary_url": row.get("url"),
        "mcp_url": row.get("mcp_url"),
        "openapi_url": row.get("openapi_url"),
        "well_known_url": row.get("well_known_url"),
        "resource_count": row.get("resource_count"),
        "resource_samples": resource_samples[:20] if isinstance(resource_samples, list) else [],
        "template": build_call_template(row),
        **{k: v for k, v in call_info.items() if k != "resource_samples"},
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