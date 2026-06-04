"""
mcpserver — x402-gated relay in front of an OpenAI-compatible inference backend.

Two parallel interfaces, same backend, same per-call price:

  1. REST   POST /v1/chat/completions      (OpenAI-compatible)
  2. MCP    POST /mcp                      (streamable-http, tool: qwen36_chat)

Both routes are gated by the x402 v2 PaymentMiddlewareASGI. Agents speaking
either dialect can buy a single inference call against Qwen3.6-35B-A3B on Base
mainnet USDC.

Flow:
    agent (x402 client)
       │  POST /v1/chat/completions  (no payment yet)        OR  POST /mcp
       ▼
    mcpserver (latex-tools:9100)
       │  → 402 + paymentRequirements
       │  ← retry with PAYMENT-SIGNATURE header (EIP-3009 USDC on Base)
       │  verify + settle via facilitator
       ▼
    upstream OpenAI-compatible inference (Qwen3.6-35B-A3B)
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
from directory.mcp_app import discover_mcp

from verticals import signals as signals_vertical
from verticals import onchain as onchain_vertical
from verticals import defi as defi_vertical

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
    # We run both the paid Qwen relay (/mcp) and the free directory
    # discovery MCP (/mcp-discovery).
    async with mcp_app.session_manager.run(), discover_mcp.session_manager.run():
        try:
            yield
        finally:
            await _http.aclose()


app = FastAPI(
    title="mcpserver", version="0.2.0", lifespan=lifespan, openapi_url=None
)

# Mount MCP streamable-http transport at /mcp.
app.mount("/mcp", mcp_app.streamable_http_app())
# Free directory-discovery MCP (search/get/list_categories/stats) — ungated.
app.mount("/mcp-discovery", discover_mcp.streamable_http_app())

# Hostname split: agent-tools.cloud serves directory + relay (full schema),
# while the subdomain relay.agent-tools.cloud presents the Qwen3.6 paid
# relay only. x402scan keys server entries by host, so a clean split lets
# each role show up as its own bazaar entry with focused metadata.
RELAY_HOST_PREFIXES = ("relay.",)


def _is_relay_host(request: Request) -> bool:
    h = (request.headers.get("host") or "").split(":", 1)[0].lower()
    return any(h.startswith(p) for p in RELAY_HOST_PREFIXES)


@app.middleware("http")
async def _relay_host_root(request: Request, call_next):
    # On relay.agent-tools.cloud the root `/` must serve the relay landing
    # page; otherwise the directory router (mounted later) would win.
    if request.method == "GET" and request.url.path == "/" and _is_relay_host(request):
        return HTMLResponse(_HOMEPAGE_HTML)
    return await call_next(request)

# x402 v2 middleware — gates both REST and MCP entrypoints.
_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=X402_FACILITATOR_URL))
_x402_server = x402ResourceServer(_facilitator)
_x402_server.register(X402_NETWORK, ExactEvmServerScheme())

_routes = {
    "POST /v1/chat/completions": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Qwen3.6-35B-A3B chat — frontier 35B MoE at flat ${X402_PRICE_USD} USDC / call. OpenAI wire format, no key, no signup.",
    ),
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
    ),
    # --- Vertical 2: on-chain analytics NL Q&A ---------------------------
    "POST /v1/onchain/ask": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_ONCHAIN_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"On-chain Q&A — free-form questions about tokens, yields, stablecoins, TVL. Grounded in live Defillama+DexScreener data. ${X402_ONCHAIN_PRICE_USD} USDC / call.",
    ),
    # --- Vertical 3: DeFi action planner (advisory only, no signing) -----
    "POST /v1/defi/plan": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_DEFI_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"DeFi action planner — best lend/swap/stake route by risk tolerance, with Qwen risk review. Advisory only, never signs. ${X402_DEFI_PRICE_USD} USDC / call.",
    ),
    # --- Pro tier: bulk signal (up to 10 tokens / call) ------------------
    "POST /v1/signal/bulk": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_SIGNAL_BULK_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Bulk momentum scan — score up to 10 tokens in one shot, with portfolio rollup (top pick, buy/hold/sell counts). ${X402_SIGNAL_BULK_PRICE_USD} USDC / call.",
    ),
    # --- Pro tier: multi-source on-chain report --------------------------
    "POST /v1/onchain/report": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_ONCHAIN_REPORT_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Synthesised on-chain analyst report — token+yields+TVL+stables fused into a 5-10 sentence brief with key findings & risks. ${X402_ONCHAIN_REPORT_PRICE_USD} USDC / call.",
    ),
    # --- Pro tier: multi-leg DeFi portfolio plan -------------------------
    "POST /v1/defi/portfolio": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_DEFI_PORTFOLIO_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"Multi-leg DeFi portfolio — allocates a USD budget across lend/stake/swap, returns blended APY + Qwen portfolio review. Advisory only, never signs. ${X402_DEFI_PORTFOLIO_PRICE_USD} USDC / call.",
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=_routes, server=_x402_server)


# --- free endpoints -------------------------------------------------------


@app.get("/healthz")
@app.get("/health")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


_HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-tools.cloud — agent-native crypto stack on x402 (Qwen + signals + on-chain + DeFi)</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Agent-native crypto stack on x402: Qwen3.6-35B-A3B chat at $0.001/call + token signals, on-chain Q&A and DeFi planner from $0.01. Eight pay-per-call endpoints, USDC on Base, no signup, no API key, no rate limits.">
<link rel="alternate" type="application/json" title="x402 discovery" href="/.well-known/x402">
<link rel="service" type="application/openapi+json" title="OpenAPI 3.1 spec" href="/openapi.json">
<meta name="x402:discovery" content="/.well-known/x402">
<meta name="x402:openapi" content="/openapi.json">
<meta name="x402:network" content="eip155:8453">
<meta name="x402:pay-to" content="0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF">
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 760px; margin: 2.5rem auto; padding: 0 1.2rem; }
h1 { margin: 0 0 .2em; font-size: 1.6rem; }
h2 { margin-top: 2rem; font-size: 1.15rem; border-bottom: 1px solid #8884; padding-bottom: .25em; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; background: #2c7be522; color: #2c7be5; font-size: 12px; margin-right: 6px; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }
pre { background: #8881; padding: .8em 1em; border-radius: 6px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: .6em 0; }
th, td { border-bottom: 1px solid #8884; padding: .35em .6em; text-align: left; }
th { font-weight: 600; }
a { color: #2c7be5; }
.muted { color: #888; font-size: 13px; }
</style>
</head>
<body>
<h1>agent-tools.cloud</h1>
<p>
  <span class="tag">x402</span><span class="tag">MCP</span><span class="tag">Base mainnet</span><span class="tag">USDC</span>
  <b>Agent-native crypto stack on <a href="https://x402.org" target="_blank" rel="noopener">x402</a>.</b>
  Frontier <b>Qwen3.6-35B-A3B</b> chat from <b>$0.001/call</b>, plus four data-grounded verticals —
  token momentum signals, on-chain Q&amp;A, DeFi action planner and multi-leg portfolio.
  Eight endpoints, one wallet, one chain. <b>No signup. No API key. No human UI.</b>
  Settles atomically in USDC on Base, per call.
</p>
<p class="muted">
  Cheapest credible price across all four categories on the x402 bazaar — built for agents that
  iterate fast, batch wide and don't want to babysit a billing dashboard.
</p>

<h2>Endpoints</h2>
<table>
<tr><th>Path</th><th>Method</th><th>Price</th><th>Transport</th></tr>
<tr><td><code>/v1/chat/completions</code></td><td>POST</td><td>$0.001</td><td>Frontier <b>Qwen3.6-35B-A3B</b> chat — OpenAI-compatible REST, drop-in for any SDK</td></tr>
<tr><td><code>/mcp</code></td><td>POST</td><td>$0.001</td><td>Same model over <b>MCP</b> streamable-http (tool: <code>qwen36_chat</code>)</td></tr>
<tr><td><code>/v1/signal/token</code></td><td>POST</td><td>$0.01</td><td>Token momentum — buy/hold/sell + score, wash-trade & pump penalties (live DexScreener + Qwen)</td></tr>
<tr><td><code>/v1/onchain/ask</code></td><td>POST</td><td>$0.02</td><td>On-chain NL Q&amp;A — yields, stablecoins, TVL, tokens (grounded in live Defillama + DexScreener)</td></tr>
<tr><td><code>/v1/defi/plan</code></td><td>POST</td><td>$0.05</td><td>DeFi planner — best lend/swap/stake by risk tolerance + Qwen risk review (advisory, never signs)</td></tr>
<tr><td colspan="4" style="padding-top:.8em;font-size:12px;color:#888"><b>Pro tier</b> — wider scan, deeper synthesis, one call</td></tr>
<tr><td><code>/v1/signal/bulk</code></td><td>POST</td><td>$0.05</td><td>Bulk momentum — score up to <b>10 tokens / call</b> with portfolio rollup & top pick</td></tr>
<tr><td><code>/v1/onchain/report</code></td><td>POST</td><td>$0.20</td><td>Analyst report — token+yields+TVL+stables fused into a 5-10 sentence brief with key findings &amp; risks</td></tr>
<tr><td><code>/v1/defi/portfolio</code></td><td>POST</td><td>$0.50</td><td>Multi-leg allocation — lend/stake/swap budget split with blended APY + Qwen portfolio review</td></tr>
<tr><td><code>/v1/models</code></td><td>GET</td><td>free</td><td>list available models</td></tr>
<tr><td><code>/healthz</code></td><td>GET</td><td>free</td><td>liveness probe</td></tr>
<tr><td><code>/.well-known/x402</code></td><td>GET</td><td>free</td><td>service discovery (JSON)</td></tr>
</table>

<h2>Payment</h2>
<table>
<tr><td>Network</td><td><code>eip155:8453</code> (Base mainnet)</td></tr>
<tr><td>Asset</td><td>USDC <code>0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913</code></td></tr>
<tr><td>Pay to</td><td><code>0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF</code></td></tr>
<tr><td>Per call</td><td>$0.001 – $0.50 USDC (see endpoints above; settled atomically per request)</td></tr>
<tr><td>Facilitator</td><td><code>facilitator.fluxapay.xyz</code> (non-custodial, gas covered)</td></tr>
</table>
<p class="muted">Live on Base mainnet. Settlement via the FluxA x402 facilitator — funds flow payer → payee directly, the facilitator covers gas. No KYC, no signup, just sign EIP-3009 and go.</p>

<h2>Quick start — REST + x402-fetch (Node)</h2>
<pre>import { wrapFetchWithPayment } from "x402-fetch";
import { privateKeyToAccount } from "viem/accounts";

const fetchPaid = wrapFetchWithPayment(
  fetch,
  privateKeyToAccount(process.env.PRIVATE_KEY),
);

const r = await fetchPaid("https://agent-tools.cloud/v1/chat/completions", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({
    model: "Qwen/Qwen3.6-35B-A3B",
    messages: [{ role: "user", content: "Say hi." }],
  }),
});
console.log(await r.json());</pre>

<h2>Quick start — MCP client config</h2>
<pre>{
  "mcpServers": {
    "agent-tools": {
      "transport": "streamable-http",
      "url": "https://agent-tools.cloud/mcp",
      "x402": {
        "privateKeyEnv": "PRIVATE_KEY",
        "network": "eip155:8453"
      }
    }
  }
}</pre>

<h2>Source</h2>
<p>
  <a href="https://github.com/JoursBleu/mcpserver" target="_blank" rel="noopener">github.com/JoursBleu/mcpserver</a>

</p>
</body>
</html>
"""


