"""
mcpserver — x402-gated relay in front of 天枢 llm-gateway.

Flow:
    agent (x402 client)
       │  POST /v1/chat/completions  (no payment yet)
       ▼
    mcpserver (latex-tools:9100)
       │  → 402 + paymentRequirements   (PaymentMiddlewareASGI)
       │  ← retry with PAYMENT-SIGNATURE header (EIP-3009 USDC transfer on Base)
       │  verify + settle via facilitator
       ▼
    upstream 天枢 (127.0.0.1:8080) → W7900D vLLM (Qwen3.6-35B-A3B)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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

# --- app ------------------------------------------------------------------

app = FastAPI(title="mcpserver", version="0.1.0")

# x402 v2 middleware
_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=X402_FACILITATOR_URL))
_x402_server = x402ResourceServer(_facilitator)
_x402_server.register(X402_NETWORK, ExactEvmServerScheme())

_routes = {
    "POST /v1/chat/completions": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=X402_PAY_TO,
                price=f"${X402_PRICE_USD}",
                network=X402_NETWORK,
            ),
        ],
        mime_type="application/json",
        description=f"Qwen3.6-35B-A3B inference (flat ${X402_PRICE_USD} / call)",
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=_routes, server=_x402_server)


# --- upstream client ------------------------------------------------------

_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient(
        base_url=TIANSHU_BASE_URL,
        headers={"Authorization": f"Bearer {TIANSHU_API_KEY}"},
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http is not None:
        await _http.aclose()


# --- free endpoints -------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    assert _http is not None
    r = await _http.get("/v1/models")
    upstream = r.json()
    data = [m for m in upstream.get("data", []) if m.get("id") in ALLOWED_MODELS]
    return {"object": "list", "data": data}


# --- paid endpoint --------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI-compatible passthrough; PaymentMiddlewareASGI gates this route."""
    assert _http is not None
    body = await request.json()

    model = body.get("model")
    if model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model {model!r} not allowed; allowed={sorted(ALLOWED_MODELS)}",
        )

    stream = bool(body.get("stream"))
    if stream:
        upstream_req = _http.build_request("POST", "/v1/chat/completions", json=body)
        upstream_resp = await _http.send(upstream_req, stream=True)

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

    r = await _http.post("/v1/chat/completions", json=body)
    return JSONResponse(content=r.json(), status_code=r.status_code)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "9100")),
        log_level="info",
    )
