"""DeFi action planner vertical (inspired by Barvis) \u2014 advisory only, no signing.

Compares lend / swap / stake candidates using free Defillama yields + DexScreener
liquidity data, filters by user risk tolerance, and asks Qwen for a risk review
on the top pick. NEVER returns calldata; never signs.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

import httpx

from . import cache_get, cache_set

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["lend", "swap", "stake"],
                   "description": "The DeFi action to plan."},
        "chain": {"type": "string", "description": "Optional chain filter."},
        "token_in": {"type": "string", "description": "Input token (swap/lend/stake)."},
        "token_out": {"type": "string", "description": "Output token (swap only)."},
        "amount_usd": {"type": "number", "minimum": 0, "description": "Notional in USD."},
        "risk_tolerance": {"type": "string",
                           "enum": ["conservative", "balanced", "aggressive"],
                           "default": "balanced"},
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "candidates", "disclaimer", "as_of_ts"],
    "properties": {
        "action": {"type": "string"},
        "best_candidate_idx": {"type": "integer"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "protocol": {"type": "string"},
                    "chain": {"type": "string"},
                    "symbol": {"type": "string"},
                    "apy": {"type": "number"},
                    "tvl_usd": {"type": "number"},
                    "expected_usd_out": {"type": "number"},
                    "expected_slippage_pct": {"type": "number"},
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                    "pool_url": {"type": "string"},
                },
            },
        },
        "execution_hint": {"type": "string"},
        "risk_review": {"type": "string"},
        "disclaimer": {"type": "string"},
        "as_of_ts": {"type": "integer"},
    },
}

_DISCLAIMER = (
    "Advisory output only. This service never signs transactions and is not "
    "financial advice. Always simulate and verify on-chain before executing."
)

_LLAMA_YIELDS = "https://yields.llama.fi"
_DEX = "https://api.dexscreener.com/latest/dex"
_CACHE_TTL = 120.0


def _risk_flags(pool: dict) -> list:
    flags = []
    tvl = float(pool.get("tvlUsd") or 0)
    apy = float(pool.get("apy") or 0)
    if tvl < 100_000:
        flags.append("micro_tvl<100k")
    elif tvl < 1_000_000:
        flags.append("low_tvl<1m")
    if apy > 200:
        flags.append("apy>200pct_yield_trap")
    elif apy > 50:
        flags.append("apy>50pct_unsustainable")
    if pool.get("ilRisk") == "yes":
        flags.append("impermanent_loss_risk")
    if not pool.get("audits"):
        flags.append("project_unaudited_signal")
    return flags


def _apply_tolerance(pools, tol):
    if tol == "conservative":
        return [p for p in pools if float(p.get("tvlUsd") or 0) >= 5_000_000
                and float(p.get("apy") or 0) <= 30]
    if tol == "balanced":
        return [p for p in pools if float(p.get("tvlUsd") or 0) >= 500_000
                and float(p.get("apy") or 0) <= 100]
    return pools


def _best_idx(action, candidates):
    if not candidates:
        return -1
    safe = [c for c in candidates if not any(
        f.startswith("micro_tvl") or f.endswith("yield_trap") for f in c.get("risk_flags", [])
    )]
    pool = safe or candidates
    if action == "swap":
        ranked = sorted(pool, key=lambda c: c.get("expected_slippage_pct") or 0)
    else:
        ranked = sorted(pool, key=lambda c: -(c.get("apy") or 0))
    return candidates.index(ranked[0])


async def _fetch_yield_pools():
    cached = cache_get("defi:yield-pools", _CACHE_TTL)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(f"{_LLAMA_YIELDS}/pools")
        r.raise_for_status()
        data = r.json()
    pools = data.get("data") or []
    cache_set("defi:yield-pools", pools)
    return pools


def _filter_pools(pools, action, chain, token_in):
    out = pools
    if chain:
        out = [p for p in out if (p.get("chain") or "").lower() == chain.lower()]
    if token_in:
        t = token_in.lower()
        out = [p for p in out if t in (p.get("symbol") or "").lower()]
    if action == "stake":
        out = [p for p in out if "staking" in (p.get("category") or "").lower()
               or "liquid staking" in (p.get("project") or "").lower()] or out
    return out


async def _fetch_token_liquidity(token, chain):
    key = f"defi:tok:{token.lower()}:{(chain or '').lower()}"
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
    if not top:
        result = (0.0, 0.0, "")
    else:
        result = (float((top.get("liquidity") or {}).get("usd") or 0),
                  float(top.get("priceUsd") or 0), top.get("url") or "")
    cache_set(key, result)
    return result


async def _plan_swap(chain, token_in, token_out, amount_usd):
    if not token_in or not token_out:
        return []
    liq_in, _, url_in = await _fetch_token_liquidity(token_in, chain)
    liq_out, _, url_out = await _fetch_token_liquidity(token_out, chain)
    min_liq = 0
    if liq_in > 0 and liq_out > 0:
        min_liq = min(liq_in, liq_out)
    slip = 0.0
    if min_liq > 0 and amount_usd > 0:
        slip = min(50.0, amount_usd / min_liq * 100.0)
    expected_out = max(0.0, amount_usd * (1.0 - slip / 100.0))
    flags = []
    if min_liq and min_liq < 250_000:
        flags.append("low_pair_liquidity<250k")
    if slip > 5:
        flags.append("slippage>5pct")
    return [{
        "label": f"{token_in}->{token_out} (best DexScreener pair)",
        "protocol": "dexscreener-aggregate",
        "chain": chain or "",
        "symbol": f"{token_in}/{token_out}",
        "apy": 0.0,
        "tvl_usd": min_liq,
        "expected_usd_out": round(expected_out, 4),
        "expected_slippage_pct": round(slip, 4),
        "risk_flags": flags,
        "pool_url": url_in or url_out,
    }]


async def handle(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    body = body or {}
    action = body.get("action")
    chain = body.get("chain")
    token_in = body.get("token_in")
    token_out = body.get("token_out")
    amount_usd = float(body.get("amount_usd") or 0)
    tol = body.get("risk_tolerance") or "balanced"
    if action not in {"lend", "swap", "stake"}:
        return {"action": action or "", "candidates": [], "best_candidate_idx": -1,
                "execution_hint": "", "risk_review": "invalid 'action' field (must be lend|swap|stake)",
                "disclaimer": _DISCLAIMER, "as_of_ts": int(time.time())}
    candidates = []
    try:
        if action == "swap":
            candidates = await _plan_swap(chain, token_in, token_out, amount_usd)
        else:
            pools = await _fetch_yield_pools()
            pools = _filter_pools(pools, action, chain, token_in)
            pools = _apply_tolerance(pools, tol)
            pools = sorted(pools, key=lambda p: float(p.get("apy") or 0), reverse=True)[:8]
            for p in pools:
                candidates.append({
                    "label": f"{p.get('project')} / {p.get('symbol')} ({p.get('chain')})",
                    "protocol": p.get("project") or "",
                    "chain": p.get("chain") or "",
                    "symbol": p.get("symbol") or "",
                    "apy": float(p.get("apy") or 0),
                    "tvl_usd": float(p.get("tvlUsd") or 0),
                    "expected_usd_out": 0.0,
                    "expected_slippage_pct": 0.0,
                    "risk_flags": _risk_flags(p),
                    "pool_url": f"https://defillama.com/yields/pool/{p.get('pool')}" if p.get("pool") else "",
                })
    except httpx.HTTPError as e:
        return {"action": action, "candidates": [], "best_candidate_idx": -1,
                "execution_hint": "", "risk_review": f"upstream data fetch failed: {e}",
                "disclaimer": _DISCLAIMER, "as_of_ts": int(time.time())}
    best = _best_idx(action, candidates)
    if action == "swap":
        hint = ("Use a router that supports the listed DEX (e.g. 1inch, Odos, "
                "KyberSwap aggregator) on the indicated chain. Verify the "
                "output token contract address before signing.")
    elif action == "lend":
        hint = ("Open the indicated Defillama pool URL, confirm the protocol's "
                "underlying contract address, check current utilisation & "
                "borrow rate, then deposit on the protocol's official UI.")
    else:
        hint = ("Confirm the staking contract is the protocol's canonical one, "
                "and inspect lock-up / unbonding period before depositing.")
    risk_review = ""
    if best >= 0 and candidates:
        top = candidates[best]
        prompt = (
            "You are a cautious DeFi risk reviewer. In 2-4 short sentences "
            "(<=120 words), review the candidate below and flag concrete risks "
            "(smart-contract, liquidity, depeg, ILoss, unlock, centralization). "
            "Be neutral, not promotional.\n\n"
            f"Action: {action}\nRisk tolerance: {tol}\n"
            f"Candidate: {json.dumps(top, ensure_ascii=False)}\n\n"
            "Return plain text, no preamble."
        )
        try:
            risk_review = await llm_call(prompt, max_tokens=300, temperature=0.3)
        except Exception:
            risk_review = ""
    return {"action": action, "best_candidate_idx": best, "candidates": candidates,
            "execution_hint": hint, "risk_review": risk_review,
            "disclaimer": _DISCLAIMER, "as_of_ts": int(time.time())}


# ===================== PRO TIER: portfolio plan ==========================

INPUT_SCHEMA_PORTFOLIO: dict[str, Any] = {
    "type": "object",
    "required": ["budget_usd"],
    "properties": {
        "budget_usd": {"type": "number", "minimum": 1,
                       "description": "Total notional in USD to allocate."},
        "chain": {"type": "string"},
        "risk_tolerance": {"type": "string",
                           "enum": ["conservative", "balanced", "aggressive"],
                           "default": "balanced"},
        "mix": {
            "type": "object",
            "description": "Optional allocation mix (fractions summing to ~1).",
            "properties": {
                "lend": {"type": "number", "minimum": 0, "maximum": 1},
                "stake": {"type": "number", "minimum": 0, "maximum": 1},
                "swap": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "token_in": {"type": "string", "description": "Default asset for lend/stake/swap legs."},
        "token_out": {"type": "string", "description": "Target asset for the swap leg."},
    },
}

OUTPUT_SCHEMA_PORTFOLIO: dict[str, Any] = {
    "type": "object",
    "required": ["legs", "disclaimer", "as_of_ts"],
    "properties": {
        "budget_usd": {"type": "number"},
        "risk_tolerance": {"type": "string"},
        "mix": {"type": "object"},
        "legs": {"type": "array", "items": {"type": "object"}},
        "blended_apy": {"type": "number"},
        "risk_review": {"type": "string"},
        "disclaimer": {"type": "string"},
        "as_of_ts": {"type": "integer"},
    },
}

_DEFAULT_MIX = {"lend": 0.5, "stake": 0.3, "swap": 0.2}


async def handle_portfolio(
    body: dict,
    llm_call: Callable[..., Awaitable[str]],
) -> dict:
    """Pro tier: produce a multi-leg DeFi portfolio plan in one call."""
    body = body or {}
    budget = float(body.get("budget_usd") or 0)
    if budget <= 0:
        return {"legs": [], "disclaimer": _DISCLAIMER, "as_of_ts": int(time.time()),
                "risk_review": "missing or invalid 'budget_usd'"}
    chain = body.get("chain")
    tol = body.get("risk_tolerance") or "balanced"
    mix = body.get("mix") or _DEFAULT_MIX
    # Normalise mix.
    total = sum(float(mix.get(k) or 0) for k in ("lend", "stake", "swap")) or 1.0
    mix_norm = {k: float(mix.get(k) or 0) / total for k in ("lend", "stake", "swap")}
    token_in = body.get("token_in")
    token_out = body.get("token_out")

    legs = []
    blended_apy_num = 0.0
    blended_apy_den = 0.0

    for action, frac in mix_norm.items():
        if frac <= 0:
            continue
        alloc = round(budget * frac, 2)
        if action == "swap":
            sub_body = {"action": "swap", "chain": chain, "token_in": token_in,
                        "token_out": token_out, "amount_usd": alloc,
                        "risk_tolerance": tol}
        else:
            sub_body = {"action": action, "chain": chain, "token_in": token_in,
                        "amount_usd": alloc, "risk_tolerance": tol}
        # Re-use the single-action planner but skip its inner LLM call by
        # passing a no-op llm_call; we run a single combined Qwen review at
        # the end.
        async def _noop(*a, **kw):
            return ""
        plan = await handle(sub_body, _noop)
        best_idx = plan.get("best_candidate_idx", -1)
        best = None
        if best_idx is not None and best_idx >= 0:
            cands = plan.get("candidates") or []
            if cands:
                best = cands[best_idx]
        legs.append({
            "action": action,
            "allocation_usd": alloc,
            "fraction": round(frac, 3),
            "best": best,
            "candidate_count": len(plan.get("candidates") or []),
            "execution_hint": plan.get("execution_hint", ""),
        })
        if best and action in ("lend", "stake"):
            apy = float(best.get("apy") or 0)
            blended_apy_num += apy * alloc
            blended_apy_den += alloc

    blended_apy = (blended_apy_num / blended_apy_den) if blended_apy_den else 0.0

    risk_review = ""
    if legs:
        prompt = (
            "You are a cautious DeFi portfolio reviewer. Review the multi-leg "
            "allocation below in 4-6 short sentences (<=180 words). Flag "
            "correlation risk, depeg risk, smart-contract risk, sufficient "
            "liquidity, and whether the mix matches the stated risk tolerance.\n\n"
            f"Budget USD: {budget}\nRisk tolerance: {tol}\n"
            f"Mix: {json.dumps(mix_norm)}\n"
            f"Legs: {json.dumps(legs, ensure_ascii=False)[:5000]}\n\n"
            "Return plain text, no preamble."
        )
        try:
            risk_review = await llm_call(prompt, max_tokens=450, temperature=0.3)
        except Exception:
            risk_review = ""

    return {
        "budget_usd": budget,
        "risk_tolerance": tol,
        "mix": mix_norm,
        "legs": legs,
        "blended_apy": round(blended_apy, 4),
        "risk_review": risk_review,
        "disclaimer": _DISCLAIMER,
        "as_of_ts": int(time.time()),
    }