@app.get("/relay", response_class=HTMLResponse)
async def relay_page() -> str:
    return _HOMEPAGE_HTML


@app.get("/.well-known/x402")
async def well_known_x402(request: Request) -> dict[str, Any]:
    relay = _is_relay_host(request)
    host = (request.headers.get("host") or "").split(":", 1)[0].lower() or (
        "relay.agent-tools.cloud" if relay else "agent-tools.cloud"
    )
    description = (
        "Agent-native crypto stack on x402: frontier Qwen3.6-35B-A3B chat from "
        "$0.001/call plus four data-grounded verticals — token momentum signals, "
        "on-chain Q&A, DeFi planner and multi-leg portfolio. Eight endpoints, USDC "
        "on Base, no signup, no API key, no rate limits."
        if relay
        else "Two-in-one x402 service: (1) the largest open directory of x402 "
             "endpoints (the full x402 ecosystem, free JSON API + agents.json manifest); "
             "(2) agent-native paid stack — Qwen3.6-35B-A3B chat from $0.001/call "
             "plus token signals, on-chain Q&A and DeFi planner from $0.01. "
             "USDC on Base, no signup, no API key."
    )
    return {
        "name": host,
        "description": description,
        "version": "0.3",
        "endpoints": [
            {"path": "/v1/chat/completions", "method": "POST", "kind": "rest-openai", "gated": True, "price_usd": X402_PRICE_USD},
            {"path": "/mcp", "method": "POST", "kind": "mcp-streamable-http", "gated": True, "price_usd": X402_PRICE_USD},
            {"path": "/v1/signal/token", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_SIGNAL_PRICE_USD, "category": "signal"},
            {"path": "/v1/onchain/ask", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_ONCHAIN_PRICE_USD, "category": "onchain-analytics"},
            {"path": "/v1/defi/plan", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_DEFI_PRICE_USD, "category": "defi-planner"},
            {"path": "/v1/signal/bulk", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_SIGNAL_BULK_PRICE_USD, "category": "signal", "tier": "pro"},
            {"path": "/v1/onchain/report", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_ONCHAIN_REPORT_PRICE_USD, "category": "onchain-analytics", "tier": "pro"},
            {"path": "/v1/defi/portfolio", "method": "POST", "kind": "rest-json", "gated": True, "price_usd": X402_DEFI_PORTFOLIO_PRICE_USD, "category": "defi-planner", "tier": "pro"},
            {"path": "/v1/models", "method": "GET", "kind": "info", "gated": False},
            {"path": "/healthz", "method": "GET", "kind": "info", "gated": False},
        ],
        "x402": {
            "version": 2,
            "scheme": "exact",
            "network": X402_NETWORK,
            "pay_to": X402_PAY_TO,
            "price_usd": X402_PRICE_USD,
            "facilitator": X402_FACILITATOR_URL,
        },
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
        "x402_paid_mcp": {
            "url": f"{base}/mcp",
            "tool": "qwen36_chat",
            "price_usd": X402_PRICE_USD,
            "network": X402_NETWORK,
            "facilitator": X402_FACILITATOR_URL,
        },
    }



