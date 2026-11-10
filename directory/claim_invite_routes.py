"""Public opt-out only; invitation approval and sending are operator commands."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import claim_invites, db, mailer

router = APIRouter()
ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
HEADERS = {
    "Cache-Control": "private, no-store",
    "CDN-Cache-Control": "no-store",
    "Cloudflare-CDN-Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}


def response(request: Request, state: str, status_code: int = 200):
    return TEMPLATES.TemplateResponse(request, "claim_invitation_unsubscribe.html",
                                      {"request": request, "state": state}, status_code=status_code, headers=HEADERS)


@router.get("/assets/claim-invitations.css", include_in_schema=False)
def stylesheet():
    return FileResponse(ROOT / "static" / "claim-invitations.css", media_type="text/css",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/assets/claim-invitations.js", include_in_schema=False)
def script():
    return FileResponse(ROOT / "static" / "claim-invitations.js", media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get(claim_invites.UNSUBSCRIBE_PATH, response_class=HTMLResponse, include_in_schema=False)
def unsubscribe_page(request: Request):
    return response(request, "confirm")


def apply_optout(token: str) -> bool:
    with db.writer(immediate=True, timeout=2.5) as connection:
        return claim_invites.unsubscribe(connection, token)


@router.post(claim_invites.UNSUBSCRIBE_PATH, response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe_post(request: Request):
    origin = request.headers.get("origin")
    private_same_origin = origin == "null" and request.headers.get("sec-fetch-site") == "same-origin"
    if origin and origin not in {mailer.SITE, str(request.base_url).rstrip("/")} and not private_same_origin:
        return response(request, "invalid", 403)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return response(request, "invalid", 415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024:
            return response(request, "invalid", 413)
    try:
        values = parse_qs(bytes(body).decode("utf-8"), strict_parsing=True, max_num_fields=1)
        if set(values) != {"token"} or len(values["token"]) != 1:
            return response(request, "invalid", 400)
        found = await run_in_threadpool(apply_optout, values["token"][0])
    except (ValueError, UnicodeError):
        return response(request, "invalid", 400)
    except sqlite3.OperationalError as error:
        if not db.is_locked_error(error):
            raise
        result = response(request, "busy", 503)
        result.headers["Retry-After"] = "2"
        return result
    return response(request, "stopped" if found else "invalid", 200 if found else 400)