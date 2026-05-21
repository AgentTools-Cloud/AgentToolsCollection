"""Token momentum signal vertical (inspired by Arvos / AI Ape).

Free data source: DexScreener public REST API. For each token we pick the
highest-liquidity pair (optionally filtered by chain) and derive a directional
signal from the short-window price + volume + liquidity profile.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

import httpx

from . import cache_get, cache_set

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["token"],
    "properties": {
        "token": {
            "type": "string",
            "description": (
                "Token symbol (e.g. 'PEPE') or contract address (EVM 0x..., "
                "Solana base58). Symbols are resolved via DexScreener search."
            ),
        },
        "chain": {
            "type": "string",
            "description": (
                "Optional chain hint (e.g. 'ethereum', 'base', 'solana'). "
                "Default: pick highest-liquidity pair across all chains."
            ),
        },
        "commentary": {
            "type": "boolean",
            "default": False,
            "description": "If true, include a short Qwen-generated commentary.",
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["found", "as_of_ts", "disclaimer"],
    "properties": {
        "found": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["buy", "hold", "sell"]},
        "score": {"type": "number", "minimum": -1, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "as_of_ts": {"type": "integer"},
        "snapshot": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "chain": {"type": "string"},
                "pair_address": {"type": "string"},
                "price_usd": {"type": "number"},
                "price_change_5m": {"type": "number"},
                "price_change_1h": {"type": "number"},
                "price_change_24h": {"type": "number"},
                "volume_24h_usd": {"type": "number"},
                "liquidity_usd": {"type": "number"},
                "fdv_usd": {"type": "number"},
                "dex": {"type": "string"},
                "url": {"type": "string"},
            },
        },
        "commentary": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

_DISCLAIMER = "Informational signal only. Not financial advice. Do your own research."
_DEX_BASE = "https://api.dexscreener.com/latest/dex"
_CACHE_TTL = 60.0


def _is_address(token: str) -> bool:
    t = token.strip()
    if t.startswith("0x") and len(t) == 42:
        return True
    return 32 <= len(t) <= 44 and not t.startswith("0x") and t.isalnum()


async def _fetch_pairs(token: str) -> list[dict[str, Any]]:
    cached = cache_get(f"pairs:{token.lower()}", _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=10.0) as client:
        if _is_address(token):
            r = await client.get(f"{_DEX_BASE}/tokens/{token}")
        else:
            r = await client.get(f"{_DEX_BASE}/search", params={"q": token})
        r.raise_for_status()
        data = r.json()
    pairs = data.get("pairs") or []
    cache_set(f"pairs:{token.lower()}", pairs)
    return pairs


def _pick_pair(pairs, chain):
    if not pairs:
        return None
    if chain:
        cl = chain.lower()
        filtered = [p for p in pairs if (p.get("chainId") or "").lower() == cl]
        if filtered:
            pairs = filtered
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def _score(pair: dict[str, Any]) -> tuple[float, float]:
    pc = pair.get("priceChange") or {}
    ch5 = float(pc.get("m5") or 0) / 100.0
    ch1h = float(pc.get("h1") or 0) / 100.0
    ch24h = float(pc.get("h24") or 0) / 100.0
    vol24 = float((pair.get("volume") or {}).get("h24") or 0)
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    raw = 0.5 * ch1h + 0.3 * ch5 + 0.2 * ch24h
    if liq < 50_000:
        raw -= 0.4
    if liq > 0 and vol24 / max(liq, 1.0) > 5.0:
        raw -= 0.3
    if ch24h > 0.5:
        raw -= 0.2
    if -0.15 < ch24h < -0.05 and ch1h > 0:
        raw += 0.15
    score = max(-1.0, min(1.0, raw))
    conf = 0.0
    if liq >= 1_000_000:
        conf += 0.5
    elif liq >= 200_000:
        conf += 0.3
    elif liq >= 50_000:
        conf += 0.15
    if vol24 >= 500_000:
        conf += 0.3
    elif vol24 >= 50_000:
        conf += 0.15
    conf = max(0.05, min(1.0, conf))
    return score, conf


def _direction(score: float) -> str:
    if score >= 0.2:
        return "buy"
    if score <= -0.2:
        return "sell"
    return "hold"


async def handle(body: dict[str, Any], llm_call: Callable[..., Awaitable[str]]) -> dict[str, Any]:
    token = (body or {}).get("token") or ""
    chain = (body or {}).get("chain")
    want_commentary = bool((body or {}).get("commentary"))
    if not token or not isinstance(token, str):
        return {"found": False, "as_of_ts": int(time.time()),
                "error": "missing or invalid 'token' field", "disclaimer": _DISCLAIMER}
    try:
        pairs = await _fetch_pairs(token)
    except httpx.HTTPError as e:
        return {"found": False, "as_of_ts": int(time.time()),
                "error": f"dexscreener upstream error: {e}", "disclaimer": _DISCLAIMER}
    pair = _pick_pair(pairs, chain)
    if not pair:
        return {"found": False, "as_of_ts": int(time.time()),
                "error": "no pair found for the requested token/chain", "disclaimer": _DISCLAIMER}
    score, conf = _score(pair)
    direction = _direction(score)
    snapshot = {
        "symbol": (pair.get("baseToken") or {}).get("symbol") or "",
        "chain": pair.get("chainId") or "",
        "pair_address": pair.get("pairAddress") or "",
        "price_usd": float(pair.get("priceUsd") or 0),
        "price_change_5m": float((pair.get("priceChange") or {}).get("m5") or 0),
        "price_change_1h": float((pair.get("priceChange") or {}).get("h1") or 0),
        "price_change_24h": float((pair.get("priceChange") or {}).get("h24") or 0),
        "volume_24h_usd": float((pair.get("volume") or {}).get("h24") or 0),
        "liquidity_usd": float((pair.get("liquidity") or {}).get("usd") or 0),
        "fdv_usd": float(pair.get("fdv") or 0),
        "dex": pair.get("dexId") or "",
        "url": pair.get("url") or "",
    }
    out = {
        "found": True, "direction": direction,
        "score": round(score, 3), "confidence": round(conf, 3),
        "as_of_ts": int(time.time()), "snapshot": snapshot,
        "disclaimer": _DISCLAIMER,
    }
    if want_commentary:
        prompt = (
            "You are a concise crypto market analyst. In 2-3 short sentences "
            "(<=80 words total), explain the directional signal for the token "
            "below. Be neutral and factual.\n\n"
            f"Direction: {direction}\nScore: {score:.2f}\nSnapshot: {snapshot}\n\n"
            "Return plain text, no preamble."
        )
        try:
            commentary = await llm_call(prompt, max_tokens=200, temperature=0.4)
        except Exception:
            commentary = ""
        if commentary:
            out["commentary"] = commentary
    return out