@app.get("/v1/models")
async def models() -> dict[str, Any]:
    r = await _client().get("/v1/models")
    upstream = r.json()
    data = [m for m in upstream.get("data", []) if m.get("id") in ALLOWED_MODELS]
    return {"object": "list", "data": data}


# --- paid REST endpoint ---------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI-compatible passthrough; PaymentMiddlewareASGI gates this route."""
    body = await request.json()

    model = body.get("model")
    if model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model {model!r} not allowed; allowed={sorted(ALLOWED_MODELS)}",
        )

    if bool(body.get("stream")):
        upstream_req = _client().build_request("POST", "/v1/chat/completions", json=body)
        upstream_resp = await _client().send(upstream_req, stream=True)

        async def _gen():
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            _gen(),
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )

    r = await _client().post("/v1/chat/completions", json=body)
    return JSONResponse(content=r.json(), status_code=r.status_code)


# --- paid vertical endpoints ---------------------------------------------
#
# Each handler is gated by its own x402 route entry above. The vertical
# modules call `_llm_short` (NOT the gated /v1/chat/completions route) so
# the agent only pays once per outer request.


@app.post("/v1/signal/token")
async def signal_token(request: Request) -> Any:
    """Token momentum signal — DexScreener data + Qwen optional commentary."""
    body = await request.json()
    result = await signals_vertical.handle(body, _llm_short)
    return JSONResponse(content=result)


