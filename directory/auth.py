"""GitHub sign-in. Identity only.

Logging in proves who you are; it grants nothing on its own. Edit rights come
from `domain_ownership` -- a token published on the host that serves the
listing. Nothing here should ever be consulted to decide whether an edit is
allowed.

Sessions are a signed cookie (stdlib HMAC, no extra dependency). The user row
is still read on every request so a blocked account loses access immediately
instead of when its cookie expires.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import db

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com"
# Enough to identify the operator and mail them; no repo or write access.
SCOPE = "user:email"

SESSION_COOKIE = "atc_session"
STATE_COOKIE = "atc_oauth_state"
SESSION_MAX_AGE = 30 * 86400
STATE_MAX_AGE = 600

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

router = APIRouter()


def configured() -> bool:
    return bool(os.getenv("GITHUB_OAUTH_CLIENT_ID")
                and os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
                and os.getenv("SESSION_SECRET"))


# --------------------------------------------------------------- cookies

def _secret() -> bytes:
    value = os.getenv("SESSION_SECRET")
    if not value:
        raise RuntimeError("SESSION_SECRET is not set")
    return value.encode("utf-8")


def sign(payload: dict, max_age: int) -> str:
    body = json.dumps({**payload, "exp": int(time.time()) + max_age},
                      separators=(",", ":")).encode("utf-8")
    b = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    return "%s.%s" % (b, sig)


def unsign(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    b, _, sig = value.rpartition(".")
    expected = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
        return None
    return data


def _set_cookie(resp, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, path="/",
                    httponly=True, secure=True, samesite="lax")


# ------------------------------------------------------------- helpers

def _safe_next(target: str | None) -> str:
    """Only same-site relative paths, so the callback cannot be used to
    bounce a visitor to an attacker's site."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _redirect_uri(request: Request) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "https://agent-tools.cloud").rstrip("/")
    return base + "/auth/github/callback"


def current_user(request: Request):
    """The signed-in user row, or None. Never implies any edit right."""
    data = unsign(request.cookies.get(SESSION_COOKIE))
    if not data or not data.get("uid"):
        return None
    try:
        with db.connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (data["uid"],)).fetchone()
    except Exception:
        return None
    if row is None or row["status"] != "active":
        return None
    return row


# --------------------------------------------------------------- routes

@router.get("/auth/github/login", include_in_schema=False)
def github_login(request: Request, next: str = "/"):
    if not configured():
        raise HTTPException(503, "GitHub sign-in is not configured")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": os.getenv("GITHUB_OAUTH_CLIENT_ID"),
        "redirect_uri": _redirect_uri(request),
        "scope": SCOPE,
        "state": state,
        "allow_signup": "true",
    }
    resp = RedirectResponse(
        AUTHORIZE_URL + "?" + urlencode(params), status_code=302)
    _set_cookie(resp, STATE_COOKIE,
                sign({"state": state, "next": _safe_next(next)}, STATE_MAX_AGE),
                STATE_MAX_AGE)
    return resp


