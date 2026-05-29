"""
mcpserver — backend for agent-tools.cloud.

Serves the open x402 service directory, the free MCP discovery server at
/mcp-discovery, /.well-known descriptors, and a read-only /v1/models proxy.
The paid x402 stack (Qwen relay + verticals + relay.agent-tools.cloud host
split) was retired 2026-05-25 after 30 days with 0 settlements; see git
history for the previous implementation.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from mcp.server.fastmcp import FastMCP

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

from directory import db as directory_db
from directory.routes import router as directory_router
from directory.mcp_app import discover_mcp, wrap_with_client_capture
from metrics import PrometheusMiddleware, metrics_endpoint

from verticals import signals as signals_vertical
from verticals import onchain as onchain_vertical
from verticals import defi as defi_vertical
from verticals import portfolio as portfolio_vertical

load_dotenv()

# --- config ---------------------------------------------------------------

UPSTREAM_BASE_URL = os.environ["UPSTREAM_BASE_URL"].rstrip("/")
UPSTREAM_API_KEY = os.environ["UPSTREAM_API_KEY"]
X402_PAY_TO = os.environ["X402_PAY_TO"]
X402_NETWORK = os.getenv("X402_NETWORK", "eip155:8453")  # Base mainnet
X402_PRICE_USD = os.getenv("X402_PRICE_USD", "0.001")
X402_FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
ALLOWED_MODELS = {
    m.strip()
    for m in os.getenv("ALLOWED_MODELS", "Qwen/Qwen3.6-35B-A3B").split(",")
    if m.strip()
}
DEFAULT_MODEL = next(iter(ALLOWED_MODELS))

# Per-call price for the new verticals. Each is gated independently by the
# x402 middleware so an agent only pays for the endpoint it actually
# invokes; see `_routes` below.
X402_SIGNAL_PRICE_USD = os.getenv("X402_SIGNAL_PRICE_USD", "0.01")
X402_ONCHAIN_PRICE_USD = os.getenv("X402_ONCHAIN_PRICE_USD", "0.02")
X402_DEFI_PRICE_USD = os.getenv("X402_DEFI_PRICE_USD", "0.05")
# Pro tiers (bulk / report / portfolio) — higher unit price for the
# heavier work and to lift the bazaar avg-USDC.
X402_SIGNAL_BULK_PRICE_USD = os.getenv("X402_SIGNAL_BULK_PRICE_USD", "0.05")
X402_ONCHAIN_REPORT_PRICE_USD = os.getenv("X402_ONCHAIN_REPORT_PRICE_USD", "0.20")
X402_DEFI_PORTFOLIO_PRICE_USD = os.getenv("X402_DEFI_PORTFOLIO_PRICE_USD", "0.50")
# Portfolio Loop Primitive (snapshot -> plan -> quote -> execute)
X402_PORTFOLIO_SNAPSHOT_PRICE_USD = os.getenv("X402_PORTFOLIO_SNAPSHOT_PRICE_USD", "0.005")
X402_PORTFOLIO_PLAN_PRICE_USD = os.getenv("X402_PORTFOLIO_PLAN_PRICE_USD", "0.05")
X402_PORTFOLIO_QUOTE_PRICE_USD = os.getenv("X402_PORTFOLIO_QUOTE_PRICE_USD", "0.20")

# --- upstream HTTP client -------------------------------------------------

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    if _http is None:  # pragma: no cover
        raise RuntimeError("HTTP client not initialised yet")
    return _http


async def _upstream_chat(body: dict[str, Any]) -> dict[str, Any]:
    r = await _client().post("/v1/chat/completions", json=body)
    r.raise_for_status()
    return r.json()


async def _llm_short(
    prompt: str,
    *,
    max_tokens: int = 256,
    temperature: float = 0.4,
) -> str:
    """Vertical-internal one-shot Qwen call against the Tianshu upstream.

    NEVER re-enters /v1/chat/completions; that route is itself gated by
    x402 and would double-charge the agent on every vertical request.
    Qwen3.6 thinking models may put final text under message.reasoning,
    so fold both content+reasoning into a single string.
    """
    body = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    result = await _upstream_chat(body)
    choices = result.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning") or ""
    return (content or reasoning).strip()


# --- MCP layer ------------------------------------------------------------

mcp_app = FastMCP("mcpserver", instructions=(
    "Frontier 35B-A3B chat at $" + X402_PRICE_USD + " per call — the cheapest credible "
    "inference endpoint on the x402 bazaar. One tool: qwen36_chat. Pay-per-call USDC on Base, "
    "no signup, no API key, no rate limits per identity. Settles in one block."
))


@mcp_app.tool(
    name="qwen36_chat",
    description=(
        "Frontier-grade chat completion against Qwen3.6-35B-A3B (open-weight MoE, 35B total / 3B active). "
        "$" + X402_PRICE_USD + " USDC per call on Base — orders of magnitude cheaper than hosted Claude/GPT, "
        "settled atomically per request with no monthly minimum. Returns assistant text."
    ),
)
async def qwen36_chat(
    messages: list[dict[str, str]],
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.95,
) -> str:
    """OpenAI-style messages → assistant string."""
    body = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    result = await _upstream_chat(body)
    return result["choices"][0]["message"]["content"]


# --- FastAPI app ----------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the directory site DB exists. Safe to run repeatedly.
    try:
        directory_db.init_db()
    except Exception:
        import logging
        logging.getLogger("mcpserver").exception("directory db init failed")
    global _http
    _http = httpx.AsyncClient(
        base_url=UPSTREAM_BASE_URL,
        headers={"Authorization": f"Bearer {UPSTREAM_API_KEY}"},
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
    )
    # FastMCP streamable-http needs its own session-manager lifespan.
    # Only the free directory-discovery MCP at /mcp-discovery is mounted now
    # (the paid /mcp mount was retired 2026-05-25).
    async with discover_mcp.session_manager.run():
        try:
            yield
        finally:
            await _http.aclose()


app = FastAPI(
    title="mcpserver", version="0.2.0", lifespan=lifespan, openapi_url=None
)

# Paid x402 endpoints retired 2026-05-25 (0 conversions in 30d). The paid
# MCP mount at /mcp and the REST verticals below are disabled. Only the free
# directory-discovery MCP remains.
# app.mount("/mcp", mcp_app.streamable_http_app())  # retired
# Free directory-discovery MCP (search/get/list_categories/stats) — ungated.
app.mount("/mcp-discovery", wrap_with_client_capture(discover_mcp.streamable_http_app()))

# ---------------------------------------------------------------------------
# Bazaar discovery extension — Coinbase Bazaar / AgentKit clients read this to
# auto-construct request bodies for our paid endpoints. Without it, AI agents
# receiving a 402 challenge don't know what JSON to POST to redeem payment.
# Spec: x402.extensions.bazaar.types.BodyDiscoveryInfo
# ---------------------------------------------------------------------------
def _bazaar(method: str, body_example: dict, output_example: dict, body_props: dict, required: list[str]) -> dict:
    return {
        "bazaar": {
            "info": {
                "input": {
                    "type": "http",
                    "method": method,
                    "bodyType": "json",
                    "body": body_example,
                },
                "output": {"type": "json", "example": output_example},
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "const": "http"},
                            "method": {"type": "string", "enum": [method]},
                            "bodyType": {"type": "string", "enum": ["json"]},
                            "body": {
                                "type": "object",
                                "properties": body_props,
                                "required": required,
                            },
                        },
                        "required": ["type", "method", "bodyType", "body"],
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "example": {"type": "object"},
                        },
                        "required": ["type"],
                    },
                },
                "required": ["input"],
            },
        }
    }


# x402 v2 middleware — paid routes retired 2026-05-25. Middleware kept
# registered with empty routes so any leftover crawler probe falls through
# to a normal 404 rather than 402.
_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=X402_FACILITATOR_URL))
_x402_server = x402ResourceServer(_facilitator)
_x402_server.register(X402_NETWORK, ExactEvmServerScheme())

_routes: dict = {}
_RETIRED_ROUTES_FOR_REFERENCE_ONLY = {
    "POST /v1/chat/completions": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Qwen3.6-35B-A3B chat — frontier 35B MoE at flat ${X402_PRICE_USD} USDC / call. OpenAI wire format, no key, no signup.",
        extensions=_bazaar(
            method="POST",
            body_example={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "Explain bonding curves in 2 sentences."}], "temperature": 0.7, "max_tokens": 256},
            output_example={"id": "chatcmpl-xxx", "object": "chat.completion", "model": "qwen3.6-35b-a3b", "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 48, "total_tokens": 60}},
            body_props={"model": {"type": "string", "description": "Model id, e.g. qwen3.6-35b-a3b"}, "messages": {"type": "array", "items": {"type": "object"}, "description": "OpenAI chat messages"}, "temperature": {"type": "number", "minimum": 0, "maximum": 2}, "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192}, "top_p": {"type": "number"}, "stream": {"type": "boolean"}},
            required=["messages"],
        ),    ),
    # Whole MCP transport (initialize/tools-list/tools-call all share the
    # same POST endpoint). One payment buys one streamable-http roundtrip.
    "POST /mcp": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"MCP streamable-http transport for Qwen3.6-35B-A3B — same model, agent-native protocol, ${X402_PRICE_USD} USDC / call.",
    ),
    # --- Vertical 1: token momentum signal -------------------------------
    "POST /v1/signal/token": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_SIGNAL_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Live token momentum signal — buy/hold/sell + score, confidence and wash-trade penalty. DexScreener + Qwen, ${X402_SIGNAL_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"chain": "base", "token": "0x4200000000000000000000000000000000000006"},
            output_example={"token": "0x4200...0006", "chain": "base", "score": 72, "action": "hold", "confidence": 0.78, "rationale": "Momentum positive but wash-trade penalty applied."},
            body_props={"chain": {"type": "string", "enum": ["base", "ethereum", "solana", "arbitrum"]}, "token": {"type": "string", "description": "Token contract address or symbol"}},
            required=["chain", "token"],
        ),    ),
    # --- Vertical 2: on-chain analytics NL Q&A ---------------------------
    "POST /v1/onchain/ask": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_ONCHAIN_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"On-chain Q&A — free-form questions about tokens, yields, stablecoins, TVL. Grounded in live Defillama+DexScreener data. ${X402_ONCHAIN_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"question": "What is the current TVL of Aave on Base and which stablecoins back it?"},
            output_example={"answer": "Aave on Base has $X TVL backed primarily by USDC...", "sources": ["defillama", "dexscreener"]},
            body_props={"question": {"type": "string", "minLength": 4, "maxLength": 500}},
            required=["question"],
        ),    ),
    # --- Vertical 3: DeFi action planner (advisory only, no signing) -----
    "POST /v1/defi/plan": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_DEFI_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"DeFi action planner — best lend/swap/stake route by risk tolerance, with Qwen risk review. Advisory only, never signs. ${X402_DEFI_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"budget_usd": 500, "risk": "balanced", "chain": "base"},
            output_example={"plan": [{"action": "lend", "protocol": "Aave", "asset": "USDC", "alloc_usd": 300, "apy": 4.5}], "blended_apy": 4.2, "risk_review": "Low protocol risk; smart-contract risk concentrated in Aave..."},
            body_props={"budget_usd": {"type": "number", "minimum": 10}, "risk": {"type": "string", "enum": ["conservative", "balanced", "aggressive"]}, "chain": {"type": "string"}},
            required=["budget_usd", "risk"],
        ),    ),
    # --- Pro tier: bulk signal (up to 10 tokens / call) ------------------
    "POST /v1/signal/bulk": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_SIGNAL_BULK_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Bulk momentum scan — score up to 10 tokens in one shot, with portfolio rollup (top pick, buy/hold/sell counts). ${X402_SIGNAL_BULK_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"chain": "base", "tokens": ["0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"]},
            output_example={"results": [{"token": "0x4200...0006", "score": 72, "action": "hold"}, {"token": "0x8335...2913", "score": 88, "action": "buy"}], "rollup": {"top_pick": "0x8335...2913", "buy": 1, "hold": 1, "sell": 0}},
            body_props={"chain": {"type": "string", "enum": ["base", "ethereum", "solana", "arbitrum"]}, "tokens": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10}},
            required=["chain", "tokens"],
        ),    ),
    # --- Pro tier: multi-source on-chain report --------------------------
    "POST /v1/onchain/report": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_ONCHAIN_REPORT_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Synthesised on-chain analyst report — token+yields+TVL+stables fused into a 5-10 sentence brief with key findings & risks. ${X402_ONCHAIN_REPORT_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"token": "0x4200000000000000000000000000000000000006", "chain": "base"},
            output_example={"report": "WETH on Base shows steady inflow... key findings: 1) ... risks: ...", "findings": ["..."], "risks": ["..."]},
            body_props={"token": {"type": "string"}, "chain": {"type": "string"}},
            required=["token"],
        ),    ),
    # --- Pro tier: multi-leg DeFi portfolio plan -------------------------
    "POST /v1/defi/portfolio": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_DEFI_PORTFOLIO_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Multi-leg DeFi portfolio — allocates a USD budget across lend/stake/swap, returns blended APY + Qwen portfolio review. Advisory only, never signs. ${X402_DEFI_PORTFOLIO_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"budget_usd": 5000, "risk": "balanced", "chain": "base", "legs": 3},
            output_example={"allocations": [{"action": "lend", "protocol": "Aave", "asset": "USDC", "alloc_usd": 2500, "apy": 4.5}, {"action": "stake", "protocol": "Lido", "asset": "ETH", "alloc_usd": 1500, "apy": 3.1}, {"action": "swap", "from": "USDC", "to": "cbBTC", "alloc_usd": 1000}], "blended_apy": 3.4, "review": "Diversified across yield + directional exposure..."},
            body_props={"budget_usd": {"type": "number", "minimum": 100}, "risk": {"type": "string", "enum": ["conservative", "balanced", "aggressive"]}, "chain": {"type": "string"}, "legs": {"type": "integer", "minimum": 1, "maximum": 5}},
            required=["budget_usd", "risk"],
        ),    ),
    # --- Portfolio primitive: snapshot ($0.005) -----------------------
    "POST /v1/portfolio/snapshot": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PORTFOLIO_SNAPSHOT_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Portfolio snapshot — signed cross-chain holdings + risk score + health factor. Required as input to /v1/portfolio/plan and /v1/portfolio/quote. ${X402_PORTFOLIO_SNAPSHOT_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"wallet": "0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF", "chains": ["base"]},
            output_example={"snapshot_id": "snap_x7k2...", "positions": [{"symbol": "USDC", "balance": 1000, "value_usd": 1000}], "total_value_usd": 1000, "risk_score": 0.05, "health_factor": 2.95, "expires_at": 1779545260, "signature": "0xabc..."},
            body_props={"wallet": {"type": "string"}, "chains": {"type": "array", "items": {"type": "string"}}},
            required=["wallet"],
        ),    ),
    # --- Portfolio primitive: plan ($0.05) ----------------------------
    "POST /v1/portfolio/plan": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PORTFOLIO_PLAN_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Portfolio plan — given a snapshot_id and a strategy goal (yield_max / risk_off / dca_btc / exit_50pct), returns a signed action list with health-budget enforcement. ${X402_PORTFOLIO_PLAN_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"snapshot_id": "snap_x7k2...", "goal": "yield_max"},
            output_example={"plan_id": "plan_q3m9...", "actions": [{"step": 1, "type": "deposit", "protocol": "aave-v3", "amount_usd": 700, "expected_apy": 0.045}], "total_expected_apy": 0.085, "signature": "0xdef..."},
            body_props={"snapshot_id": {"type": "string"}, "goal": {"type": "string", "enum": ["yield_max", "risk_off", "dca_btc", "exit_50pct"]}},
            required=["snapshot_id", "goal"],
        ),    ),
    # --- Portfolio primitive: quote ($0.20) ---------------------------
    "POST /v1/portfolio/quote": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PORTFOLIO_QUOTE_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Portfolio quote — given a plan_id and step number, returns a signed tx with attestation. v0 returns simulated=true; production routing (1inch/Odos + Flashbots) on roadmap. ${X402_PORTFOLIO_QUOTE_PRICE_USD} USDC / call.",
        extensions=_bazaar(
            method="POST",
            body_example={"plan_id": "plan_q3m9...", "step": 1},
            output_example={"quote_id": "quote_a1b2...", "chain": "base", "tx": {"to": "0x...", "data": "0x...", "value": "0x0"}, "simulated": True, "valid_until": 1779545320, "attestation": "0x..."},
            body_props={"plan_id": {"type": "string"}, "step": {"type": "integer", "minimum": 1}},
            required=["plan_id", "step"],
        ),    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=_routes, server=_x402_server)
# Outermost: instrument every request (incl. 402 challenges) for Prometheus.
app.add_middleware(PrometheusMiddleware)


# --- free endpoints -------------------------------------------------------


@app.get("/healthz")
@app.get("/health")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


_HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-tools.cloud — x402 directory + MCP discovery</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="agent-tools.cloud is a free directory and MCP discovery server for x402 paid endpoints. The previously hosted paid Qwen relay and verticals were retired on 2026-05-25.">
<meta name="robots" content="noindex, nofollow">
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 720px; margin: 3rem auto; padding: 0 1.2rem; }
h1 { margin: 0 0 .2em; font-size: 1.5rem; }
h2 { margin-top: 1.8rem; font-size: 1.05rem; border-bottom: 1px solid #8884; padding-bottom: .25em; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }
pre { background: #8881; padding: .8em 1em; border-radius: 6px; overflow-x: auto; }
a { color: #2c7be5; }
.muted { color: #888; font-size: 13px; }
.notice { padding: .8em 1em; border-left: 3px solid #c4732d; background: #c4732d18; border-radius: 4px; }
</style>
</head>
<body>
<h1>agent-tools.cloud</h1>
<p class="notice">
  <b>Paid x402 relay retired on 2026-05-25.</b>
  The previously hosted Qwen3.6-35B-A3B chat ($0.001/call) and the four verticals
  (token signal, on-chain Q&amp;A, DeFi planner, multi-leg portfolio) are no longer served.
  All paid endpoints now return <code>404</code>. This host is directory + MCP discovery only.
</p>

<h2>What is still here (free)</h2>
<ul>
  <li><code>GET /api/v1/search?q=&amp;category=&amp;chain=</code> — directory search across 2300+ indexed x402 services</li>
  <li><code>GET /api/v1/services/{slug}</code>, <code>/api/v1/categories</code>, <code>/api/v1/stats</code></li>
  <li><code>POST /mcp-discovery/</code> — MCP streamable-http with tools <code>search</code> / <code>get</code> / <code>list_categories</code> / <code>stats</code> / <code>register</code></li>
  <li><code>GET /.well-known/agent-tools.json</code> — agents.json manifest</li>
  <li><code>GET /.well-known/x402</code>, <code>/.well-known/mcp.json</code>, <code>/v1/models</code>, <code>/healthz</code></li>
</ul>

<h2>MCP client config</h2>
<pre>{
  "mcpServers": {
    "agent-tools": {
      "transport": "streamable-http",
      "url": "https://agent-tools.cloud/mcp-discovery/"
    }
  }
}</pre>

<h2>Source</h2>
<p>
  <a href="https://github.com/JoursBleu/mcpserver" target="_blank" rel="noopener">github.com/JoursBleu/mcpserver</a>
</p>
<p class="muted">Last paid call: 2026-05-23. 30-day window showed 0 settled payments &mdash; retiring the paid stack to keep the directory layer clean.</p>
</body>
</html>
"""


