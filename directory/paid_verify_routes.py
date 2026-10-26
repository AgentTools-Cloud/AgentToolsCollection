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
    """Quote a one-off paid call; returns EIP-712 typed data for the user's wallet."""
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
    """Relay the signed authorization once. Never retried by the server."""
    user = _user(request)
    try:
        out = await run_in_threadpool(paid_verify.execute, vid, int(user["id"]), body.signature)
    except paid_verify.VerifyError as exc:
        return _error(exc)
    return JSONResponse(out, headers=_NO_STORE)


@router.get("/api/v1/paid-verifications/{vid}")
def get_verification(vid: str, request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in required", headers=_NO_STORE)
    try:
        return JSONResponse(paid_verify.result(vid, int(user["id"])), headers=_NO_STORE)
    except paid_verify.VerifyError as exc:
        return _error(exc)
