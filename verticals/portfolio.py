"""Portfolio Loop Primitive vertical — snapshot/plan/quote/execute.

Implements the three-stage agent decision primitive described in
docs/A2A经济/agent-tools-cloud/03_portfolio_primitive_design.md:

  snapshot (cheap)  → plan (medium)  → quote (premium)  → execute (free)

Each stage returns a signed token that the next stage requires, so the
sequence is non-cacheable and the agent is bound to the full loop rather
than a one-off data query.

v0 scope (intentionally minimal):
  - Base mainnet only (Solana coming in P1).
  - 8-token whitelist + native ETH via public Base RPC eth_call balanceOf.
  - DexScreener prices (already used by other verticals).
  - Quote returns a *simulated* tx with a clearly-flagged `simulated=true`;
    real 1inch / Odos / private-mempool routing is P2.
  - In-memory TTL cache, HMAC-SHA256 signing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Awaitable, Callable

import httpx

from . import cache_get, cache_set

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

_SIGNING_KEY = (
    os.getenv("PORTFOLIO_SIGNING_KEY")
    or hashlib.sha256(b"agent-tools-cloud-portfolio-dev").hexdigest()
).encode()

# 60s TTL for snapshot/plan/quote artefacts — tight enough that re-pricing
# is forced on each loop iteration, loose enough for agent latency.
_TTL_SECONDS = 60.0

_BASE_RPC = os.getenv("PORTFOLIO_BASE_RPC", "https://mainnet.base.org")
_DEX = "https://api.dexscreener.com/latest/dex"

# v0 Base whitelist. Symbol, decimals, contract.
_BASE_TOKENS: list[tuple[str, int, str]] = [
    ("USDC",  6,  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    ("USDbC", 6,  "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"),
    ("WETH",  18, "0x4200000000000000000000000000000000000006"),
    ("cbETH", 18, "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"),
    ("cbBTC", 8,  "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"),
    ("AERO",  18, "0x940181a94A35A4569E4529A3CDfB74e38FD98631"),
    ("DAI",   18, "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"),
    ("USDe",  18, "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34"),
]

_DISCLAIMER = (
    "Advisory output only. Signed for tamper-detection, not financial advice. "
    "Quote-stage tx in v0 is SIMULATED (simulated=true); production routing "
    "(1inch/Odos + Flashbots Protect) is on the P2 roadmap."
)


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------

def _sign(payload: dict[str, Any]) -> str:
    """HMAC-SHA256 over canonical JSON. Returns hex digest."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(_SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


# --------------------------------------------------------------------------
# Base RPC helpers
# --------------------------------------------------------------------------

_BALANCE_OF_SELECTOR = "0x70a08231"  # balanceOf(address)


def _eth_call_data(address: str) -> str:
    addr = address.lower().removeprefix("0x").rjust(64, "0")
    return _BALANCE_OF_SELECTOR + addr