@app.get("/.well-known/x402")
async def well_known_x402(request: Request) -> dict[str, Any]:
    host = (request.headers.get("host") or "").split(":", 1)[0].lower() or "agent-tools.cloud"
    description = (
        "Free MCP discovery server for x402 paid services across the ecosystem. "
        "Search 2000+ x402 endpoints, get call details, browse categories. "
        "The paid relay previously hosted here has been retired; this host is "
        "now directory + discovery only."
    )
    return {
        "name": host,
        "description": description,
        "version": "0.4",
        "endpoints": [
            {"path": "/mcp-discovery", "method": "POST", "kind": "mcp-streamable-http", "gated": False},
            {"path": "/api/v1/search", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/services/{slug}", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/categories", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/api/v1/stats", "method": "GET", "kind": "rest-json", "gated": False, "category": "directory"},
            {"path": "/.well-known/agent-tools.json", "method": "GET", "kind": "agents.json", "gated": False},
            {"path": "/v1/models", "method": "GET", "kind": "info", "gated": False},
            {"path": "/healthz", "method": "GET", "kind": "info", "gated": False},
        ],
        "models": sorted(ALLOWED_MODELS),
        "source": "https://github.com/JoursBleu/mcpserver",
    }


@app.get("/.well-known/mcp.json", tags=["discovery"])
async def well_known_mcp(request: Request) -> dict[str, Any]:
    """Lightweight MCP discovery doc — points clients to stdio + streamable-http."""
    host = (request.headers.get("host") or "agent-tools.cloud").split(":", 1)[0].lower()
    scheme = "https" if request.url.scheme == "https" else "http"
    base = f"{scheme}://{host}"
    return {
        "name": "agent-tools",
        "description": (
            "Free MCP discovery server for x402 paid services. "
            "Search the x402 ecosystem, get call details, browse categories. "
            "Same tools also available as a stdio MCP via `uvx agent-tools-mcp`."
        ),
        "version": "0.1.0",
        "homepage": "https://agent-tools.cloud",
        "repository": "https://github.com/JoursBleu/agent-tools-mcp",
        "license": "Apache-2.0",
        "transports": {
            "stdio": {
                "command": "uvx",
                "args": ["agent-tools-mcp"],
                "package": "agent-tools-mcp",
                "registry": "pypi",
            },
            "streamable_http": {
                "url": f"{base}/mcp-discovery",
            },
        },
        "tools": [
            {"name": "search", "description": "Find services by natural-language intent (with optional max price, category, chain)."},
            {"name": "get", "description": "Get full call details for a service by slug."},
            {"name": "list_categories", "description": "List all directory categories."},
            {"name": "stats", "description": "Directory size, healthy count, source breakdown."},
        ],
    }



@app.get("/v1/models")
async def models() -> dict[str, Any]:
    r = await _client().get("/v1/models")
    upstream = r.json()
    data = [m for m in upstream.get("data", []) if m.get("id") in ALLOWED_MODELS]
    return {"object": "list", "data": data}


# --- paid endpoints retired 2026-05-25 ------------------------------------
#
# All previously-gated paid routes (POST /v1/chat/completions, /v1/signal/*,
# /v1/onchain/*, /v1/defi/*, /v1/portfolio/*, paid MCP at /mcp) have been
# removed after 30 days with 0 successful x402 settlements. They now 404.
# History preserved in git; bring back individually with `git show` if
# we revisit the paid stack. Free MCP discovery at /mcp-discovery and the
# directory API under /api/v1/ are unaffected.


# --- OpenAPI customization for x402scan discovery ------------------------
#
# x402scan resolves payable services via /openapi.json. To be reliably
# discovered & invocable, each paid operation needs:
#   * x-payment-info with price (mode/currency/amount) + protocols (x402)
#   * responses.402 declaration
#   * requestBody schema (input contract)
# We also publish top-level info.x-guidance so agents can self-route.
# Spec: https://www.x402scan.com/discovery/spec

from fastapi.openapi.utils import get_openapi  # noqa: E402


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


DIRECTORY_PATH_PREFIXES = (
    "/api/v1/",
    "/categories",
    "/submit",
    "/about",
    "/services/",
    "/.well-known/agent-tools.json",
)


def _build_openapi() -> dict[str, Any]:
    cache_key = "full"
    if cache_key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[cache_key]

    title = "agent-tools.cloud — x402 directory + agent-native crypto stack"
    description = (
        "Two-in-one x402 service for autonomous agents. "
        "(1) The largest open directory of x402 endpoints in the ecosystem — paid APIs "
        "across inference, payments, data and DeFi, browsable at https://agent-tools.cloud "
        "with a free JSON API (/api/v1/search, /api/v1/services/{slug}, /api/v1/categories, "
        "/api/v1/stats) and an agents.json manifest at /.well-known/agent-tools.json. "
        "(2) An agent-native paid stack on the same host: frontier Qwen3.6-35B-A3B chat at "
        "$0.001/call (OpenAI-REST + MCP) plus four data-grounded crypto verticals — token "
        "momentum signal/bulk scan, on-chain Q&A/analyst report, DeFi planner/portfolio — "
        "from $0.01/call. USDC on Base, no signup, no API key, no rate limits per identity."
    )
    guidance = (
        "Two capabilities on one host. (A) Directory API (free, no "
        "x402 challenge): GET /api/v1/search?q=&category=&chain= "
        "returns indexed x402 services; GET /api/v1/services/{slug} "
        "returns a single service; GET /api/v1/categories and "
        "/api/v1/stats expose facets; GET "
        "/.well-known/agent-tools.json is the discoverable agents.json "
        "manifest. (B) Paid inference relay: POST /v1/chat/completions "
        "is an OpenAI-compatible chat completions endpoint gated by "
        "x402. Send a JSON body with 'model' (Qwen/Qwen3.6-35B-A3B) "
        "and 'messages' (array of {role, content}). First request "
        "returns HTTP 402 with paymentRequirements; resubmit with an "
        "X-PAYMENT header (EIP-3009 USDC TransferWithAuthorization on "
        "Base mainnet, chain 8453, asset "
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) to receive the "
        "model response. Flat $0.001 USDC per call. The MCP "
        "streamable-http transport at POST /mcp accepts the same "
        "payment and exposes a 'qwen36_chat' tool. Facilitator: "
        "facilitator.fluxapay.xyz (non-custodial, gas covered). "
        "PayTo: 0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF."
    )

    schema = get_openapi(
        title=title,
        version="0.3.0",
        description=description,
        routes=app.routes,
    )

    schema["info"]["x-guidance"] = guidance

    chat_input_schema = {
        "type": "object",
        "required": ["model", "messages"],
        "properties": {
            "model": {
                "type": "string",
                "enum": sorted(ALLOWED_MODELS),
                "description": (
                    "Model identifier. Currently only Qwen/Qwen3.6-35B-A3B."
                ),
            },
            "messages": {
                "type": "array",
                "minItems": 1,
                "description": "OpenAI-style chat messages.",
                "items": {
                    "type": "object",
                    "required": ["role", "content"],
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": ["system", "user", "assistant", "tool"],
                        },
                        "content": {"type": "string"},
                    },
                },
            },
            "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            "top_p": {"type": "number", "minimum": 0, "maximum": 1},
            "max_tokens": {"type": "integer", "minimum": 1},
            "stream": {"type": "boolean", "default": False},
        },
    }

    chat_output_schema = {
        "type": "object",
        "description": "OpenAI-compatible chat.completion response.",
        "properties": {
            "id": {"type": "string"},
            "object": {"type": "string"},
            "created": {"type": "integer"},
            "model": {"type": "string"},
            "choices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "message": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                        "finish_reason": {"type": "string"},
                    },
                },
            },
            "usage": {
                "type": "object",
                "properties": {
                    "prompt_tokens": {"type": "integer"},
                    "completion_tokens": {"type": "integer"},
                    "total_tokens": {"type": "integer"},
                },
            },
        },
        "required": ["id", "object", "choices"],
    }

    chat_op = schema.get("paths", {}).get("/v1/chat/completions", {}).get("post")
    if chat_op is not None:
        chat_op["summary"] = "Frontier Qwen3.6-35B-A3B chat at $0.001/call (x402)"
        chat_op["description"] = (
            "OpenAI-compatible chat completions against Qwen3.6-35B-A3B — open-weight 35B MoE "
            "(3B active), strong reasoning, drop-in for any OpenAI SDK. $0.001 USDC per call, "
            "orders of magnitude cheaper than hosted Claude/GPT, with no monthly minimum and "
            "no per-identity rate limit. First call returns 402 with paymentRequirements; "
            "resubmit with X-PAYMENT header to get the completion."
        )
        chat_op["tags"] = ["inference"]
        chat_op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": chat_input_schema}},
        }
        chat_op["responses"] = {
            "200": {
                "description": "Successful inference response.",
                "content": {"application/json": {"schema": chat_output_schema}},
            },
            "402": {
                "description": (
                    "Payment Required. Body contains x402 paymentRequirements; "
                    "resubmit with X-PAYMENT header."
                ),
            },
            "400": {"description": "Bad request (e.g. model not allowed)."},
        }
        chat_op["x-payment-info"] = {
            "price": {
                "mode": "fixed",
                "currency": "USD",
                "amount": f"{float(X402_PRICE_USD):.6f}",
            },
            "protocols": [{"x402": {}}],
        }
        # agentcash/discovery's "bazaar" validator wants input/output schemas
        # mirrored under x-bazaar.schema as well (in addition to OpenAPI standard
        # requestBody / responses). Without this, registration emits
        # SCHEMA_INPUT_MISSING / SCHEMA_OUTPUT_MISSING errors.
        chat_op["x-bazaar"] = {
            "schema": {
                "properties": {
                    "input": chat_input_schema,
                    "output": chat_output_schema,
                },
            },
        }

    # --- vertical endpoints (signal / onchain / defi) --------------------
    _vertical_specs = [
        (
            "/v1/signal/token",
            X402_SIGNAL_PRICE_USD,
            "Token momentum signal — buy/hold/sell + score at $0.01/call",
            (
                "Live directional signal (buy/hold/sell) with score and confidence for any "
                "token across 100+ chains. Penalises wash trading, pump-and-dump spikes and "
                "low-TVL traps so the score reflects real momentum. Grounded in DexScreener "
                "(price, volume, txns, liquidity) + a one-sentence Qwen rationale."
            ),
            ["signal"],
            signals_vertical.INPUT_SCHEMA,
            signals_vertical.OUTPUT_SCHEMA,
        ),
        (
            "/v1/onchain/ask",
            X402_ONCHAIN_PRICE_USD,
            "On-chain NL Q&A — tokens, yields, stables, TVL at $0.02/call",
            (
                "Ask any free-form on-chain question and get a Qwen answer grounded strictly "
                "in live Defillama + DexScreener data, plus structured fields the agent can "
                "consume directly (data snapshot, sources, as_of_ts, confidence). No hallucinated "
                "numbers — if the data isn't there, the response says so."
            ),
            ["onchain-analytics"],
            onchain_vertical.INPUT_SCHEMA,
            onchain_vertical.OUTPUT_SCHEMA,
        ),
        (
            "/v1/defi/plan",
            X402_DEFI_PRICE_USD,
            "DeFi action planner — best lend/swap/stake at $0.05/call",
            (
                "Picks the best lending pool, swap route or stake/restake candidate across "
                "chains and filters by risk tolerance (conservative/balanced/aggressive). "
                "Live Defillama + DexScreener data, plus a Qwen risk review flagging APY traps, "
                "micro-TVL, impermanent-loss and unaudited-protocol signals. Advisory only — "
                "never holds keys, never signs."
            ),
            ["defi-planner"],
            defi_vertical.INPUT_SCHEMA,
            defi_vertical.OUTPUT_SCHEMA,
        ),
        (
            "/v1/signal/bulk",
            X402_SIGNAL_BULK_PRICE_USD,
            "Bulk momentum scan — up to 10 tokens / call at $0.05 (pro)",
            (
                "Score a whole watchlist in one payment. Up to 10 tokens per call with the "
                "same wash-trade-aware heuristic as /v1/signal/token, plus a portfolio rollup "
                "(buy/hold/sell counts, top pick, top score). Half the cost of 10 individual "
                "signals — built for agents that rotate fast."
            ),
            ["signal", "pro"],
            signals_vertical.INPUT_SCHEMA_BULK,
            signals_vertical.OUTPUT_SCHEMA_BULK,
        ),
        (
            "/v1/onchain/report",
            X402_ONCHAIN_REPORT_PRICE_USD,
            "On-chain analyst report — 5-10 sentence brief at $0.20 (pro)",
            (
                "An analyst brief in one call. Pulls token, yield, TVL and stablecoin snapshots "
                "in parallel and asks Qwen for a 5-10 sentence synthesis with explicit "
                "key_findings[] and risks[]. Strictly grounded in the fetched data — no model "
                "hallucinated numbers, full data_snapshots + sources returned for audit."
            ),
            ["onchain-analytics", "pro"],
            onchain_vertical.INPUT_SCHEMA_REPORT,
            onchain_vertical.OUTPUT_SCHEMA_REPORT,
        ),
        (
            "/v1/defi/portfolio",
            X402_DEFI_PORTFOLIO_PRICE_USD,
            "Multi-leg DeFi portfolio — blended APY at $0.50/call (pro)",
            (
                "Allocates a USD budget across lend / stake / swap legs according to an "
                "explicit mix and risk tolerance (conservative/balanced/aggressive), ranks "
                "the best candidate per leg, returns blended portfolio APY and a single Qwen "
                "portfolio risk review covering chain concentration, IL exposure and audit "
                "signals. Advisory only — never holds keys, never signs."
            ),
            ["defi-planner", "pro"],
            defi_vertical.INPUT_SCHEMA_PORTFOLIO,
            defi_vertical.OUTPUT_SCHEMA_PORTFOLIO,
        ),
    ]

    for path, price, summary, desc, tags, in_schema, out_schema in _vertical_specs:
        if path not in schema.get("paths", {}):
            continue
        op = schema["paths"][path]["post"]
        op["summary"] = summary
        op["description"] = desc
        op["tags"] = tags
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": in_schema}},
        }
        op["responses"] = {
            "200": {
                "description": "Successful response.",
                "content": {"application/json": {"schema": out_schema}},
            },
            "402": {
                "description": (
                    "Payment Required. Body contains x402 paymentRequirements; "
                    "resubmit with X-PAYMENT header."
                ),
            },
            "400": {"description": "Bad request (validation failure)."},
        }
        op["x-payment-info"] = {
            "price": {
                "mode": "fixed",
                "currency": "USD",
                "amount": f"{float(price):.6f}",
            },
            "protocols": [{"x402": {}}],
        }
        op["x-bazaar"] = {
            "schema": {
                "properties": {
                    "input": in_schema,
                    "output": out_schema,
                },
            },
        }

    _SCHEMA_CACHE[cache_key] = schema
    return schema


app.include_router(directory_router)
app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False, methods=["GET"])


@app.get("/openapi.json", include_in_schema=False)
async def openapi_endpoint(request: Request):
    return JSONResponse(_build_openapi())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
