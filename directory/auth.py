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
import html
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit
import uuid

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import db, limits, owner_declarations, owner_lifecycle, ownership

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
# Separate audience so a hub-readable credential can never act as a main-site session.
HUB_COOKIE = "atc_hub"
HUB_AUDIENCE = "hub.agent-tools.cloud"
ARENA_AUDIENCE = "radeon-arena.com"
ARENA_PUBLIC_URL = "https://radeon-arena.com"

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


def _hub_domain() -> str | None:
    return os.getenv("HUB_COOKIE_DOMAIN", ".agent-tools.cloud").strip() or None


def _set_hub_cookie(resp, user_id: int) -> None:
    domain = _hub_domain()
    if not domain:
        return
    resp.set_cookie(HUB_COOKIE, sign({"uid": user_id, "aud": HUB_AUDIENCE},
                                     SESSION_MAX_AGE),
                    max_age=SESSION_MAX_AGE, path="/", domain=domain,
                    httponly=True, secure=True, samesite="lax")


def _clear_hub_cookie(resp) -> None:
    domain = _hub_domain()
    if domain:
        resp.delete_cookie(HUB_COOKIE, path="/", domain=domain)


def _arena_secret() -> str:
    return (os.getenv("RADEON_ARENA_ACCOUNT_SECRET") or "").strip()


def _arena_assertion(payload: dict, max_age: int = 120) -> str:
    secret = _arena_secret()
    if not secret:
        raise RuntimeError("RADEON_ARENA_ACCOUNT_SECRET is not set")
    body = json.dumps({**payload, "aud": ARENA_AUDIENCE,
                       "exp": int(time.time()) + max_age},
                      separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"),
                         hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


# ------------------------------------------------------------- helpers

def _safe_next(target: str | None) -> str:
    """Only same-site relative paths, so the callback cannot be used to
    bounce a visitor to an attacker's site. The hub is the one allowed
    sibling, so sign-in from it can return there."""
    if target and target.startswith("https://"):
        hub = os.getenv("HUB_PUBLIC_URL", "https://hub.agent-tools.cloud").rstrip("/")
        parts = urlsplit(target)
        if (hub and parts.scheme == "https" and not parts.username
                and not parts.password and not parts.port
                and "%s://%s" % (parts.scheme, parts.netloc) == hub):
            return target
        return "/"
    if not target or not target.startswith("/") or target.startswith("//") \
            or "\\" in target:
        return "/"
    return target


def _safe_arena_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") \
            or "\\" in target:
        return "/me"
    return target[:500]


def _redirect_uri(request: Request) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "https://agent-tools.cloud").rstrip("/")
    return base + "/auth/github/callback"


def _uid_from_bearer(request: Request) -> int | None:
    """Resolve `Authorization: Bearer <key>` to an account id.

    A key is a second way to present an identity, not a second set of powers:
    whatever it reaches is still gated by domain_ownership further down.
    """
    scheme, _, raw = (request.headers.get("authorization") or "").partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw:
        return None
    try:
        with db.connect(read_only=True) as conn:
            row = db.api_key_row(conn, raw)
    except Exception:
        return None
    if row is None:
        return None
    db.touch_api_key(row["id"], row["last_used_at"])
    return int(row["user_id"])


def current_user(request: Request):
    """The caller's user row, or None. Never implies any edit right."""
    data = unsign(request.cookies.get(SESSION_COOKIE))
    uid = data.get("uid") if data and "aud" not in data else None
    if not uid:
        uid = _uid_from_bearer(request)
    if not uid:
        return None
    try:
        with db.connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (uid,)).fetchone()
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
    _set_hub_cookie(resp, user_id)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.get("/auth/hub", include_in_schema=False)
def hub_handoff(request: Request, next: str = ""):
    """Grant the hub credential to an already signed-in visitor, or sign in first."""
    target = _safe_next(next or os.getenv("HUB_PUBLIC_URL",
                                          "https://hub.agent-tools.cloud") + "/my")
    user = current_user(request) if unsign(
        request.cookies.get(SESSION_COOKIE)) else None
    if user is None:
        return RedirectResponse(
            "/auth/github/login?" + urlencode(
                {"next": "/auth/hub?" + urlencode({"next": target})}),
            status_code=302)
    resp = RedirectResponse(target, status_code=302)
    _set_hub_cookie(resp, int(user["id"]))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@router.get("/auth/radeon-arena", include_in_schema=False)