async def _rpc_batch(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(_BASE_RPC, json=calls)
        r.raise_for_status()
        out = r.json()
    if isinstance(out, dict):
        out = [out]
    return out


async def _base_balances(wallet: str) -> list[dict[str, Any]]:
    """Native ETH + whitelist ERC20 balances on Base."""
    wallet = wallet.lower()
    if not (wallet.startswith("0x") and len(wallet) == 42):
        raise ValueError("wallet must be a 0x-prefixed EVM address")
    calls: list[dict[str, Any]] = [{
        "jsonrpc": "2.0", "id": 0, "method": "eth_getBalance",
        "params": [wallet, "latest"],
    }]
    for i, (_sym, _dec, contract) in enumerate(_BASE_TOKENS, start=1):
        calls.append({
            "jsonrpc": "2.0", "id": i, "method": "eth_call",
            "params": [
                {"to": contract, "data": _eth_call_data(wallet)},
                "latest",
            ],
        })
    results = await _rpc_batch(calls)
    by_id = {item.get("id"): item for item in results}
    positions: list[dict[str, Any]] = []
    # native
    native_hex = (by_id.get(0) or {}).get("result") or "0x0"
    native_wei = int(native_hex, 16) if native_hex != "0x" else 0
    if native_wei > 0:
        positions.append({
            "symbol": "ETH", "chain": "base", "contract": None,
            "balance_raw": str(native_wei), "decimals": 18,
            "balance": native_wei / 1e18,
        })
    for i, (sym, dec, contract) in enumerate(_BASE_TOKENS, start=1):
        raw_hex = (by_id.get(i) or {}).get("result") or "0x0"
        try:
            raw = int(raw_hex, 16) if raw_hex != "0x" else 0
        except ValueError:
            raw = 0
        if raw == 0:
            continue
        positions.append({
            "symbol": sym, "chain": "base", "contract": contract,
            "balance_raw": str(raw), "decimals": dec,
            "balance": raw / (10 ** dec),
        })
    return positions


async def _price_usd(symbol: str, contract: str | None) -> float:
    """DexScreener best-pair price; cached 120s."""
    key = f"port:px:{(contract or symbol).lower()}"
    cached = cache_get(key, 120.0)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=10.0) as client:
        if contract:
            r = await client.get(f"{_DEX}/tokens/{contract}")
        else:
            r = await client.get(f"{_DEX}/search", params={"q": symbol})
        if r.status_code != 200:
            cache_set(key, 0.0)
            return 0.0
        data = r.json()
    pairs = data.get("pairs") or []
    if not pairs:
        cache_set(key, 0.0)
        return 0.0
    top = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    px = float(top.get("priceUsd") or 0)
    cache_set(key, px)
    return px


# --------------------------------------------------------------------------
# Risk metrics
# --------------------------------------------------------------------------

def _risk_score(positions: list[dict[str, Any]]) -> tuple[float, float]:
    """Returns (risk_score, health_factor).

    risk_score in [0, 1]: 0 = all stable, 1 = all volatile-illiquid.
    health_factor: stable-weighted leverage proxy (>=1 = safe).
    """
    total = sum(p.get("value_usd") or 0 for p in positions)
    if total <= 0:
        return 0.0, 0.0
    stable_syms = {"USDC", "USDbC", "DAI", "USDe"}
    stable_val = sum(p["value_usd"] for p in positions if p["symbol"] in stable_syms)
    btc_eth_val = sum(p["value_usd"] for p in positions if p["symbol"] in {"WETH", "ETH", "cbETH", "cbBTC"})
    alt_val = total - stable_val - btc_eth_val
    risk = (alt_val * 1.0 + btc_eth_val * 0.5 + stable_val * 0.05) / total
    # Simple proxy: more stable -> healthier. Range ~[0.5, 3].
    health = 0.5 + 2.5 * (stable_val / total)
    return round(risk, 4), round(health, 4)


# --------------------------------------------------------------------------
# Stage 1: snapshot
# --------------------------------------------------------------------------

INPUT_SCHEMA_SNAPSHOT: dict[str, Any] = {
    "type": "object",
    "required": ["wallet"],
    "properties": {
        "wallet": {"type": "string", "description": "0x EVM wallet address."},
        "chains": {
            "type": "array",
            "items": {"type": "string", "enum": ["base"]},
            "description": "v0: ['base'] only. Solana in P1.",
        },
    },
}

OUTPUT_SCHEMA_SNAPSHOT: dict[str, Any] = {
    "type": "object",
    "required": ["snapshot_id", "wallet", "chains", "positions",
                 "total_value_usd", "risk_score", "health_factor",
                 "ts", "expires_at", "signature"],
    "properties": {
        "snapshot_id": {"type": "string"},
        "wallet": {"type": "string"},
        "chains": {"type": "array"},
        "positions": {"type": "array"},
        "total_value_usd": {"type": "number"},
        "risk_score": {"type": "number"},
        "health_factor": {"type": "number"},
        "ts": {"type": "integer"},
        "expires_at": {"type": "integer"},
        "signature": {"type": "string"},
    },
}