@router.get("/auth/github/callback", include_in_schema=False)
def github_callback(request: Request, code: str = "", state: str = "",
                    error: str = ""):
    if error:
        raise HTTPException(400, "GitHub sign-in was cancelled")
    if not configured():
        raise HTTPException(503, "GitHub sign-in is not configured")
    saved = unsign(request.cookies.get(STATE_COOKIE))
    if not saved or not code or not hmac.compare_digest(
            str(saved.get("state", "")), state):
        raise HTTPException(400, "Invalid or expired sign-in request")

    with httpx.Client(timeout=_TIMEOUT,
                      headers={"Accept": "application/json",
                               "User-Agent": "agent-tools.cloud"}) as client:
        tok = client.post(TOKEN_URL, data={
            "client_id": os.getenv("GITHUB_OAUTH_CLIENT_ID"),
            "client_secret": os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
            "code": code,
            "redirect_uri": _redirect_uri(request),
        })
        payload = tok.json() if tok.status_code == 200 else {}
        access = payload.get("access_token")
        if not access:
            log.warning("github token exchange failed: %s",
                        payload.get("error") or tok.status_code)
            raise HTTPException(400, "GitHub did not return an access token")

        auth = {"Authorization": "Bearer %s" % access,
                "Accept": "application/vnd.github+json",
                "User-Agent": "agent-tools.cloud"}
        me = client.get(API_URL + "/user", headers=auth)
        if me.status_code != 200:
            raise HTTPException(400, "Could not read your GitHub profile")
        me = me.json()
        emails = client.get(API_URL + "/user/emails", headers=auth)
        emails = emails.json() if emails.status_code == 200 else []

    # Only an address GitHub itself asserts as verified. Matching on an
    # unverified address would let anyone register it elsewhere and walk in.
    email = None
    if isinstance(emails, list):
        email = next((e.get("email") for e in emails
                      if isinstance(e, dict) and e.get("primary")
                      and e.get("verified")), None)

    def op():
        with db.writer() as conn:
            return db.upsert_user(
                conn, provider="github", provider_uid=str(me.get("id")),
                login=me.get("login"), avatar_url=me.get("avatar_url"),
                email=email, email_verified=bool(email))
    user_id = db.with_retry(op)

    resp = RedirectResponse(_safe_next(saved.get("next")), status_code=302)
    _set_cookie(resp, SESSION_COOKIE, sign({"uid": user_id}, SESSION_MAX_AGE),
                SESSION_MAX_AGE)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.post("/auth/logout", include_in_schema=False)
