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
import http.client
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import db

log = logging.getLogger(__name__)

UA = "agent-tools.cloud-verifier/0.1 (+https://agent-tools.cloud)"
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0
_MAX_BODY = 64 * 1024
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")

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

# The same four steps are quoted by every surface that has to refuse a write:
# the REST 409, the REST already_listed, and the MCP register tool. Keeping one
# copy is the only way they stay true to each other.
API_FLOW = {
    "message": ("No browser needed. Mint a key, publish the token on your host, "
                "verify, then edit. The key identifies you; the published token "
                "is what proves the host is yours."),
    "steps": [
        {"method": "POST", "url": "https://agent-tools.cloud/api/v1/keys",
         "note": "No credentials needed. Store the key; it is shown once."},
        {"method": "POST", "url": "https://agent-tools.cloud/api/v1/claims",
         "auth": "Authorization: Bearer <api_key>",
         "body": {"host": "<your-host>", "method": "wellknown_file"},
         "note": "Returns a token and where to publish it."},
        {"method": "POST", "url": "https://agent-tools.cloud/api/v1/claims/<claim_id>/verify",
         "auth": "Authorization: Bearer <api_key>",
         "body": {"token": "<the token you just published>"},
         "note": "Returns every listing on the host, with its edit URL."},
        {"method": "PATCH", "url": "https://agent-tools.cloud/api/v1/listings/<kind>/<slug>",
         "auth": "Authorization: Bearer <api_key>",
         "note": "Send only the fields you want to change."},
    ],
    "docs": "https://agent-tools.cloud/docs/claim",
}
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


class UnsafeTarget(ValueError):
    """A claim target is not a public HTTPS hostname."""


def normalize_host(value: str) -> str:
    raw = (value or "").strip().rstrip(".").lower()
    if any(char in raw for char in "/\\@?#:"):
        raise UnsafeTarget("host must be a DNS name without scheme, path, credentials, or port")
    try:
        host = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeTarget("host is not a valid DNS name") from exc
    if len(host) > 253 or "." not in host:
        raise UnsafeTarget("host must be a fully-qualified DNS name")
    labels = host.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise UnsafeTarget("host is not a valid DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeTarget("IP literals are not valid claim hosts")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeTarget("local hostnames are not valid claim hosts")
    return host


def _public_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeTarget("host does not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise UnsafeTarget("host resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafeTarget("host resolves to a non-public address")
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise UnsafeTarget("host has no usable address")
    return addresses


class _FetchResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self.body = body
        self.headers = headers


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(
            host, 443, timeout=_CONNECT_TIMEOUT,
            context=ssl.create_default_context())
        self._verified_address = address

    def connect(self):
        sock = socket.create_connection(
            (self._verified_address, self.port), _CONNECT_TIMEOUT,
            self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(_READ_TIMEOUT)


def _read_https(host: str, path: str, accept: str) -> _FetchResponse:
    last_error: Exception | None = None
    for address in _public_addresses(host):
        conn = _PinnedHTTPSConnection(host, address)
        try:
            conn.request("GET", path, headers={
                "User-Agent": UA,
                "Accept": accept,
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = conn.getresponse()
            body = response.read(_MAX_BODY + 1)
            if len(body) > _MAX_BODY:
                raise UnsafeTarget("verification response exceeds 64 KiB")
            headers = {key.lower(): value for key, value in response.getheaders()}
            return _FetchResponse(response.status, body, headers)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            conn.close()
    if last_error is not None:
        raise last_error
    raise UnsafeTarget("host has no reachable public address")


def _fetch(host: str, path: str, accept: str) -> _FetchResponse:
    url = "https://%s%s" % (normalize_host(host), path)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise UnsafeTarget("verification redirects must use public HTTPS URLs")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeTarget("verification redirect has an invalid port") from exc
        if port not in (None, 443):
            raise UnsafeTarget("verification redirects may only use port 443")
        target_host = normalize_host(parsed.hostname or "")
        target_path = parsed.path or "/"
        if parsed.query:
            target_path += "?" + parsed.query
        response = _read_https(target_host, target_path, accept)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        if redirect_count >= _MAX_REDIRECTS:
            raise UnsafeTarget("too many verification redirects")
        url = urljoin(url, location)
    raise UnsafeTarget("too many verification redirects")


def _get(host: str, path: str, accept: str) -> _FetchResponse | None:
    try:
        return _fetch(host, path, accept)
    except Exception as exc:
        log.debug("verify fetch failed https://%s%s: %s", host, path, exc)
        return None


def check_descriptor(host: str, token: str) -> tuple[bool, str]:
    for path in DESCRIPTOR_PATHS:
        response = _get(host, path, "application/json")
        if response is None or response.status_code >= 400:
            continue
        try:
            body = response.body.decode("utf-8", "replace")
            if _find_token(json.loads(body), token):
                return True, path
        except ValueError:
            continue
    return False, "%s not found in %s" % (TOKEN_FIELD, ", ".join(DESCRIPTOR_PATHS))


def check_wellknown_file(host: str, token: str) -> tuple[bool, str]:
    response = _get(host, VERIFY_FILE, "text/plain")
    if response is None:
        return False, "fetch failed"
    if response.status_code >= 400:
        return False, "HTTP %d" % response.status_code
    body = response.body.decode("utf-8", "replace")
    return (token in body), VERIFY_FILE


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
    try:
        host = normalize_host(host)
    except UnsafeTarget as exc:
        return False, str(exc)
    return check(host, token)


# ------------------------------------------------------------- claim flow

def issue(conn: sqlite3.Connection, user_id: int, host: str,
          method: str) -> tuple[int, str]:
    """Open a pending claim. Returns (ownership_id, plaintext token).

    The token is shown once; only its hash is stored, so a database read
    during the pending window still cannot forge a claim.
    """
    try:
        host = normalize_host(host)
    except UnsafeTarget as exc:
        raise ClaimError(str(exc)) from exc
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