async def handle_snapshot(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    body = body or {}
    wallet = (body.get("wallet") or "").strip()
    if not wallet:
        return {"error": "missing 'wallet'", "disclaimer": _DISCLAIMER}
    try:
        positions = await _base_balances(wallet)
    except ValueError as e:
        return {"error": str(e), "disclaimer": _DISCLAIMER}
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        return {"error": f"rpc fetch failed: {e}", "disclaimer": _DISCLAIMER}
    # Price each position.
    total = 0.0
    for p in positions:
        px = await _price_usd(p["symbol"], p.get("contract"))
        p["price_usd"] = px
        p["value_usd"] = round(p["balance"] * px, 4)
        total += p["value_usd"]
    risk, health = _risk_score(positions)
    now = int(time.time())
    snap_id = _new_id("snap")
    payload = {
        "snapshot_id": snap_id, "wallet": wallet.lower(),
        "chains": ["base"], "positions": positions,
        "total_value_usd": round(total, 4),
        "risk_score": risk, "health_factor": health,
        "ts": now, "expires_at": now + int(_TTL_SECONDS),
    }
    payload["signature"] = _sign(payload)
    # Cache so /plan can resolve snapshot_id back to positions.
    cache_set(f"port:snap:{snap_id}", payload)
    payload["disclaimer"] = _DISCLAIMER
    return payload


# --------------------------------------------------------------------------
# Stage 2: plan
# --------------------------------------------------------------------------

_GOALS = {"yield_max", "risk_off", "dca_btc", "exit_50pct"}

INPUT_SCHEMA_PLAN: dict[str, Any] = {
    "type": "object",
    "required": ["snapshot_id", "goal"],
    "properties": {
        "snapshot_id": {"type": "string"},
        "goal": {"type": "string", "enum": sorted(_GOALS)},
    },
}

OUTPUT_SCHEMA_PLAN: dict[str, Any] = {
    "type": "object",
    "required": ["plan_id", "snapshot_id", "goal", "actions",
                 "ts", "expires_at", "signature"],
    "properties": {
        "plan_id": {"type": "string"},
        "snapshot_id": {"type": "string"},
        "goal": {"type": "string"},
        "actions": {"type": "array"},
        "total_expected_apy": {"type": "number"},
        "total_risk": {"type": "number"},
        "ts": {"type": "integer"},
        "expires_at": {"type": "integer"},
        "signature": {"type": "string"},
    },
}


def _plan_actions_for_goal(snapshot: dict, goal: str) -> tuple[list[dict], float]:
    positions = snapshot["positions"]
    total = snapshot["total_value_usd"]
    actions: list[dict] = []
    expected_apy = 0.0
    if total <= 0:
        return actions, 0.0
    stable_val = sum(p["value_usd"] for p in positions
                     if p["symbol"] in {"USDC", "USDbC", "DAI", "USDe"})
    eth_val = sum(p["value_usd"] for p in positions
                  if p["symbol"] in {"ETH", "WETH", "cbETH"})
    btc_val = sum(p["value_usd"] for p in positions if p["symbol"] == "cbBTC")
    if goal == "yield_max":
        # Move idle USDC into Aave / Morpho USDC vault; ETH into Aero ETH/USDC LP.
        if stable_val > 1:
            actions.append({
                "step": 1, "type": "deposit", "protocol": "aave-v3",
                "from": "USDC", "amount_usd": round(stable_val * 0.7, 2),
                "expected_apy": 0.045, "chain": "base",
                "rationale": "park stables in audited blue-chip lender",
            })
            actions.append({
                "step": 2, "type": "deposit", "protocol": "morpho",
                "from": "USDC", "amount_usd": round(stable_val * 0.3, 2),
                "expected_apy": 0.072, "chain": "base",
                "rationale": "higher-yield Morpho vault for residual stables",
            })
        if eth_val > 1:
            actions.append({
                "step": len(actions) + 1, "type": "deposit",
                "protocol": "aerodrome", "from": "ETH/USDC LP",
                "amount_usd": round(eth_val * 0.5, 2),
                "expected_apy": 0.18, "chain": "base",
                "rationale": "ve(3,3) LP, accept IL for high APY",
            })
        expected_apy = 0.085
    elif goal == "risk_off":
        # Swap all volatile to USDC, deposit in Aave.
        if eth_val + btc_val > 0:
            actions.append({
                "step": 1, "type": "swap", "from": "ETH/cbBTC",
                "to": "USDC", "amount_usd": round(eth_val + btc_val, 2),
                "chain": "base", "rationale": "exit directional exposure",
            })
        if total > 1:
            actions.append({
                "step": len(actions) + 1, "type": "deposit",
                "protocol": "aave-v3", "from": "USDC",
                "amount_usd": round(total * 0.95, 2),
                "expected_apy": 0.045, "chain": "base",
                "rationale": "principal preservation",
            })
        expected_apy = 0.045
    elif goal == "dca_btc":
        if stable_val > 10:
            slice_usd = max(1.0, round(stable_val * 0.1, 2))
            for i in range(1, 6):
                actions.append({
                    "step": i, "type": "swap", "from": "USDC",
                    "to": "cbBTC", "amount_usd": slice_usd,
                    "chain": "base",
                    "rationale": f"DCA slice {i}/5 (10% per slice)",
                })
        expected_apy = 0.0
    elif goal == "exit_50pct":
        for p in positions:
            if p["value_usd"] < 5:
                continue
            half = round(p["value_usd"] * 0.5, 2)
            if p["symbol"] in {"USDC", "USDbC", "DAI", "USDe"}:
                continue  # already stable
            actions.append({
                "step": len(actions) + 1, "type": "swap",
                "from": p["symbol"], "to": "USDC",
                "amount_usd": half, "chain": "base",
                "rationale": "exit 50% to lock in gains",
            })
        expected_apy = 0.0
    return actions, expected_apy


async def handle_plan(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    body = body or {}
    snap_id = (body.get("snapshot_id") or "").strip()
    goal = (body.get("goal") or "").strip()
    if not snap_id:
        return {"error": "missing 'snapshot_id' (call /v1/portfolio/snapshot first)",
                "disclaimer": _DISCLAIMER}
    if goal not in _GOALS:
        return {"error": f"invalid 'goal'; allowed={sorted(_GOALS)}",
                "disclaimer": _DISCLAIMER}
    snapshot = cache_get(f"port:snap:{snap_id}", _TTL_SECONDS)
    if not snapshot:
        return {"error": "snapshot_id expired or unknown; please re-snapshot",
                "disclaimer": _DISCLAIMER}
    # Enforce risk_off override if health_factor critical.
    if snapshot["health_factor"] < 0.6 and goal == "yield_max":
        goal_effective = "risk_off"
    else:
        goal_effective = goal
    actions, expected_apy = _plan_actions_for_goal(snapshot, goal_effective)
    now = int(time.time())
    plan_id = _new_id("plan")
    payload = {
        "plan_id": plan_id,
        "snapshot_id": snap_id,
        "goal": goal,
        "goal_effective": goal_effective,
        "actions": actions,
        "total_expected_apy": round(expected_apy, 4),
        "total_risk": snapshot["risk_score"],
        "ts": now,
        "expires_at": now + int(_TTL_SECONDS),
    }
    payload["signature"] = _sign(payload)
    cache_set(f"port:plan:{plan_id}", {"plan": payload, "snapshot": snapshot})
    payload["disclaimer"] = _DISCLAIMER
    return payload


# --------------------------------------------------------------------------
# Stage 3: quote
# --------------------------------------------------------------------------

INPUT_SCHEMA_QUOTE: dict[str, Any] = {
    "type": "object",
    "required": ["plan_id", "step"],
    "properties": {
        "plan_id": {"type": "string"},
        "step": {"type": "integer", "minimum": 1},
    },
}

OUTPUT_SCHEMA_QUOTE: dict[str, Any] = {
    "type": "object",
    "required": ["quote_id", "plan_id", "step", "chain",
                 "tx", "valid_until", "attestation"],
    "properties": {
        "quote_id": {"type": "string"},
        "plan_id": {"type": "string"},
        "step": {"type": "integer"},
        "chain": {"type": "string"},
        "tx": {"type": "object"},
        "private_pool": {"type": "string"},
        "simulated": {"type": "boolean"},
        "valid_until": {"type": "integer"},
        "attestation": {"type": "string"},
    },
}

# Placeholder router for v0 simulated quotes — agents must NOT broadcast.
_SIMULATED_ROUTER = "0x0000000000000000000000000000000000000000"


async def handle_quote(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    body = body or {}
    plan_id = (body.get("plan_id") or "").strip()
    step = int(body.get("step") or 0)
    if not plan_id or step <= 0:
        return {"error": "need 'plan_id' and positive 'step'",
                "disclaimer": _DISCLAIMER}
    bundle = cache_get(f"port:plan:{plan_id}", _TTL_SECONDS)
    if not bundle:
        return {"error": "plan_id expired or unknown; please re-plan",
                "disclaimer": _DISCLAIMER}
    plan = bundle["plan"]
    actions = plan["actions"]
    if step > len(actions):
        return {"error": f"step {step} out of range (plan has {len(actions)} actions)",
                "disclaimer": _DISCLAIMER}
    action = actions[step - 1]
    now = int(time.time())
    quote_id = _new_id("quote")
    # v0 simulated tx — clearly flagged; do NOT broadcast.
    tx = {
        "to": _SIMULATED_ROUTER,
        "data": "0x" + hashlib.sha256(
            f"{plan_id}:{step}".encode()).hexdigest()[:64],
        "value": "0x0",
        "gas": "0x3d090",  # 250000
    }
    payload = {
        "quote_id": quote_id,
        "plan_id": plan_id,
        "step": step,
        "action": action,
        "chain": action.get("chain", "base"),
        "tx": tx,
        "private_pool": "flashbots",  # placeholder for P3
        "simulated": True,
        "valid_until": now + int(_TTL_SECONDS),
    }
    payload["attestation"] = _sign(payload)
    cache_set(f"port:quote:{quote_id}", payload)
    payload["disclaimer"] = _DISCLAIMER
    payload["note"] = (
        "v0: tx.to is a zero-address placeholder; this quote is for shape "
        "validation only. Real router calldata (1inch / Odos) + MEV protection "
        "are scheduled for P2/P3."
    )
    return payload


# --------------------------------------------------------------------------
# Stage 4: execute (free confirm)
# --------------------------------------------------------------------------

INPUT_SCHEMA_EXECUTE: dict[str, Any] = {
    "type": "object",
    "required": ["quote_id", "tx_hash"],
    "properties": {
        "quote_id": {"type": "string"},
        "tx_hash": {"type": "string"},
    },
}

OUTPUT_SCHEMA_EXECUTE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quote_id": {"type": "string"},
        "tx_hash": {"type": "string"},
        "recorded": {"type": "boolean"},
        "warning": {"type": "string"},
    },
}


async def handle_execute(body: dict, llm_call: Callable[..., Awaitable[str]]) -> dict:
    body = body or {}
    quote_id = (body.get("quote_id") or "").strip()
    tx_hash = (body.get("tx_hash") or "").strip()
    if not quote_id or not tx_hash:
        return {"recorded": False, "error": "need 'quote_id' and 'tx_hash'",
                "disclaimer": _DISCLAIMER}
    quote = cache_get(f"port:quote:{quote_id}", 24 * 3600.0)
    warning = ""
    if not quote:
        warning = "quote not found in cache (TTL 24h); execution recorded but not cross-checked"
    elif quote.get("simulated"):
        warning = "quote was SIMULATED (v0); if you broadcast this tx you sent to the zero address — funds lost"
    return {
        "quote_id": quote_id,
        "tx_hash": tx_hash,
        "recorded": True,
        "warning": warning,
        "disclaimer": _DISCLAIMER,
    }
