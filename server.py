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


# --- MCP layer ------------------------------------------------------------

mcp_app = FastMCP("mcpserver", instructions=(
    "Paid inference relay (x402 + USDC on Base). One tool: qwen36_chat. "
    "Each tool call costs $" + X402_PRICE_USD + " USDC, settled on Base mainnet."
))


@mcp_app.tool(
    name="qwen36_chat",
    description=(
        "Run a chat completion against Qwen/Qwen3.6-35B-A3B. "
        "Each invocation costs $" + X402_PRICE_USD + " USDC on Base mainnet. "
        "Returns the assistant text content."
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
    async with mcp_app.session_manager.run():
        try:
            yield
        finally:
            await _http.aclose()


app = FastAPI(
    title="mcpserver", version="0.2.0", lifespan=lifespan, openapi_url=None
)

# Mount MCP streamable-http transport at /mcp.
app.mount("/mcp", mcp_app.streamable_http_app())

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
        description=f"Qwen3.6-35B-A3B inference (flat ${X402_PRICE_USD} / call)",
    ),
    # Whole MCP transport (initialize/tools-list/tools-call all share the
    # same POST endpoint). One payment buys one streamable-http roundtrip.
    "POST /mcp": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact", pay_to=X402_PAY_TO,
            price=f"${X402_PRICE_USD}", network=X402_NETWORK,
        )],
        mime_type="application/json",
        description=f"MCP tool call against Qwen3.6-35B-A3B (${X402_PRICE_USD} / call)",
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=_routes, server=_x402_server)


# --- free endpoints -------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


_HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-tools.cloud — x402 + MCP relay for Qwen3.6-35B-A3B</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
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
  <span class="tag">x402</span><span class="tag">MCP</span><span class="tag">Base mainnet</span>
  Pay-per-call inference API for <b>Qwen/Qwen3.6-35B-A3B</b>, settled in USDC on Base via the
  <a href="https://x402.org" target="_blank" rel="noopener">x402</a> protocol.
  Agent-native — no signup, no human UI.
</p>

<h2>Endpoints</h2>
<table>
<tr><th>Path</th><th>Method</th><th>Price</th><th>Transport</th></tr>
<tr><td><code>/v1/chat/completions</code></td><td>POST</td><td>$0.001</td><td>OpenAI-compatible REST</td></tr>
<tr><td><code>/mcp</code></td><td>POST</td><td>$0.001</td><td>MCP streamable-http (tool: <code>qwen36_chat</code>)</td></tr>
<tr><td><code>/v1/models</code></td><td>GET</td><td>free</td><td>list available models</td></tr>
<tr><td><code>/healthz</code></td><td>GET</td><td>free</td><td>liveness probe</td></tr>
<tr><td><code>/.well-known/x402</code></td><td>GET</td><td>free</td><td>service discovery (JSON)</td></tr>
</table>

<h2>Payment</h2>
<table>
<tr><td>Network</td><td><code>eip155:8453</code> (Base mainnet)</td></tr>
<tr><td>Asset</td><td>USDC <code>0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913</code></td></tr>
<tr><td>Pay to</td><td><code>0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF</code></td></tr>
<tr><td>Per call</td><td>$0.001 USDC (1000 atomic units)</td></tr>
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
        "Pay-per-call Qwen3.6-35B-A3B inference relay at flat $0.001 USDC / "
        "call on Base mainnet via the x402 protocol (v2). Agent-native — "
        "no signup, no API key."
        if relay
        else "Global x402 service directory (470+ endpoints) + pay-per-call "
             "Qwen3.6-35B-A3B inference relay at flat $0.001 USDC / call on "
             "Base (x402 v2)."
    )
    return {
        "name": host,
        "description": description,
        "version": "0.3",
        "endpoints": [
            {"path": "/v1/chat/completions", "method": "POST", "kind": "rest-openai", "gated": True},
            {"path": "/mcp", "method": "POST", "kind": "mcp-streamable-http", "gated": True},
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
        title = "relay.agent-tools.cloud — Qwen3.6-35B-A3B paid relay (x402)"
        description = (
            "Pay-per-call inference relay for Qwen/Qwen3.6-35B-A3B settled in "
            "USDC on Base mainnet via the x402 protocol (v2) at a flat "
            "$0.001 / call. Agent-native — no signup, no API key, no human "
            "UI. Two equivalent transports: OpenAI-compatible REST at "
            "POST /v1/chat/completions and MCP streamable-http at POST /mcp "
            "(tool name: qwen36_chat). Both routes are gated by the same "
            "x402 payment middleware and share the same per-call price."
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
        title = "agent-tools.cloud — x402 directory + Qwen3.6-35B-A3B relay"
        description = (
            "Hybrid x402 service for autonomous agents. (1) Public, "
            "agent-readable directory of 470+ x402 endpoints across the "
            "ecosystem — browsable at https://agent-tools.cloud, with a "
            "JSON API at /api/v1/search, /api/v1/services/{slug}, "
            "/api/v1/categories, /api/v1/stats and an agents.json manifest "
            "at /.well-known/agent-tools.json (all free, no payment "
            "required). (2) Pay-per-call inference relay for "
            "Qwen/Qwen3.6-35B-A3B settled in USDC on Base mainnet via the "
            "x402 protocol at flat $0.001/call — no signup, no API key, "
            "no human UI. The paid relay is also reachable on its own "
            "hostname https://relay.agent-tools.cloud for clients that "
            "want a relay-only entry."
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
    chat_op["summary"] = "Chat completions (Qwen3.6-35B-A3B, paid via x402)"
    chat_op["description"] = (
        "OpenAI-compatible chat completions. Gated by x402: first call "
        "returns 402 with paymentRequirements, retry with X-PAYMENT header."
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
