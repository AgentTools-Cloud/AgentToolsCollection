"""HTTP endpoints for the MCP credential test (see mcp_credential_test.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import db, limits, mcp_credential_test as mct
from .auth import _require_same_origin_write, current_user

router = APIRouter(tags=["mcp"])

_NO_STORE = {"Cache-Control": "private, no-store"}


class CredentialTestIn(BaseModel):
    credential: str
    header: str = mct.DEFAULT_HEADER
    publish_tools: bool = True


def _server(slug: str) -> dict:
    with db.connect(read_only=True) as conn:
        row = conn.execute("SELECT id, slug, endpoint_url FROM mcp_servers WHERE slug=?",
                           (slug,)).fetchone()
    if row is None or not (row["endpoint_url"] or "").strip():
        raise HTTPException(404, "MCP server not found")
    return dict(row)


@router.post("/api/v1/mcp/servers/{slug}/credential-tests")
async def run_credential_test(slug: str, body: CredentialTestIn, request: Request):
    """Test an MCP server that asks for authentication, with your own credential.

    We send `credential` once, only to the listing's endpoint over HTTPS, as the
    `header` you name (`Authorization` by default; a bare token becomes
    `Bearer <token>`), and call `initialize`, `notifications/initialized` and
    `tools/list` - never a tool. Redirects are followed only within the same
    origin. The credential is not stored or logged.

    Every test is kept on the listing's public record. The latest successful
    one (`succeeded`, or `partial` when tools/list failed) scores the handshake,
    protocol revision and tool items of readiness and the conformance and
    descriptor parts of the quality grade, as our own probe does for an open
    server. Tool names and descriptions are public unless `publish_tools` is
    false, in which case only the counts are shown; the score is the same.
    `status` is `succeeded`, `partial`, `auth_rejected` or `failed` (see `error`).
    Auth: `Authorization: Bearer <api_key>` (mint at `POST /api/v1/keys`) or a
    signed-in same-origin browser session. Limits: 6/min and 60/day per IP,
    30/day per account.
    """
    _require_same_origin_write(request)
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in to run a credential test", headers=_NO_STORE)
    blocked = limits.check_ip_limits(
        limits.client_ip_from_request(request), "mcp_credential_test",
        (("minute", 6, 60), ("day", 60, 86400)), db_path=db.DEFAULT_DB_PATH)
    if blocked:
        limits.raise_rate_limited(blocked, "too many credential tests")
    server = _server(slug)
    try:
        out = await run_in_threadpool(mct.run, int(user["id"]), server, body.credential,
                                      body.header, body.publish_tools)
    except mct.CredentialTestError as exc:
        return JSONResponse({"error": exc.code, "message": str(exc)},
                            status_code=exc.status, headers=_NO_STORE)
    return JSONResponse(out, status_code=201, headers=_NO_STORE)


@router.get("/api/v1/mcp/servers/{slug}/credential-tests")
def list_credential_tests(slug: str):
    """Every credential test of one MCP server, newest first (public record)."""
    server = _server(slug)
    with db.connect(read_only=True) as conn:
        tests = mct.history(conn, server["id"])
    return {"slug": server["slug"], "tests": tests}
