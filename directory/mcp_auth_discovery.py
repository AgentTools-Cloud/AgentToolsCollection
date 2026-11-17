"""Anonymous checks of the authorization discovery of an MCP server that answers 401.

The MCP authorization spec has a protected server answer an unauthenticated
request with HTTP 401 and a ``WWW-Authenticate: Bearer`` challenge, publish
OAuth 2.0 protected-resource metadata (RFC 9728) naming its authorization
servers, and those servers publish their own metadata (RFC 8414 or OpenID
discovery) with PKCE S256. All of it is readable without credentials, so the
daily health job reads it weekly for servers that answered 401, and readiness
scores what it found (listing_readiness.assess_mcp).

Every URL here may come from the server's own header or metadata, so requests
go through public_http only (public addresses, redirects included).
"""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import public_http

VERSION = 1
RECHECK_DAYS = 7
CHECKS = ("challenge", "resource_metadata", "server_metadata", "pkce", "registration")
UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
_BEARER = re.compile(r"(?:^|,)\s*bearer\b", re.I)
_RESOURCE_METADATA = re.compile(r'resource_metadata\s*=\s*"?([^",\s]+)', re.I)
_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
_MAX_BODY = 256 * 1024
# Hosted platforms share one authorization server (and sometimes one metadata
# URL) across thousands of servers: keep what was found for a few hours.
# Failures are not kept, so one timeout cannot mark a whole platform.
_CACHE_TTL = 6 * 3600
_CACHE_MAX = 20000
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def due(checked, listing_id, now: int) -> bool:
    """Weekly, each listing on its own weekday so the reads spread over the week."""
    if not checked:
        return True
    age = int(now) - int(checked)
    if age >= (RECHECK_DAYS + 1) * 86400:
        return True
    return age >= 43200 and (int(now) // 86400 + int(listing_id)) % RECHECK_DAYS == 0


def _origin_path(url: str) -> tuple | None:
    try:
        p = urlsplit(str(url or "").strip())
        scheme = p.scheme.lower()
        port = p.port or {"http": 80, "https": 443}.get(scheme)
    except ValueError:
        return None
    if scheme not in ("http", "https") or not p.hostname:
        return None
    return scheme, p.hostname.lower(), port, p.path.rstrip("/")


def resource_matches(resource, endpoint) -> bool:
    """The metadata's ``resource`` names this endpoint: same origin, and its path
    is the endpoint's path or a parent of it (what the MCP SDKs accept)."""
    r, e = _origin_path(resource), _origin_path(endpoint)
    return bool(r and e) and r[:3] == e[:3] and (e[3] + "/").startswith(r[3] + "/")


def _get_json(c: httpx.Client, url: str) -> dict | None:
    """A JSON object from a 200 GET; None on any error, unsafe URL or oversized body."""
    try:
        with c.stream("GET", url, headers={"Accept": "application/json"}) as r:
            if r.status_code != 200:
                return None
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > _MAX_BODY:
                    return None
        data = json.loads(bytes(body))
    except Exception:  # noqa: BLE001 - unsafe URL, network and JSON errors all mean "not found"
        return None
    return data if isinstance(data, dict) else None


def _cached(key: str, fetch) -> dict | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fetch()
    if value is not None:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = (now + _CACHE_TTL, value)
    return value


def _resource_metadata(c: httpx.Client, endpoint: str, www: str) -> dict | None:
    """RFC 9728 metadata at the URL the challenge names, else at the well-known
    URIs derived from the endpoint (path-specific first, then the root)."""
    m = _RESOURCE_METADATA.search(www)
    if m:
        urls = [urljoin(endpoint, m.group(1))]
    else:
        p = urlsplit(endpoint)
        origin, path = f"{p.scheme}://{p.netloc}", p.path.rstrip("/")
        urls = list(dict.fromkeys((f"{origin}/.well-known/oauth-protected-resource{path}",
                                   f"{origin}/.well-known/oauth-protected-resource")))

    def fetch(url):
        meta = _get_json(c, url)
        if meta is None:
            return None
        servers = meta.get("authorization_servers")
        servers = [s for s in servers if isinstance(s, str)][:5] if isinstance(servers, list) else []
        resource = meta.get("resource")
        return {"resource": resource if isinstance(resource, str) else None,
                "authorization_servers": servers}

    for url in urls:
        found = _cached("prm " + url, lambda url=url: fetch(url))
        if found is not None:
            return found
    return None


def _server_metadata(c: httpx.Client, issuer: str) -> dict | None:
    """RFC 8414 / OpenID discovery for one authorization server, in the order the
    MCP spec lists; {pkce, registration} when it names both endpoints, else None."""
    try:
        p = urlsplit(issuer.strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return None
    base, path = f"{p.scheme}://{p.netloc}", p.path.rstrip("/")
    urls = dict.fromkeys((f"{base}/.well-known/oauth-authorization-server{path}",
                          f"{base}/.well-known/openid-configuration{path}",
                          f"{base}{path}/.well-known/openid-configuration"))

    def fetch():
        for url in urls:
            meta = _get_json(c, url)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                methods = meta.get("code_challenge_methods_supported")
                return {"pkce": isinstance(methods, list) and "S256" in methods,
                        "registration": bool(meta.get("registration_endpoint"))
                        or meta.get("client_id_metadata_document_supported") is True}
        return None

    return _cached("as " + issuer.strip(), fetch)


def check(endpoint: str, www_authenticate: str | None, http_status) -> dict:
    """Which discovery steps an anonymous client completes: a boolean per CHECKS.

    challenge: HTTP 401 with a Bearer WWW-Authenticate challenge.
    resource_metadata: protected-resource metadata found (named by the challenge,
        else at the well-known URI) listing authorization servers, its resource
        naming this endpoint.
    server_metadata: the first authorization server's metadata names its
        authorization and token endpoints.
    pkce / registration: that metadata offers PKCE S256 / automatic client
        registration (dynamic registration or client ID metadata documents).
    """
    www = str(www_authenticate or "")
    out = dict.fromkeys(CHECKS, False)
    out["challenge"] = http_status == 401 and bool(_BEARER.search(www))
    with public_http.client(timeout=_TIMEOUT, follow_redirects=True,
                            headers={"User-Agent": UA}) as c:
        prm = _resource_metadata(c, endpoint, www)
        servers = (prm or {}).get("authorization_servers") or []
        out["resource_metadata"] = bool(servers) and resource_matches(prm.get("resource"), endpoint)
        meta = _server_metadata(c, servers[0]) if servers else None
    if meta:
        out["server_metadata"] = True
        out["pkce"] = meta["pkce"]
        out["registration"] = meta["registration"]
    out["v"] = VERSION
    return out
