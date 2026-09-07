"""Domain ownership: proving an operator controls the host behind a listing.

Identity (who you are) comes from OAuth. Authorisation (what you may edit)
comes only from a token published on the host that serves the listing --
never from an email address, a repository, or a login name.

All three methods prove control of the *host*, so a verified claim covers
every listing on that host. `scope_path` is reserved for a future per-path
mode; it is always NULL today.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from typing import Any

import httpx

from . import db

log = logging.getLogger(__name__)

UA = "agent-tools.cloud-verifier/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_BODY = 64 * 1024

VERIFY_FILE = "/.well-known/agent-tools-verify.txt"
DNS_PREFIX = "_agent-tools."
DNS_MARKER = "atc-verify="
TOKEN_FIELD = "agentToolsVerify"

# Documents an operator already publishes; accepting the token here means the
# 96.1% of x402 services and 95.8% of A2A agents that have one add a field
# instead of a file.
DESCRIPTOR_PATHS = (
    "/.well-known/x402",
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
)

METHODS = ("descriptor", "wellknown_file", "dns_txt")
MAX_FAILS = 3   # consecutive misses before a verified claim is revoked


class ClaimError(Exception):
    """A claim cannot be issued (shared host, already owned, bad method)."""


def new_token() -> str:
    return "atc_" + secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- probing

def _find_token(obj: Any, token: str) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == TOKEN_FIELD and isinstance(v, str) and v.strip() == token:
                return True
            if _find_token(v, token):
                return True
    elif isinstance(obj, list):
        return any(_find_token(x, token) for x in obj)
    return False


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        return client.get(url)
    except Exception as exc:
        log.debug("verify fetch failed %s: %s", url, exc)
        return None


def check_descriptor(host: str, token: str) -> tuple[bool, str]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for path in DESCRIPTOR_PATHS:
            url = "https://%s%s" % (host, path)
            r = _get(c, url)
            if r is None or r.status_code >= 400:
                continue
            body = r.text[:_MAX_BODY]
            try:
                if _find_token(json.loads(body), token):
                    return True, path
            except ValueError:
                continue
    return False, "%s not found in %s" % (TOKEN_FIELD, ", ".join(DESCRIPTOR_PATHS))


def check_wellknown_file(host: str, token: str) -> tuple[bool, str]:
    url = "https://%s%s" % (host, VERIFY_FILE)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "text/plain"}) as c:
        r = _get(c, url)
    if r is None:
        return False, "fetch failed"
    if r.status_code >= 400:
        return False, "HTTP %d" % r.status_code
    return (token in r.text[:_MAX_BODY]), VERIFY_FILE


def check_dns_txt(host: str, token: str) -> tuple[bool, str]:
    try:
        import dns.resolver
    except ImportError:                                   # pragma: no cover
        return False, "dnspython not installed"
    name = DNS_PREFIX + host
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=8.0)
    except Exception as exc:
        return False, "%s: %s" % (name, exc)
    want = DNS_MARKER + token
    for rec in answer:
        txt = b"".join(getattr(rec, "strings", [])).decode("utf-8", "replace")
        if want in txt:
            return True, name
    return False, "%s has TXT but not %s" % (name, want)


_CHECKS = {
    "descriptor": check_descriptor,
    "wellknown_file": check_wellknown_file,
    "dns_txt": check_dns_txt,
}


def probe(method: str, host: str, token: str) -> tuple[bool, str]:
    check = _CHECKS.get(method)
    if check is None:
        return False, "unknown method %r" % method
    return check(host, token)


# ------------------------------------------------------------- claim flow

def issue(conn: sqlite3.Connection, user_id: int, host: str,
          method: str) -> tuple[int, str]:
    """Open a pending claim. Returns (ownership_id, plaintext token).

    The token is shown once; only its hash is stored, so a database read
    during the pending window still cannot forge a claim.
    """
    host = (host or "").strip().lower()
    if not host:
        raise ClaimError("missing host")
    if method not in METHODS:
        raise ClaimError("unknown method: %s" % method)
    if db.is_shared_host(conn, host):
        raise ClaimError(
            "%s hosts listings by unrelated authors; self-claim is not "
            "available there" % host
        )
    # Deliberately no "already claimed" check: domains change hands, and the
    # only thing that matters is who can publish on the host right now.

    token = new_token()
    now = int(time.time())
    conn.execute(
        "DELETE FROM domain_ownership "
        "WHERE user_id=? AND host=? AND status='pending'",
        (user_id, host),
    )
    cur = conn.execute(
        "INSERT INTO domain_ownership(user_id, host, scope_path, method, "
        "token_hash, status, created_at) VALUES (?,?,NULL,?,?,'pending',?)",
        (user_id, host, method, token_hash(token), now),
    )
    conn.commit()
    return int(cur.lastrowid), token


def verify_claim(conn: sqlite3.Connection, ownership_id: int,
                 token: str) -> tuple[bool, str]:
    """Check a pending claim. The caller supplies the token it was shown."""
    row = conn.execute(
        "SELECT * FROM domain_ownership WHERE id=?", (ownership_id,)
    ).fetchone()
    if row is None:
        return False, "no such claim"
    if row["token_hash"] != token_hash(token):
        return False, "token mismatch"
    ok, detail = probe(row["method"], row["host"], token)
    now = int(time.time())
    if ok:
        # Whoever can publish on the host today owns it. If someone else held
        # it, either the domain changed hands or it was taken over -- both are
        # things the previous holder needs to hear about, not reasons to refuse.
        # Any earlier verified claim steps aside, including the same
        # account's -- re-verifying with a different method is normal, and one
        # host may only have one live claim.
        previous = owner_of_host(conn, row["host"])
        if previous is not None and previous["id"] != ownership_id:
            conn.execute(
                "UPDATE domain_ownership SET status='revoked', last_checked=? "
                "WHERE id=?", (now, previous["id"]))
        displaced = None
        if previous is not None and previous["user_id"] != row["user_id"]:
            displaced = previous
            log.warning("ownership of %s moved from user %s to user %s",
                        row["host"], previous["user_id"], row["user_id"])
        # Only now is the plaintext stored: it is published on their own site
        # from this moment on, and re-checks need to look for it again.
        conn.execute(
            "UPDATE domain_ownership SET status='verified', token=?, "
            "verified_at=?, last_checked=?, fail_count=0 WHERE id=?",
            (token, now, now, ownership_id),
        )
        if displaced is not None:
            _notify_displaced(conn, row["host"], displaced["user_id"],
                              row["user_id"], row["method"])
    else:
        conn.execute(
            "UPDATE domain_ownership SET last_checked=?, "
            "fail_count=fail_count+1 WHERE id=?",
            (now, ownership_id),
        )
    conn.commit()
    return ok, detail


def recheck(conn: sqlite3.Connection) -> tuple[int, int]:
    """Re-probe verified claims; revoke after MAX_FAILS consecutive misses.

    Domains change hands, so a badge that is never re-checked is a lie with a
    timestamp on it.
    """
    kept = revoked = 0
    now = int(time.time())
    rows = list(conn.execute(
        "SELECT * FROM domain_ownership WHERE status='verified'"))
    for row in rows:
        token = row["token"]
        ok = bool(token) and probe(row["method"], row["host"], token)[0]
        if ok:
            conn.execute(
                "UPDATE domain_ownership SET last_checked=?, fail_count=0 "
                "WHERE id=?", (now, row["id"]))
            kept += 1
        elif row["fail_count"] + 1 >= MAX_FAILS:
            conn.execute(
                "UPDATE domain_ownership SET status='revoked', last_checked=?, "
                "fail_count=fail_count+1 WHERE id=?", (now, row["id"]))
            revoked += 1
        else:
            conn.execute(
                "UPDATE domain_ownership SET last_checked=?, "
                "fail_count=fail_count+1 WHERE id=?", (now, row["id"]))
            kept += 1
    conn.commit()
    return kept, revoked


# ------------------------------------------------------------- lookups

def owner_of_host(conn: sqlite3.Connection, host: str):
    return conn.execute(
        "SELECT * FROM domain_ownership WHERE host=? AND status='verified'",
        ((host or "").lower(),),
    ).fetchone()


def hosts_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
    return [
        r["host"]
        for r in conn.execute(
            "SELECT host FROM domain_ownership "
            "WHERE user_id=? AND status='verified' ORDER BY host",
            (user_id,),
        )
    ]


def owner_for_url(conn: sqlite3.Connection, url: str):
    """The verified claim covering this listing URL, if any."""
    host, _ = db._host_and_path(url or "")
    if not host:
        return None
    return owner_of_host(conn, host)


def may_edit(conn: sqlite3.Connection, user_id: int, url: str) -> bool:
    owner = owner_for_url(conn, url)
    return bool(owner) and owner["user_id"] == user_id


def _notify_displaced(conn: sqlite3.Connection, host: str, old_user_id: int,
                      new_user_id: int, method: str) -> None:
    """Tell the previous holder that their claim on `host` was taken over.

    Never blocks the takeover: if the mail cannot go out we still log it, so a
    dead mailbox does not leave the directory pointing at the wrong owner.
    """
    try:
        old = conn.execute("SELECT login, email FROM users WHERE id=?",
                           (old_user_id,)).fetchone()
        new = conn.execute("SELECT login FROM users WHERE id=?",
                           (new_user_id,)).fetchone()
        if old is None or not old["email"]:
            log.warning("displaced owner %s of %s has no email on file",
                        old_user_id, host)
            return
        from . import mailer
        who = (new["login"] if new else "another account")
        subject = "Your claim on %s was transferred" % host
        text = (
            "Someone proved control of %s and now holds the verified claim "
            "on it.\n\n"
            "GitHub account: %s\n"
            "Method: %s\n\n"
            "We verify ownership by asking for a token published on the host "
            "itself, so this means the token we issued to them was reachable "
            "at %s. If you still control that domain, sign in and claim it "
            "again -- proving control is all it takes. If you did not expect "
            "this, check who can publish on that host.\n\n"
            "Edits you already made to listings there have been kept.\n\n"
            "https://agent-tools.cloud/account\n"
        ) % (host, who, method, host)
        html = (
            "<p>Someone proved control of <b>%s</b> and now holds the verified "
            "claim on it.</p><ul><li>GitHub account: <b>%s</b></li>"
            "<li>Method: <code>%s</code></li></ul>"
            "<p>We verify ownership by asking for a token published on the host "
            "itself, so this means the token we issued to them was reachable at "
            "%s. If you still control that domain, "
            "<a href=\"https://agent-tools.cloud/account\">sign in and claim it "
            "again</a> &mdash; proving control is all it takes. If you did not "
            "expect this, check who can publish on that host.</p>"
            "<p>Edits you already made to listings there have been kept.</p>"
        ) % (host, who, method, host)
        mailer._send(old["email"], subject, text, html)
    except Exception as exc:                       # notification is best-effort
        log.warning("could not notify displaced owner of %s: %s", host, exc)