def radeon_arena_handoff(request: Request, next: str = "/me"):
    """Use the existing GitHub login, then POST a one-use identity assertion."""
    if not _arena_secret():
        raise HTTPException(503, "Radeon Arena sign-in is not configured")
    user = current_user(request) if unsign(
        request.cookies.get(SESSION_COOKIE)) else None
    safe_next = _safe_arena_next(next)
    if user is None:
        return RedirectResponse(
            "/auth/github/login?" + urlencode({
                "next": "/auth/radeon-arena?" + urlencode({"next": safe_next})}),
            status_code=302)
    assertion = _arena_assertion({
        "sub": f"github:{user['provider_uid']}",
        "login": user["login"],
        "email": user["email"] if user["email_verified"] else None,
        "avatar_url": user["avatar_url"],
        "next": safe_next,
        "jti": secrets.token_urlsafe(24),
    })
    escaped = html.escape(assertion, quote=True)
    body = ("<!doctype html><meta charset=utf-8><title>Continue to Radeon Arena</title>"
            "<p>Continuing to Radeon Arena…</p>"
            f"<form id=f method=post action={ARENA_PUBLIC_URL}/api/auth/sso>"
            f"<input type=hidden name=assertion value=\"{escaped}\"></form>"
            "<script>document.getElementById('f').submit()</script>"
            "<noscript><button form=f>Continue</button></noscript>")
    return HTMLResponse(body, headers={"Cache-Control": "private, no-store",
                                       "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; form-action https://radeon-arena.com; base-uri 'none'",
                                       "X-Robots-Tag": "noindex, nofollow"})


