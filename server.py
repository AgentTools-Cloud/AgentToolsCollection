"""
mcpserver — x402-gated relay in front of 天枢 llm-gateway.

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
    upstream 天枢 (127.0.0.1:8080) → W7900D vLLM (Qwen3.6-35B-A3B)
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mcp.server.fastmcp import FastMCP

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

load_dotenv()

# --- config ---------------------------------------------------------------

TIANSHU_BASE_URL = os.environ["TIANSHU_BASE_URL"].rstrip("/")
TIANSHU_API_KEY = os.environ["TIANSHU_API_KEY"]
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
        "Run a chat completion against Qwen/Qwen3.6-35B-A3B (AWQ on W7900D). "
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
    global _http
    _http = httpx.AsyncClient(
        base_url=TIANSHU_BASE_URL,
        headers={"Authorization": f"Bearer {TIANSHU_API_KEY}"},
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
    )
    # FastMCP streamable-http needs its own session-manager lifespan.
    async with mcp_app.session_manager.run():
        try:
            yield
        finally:
            await _http.aclose()


app = FastAPI(title="mcpserver", version="0.2.0", lifespan=lifespan)

# Mount MCP streamable-http transport at /mcp.
app.mount("/mcp", mcp_app.streamable_http_app())

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