@router.get("/auth/logout", include_in_schema=False)
def logout(next: str = "/"):
    resp = RedirectResponse(_safe_next(next), status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ------------------------------------------------------- account pages

def _require_user(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    return user


def _ago(ts_val) -> str | None:
    if not ts_val:
        return None
    mins = max(0, int((time.time() - ts_val) / 60))
    if mins < 60:
        return "%d min ago" % mins
    if mins < 60 * 48:
        return "%d h ago" % (mins // 60)
    return "%d d ago" % (mins // 1440)


_INSTRUCTIONS = {
    "descriptor": (
        "Add a top-level field to the JSON you already serve at "
        "/.well-known/x402 (or your agent card): "
        '"agentToolsVerify": "<the token above>"'
    ),
    "wellknown_file": (
        "Serve the token as plain text at "
        "https://<your host>/.well-known/agent-tools-verify.txt"
    ),
    "dns_txt": (
        "Add a TXT record on _agent-tools.<your host> with the value "
        "atc-verify=<the token above>"
    ),
}


def _listing_counts(conn, hosts: list[str]) -> dict[str, int]:
    counts = {h: 0 for h in hosts}
    if not hosts:
        return counts
    for sql in (
        "SELECT url AS u FROM services WHERE url IS NOT NULL",
        "SELECT endpoint_url AS u FROM mcp_servers WHERE endpoint_url IS NOT NULL",
        "SELECT COALESCE(endpoint_url, card_url) AS u FROM a2a_agents "
        "WHERE COALESCE(endpoint_url, card_url) IS NOT NULL",
    ):
        for row in conn.execute(sql):
            host, _ = db._host_and_path(row["u"] or "")
            if host in counts:
                counts[host] += 1
    return counts


def _account_context(request: Request, user, issued=None, error=None) -> dict:
    with db.connect(read_only=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM domain_ownership WHERE user_id=? "
            "AND status IN ('pending','verified') ORDER BY created_at DESC",
            (user["id"],))]
        verified = [r for r in rows if r["status"] == "verified"]
        counts = _listing_counts(conn, [r["host"] for r in verified])
    for r in verified:
        r["verified_ago"] = _ago(r["verified_at"])
        r["listing_count"] = counts.get(r["host"], 0)
    return {
        "request": request,
        "user": user,
        "verified": verified,
        "pending": [r for r in rows if r["status"] == "pending"],
        "issued": issued,
        "error": error,
    }


@router.get("/account", include_in_schema=False)
def account_page(request: Request):
    from .routes import TEMPLATES
    user = current_user(request)
    if user is None:
        return RedirectResponse("/auth/github/login?next=/account",
                                status_code=302)
    return TEMPLATES.TemplateResponse(request, "account.html",
                                      _account_context(request, user))


@router.post("/account/domains", include_in_schema=False)
def claim_domain(request: Request, host: str = Form(...),
                 method: str = Form("descriptor")):
    from . import ownership as own
    from .routes import TEMPLATES
    user = _require_user(request)
    host = (host or "").strip().lower()
    if "//" in host:                      # accept a pasted URL, not just a host
        host, _ = db._host_and_path(host)
    try:
        def op():
            with db.writer() as conn:
                return own.issue(conn, user["id"], host, method)
        oid, token = db.with_retry(op)
    except own.ClaimError as exc:
        return TEMPLATES.TemplateResponse(
            request, "account.html",
            _account_context(request, user, error=str(exc)),
            status_code=400)
    issued = {"id": oid, "host": host, "token": token,
              "instruction": _INSTRUCTIONS.get(method, "")}
    return TEMPLATES.TemplateResponse(
        request, "account.html",
        _account_context(request, user, issued=issued))


@router.post("/account/domains/{ownership_id}/verify", include_in_schema=False)
def verify_domain(request: Request, ownership_id: int, token: str = Form(...)):
    from . import ownership as own
    from .routes import TEMPLATES
    user = _require_user(request)
    with db.connect(read_only=True) as conn:
        row = conn.execute("SELECT user_id FROM domain_ownership WHERE id=?",
                           (ownership_id,)).fetchone()
    if row is None or row["user_id"] != user["id"]:
        raise HTTPException(404, "No such claim")

    def op():
        with db.writer() as conn:
            result = own.verify_claim(conn, ownership_id, (token or "").strip())
            if result[0]:
                db.sync_owner_verified(conn)
            return result
    ok, detail = db.with_retry(op)
    if ok:
        return RedirectResponse("/account", status_code=302)
    return TEMPLATES.TemplateResponse(
        request, "account.html",
        _account_context(request, user, error="Not verified yet: %s" % detail),
        status_code=400)


# ------------------------------------------------------ owner edits

_KIND_LOOKUP = {
    "x402": ("services", "/services/%s"),
    "mcp": ("mcp_servers", "/mcp/servers/%s"),
    "a2a": ("a2a_agents", "/a2a/agents/%s"),
}
_LISTING_URL_COL = {
    "x402": "url",
    "mcp": "endpoint_url",
    "a2a": "COALESCE(endpoint_url, card_url)",
}
_FIELD_LABELS = {
    "name": "Name",
    "description": "Description",
    "category": "Category",
    "homepage_url": "Homepage URL",
    "documentation_url": "Documentation URL",
    "provider_name": "Provider name",
    "url": "Endpoint URL (must be on a host you have verified)",
    "mcp_url": "MCP endpoint URL (must be on a host you have verified)",
    "endpoint_url": "Endpoint URL (must be on a host you have verified)",
    "card_url": "Agent Card URL (must be on a host you have verified)",
}


def _load_editable(request: Request, kind: str, slug: str):
    """Return (user, listing_row, host, ownership) or raise.

    Authorisation is the whole point of this function: being signed in is not
    enough, the account must hold a verified claim on the host that serves
    this listing.
    """
    if kind not in _KIND_LOOKUP:
        raise HTTPException(404, "Unknown listing type")
    user = current_user(request)
    if user is None:
        return None, None, None, None
    table, _ = _KIND_LOOKUP[kind]
    with db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT *, %s AS _listing_url FROM %s WHERE slug=?"
            % (_LISTING_URL_COL[kind], table), (slug,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such listing")
        host, _ = db._host_and_path(row["_listing_url"] or "")
        owner = conn.execute(
            "SELECT * FROM domain_ownership WHERE host=? AND status='verified'",
            (host,)).fetchone()
    if owner is None or owner["user_id"] != user["id"]:
        raise HTTPException(403, "You have not verified control of %s" % host)
    return user, row, host, owner


def may_edit_listing(request: Request, kind: str, listing_url: str) -> bool:
    """Used by the detail templates to decide whether to show an Edit link."""
    user = current_user(request)
    if user is None:
        return False
    host, _ = db._host_and_path(listing_url or "")
    if not host:
        return False
    with db.connect(read_only=True) as conn:
        owner = conn.execute(
            "SELECT user_id FROM domain_ownership "
            "WHERE host=? AND status='verified'", (host,)).fetchone()
    return owner is not None and owner["user_id"] == user["id"]


def _edit_context(request, kind, slug, user, row, host, saved=None, error=None):
    fields = [
        {"name": f, "label": _FIELD_LABELS.get(f, f), "value": row[f],
         "multiline": f == "description"}
        for f in db._EDITABLE_FIELDS[kind]
    ]
    with db.connect(read_only=True) as conn:
        history = db.listing_edit_history(conn, kind, row["id"])
    for h in history:
        h["ago"] = _ago(h["applied_at"])
    return {
        "request": request, "user": user, "listing": row, "host": host,
        "fields": fields, "history": history, "saved": saved, "error": error,
        "back_url": _KIND_LOOKUP[kind][1] % slug,
    }


@router.get("/listings/{kind}/{slug}/edit", include_in_schema=False)
def edit_listing_form(request: Request, kind: str, slug: str):
    from .routes import TEMPLATES
    user, row, host, _owner = _load_editable(request, kind, slug)
    if user is None:
        return RedirectResponse(
            "/auth/github/login?next=/listings/%s/%s/edit" % (kind, slug),
            status_code=302)
    return TEMPLATES.TemplateResponse(
        request, "edit_listing.html",
        _edit_context(request, kind, slug, user, row, host))


@router.post("/listings/{kind}/{slug}/edit", include_in_schema=False)
async def edit_listing(request: Request, kind: str, slug: str):
    from .routes import TEMPLATES
    user, row, host, owner = _load_editable(request, kind, slug)
    if user is None:
        raise HTTPException(401, "Sign in first")
    form = await request.form()
    changes = {f: str(form.get(f) or "") for f in db._EDITABLE_FIELDS[kind]
               if f in form}

    with db.connect(read_only=True) as conn:
        verified_hosts = {r["host"] for r in conn.execute(
            "SELECT host FROM domain_ownership "
            "WHERE user_id=? AND status='verified'", (user["id"],))}

    def op():
        with db.writer() as conn:
            return db.apply_listing_edits(conn, kind, row["id"], user["id"],
                                          owner["id"], changes,
                                          verified_hosts=verified_hosts)
    applied, rejected = db.with_retry(op)

    with db.connect(read_only=True) as conn:
        table = _KIND_LOOKUP[kind][0]
        fresh = conn.execute(
            "SELECT *, %s AS _listing_url FROM %s WHERE id=?"
            % (_LISTING_URL_COL[kind], table), (row["id"],)).fetchone()
    return TEMPLATES.TemplateResponse(
        request, "edit_listing.html",
        _edit_context(request, kind, slug, user, fresh, host,
                      saved=applied or None,
                      error="; ".join(rejected) if rejected
                      else (None if applied else "Nothing changed.")))


@router.get("/api/v1/me", include_in_schema=False)
def whoami(request: Request):
    """Everything about the visitor, kept out of the cacheable HTML.

    Detail pages are deliberately CDN-cached, so rendering a name or an Edit
    button into them either shows the wrong thing or -- worse -- lets one
    visitor's personalised page be served to everyone else.
    """
    user = current_user(request)
    if user is None:
        payload = {"signed_in": False}
    else:
        with db.connect(read_only=True) as conn:
            hosts = [r["host"] for r in conn.execute(
                "SELECT host FROM domain_ownership "
                "WHERE user_id=? AND status='verified'", (user["id"],))]
        payload = {"signed_in": True, "login": user["login"],
                   "avatar_url": user["avatar_url"], "hosts": hosts}
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