@router.post("/auth/logout", include_in_schema=False)
@router.get("/auth/logout", include_in_schema=False)
def logout(next: str = "/"):
    resp = RedirectResponse(_safe_next(next), status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    _clear_hub_cookie(resp)
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
        managed = owner_lifecycle.managed_listings(conn, user["id"])
    for r in verified:
        r["verified_ago"] = _ago(r["verified_at"])
        r["listing_count"] = counts.get(r["host"], 0)
    return {
        "request": request,
        "user": user,
        "verified": verified,
        "pending": [r for r in rows if r["status"] == "pending"],
        "managed_listings": managed,
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
    "method": "Primary HTTP method",
    "price_usdc": "Primary price (USDC)",
    "resources": "Resources (JSON array)",
    "well_known_url": "x402 descriptor URL",
    "openapi_url": "OpenAPI URL",
}


def _require_same_origin_write(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not unsign(request.cookies.get(SESSION_COOKIE)):
        return

    def origin(value):
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                return None
            return (parsed.scheme, parsed.hostname.lower(),
                    parsed.port or (443 if parsed.scheme == "https" else 80))
        except ValueError:
            return None

    expected = origin(os.getenv("PUBLIC_BASE_URL", "https://agent-tools.cloud"))
    supplied = origin(request.headers.get("origin") or request.headers.get("referer") or "")
    if expected is None or supplied != expected:
        raise HTTPException(403, "Cookie-authenticated writes require the same origin",
                            headers={"Cache-Control": "private, no-store"})


def _load_editable(request: Request, kind: str, slug: str):
    """Return (user, listing_row, host, ownership) or raise.

    Authorisation is the whole point of this function: being signed in is not
    enough, the account must hold a verified claim on the host that serves
    this listing.
    """
    if kind not in _KIND_LOOKUP:
        raise HTTPException(404, "Unknown listing type")
    with db.connect(read_only=True) as conn:
        suppressed = db.find_privacy_suppression_any(
            conn, kind, {"slug": slug})
    if suppressed is not None:
        raise HTTPException(
            404, "No such listing",
            headers={"X-Agent-Tools-Privacy-Suppressed": "1"})
    user = current_user(request)
    if user is None:
        return None, None, None, None
    _require_same_origin_write(request)
    table, _ = _KIND_LOOKUP[kind]
    with db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT *, %s AS _listing_url FROM %s WHERE slug=?"
            % (_LISTING_URL_COL[kind], table), (slug,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such listing")
        host, _ = db._host_and_path(row["_listing_url"] or "")
        owner = conn.execute(
            "SELECT * FROM domain_ownership WHERE host=? AND status='verified' "
            "AND scope_path IS NULL",
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
            "WHERE host=? AND status='verified' AND scope_path IS NULL", (host,)).fetchone()
    return owner is not None and owner["user_id"] == user["id"]


def editable_values(kind, row):
    if kind == "x402":
        return owner_declarations.editable_fields(row)
    return {name: row[name] for name in db._EDITABLE_FIELDS[kind]}


async def verify_owner_listing(request, kind, row, user, owner):
    if kind != "x402":
        raise HTTPException(404, "Interface declaration verification is available for x402 listings only")
    if not owner_declarations.stored(row):
        raise HTTPException(422, "Save an interface declaration before verification")
    for identity, scope, windows in (
        ("owner:%s" % user["id"], "owner-declaration", (("minute", 2, 60), ("day", 20, 86400))),
        (limits.client_ip_from_request(request), "owner-declaration-ip", (("minute", 6, 60), ("day", 60, 86400))),
    ):
        state = limits.check_ip_limits(identity, scope, windows, db_path=db.DEFAULT_DB_PATH)
        if state:
            limits.raise_rate_limited(state, "Too many interface verification requests. Try again later.")
    try:
        return await run_in_threadpool(owner_declarations.verify_listing, row["id"], user["id"], owner["id"])
    except PermissionError:
        raise HTTPException(403, "Verified ownership or account status changed") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def _edit_context(request, kind, slug, user, row, host, saved=None, error=None):
    values = editable_values(kind, row)
    fields = [
        {"name": f, "label": _FIELD_LABELS.get(f, f), "value": values[f],
         "multiline": f == "description"}
        for f in db._EDITABLE_FIELDS[kind] if f not in owner_declarations.FIELDS
    ]
    declaration = owner_declarations.public_state(row) if kind == "x402" else None
    interface_fields = []
    if kind == "x402":
        for name in owner_declarations.FIELDS:
            value = values[name]
            if name == "resources":
                value = json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ""
            interface_fields.append({"name": name, "label": _FIELD_LABELS[name], "value": value})
    with db.connect(read_only=True) as conn:
        history = db.listing_edit_history(conn, kind, row["id"])
    for h in history:
        h["ago"] = _ago(h["applied_at"])
    return {
        "request": request, "user": user, "listing": row, "host": host,
        "fields": fields, "history": history, "saved": saved, "error": error,
        "interface_fields": interface_fields, "declaration": declaration,
        "declaration_blocker": owner_declarations.BLOCKERS.get((declaration or {}).get("blocker")),
        "verify_url": "/listings/%s/%s/verify" % (kind, slug),
        "lifecycle_url": "/listings/%s/%s/manage" % (kind, slug),
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
        _edit_context(request, kind, slug, user, row, host),
        headers={"Cache-Control": "private, no-store"})


@router.post("/listings/{kind}/{slug}/edit", include_in_schema=False)
async def edit_listing(request: Request, kind: str, slug: str):
    from .routes import TEMPLATES
    user, row, host, owner = _load_editable(request, kind, slug)
    if user is None:
        raise HTTPException(401, "Sign in first")
    form = await request.form()
    changes = {f: str(form.get(f) or "") for f in db._EDITABLE_FIELDS[kind]
               if f in form}
    if kind == "x402" and not owner_declarations.stored(row):
        changes = {name: value for name, value in changes.items()
                   if name not in owner_declarations.FIELDS or value.strip()}

    def op():
        with db.writer(immediate=True) as conn:
            return db.apply_listing_edits(conn, kind, row["id"], user["id"],
                                          owner["id"], changes, via="web")
    try:
        applied, rejected = db.with_retry(op)
    except PermissionError:
        raise HTTPException(403, "Verified ownership or account status changed") from None
    except ValueError as exc:
        applied, rejected = [], [str(exc)]

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
                      else (None if applied else "Nothing changed.")),
        status_code=422 if rejected and not applied else 200,
        headers={"Cache-Control": "private, no-store"})


@router.post("/listings/{kind}/{slug}/verify", include_in_schema=False)
async def verify_listing_form(request: Request, kind: str, slug: str):
    user, row, _host, owner = _load_editable(request, kind, slug)
    if user is None:
        raise HTTPException(401, "Sign in first")
    await verify_owner_listing(request, kind, row, user, owner)
    return RedirectResponse("/listings/%s/%s/edit" % (kind, slug), status_code=303,
                            headers={"Cache-Control": "private, no-store"})


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


def lifecycle_state(request, kind, slug, user):
    try:
        with db.connect(read_only=True) as conn:
            state = owner_lifecycle.state(conn, kind, slug, user["id"])
    except (owner_lifecycle.LifecycleBlocked, ownership.OwnershipBlocked) as exc:
        lifecycle_error(exc)
    if request.cookies.get(SESSION_COOKIE):
        state["csrf_token"] = sign({"purpose": "lifecycle", "uid": user["id"],
                                    "kind": kind, "slug": slug, "version": state["version"]}, 600)
    return state


def lifecycle_error(exc):
    code = getattr(exc, "code", "operation_conflict")
    messages = {
        "control_unavailable": "The published verification marker could not be confirmed. No changes were made; try again later.",
        "not_verified_owner": "This account does not hold a verified claim covering the listing.",
        "claim_changed": "The domain claim changed. Refresh the listing before retrying.",
        "credential_changed": "The API key was revoked or changed during the request.",
        "proof_expired": "The control check expired. Refresh the listing before retrying.",
        "listing_changed": "The listing changed. Refresh this page before retrying.",
        "service_unverified": "The service could not be verified. The listing remains withdrawn.",
        "shared_endpoint": "An endpoint or source identity is shared with another listing. Staff review is required.",
        "merge_target_in_use": "Other merged listings point to this primary. Restore those listings before withdrawing it.",
        "staff_review_required": "This withdrawal requires staff review and cannot be restored here.",
        "lifecycle_unavailable": "Listing management is temporarily unavailable. No changes were made.",
        "idempotency_conflict": "This request identifier belongs to another operation. Refresh before retrying.",
    }
    status = (503 if code in ("control_unavailable", "lifecycle_unavailable") else
              409 if code == "proof_expired" else 403 if isinstance(exc, ownership.OwnershipBlocked) else
              404 if code == "not_found" else 422 if code.startswith("invalid_")
              or code == "version_required" else 409)
    raise HTTPException(status, {"error": code, "message": messages.get(code, "The operation was not applied.")},
                        headers={"Cache-Control": "private, no-store"}) from None


def _lifecycle_request_guard(request, user, kind, slug, version, csrf_token):
    credential_id = None
    if request.cookies.get(SESSION_COOKIE):
        _require_same_origin_write(request)
        payload = unsign(csrf_token)
        if not payload or any(payload.get(key) != value for key, value in {
                "purpose": "lifecycle", "uid": user["id"], "kind": kind,
                "slug": slug, "version": version}.items()):
            raise HTTPException(403, {"error": "csrf_invalid"})
    else:
        scheme, _, raw = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            with db.connect(read_only=True) as conn:
                key = db.api_key_row(conn, raw.strip())
            if key is None or int(key["user_id"]) != int(user["id"]):
                raise HTTPException(401, {"error": "invalid_api_key"})
            credential_id = int(key["id"])
    for identity, scope, windows in (
            ("owner:%s" % user["id"], "owner-lifecycle", (("minute", 2, 60), ("day", 20, 86400))),
            (limits.client_ip_from_request(request), "owner-lifecycle-ip", (("minute", 6, 60), ("day", 60, 86400)))):
        state = limits.check_ip_limits(identity, scope, windows, db_path=db.DEFAULT_DB_PATH)
        if state:
            limits.raise_rate_limited(state, "Too many listing management requests. Try again later.")
    return credential_id


async def perform_lifecycle(request, user, kind, slug, action, version, request_id,
                            confirmed, source_slug=None, csrf_token=None,
                            target_slug=None, target_version=None):
    if confirmed is not True:
        raise HTTPException(422, {"error": "confirmation_required"})
    credential_id = _lifecycle_request_guard(request, user, kind, slug, version, csrf_token)
    with db.connect(read_only=True) as conn:
        try:
            owner_lifecycle.state(conn, kind, slug, user["id"])
        except (owner_lifecycle.LifecycleBlocked, ownership.OwnershipBlocked) as exc:
            lifecycle_error(exc)
    operations = {"retire": owner_lifecycle.retire, "restore": owner_lifecycle.restore,
                  "migration": owner_lifecycle.request_migration, "merge": owner_lifecycle.merge}
    if action not in operations:
        raise HTTPException(404, {"error": "not_found"})
    arguments = (kind, slug, user["id"], version, request_id)
    if action == "migration":
        arguments = (kind, slug, user["id"], source_slug, version, request_id)
    if action == "merge":
        arguments = (kind, slug, user["id"], version, request_id, target_slug, target_version)
    try:
        result = await run_in_threadpool(operations[action], *arguments, credential_id=credential_id)
    except (owner_lifecycle.LifecycleBlocked, ownership.OwnershipBlocked) as exc:
        lifecycle_error(exc)
    except db.RetiredListingError:
        raise HTTPException(409, {"error": "listing_changed"}) from None
    except ValueError:
        raise HTTPException(409, {"error": "operation_conflict"}) from None
    return result


@router.get("/listings/{kind}/{slug}/manage", include_in_schema=False)
def manage_listing(request: Request, kind: str, slug: str):
    from .routes import TEMPLATES
    user = current_user(request)
    if user is None:
        return RedirectResponse("/auth/github/login?next=/listings/%s/%s/manage" % (kind, slug),
                                status_code=302, headers={"Cache-Control": "private, no-store"})
    state = lifecycle_state(request, kind, slug, user)
    if "csrf_token" not in state:
        state["csrf_token"] = sign({"purpose": "lifecycle", "uid": user["id"],
                                   "kind": kind, "slug": slug, "version": state["version"]}, 600)
    return TEMPLATES.TemplateResponse(request, "manage_listing.html", {
        "request": request, "user": user, "state": state, "request_id": uuid.uuid4().hex,
        "back_url": _KIND_LOOKUP[kind][1] % slug},
        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


@router.post("/listings/{kind}/{slug}/manage", include_in_schema=False)
async def manage_listing_action(request: Request, kind: str, slug: str):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    form = await request.form()
    if not request.cookies.get(SESSION_COOKIE):
        raise HTTPException(403, {"error": "cookie_session_required"})
    target_slug, target_version = None, None
    if form.get("action") == "merge":
        selected = str(form.get("merge_target") or "").split(":", 1)
        if len(selected) == 2:
            target_slug, target_version = selected
    try:
        await perform_lifecycle(request, user, kind, slug, form.get("action"), form.get("version"),
                                form.get("request_id"), form.get("confirm") == "yes",
                                form.get("source_slug"), form.get("csrf_token"),
                                target_slug, target_version)
    except HTTPException as exc:
        if exc.status_code not in (409, 422, 429, 503):
            raise
        from .routes import TEMPLATES
        state = lifecycle_state(request, kind, slug, user)
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        return TEMPLATES.TemplateResponse(request, "manage_listing.html", {
            "request": request, "user": user, "state": state, "request_id": uuid.uuid4().hex,
            "error": detail.get("message") or detail.get("error"),
            "back_url": _KIND_LOOKUP[kind][1] % slug}, status_code=exc.status_code,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})
    return RedirectResponse("/listings/%s/%s/manage" % (kind, slug), status_code=303,
                            headers={"Cache-Control": "private, no-store"})
