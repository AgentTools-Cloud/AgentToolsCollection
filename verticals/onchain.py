"""On-chain analytics natural-language Q&A vertical (inspired by Ainalyst / Canza).

Free upstream data: Defillama (api.llama.fi, yields.llama.fi,
stablecoins.llama.fi) + DexScreener. The handler classifies the question,
fetches a relevant snapshot, then asks Qwen to ground its answer strictly
on that snapshot (JSON-out).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from . import cache_get, cache_set

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["question"],
    "properties": {
        "question": {"type": "string",
                     "description": "Natural-language on-chain analytics question."},
        "context": {
            "type": "object",
            "description": "Optional hints to disambiguate the question.",
            "properties": {
                "chain": {"type": "string"},
                "token": {"type": "string"},
                "address": {"type": "string"},
                "protocol": {"type": "string"},
            },
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer", "as_of_ts", "sources"],
    "properties": {
        "answer": {"type": "string"},
        "structured": {"type": "object"},
        "data_snapshot": {"type": "object"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "as_of_ts": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

_DEX = "https://api.dexscreener.com/latest/dex"
_LLAMA = "https://api.llama.fi"
_LLAMA_YIELDS = "https://yields.llama.fi"
_LLAMA_STABLES = "https://stablecoins.llama.fi"
_CACHE_TTL = 120.0


def _classify(question: str, ctx: dict) -> str:
    q = question.lower()
    if ctx.get("address") or ctx.get("token") or re.search(r"\b0x[a-f0-9]{40}\b", q):
        return "token"
    if "apy" in q or "yield" in q or "farm" in q or "pool" in q:
        return "yields"
    if "stable" in q or "usdt" in q or "usdc" in q or "dai" in q:
        return "stables"
    if "tvl" in q or "protocol" in q or "defi" in q:
        return "tvl"
    return "general"


async def _fetch_token(token: str, chain):
    key = f"onchain:token:{token.lower()}:{(chain or '').lower()}"
    cached = cache_get(key, _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=10.0) as client:
        if token.startswith("0x") and len(token) == 42:
            r = await client.get(f"{_DEX}/tokens/{token}")
        else:
            r = await client.get(f"{_DEX}/search", params={"q": token})
        r.raise_for_status()
        data = r.json()
    pairs = data.get("pairs") or []
    if chain:
        pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain.lower()] or pairs
    top = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), default=None)
    snap = {"source": "dexscreener", "pairs_seen": len(pairs)}
    if top:
        snap.update({
            "symbol": (top.get("baseToken") or {}).get("symbol") or "",
            "chain": top.get("chainId") or "",
            "price_usd": float(top.get("priceUsd") or 0),
            "price_change_24h": float((top.get("priceChange") or {}).get("h24") or 0),
            "volume_24h_usd": float((top.get("volume") or {}).get("h24") or 0),
            "liquidity_usd": float((top.get("liquidity") or {}).get("usd") or 0),
            "fdv_usd": float(top.get("fdv") or 0),
            "dex": top.get("dexId") or "",
            "url": top.get("url") or "",
        })
    cache_set(key, snap)
    return snap


async def _fetch_yields(token, chain):
    key = f"onchain:yields:{(token or '').lower()}:{(chain or '').lower()}"
    cached = cache_get(key, _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_LLAMA_YIELDS}/pools")
        r.raise_for_status()
        data = r.json()
    pools = data.get("data") or []
    if chain:
        pools = [p for p in pools if (p.get("chain") or "").lower() == chain.lower()]
    if token:
        t = token.lower()
        pools = [p for p in pools if t in (p.get("symbol") or "").lower()]
    pools = sorted(pools, key=lambda p: float(p.get("apy") or 0), reverse=True)[:10]
    snap = {
        "source": "defillama-yields",
        "pool_count": len(pools),
        "top_pools": [
            {"project": p.get("project"), "symbol": p.get("symbol"),
             "chain": p.get("chain"), "apy": p.get("apy"),
             "tvl_usd": p.get("tvlUsd"), "il_risk": p.get("ilRisk"),
             "exposure": p.get("exposure"), "pool_id": p.get("pool")}
            for p in pools
        ],
    }
    cache_set(key, snap)
    return snap


async def _fetch_stables():
    key = "onchain:stables"
    cached = cache_get(key, _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_LLAMA_STABLES}/stablecoins", params={"includePrices": "true"})
        r.raise_for_status()
        data = r.json()
    items = data.get("peggedAssets") or []
    items = sorted(items, key=lambda x: float((x.get("circulating") or {}).get("peggedUSD") or 0),
                   reverse=True)[:10]
    snap = {
        "source": "defillama-stablecoins",
        "top_stables": [
            {"name": x.get("name"), "symbol": x.get("symbol"),
             "pegType": x.get("pegType"),
             "circulating_usd": (x.get("circulating") or {}).get("peggedUSD"),
             "price": x.get("price")}
            for x in items
        ],
    }
    cache_set(key, snap)
    return snap


async def _fetch_tvl(protocol, chain):
    key = f"onchain:tvl:{(protocol or '').lower()}:{(chain or '').lower()}"
    cached = cache_get(key, _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
        if protocol:
            r = await client.get(f"{_LLAMA}/tvl/{protocol}")
            r.raise_for_status()
            snap = {"source": "defillama-tvl", "protocol": protocol, "tvl_usd": r.json()}
        elif chain:
            r = await client.get(f"{_LLAMA}/v2/historicalChainTvl/{chain}")
            r.raise_for_status()
            arr = r.json() or []
            latest = arr[-1] if arr else {}
            snap = {"source": "defillama-chain-tvl", "chain": chain,
                    "tvl_usd": latest.get("tvl"), "as_of_ts": latest.get("date")}
        else:
            r = await client.get(f"{_LLAMA}/protocols")
            r.raise_for_status()
            arr = r.json() or []
            arr = sorted(arr, key=lambda p: float(p.get("tvl") or 0), reverse=True)[:10]
            snap = {"source": "defillama-protocols",
                    "top_protocols": [
                        {"name": p.get("name"), "category": p.get("category"),
                         "chain": p.get("chain"), "tvl_usd": p.get("tvl")} for p in arr
                    ]}
    cache_set(key, snap)
    return snap


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


async def handle(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    question = (body or {}).get("question") or ""
    ctx = (body or {}).get("context") or {}
    if not question or not isinstance(question, str):
        return {"answer": "missing or invalid 'question' field", "structured": {},
                "data_snapshot": {}, "sources": [], "as_of_ts": int(time.time()),
                "confidence": "low"}
    intent = _classify(question, ctx)
    snapshot, sources = {}, []
    try:
        if intent == "token":
            tok = ctx.get("address") or ctx.get("token") or ""
            if not tok:
                m = re.search(r"\b0x[a-fA-F0-9]{40}\b", question)
                if m:
                    tok = m.group(0)
            if tok:
                snapshot = await _fetch_token(tok, ctx.get("chain"))
                sources.append("dexscreener")
        elif intent == "yields":
            snapshot = await _fetch_yields(ctx.get("token"), ctx.get("chain"))
            sources.append("defillama-yields")
        elif intent == "stables":
            snapshot = await _fetch_stables()
            sources.append("defillama-stablecoins")
        elif intent == "tvl":
            snapshot = await _fetch_tvl(ctx.get("protocol"), ctx.get("chain"))
            sources.append("defillama")
    except httpx.HTTPError as e:
        return {"answer": f"upstream data fetch failed: {e}", "structured": {},
                "data_snapshot": {}, "sources": [], "as_of_ts": int(time.time()),
                "confidence": "low"}
    prompt = (
        "You are an on-chain analytics agent. Answer the user's question "
        "STRICTLY using the JSON snapshot below \u2014 if the snapshot does not "
        "contain enough information, say so explicitly. Respond with a single "
        "JSON object: {\"answer\": \"<concise natural-language answer, 1-3 "
        "sentences>\", \"structured\": {<derived key facts as flat JSON>}}.\n\n"
        f"Question: {question}\nContext: {json.dumps(ctx, ensure_ascii=False)}\n"
        f"Intent: {intent}\nSnapshot: {json.dumps(snapshot, ensure_ascii=False)[:6000]}\n\n"
        "Return only the JSON object, no preamble."
    )
    try:
        raw = await llm_call(prompt, max_tokens=600, temperature=0.2)
    except Exception:
        raw = ""
    parsed = _extract_json(raw)
    answer = parsed.get("answer") or (raw.strip() if raw else "no answer")
    structured = parsed.get("structured") or {}
    has_data = bool(snapshot) and intent != "general"
    confidence = "high" if has_data and parsed else ("medium" if has_data else "low")
    return {"answer": answer, "structured": structured, "data_snapshot": snapshot,
            "sources": sources, "as_of_ts": int(time.time()), "confidence": confidence}


# ===================== PRO TIER: multi-source report =====================

INPUT_SCHEMA_REPORT: dict[str, Any] = {
    "type": "object",
    "required": ["topic"],
    "properties": {
        "topic": {
            "type": "string",
            "description": "Free-form report topic (e.g. 'is wstETH safe', "
                           "'best stablecoin yields on Base').",
        },
        "chain": {"type": "string"},
        "token": {"type": "string"},
        "protocol": {"type": "string"},
    },
}

OUTPUT_SCHEMA_REPORT: dict[str, Any] = {
    "type": "object",
    "required": ["report", "as_of_ts", "sources"],
    "properties": {
        "report": {"type": "string"},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "data_snapshots": {"type": "object"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "as_of_ts": {"type": "integer"},
    },
}


async def handle_report(
    body: dict,
    llm_call: Callable[..., Awaitable[str]],
) -> dict:
    """Pro tier: fetch token + yields + tvl + stables snapshots in parallel
    and ask Qwen for a longer synthesized report."""
    topic = (body or {}).get("topic") or ""
    chain = (body or {}).get("chain")
    token = (body or {}).get("token")
    protocol = (body or {}).get("protocol")
    if not topic or not isinstance(topic, str):
        return {
            "report": "missing or invalid 'topic' field",
            "key_findings": [], "risks": [], "data_snapshots": {},
            "sources": [], "as_of_ts": int(time.time()),
        }

    snapshots: dict = {}
    sources: list = []
    try:
        if token:
            snapshots["token"] = await _fetch_token(token, chain)
            sources.append("dexscreener")
        snapshots["yields"] = await _fetch_yields(token, chain)
        sources.append("defillama-yields")
        snapshots["tvl"] = await _fetch_tvl(protocol, chain)
        sources.append("defillama-tvl")
        snapshots["stables"] = await _fetch_stables()
        sources.append("defillama-stablecoins")
    except httpx.HTTPError as e:
        return {
            "report": f"upstream data fetch failed: {e}",
            "key_findings": [], "risks": [], "data_snapshots": snapshots,
            "sources": sources, "as_of_ts": int(time.time()),
        }

    prompt = (
        "You are a senior on-chain analyst. Write a structured report on the "
        "topic below, grounded STRICTLY in the JSON snapshots. Respond with a "
        "single JSON object: {\"report\": \"<5-10 sentence narrative report>\", "
        "\"key_findings\": [\"<bullet1>\", ...], "
        "\"risks\": [\"<risk1>\", ...]}. If the snapshots don't support a "
        "claim, omit it.\n\n"
        f"Topic: {topic}\n"
        f"Context: chain={chain}, token={token}, protocol={protocol}\n"
        f"Snapshots: {json.dumps(snapshots, ensure_ascii=False)[:10000]}\n\n"
        "Return only the JSON object."
    )
    try:
        raw = await llm_call(prompt, max_tokens=1200, temperature=0.2)
    except Exception:
        raw = ""
    parsed = _extract_json(raw)
    report = parsed.get("report") or (raw.strip() if raw else "no report")
    return {
        "report": report,
        "key_findings": parsed.get("key_findings") or [],
        "risks": parsed.get("risks") or [],
        "data_snapshots": snapshots,
        "sources": sources,
        "as_of_ts": int(time.time()),
    }
