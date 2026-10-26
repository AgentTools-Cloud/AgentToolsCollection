"""HTTP endpoints for sponsored paid verification (see paid_verify.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db, limits, paid_verify
from .auth import _require_same_origin_write, current_user

router = APIRouter(tags=["paid-verification"])

_NO_STORE = {"Cache-Control": "private, no-store"}


class PrepareIn(BaseModel):
    payer: str = Field(..., min_length=42, max_length=42)


class ExecuteIn(BaseModel):
    signature: str = Field(..., min_length=132, max_length=134)


def _user(request: Request):
    _require_same_origin_write(request)
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in to run a paid verification", headers=_NO_STORE)
    return user


def _error(exc: paid_verify.VerifyError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": str(exc)},
                        status_code=exc.status, headers=_NO_STORE)


@router.post("/api/v1/services/{slug}/paid-verifications")
async def prepare_verification(slug: str, body: PrepareIn, request: Request):
    """Quote a sponsored paid verification of an x402 service.

    Fetches the service's live 402 quote and accepts only x402 v2, `exact`, Base
    USDC (`eip155:8453`), at most $1.00. Returns `typed_data`: an EIP-712
    `TransferWithAuthorization` (EIP-3009) whose `to` is the service's own payTo.
    Sign it with `payer` and call `/api/v1/paid-verifications/{id}/execute`
    before `valid_before`. Non-custodial: funds never pass through Agent Tools.
    Auth: `Authorization: Bearer <api_key>` (mint at `POST /api/v1/keys`, no signup)
    or a signed-in same-origin browser session. At most 3 open verifications per
    account; 6/min and 60/day per IP.
    """
    user = _user(request)
    blocked = limits.check_ip_limits(
        limits.client_ip_from_request(request), "paid_verify_prepare",
        (("minute", 6, 60), ("day", 60, 86400)), db_path=db.DEFAULT_DB_PATH)
    if blocked:
        limits.raise_rate_limited(blocked, "too many paid verification requests")
    with db.connect(read_only=True) as conn:
        svc = db.get_by_slug(conn, slug)
    if not svc:
        raise HTTPException(404, "service not found")
    try:
        out = await run_in_threadpool(paid_verify.prepare, int(user["id"]), svc, body.payer)
    except paid_verify.VerifyError as exc:
        return _error(exc)
    return JSONResponse(out, status_code=201, headers=_NO_STORE)


@router.post("/api/v1/paid-verifications/{vid}/execute")
async def execute_verification(vid: str, body: ExecuteIn, request: Request):
    """Relay the signed authorization to the service exactly once.

    The signature must recover to the quoted `payer`. The live price is
    re-checked first; if amount or recipient changed nothing is sent and the
    result is `quote_changed`. Never retried: a timeout is `indeterminate`.
    `status` is one of `succeeded`, `delivery_failed`, `payment_rejected`,
    `indeterminate`. `score` (0-100) = delivered 35, settlement receipt 20,
    price match 10, non-empty 10, well-formed 10, latency 15 (capped at 30
    when not delivered). Signatures and response bodies are not stored.
    """
    user = _user(request)
    try:
        out = await run_in_threadpool(paid_verify.execute, vid, int(user["id"]), body.signature)
    except paid_verify.VerifyError as exc:
        return _error(exc)
    return JSONResponse(out, headers=_NO_STORE)


@router.get("/api/v1/paid-verifications/{vid}")
def get_verification(vid: str, request: Request):
    """Read one of your own paid verifications (same fields as execute)."""
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in required", headers=_NO_STORE)
    try:
        return JSONResponse(paid_verify.result(vid, int(user["id"])), headers=_NO_STORE)
    except paid_verify.VerifyError as exc:
        return _error(exc)