@app.post("/v1/onchain/ask")
async def onchain_ask(request: Request) -> Any:
    """Natural-language Q&A over free on-chain data (Defillama + DexScreener)."""
    body = await request.json()
    result = await onchain_vertical.handle(body, _llm_short)
    return JSONResponse(content=result)


@app.post("/v1/defi/plan")
async def defi_plan(request: Request) -> Any:
    """DeFi action planner — lend/swap/stake comparison with Qwen risk review."""
    body = await request.json()
    result = await defi_vertical.handle(body, _llm_short)
    return JSONResponse(content=result)


@app.post("/v1/signal/bulk")
async def signal_bulk(request: Request) -> Any:
    """Pro tier: bulk token signal — up to 10 tokens / call."""
    body = await request.json()
    result = await signals_vertical.handle_bulk(body, _llm_short)
    return JSONResponse(content=result)


@app.post("/v1/onchain/report")
async def onchain_report(request: Request) -> Any:
    """Pro tier: multi-source on-chain analyst report."""
    body = await request.json()
    result = await onchain_vertical.handle_report(body, _llm_short)
    return JSONResponse(content=result)


@app.post("/v1/defi/portfolio")
async def defi_portfolio(request: Request) -> Any:
    """Pro tier: multi-leg DeFi portfolio plan."""
    body = await request.json()
    result = await defi_vertical.handle_portfolio(body, _llm_short)
    return JSONResponse(content=result)


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


def _build_openapi(*, relay_only: bool = False) -> dict[str, Any]:
    cache_key = "relay" if relay_only else "full"
    if cache_key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[cache_key]

    if relay_only:
        title = "relay.agent-tools.cloud — agent-native crypto stack on x402"
        description = (
            "Agent-native crypto stack on x402, settled in USDC on Base mainnet (chain 8453). "
            "Eight pay-per-call endpoints: (1) frontier Qwen3.6-35B-A3B chat at $0.001/call over "
            "OpenAI-compatible REST (POST /v1/chat/completions) and MCP streamable-http (POST /mcp, "
            "tool qwen36_chat); (2) four data-grounded verticals — token momentum signal "
            "($0.01) / bulk scan ($0.05), on-chain Q&A ($0.02) / analyst report ($0.20), "
            "DeFi action planner ($0.05) / multi-leg portfolio ($0.50). All routes share the same "
            "x402 v2 middleware and pay-to address, so a single wallet pays for the entire stack. "
            "No signup, no API key, no human UI, no rate limits per identity — built for autonomous agents."
        )
        guidance = (
            "POST /v1/chat/completions is an OpenAI-compatible chat "
            "completions endpoint gated by x402. Send a JSON body with "
            "'model' (Qwen/Qwen3.6-35B-A3B) and 'messages' (array of "
            "{role, content}). First request returns HTTP 402 with "
            "paymentRequirements; resubmit with an X-PAYMENT header "
            "(EIP-3009 USDC TransferWithAuthorization on Base mainnet, "
            "chain 8453, asset 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) "
            "to receive the model response. Flat $0.001 USDC per call. The "
            "MCP streamable-http transport at POST /mcp accepts the same "
            "payment and exposes a 'qwen36_chat' tool. Facilitator: "
            "facilitator.fluxapay.xyz (non-custodial, gas covered). PayTo: "
            "0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF."
        )
    else:
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
            "from $0.01/call. USDC on Base, no signup, no API key, no rate limits per identity. "
            "The paid stack is also reachable relay-only at https://relay.agent-tools.cloud."
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

    if relay_only:
        schema["paths"] = {
            p: v for p, v in schema.get("paths", {}).items()
            if not any(p == pre or p.startswith(pre) for pre in DIRECTORY_PATH_PREFIXES)
        }

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

    chat_op = schema["paths"]["/v1/chat/completions"]["post"]
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


@app.get("/openapi.json", include_in_schema=False)
async def openapi_endpoint(request: Request):
    return JSONResponse(_build_openapi(relay_only=_is_relay_host(request)))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
