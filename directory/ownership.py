"""Domain ownership: proving an operator controls the host behind a listing.

Identity (who you are) comes from OAuth. Authorisation (what you may edit)
comes only from a token published on the host that serves the listing --
never from an email address, a repository, or a login name.

Domain methods prove control of the host. URL methods prove control of one
exact HTTPS endpoint, including its query, and never grant whole-host rights.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import timedelta
from html import escape
import hashlib
import http.client
import idna
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import anyio
import httpx

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


def recommended_method(kinds) -> tuple[str, str]:
    """Pick the whole-host method that fits what the host already serves.

    x402 hosts already publish /.well-known/x402 and A2A hosts an agent card,
    so adding one field is easiest there. MCP servers have neither by
    protocol, and creating one would falsely advertise x402 or A2A, so they
    get the dedicated verification file instead.
    """
    kinds = set(kinds or ())
    if "x402" in kinds:
        return "descriptor", "x402"
    if "a2a" in kinds:
        return "descriptor", "a2a"
    return "wellknown_file", "mcp" if "mcp" in kinds else ""
URL_METHODS = ("url_descriptor", "mcp_resource", "github_file", "npm_package")
URL_PENDING_SECONDS = 24 * 60 * 60
MCP_VERIFY_RESOURCE = "agent-tools://ownership"
# Hosted MCP gateways that refuse anonymous requests. The proof is still the
# token served by the claimed URL; the gateway key only gets the request
# through. Agent Tools uses its own operator key (root-only file, read per
# probe) for verification and re-checks. A claimant may still pass a key for a
# single check. Keys go only into that probe's query string; they are never
# stored in the database, logged, echoed, or made part of the claimed URL.
AUTHENTICATED_MCP_GATEWAYS = {"server.smithery.ai": "api_key"}
OPERATOR_GATEWAY_KEY_FILES = {"server.smithery.ai": "/etc/agent-tools/smithery-gateway.key"}
_GATEWAY_KEY = re.compile(r"^[A-Za-z0-9._~-]{8,200}$")
PUBLICATION_PROOFS = {
    "github": {"prefix": "https://github.com/", "pattern": r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", "method": "github_file"},
    "npm": {"prefix": "https://www.npmjs.com/package/", "pattern": r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", "method": "npm_package"},
}

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


@dataclass(frozen=True)
class ClaimVerification:
    claim: tuple[Any, ...] | None
    ok: bool
    detail: str


ClaimNotification = tuple[str, int, int, str]


def new_token() -> str:
    return "atc_" + secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- probing

_TOKEN_SHAPE = re.compile(r"atc_[A-Za-z0-9_-]{32}")

# Automatic checks of pending claims: dense while the owner is probably still
# publishing, then sparse; the window bounds the total, a match or a new token
# ends it early, and a manual check counts as one of these points.
PENDING_RECHECK_OFFSETS = (600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
                           24 * 3600, 36 * 3600, 48 * 3600, 72 * 3600)
HOST_PENDING_RECHECK_SECONDS = 72 * 3600
PENDING_RECHECK_BATCH = 50


class HashedToken:
    """Recognise a published token from its stored hash.

    Pending claims keep only the hash, so an automatic check cannot know the
    plaintext. Any published value whose hash matches is the issued token; it
    is remembered so the normal verification path can run with it.
    """

    def __init__(self, digest: str):
        self.digest = digest
        self.found: str | None = None

    def matches(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        value = value.strip()
        if (_TOKEN_SHAPE.fullmatch(value)
                and secrets.compare_digest(token_hash(value), self.digest)):
            self.found = value
            return True
        return False

    def in_text(self, text: str) -> bool:
        return any(self.matches(match.group(0)) for match in _TOKEN_SHAPE.finditer(text))


def _token_equal(value: Any, token: "str | HashedToken") -> bool:
    if isinstance(token, HashedToken):
        return token.matches(value)
    return isinstance(value, str) and secrets.compare_digest(value.strip(), token)


def _find_token(obj: Any, token: str) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == TOKEN_FIELD and _token_equal(v, token):
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
        host = idna.encode(raw, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError) as exc:
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


def normalize_claim_url(value: str) -> str:
    raw = (value or "").strip()
    if (not raw or len(raw) > 2000
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw)
            or re.search(r"%(?![0-9a-fA-F]{2})", raw)):
        raise UnsafeTarget("claim URL must be a public HTTPS URL without whitespace")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeTarget("claim URL is invalid") from exc
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or "#" in raw or port not in (None, 443)):
        raise UnsafeTarget("claim URL must use public HTTPS on port 443 without credentials or a fragment")
    host = normalize_host(parsed.hostname or "")
    path = parsed.path or "/"
    decoded = unquote(path)
    if ("\\" in decoded or "//" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in (".", "..") for part in decoded.split("/"))
            or any(marker in path.lower() or marker in decoded.lower()
                   for marker in ("%2f", "%5c", "%00"))):
        raise UnsafeTarget("claim URL has an ambiguous path")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        field = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_")
        if (field in ("token", "key", "auth", "authorization", "secret", "password", "sig", "signature", "code", "jwt", "bearer", "apikey", "credential", "credentials")
                or field.endswith(("_token", "_key", "_secret", "_signature"))):
            raise UnsafeTarget("claim URL must not contain credentials in its query")
    path = quote(path, safe="/:@-._~!$&'()*+,;=%")
    query = quote(parsed.query, safe="/?:@-._~!$&'()*+,;=%")
    return urlunsplit(("https", host, path, query, ""))


def claim_url_scope(value: str) -> tuple[str, str]:
    parsed = urlsplit(normalize_claim_url(value))
    scope = parsed.path
    if parsed.query:
        scope += "?" + parsed.query
    return parsed.hostname or "", scope


def gateway_key_parameter(url: str) -> str | None:
    """Query parameter a supported hosted MCP gateway expects its key in."""
    try:
        host, _scope = claim_url_scope(url)
    except UnsafeTarget:
        return None
    return AUTHENTICATED_MCP_GATEWAYS.get(host)


def operator_gateway_key(url: str) -> str | None:
    """Agent Tools' own key for the gateway serving ``url``, if configured."""
    try:
        host, _scope = claim_url_scope(url)
    except UnsafeTarget:
        return None
    path = OPERATOR_GATEWAY_KEY_FILES.get(host)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            value = stream.read(256).strip()
    except OSError:
        return None
    return value if _GATEWAY_KEY.match(value) else None


def resolve_service_url(url: str) -> str:
    try:
        return normalize_claim_url(url or "")
    except UnsafeTarget as exc:
        raise ClaimError(str(exc)) from exc


def _platform_suffix(url: str, platform: str) -> str:
    target = normalize_claim_url(url)
    option = PUBLICATION_PROOFS[platform]
    if not target.startswith(option["prefix"]):
        raise UnsafeTarget("verification method does not match this platform URL")
    suffix = target[len(option["prefix"]):]
    if not re.fullmatch(option["pattern"], suffix):
        raise UnsafeTarget("verification requires the platform's canonical public service URL")
    return suffix


def default_url_method(url: str) -> str:
    for platform, option in PUBLICATION_PROOFS.items():
        try:
            _platform_suffix(url, platform)
            return option["method"]
        except UnsafeTarget:
            continue
    return "mcp_resource" if urlsplit(url).path.rstrip("/").endswith("/mcp") else "url_descriptor"


def url_publish_hint(url: str, method: str) -> str:
    return {
        "url_descriptor": "Add agentToolsVerify to the top-level public JSON returned by " + url,
        "mcp_resource": "Expose the public MCP resource " + MCP_VERIFY_RESOURCE + " containing the verification document.",
        "github_file": "Commit .well-known/agent-tools.json to the default branch of " + url,
        "npm_package": "Publish agentToolsVerify in the latest package.json for " + url,
    }.get(method, "")


def _find_url_token(document: Any, token: str) -> bool:
    value = document.get(TOKEN_FIELD) if isinstance(document, dict) else None
    return _token_equal(value, token)


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


def _interrupt_socket(connection_socket) -> None:
    if connection_socket is not None:
        try:
            connection_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _BoundedSSEStream(httpx.SyncByteStream):
    def __init__(self, response):
        self.response = response
        self.received = bytearray()

    def __iter__(self):
        while True:
            line = self.response.readline(_MAX_BODY + 1 - len(self.received))
            if not line:
                return
            self.received.extend(line)
            if len(self.received) > _MAX_BODY:
                raise UnsafeTarget("verification response exceeds 64 KiB")
            yield line


def _read_https(host: str, path: str, accept: str, *, method: str = "GET",
                body: bytes | None = None, request_headers: dict[str, str] | None = None,
                sse_frame: bool = False, response_id: str | int | None = None) -> _FetchResponse:
    last_error: Exception | None = None
    for address in _public_addresses(host):
        conn = _PinnedHTTPSConnection(host, address)
        deadline = None
        try:
            headers = {
                "User-Agent": UA,
                "Accept": accept,
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            headers.update(request_headers or {})
            conn.request(method, path, body=body, headers=headers)
            deadline = threading.Timer(_READ_TIMEOUT, _interrupt_socket, args=(getattr(conn, "sock", None),))
            deadline.daemon = True
            deadline.start()
            response = conn.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise UnsafeTarget("verification responses must not be compressed")
            if sse_frame and "text/event-stream" in headers.get("content-type", "").lower():
                from httpx_sse import EventSource
                stream = _BoundedSSEStream(response)
                decoded = httpx.Response(response.status, headers=headers, stream=stream)
                try:
                    for event in EventSource(decoded).iter_sse():
                        if response_id is None:
                            break
                        if event.event != "message":
                            continue
                        try:
                            message = json.loads(event.data)
                        except ValueError:
                            continue
                        if (isinstance(message, dict) and message.get("id") == response_id
                                and (("result" in message) != ("error" in message))):
                            break
                finally:
                    decoded.close()
                content = bytes(stream.received)
            else:
                content = response.read(_MAX_BODY + 1)
            if len(content) > _MAX_BODY:
                raise UnsafeTarget("verification response exceeds 64 KiB")
            return _FetchResponse(response.status, content, headers)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if deadline is not None:
                deadline.cancel()
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
    if isinstance(token, HashedToken):
        return token.in_text(body), VERIFY_FILE
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
    for rec in answer:
        txt = b"".join(getattr(rec, "strings", [])).decode("utf-8", "replace")
        if isinstance(token, HashedToken):
            if DNS_MARKER in txt and token.in_text(txt.split(DNS_MARKER, 1)[1]):
                return True, name
        elif DNS_MARKER + token in txt:
            return True, name
    return False, "%s has TXT but not the issued %s value" % (name, DNS_MARKER)


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


class _MCPClaimTransport(httpx.AsyncBaseTransport):
    def __init__(self, url: str, gateway_api_key: str | None = None):
        self.url = normalize_claim_url(url)
        self._credential_query = None
        if gateway_api_key is not None:
            parameter = gateway_key_parameter(self.url)
            if parameter is None or not _GATEWAY_KEY.match(gateway_api_key):
                raise UnsafeTarget("gateway API key is not accepted for this URL")
            self._credential_query = urlencode({parameter: gateway_api_key})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if normalize_claim_url(str(request.url)) != self.url:
            raise UnsafeTarget("MCP verification cannot change its target URL")
        if request.method not in ("GET", "POST", "DELETE"):
            raise UnsafeTarget("unsupported MCP verification request")
        payload = await request.aread()
        response_id = None
        if len(payload) > _MAX_BODY:
            raise UnsafeTarget("MCP verification request is too large")
        if request.method == "POST":
            message = json.loads(payload)
            response_id = message.get("id") if isinstance(message, dict) else None
            method = message.get("method") if isinstance(message, dict) else None
            if method not in ("initialize", "notifications/initialized", "resources/read"):
                raise UnsafeTarget("MCP verification only reads its ownership resource")
            if (method == "resources/read"
                    and message.get("params", {}).get("uri") != MCP_VERIFY_RESOURCE):
                raise UnsafeTarget("MCP verification cannot read other resources")
        host, scope = claim_url_scope(self.url)
        if self._credential_query is not None:
            scope += ("&" if "?" in scope else "?") + self._credential_query
        headers = {name: request.headers[name] for name in (
            "content-type", "mcp-session-id", "mcp-protocol-version") if name in request.headers}

        def read():
            return _read_https(host, scope, request.headers.get("accept", "application/json"),
                               method=request.method, body=payload or None,
                               request_headers=headers, sse_frame=True, response_id=response_id)

        response = await anyio.to_thread.run_sync(read, abandon_on_cancel=True)
        if response.status_code in _REDIRECT_STATUSES:
            raise UnsafeTarget("MCP verification does not follow redirects")
        if len(response.body) > _MAX_BODY:
            raise UnsafeTarget("MCP verification response is too large")
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.body, request=request)


def _probe_mcp_resource(url: str, token: str,
                        gateway_api_key: str | None = None) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.types import Implementation
    from pydantic import AnyUrl

    def client_factory(**options):
        return httpx.AsyncClient(
            transport=_MCPClaimTransport(url, gateway_api_key), headers=options.get("headers"),
            timeout=options.get("timeout", _READ_TIMEOUT), follow_redirects=False,
            trust_env=False)

    async def read_resource():
        async with streamablehttp_client(
                url, timeout=_READ_TIMEOUT, sse_read_timeout=_READ_TIMEOUT,
                httpx_client_factory=client_factory) as streams:
            async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=_READ_TIMEOUT),
                    client_info=Implementation(name="agent-tools-verifier", version="0.1")) as session:
                await session.initialize()
                result = await session.read_resource(AnyUrl(MCP_VERIFY_RESOURCE))
                for content in result.contents:
                    text = getattr(content, "text", None)
                    if str(content.uri) != MCP_VERIFY_RESOURCE or not isinstance(text, str):
                        continue
                    if _find_url_token(json.loads(text), token):
                        return True, MCP_VERIFY_RESOURCE
        return False, "ownership resource does not contain the issued token"

    async def bounded_read():
        return await asyncio.wait_for(read_resource(), timeout=3 * _READ_TIMEOUT)

    try:
        return asyncio.run(bounded_read())
    except Exception:
        if gateway_api_key is None and gateway_key_parameter(url) is not None:
            return False, "the hosted MCP gateway refused our verification request"
        return False, "could not read the endpoint's public MCP ownership resource"


def probe_url(method: str, url: str, token: str, *,
              gateway_api_key: str | None = None) -> tuple[bool, str]:
    if gateway_api_key is not None and (
            method != "mcp_resource" or gateway_key_parameter(url) is None
            or not _GATEWAY_KEY.match(gateway_api_key)):
        return False, ("gateway_api_key is only accepted for mcp_resource claims "
                       "on supported hosted MCP gateways")
    if gateway_api_key is None and method == "mcp_resource":
        gateway_api_key = operator_gateway_key(url)
    try:
        host, scope = claim_url_scope(url)
        if method == "mcp_resource":
            return _probe_mcp_resource(normalize_claim_url(url), token, gateway_api_key)
        if method == "github_file":
            repository = _platform_suffix(url, "github")
            response = _read_https("api.github.com", "/repos/%s/contents/.well-known/agent-tools.json" % repository,
                                   "application/vnd.github.raw+json")
            if response.status_code != 200:
                return False, "could not read the repository's default-branch verification file"
            document = json.loads(response.body.decode("utf-8"))
            if isinstance(document, dict) and document.get("encoding") == "base64":
                if document.get("type") != "file" or document.get("path") != ".well-known/agent-tools.json":
                    return False, "repository proof is not the expected file"
                content = base64.b64decode("".join(document["content"].split()), validate=True)
                document = json.loads(content.decode("utf-8"))
            return _find_url_token(document, token), ".well-known/agent-tools.json"
        if method == "npm_package":
            package = _platform_suffix(url, "npm")
            response = _read_https("registry.npmjs.org", "/%s/latest" % quote(package, safe="@"), "application/json")
            if response.status_code != 200:
                return False, "could not read the latest public package manifest"
            document = json.loads(response.body.decode("utf-8"))
            if not isinstance(document, dict) or document.get("name") != package:
                return False, "package proof does not match the claimed package"
            return _find_url_token(document, token), "latest package.json"
        if method != "url_descriptor":
            return False, "unknown URL verification method"
        response = _read_https(host, scope, "application/json")
        if not (200 <= response.status_code < 300 or response.status_code == 402):
            return False, "HTTP %d; URL verification does not follow redirects" % response.status_code
        document = json.loads(response.body.decode("utf-8"))
        return _find_url_token(document, token), TOKEN_FIELD
    except (UnsafeTarget, ValueError, KeyError, TypeError, RecursionError, OSError,
            http.client.HTTPException):
        return False, "could not read the endpoint's public verification document"


# ------------------------------------------------------------- claim flow

def issue(conn: sqlite3.Connection, user_id: int, host: str,
          method: str) -> tuple[int, str]:
    """Open a pending claim. Returns (ownership_id, plaintext token).

    The token is kept so the owner can see it again: it becomes public the
    moment it is published, and knowing it proves nothing without the
    ability to publish it on the target. Matching still uses the hash.
    """
    try:
        host = normalize_host(host)
    except UnsafeTarget as exc:
        raise ClaimError(str(exc)) from exc
    if method not in METHODS:
        raise ClaimError("unknown method: %s" % method)
    if db.find_privacy_suppression(conn, "x402", {"host": host}) is not None:
        raise ClaimError("this host was removed by its operator")
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
        "WHERE user_id=? AND host=? AND status='pending' AND scope_path IS NULL",
        (user_id, host),
    )
    cur = conn.execute(
        "INSERT INTO domain_ownership(user_id, host, scope_path, method, "
        "token_hash, token, status, created_at) VALUES (?,?,NULL,?,?,?,'pending',?)",
        (user_id, host, method, token_hash(token), token, now),
    )
    conn.commit()
    return int(cur.lastrowid), token


def _check_url_claim_policy(conn: sqlite3.Connection, user_id: int, url: str) -> None:
    user = conn.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or user["status"] != "active":
        raise ClaimError("claim account is not active")
    host, _scope = claim_url_scope(url)
    if db.find_privacy_suppression_any(conn, "mcp", {
            "host": host, "url": url, "endpoint_url": url, "card_url": url}) is not None:
        raise ClaimError("this URL was removed by its operator")


def issue_url(conn: sqlite3.Connection, user_id: int, url: str,
              method: str) -> tuple[int, str]:
    try:
        host, scope = claim_url_scope(url)
    except UnsafeTarget as exc:
        raise ClaimError(str(exc)) from exc
    if method not in URL_METHODS:
        raise ClaimError("unknown URL verification method")
    target = normalize_claim_url(url)
    if method in ("github_file", "npm_package"):
        try:
            _platform_suffix(target, "github" if method == "github_file" else "npm")
        except UnsafeTarget as exc:
            raise ClaimError(str(exc)) from exc
    _check_url_claim_policy(conn, user_id, target)
    token = new_token()
    now = int(time.time())
    conn.execute(
        "DELETE FROM domain_ownership WHERE user_id=? AND host=? AND scope_path=? "
        "AND status='pending'", (user_id, host, scope))
    cursor = conn.execute(
        "INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,token,"
        "status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
        (user_id, host, scope, method, token_hash(token), token, now))
    conn.commit()
    return int(cursor.lastrowid), token


def _url_claim_target(row: sqlite3.Row) -> str:
    if row["scope_path"] is None or row["method"] not in URL_METHODS:
        raise UnsafeTarget("invalid URL claim scope or method")
    target = normalize_claim_url("https://%s%s" % (row["host"], row["scope_path"]))
    if claim_url_scope(target) != (row["host"], row["scope_path"]):
        raise UnsafeTarget("URL claim target is not canonical")
    if row["status"] not in ("pending", "verified"):
        raise UnsafeTarget("URL claim is no longer active; request a new token")
    if (row["status"] == "pending"
            and int(time.time()) - row["created_at"] > URL_PENDING_SECONDS):
        raise UnsafeTarget("URL claim expired; request a new token")
    return target


def _claim_snapshot(row: sqlite3.Row) -> tuple[Any, ...]:
    return tuple(row[key] for key in (
        "id", "user_id", "host", "scope_path", "method", "token_hash", "status", "created_at"))


def _recheck_snapshot(row: sqlite3.Row) -> tuple[Any, ...]:
    return _claim_snapshot(row) + tuple(row[key] for key in (
    "token", "verified_at", "last_checked", "fail_count", "verification_revision"))


def _apply_recheck_result(conn: sqlite3.Connection, expected: sqlite3.Row,
                          ok: bool | None) -> tuple[int, int]:
    current = conn.execute('SELECT * FROM domain_ownership WHERE id=?', (expected['id'],)).fetchone()
    if current is None or _recheck_snapshot(current) != _recheck_snapshot(expected):
        return 0, 0
    shared = current['scope_path'] is None and db.is_shared_host(conn, current['host'])
    if ok is None and not shared:
        return 0, 0
    now = int(time.time())
    if shared or (not ok and current['fail_count'] + 1 >= MAX_FAILS):
        conn.execute("UPDATE domain_ownership SET status='revoked',last_checked=?,fail_count=? WHERE id=?",
                     (now, current['fail_count'] if shared else current['fail_count'] + 1, current['id']))
        return 0, 1
    conn.execute('UPDATE domain_ownership SET last_checked=?,fail_count=? WHERE id=?',
                 (now, 0 if ok else current['fail_count'] + 1, current['id']))
    return 1, 0


def prepare_claim_verification(conn: sqlite3.Connection, ownership_id: int,
                               token: str, *, gateway_api_key: str | None = None
                               ) -> ClaimVerification:
    row = conn.execute(
        "SELECT * FROM domain_ownership WHERE id=?", (ownership_id,)
    ).fetchone()
    if row is None:
        return ClaimVerification(None, False, "no such claim")
    if row["token_hash"] != token_hash(token):
        return ClaimVerification(None, False, "token mismatch")
    if row["scope_path"] is not None:
        try:
            target = _url_claim_target(row)
            _check_url_claim_policy(conn, row["user_id"], target)
        except (UnsafeTarget, ClaimError) as exc:
            return ClaimVerification(None, False, str(exc))
        options = {} if gateway_api_key is None else {"gateway_api_key": gateway_api_key}
        ok, detail = probe_url(row["method"], target, token, **options)
        return ClaimVerification(_claim_snapshot(row), ok, detail)
    if gateway_api_key is not None:
        return ClaimVerification(
            None, False, "gateway_api_key only applies to service URL claims")
    if row["method"] not in METHODS or db.is_shared_host(conn, row["host"]):
        return ClaimVerification(
            None, False, "host is shared; whole-host verification is unavailable")
    ok, detail = probe(row["method"], row["host"], token)
    return ClaimVerification(_claim_snapshot(row), ok, detail)


def apply_claim_verification(conn: sqlite3.Connection, ownership_id: int,
                             token: str, *, verification: ClaimVerification | None = None
                             ) -> tuple[bool, str, ClaimNotification | None]:
    prepared = verification or prepare_claim_verification(conn, ownership_id, token)
    if prepared.claim is None:
        return False, prepared.detail, None
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM domain_ownership WHERE id=?", (ownership_id,)
    ).fetchone()
    if (row is None or _claim_snapshot(row) != prepared.claim
            or row["token_hash"] != token_hash(token)):
        return False, "claim changed during verification", None
    if row["scope_path"] is not None:
        try:
            target = _url_claim_target(row)
            _check_url_claim_policy(conn, row["user_id"], target)
        except (UnsafeTarget, ClaimError) as exc:
            return False, str(exc), None
    elif row["method"] not in METHODS or db.is_shared_host(conn, row["host"]):
        return False, "host is shared; whole-host verification is unavailable", None
    ok, detail = prepared.ok, prepared.detail
    notification = None
    now = int(time.time())
    if ok:
        # Whoever can publish on the host today owns it. If someone else held
        # it, either the domain changed hands or it was taken over -- both are
        # things the previous holder needs to hear about, not reasons to refuse.
        # Any earlier verified claim steps aside, including the same
        # account's -- re-verifying with a different method is normal, and one
        # host may only have one live claim.
        if row["scope_path"] is not None:
            previous = conn.execute(
                "SELECT * FROM domain_ownership WHERE host=? AND scope_path=? "
                "AND status='verified'", (row["host"], row["scope_path"])).fetchone()
        else:
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
            target = ("https://%s%s" % (row["host"], row["scope_path"])
                      if row["scope_path"] is not None else row["host"])
            notification = (target, int(displaced["user_id"]),
                            int(row["user_id"]), row["method"])
    else:
        conn.execute(
            "UPDATE domain_ownership SET last_checked=?, "
            "fail_count=fail_count+1 WHERE id=?",
            (now, ownership_id),
        )
    return ok, detail, notification


def verify_claim(conn: sqlite3.Connection, ownership_id: int,
                 token: str, *, verification: ClaimVerification | None = None
                 ) -> tuple[bool, str]:
    """Check a pending claim. The caller supplies the token it was shown."""
    ok, detail, notification = apply_claim_verification(
        conn, ownership_id, token, verification=verification)
    if notification is not None:
        _notify_displaced(conn, *notification)
    return ok, detail


def notify_claim_displaced(notification: ClaimNotification | None) -> None:
    if notification is None:
        return
    try:
        conn = db.connect(read_only=True, timeout=0.25)
        try:
            _notify_displaced(conn, *notification)
        finally:
            conn.close()
    except Exception:
        log.warning("could not open the displaced-owner notification reader")


def recheck(conn: sqlite3.Connection) -> tuple[int, int]:
    """Re-probe verified claims; revoke after MAX_FAILS consecutive misses.

    Domains change hands, so a badge that is never re-checked is a lie with a
    timestamp on it.
    """
    if conn.in_transaction:
        raise ValueError('Ownership recheck requires a reader outside a write transaction')
    kept = revoked = 0
    rows = list(conn.execute(
        "SELECT * FROM domain_ownership WHERE status='verified'"))
    for row in rows:
        if row["scope_path"] is None and db.is_shared_host(conn, row["host"]):
            ok = None
        elif row["scope_path"] is not None:
            try:
                target = _url_claim_target(row)
                ok = bool(row['token']) and probe_url(row["method"], target, row['token'])[0]
                if (not ok and row['token'] and row["method"] == "mcp_resource"
                        and gateway_key_parameter(target) is not None
                        and operator_gateway_key(target) is None):
                    # Without our gateway key a refusal proves nothing.
                    ok = None
            except UnsafeTarget:
                ok = False
        else:
            ok = bool(row['token']) and probe(row["method"], row["host"], row['token'])[0]

        def write():
            with db.writer(immediate=True) as writer_conn:
                return _apply_recheck_result(writer_conn, row, ok)

        accepted, removed = db.with_retry(write)
        kept += accepted
        revoked += removed
    return kept, revoked


def _pending_recheck_due(row: sqlite3.Row, now: int) -> bool:
    window = (URL_PENDING_SECONDS if row["scope_path"] is not None
              else HOST_PENDING_RECHECK_SECONDS)
    if now - row["created_at"] > window:
        return False
    points = [row["created_at"] + offset for offset in PENDING_RECHECK_OFFSETS
              if offset <= window and row["created_at"] + offset <= now]
    return bool(points) and (row["last_checked"] or 0) < points[-1]


def recheck_pending(conn: sqlite3.Connection, *,
                    limit: int = PENDING_RECHECK_BATCH) -> tuple[int, int]:
    """Check pending claims on a bounded schedule so owners need not return.

    The published value is matched against the stored hash; only a match is
    passed to the normal verification path, which probes again and records
    the plaintext exactly as a manual check would.
    """
    if conn.in_transaction:
        raise ValueError("Pending recheck requires a reader outside a write transaction")
    now = int(time.time())
    rows = [row for row in conn.execute(
        "SELECT * FROM domain_ownership WHERE status='pending' AND created_at>=? "
        "ORDER BY created_at", (now - HOST_PENDING_RECHECK_SECONDS,))
        if _pending_recheck_due(row, now)][:limit]
    checked = verified = 0
    for row in rows:
        hashed = HashedToken(row["token_hash"])
        try:
            if row["scope_path"] is not None:
                target = _url_claim_target(row)
                _check_url_claim_policy(conn, row["user_id"], target)
                probe_url(row["method"], target, hashed)
            elif row["method"] in METHODS and not db.is_shared_host(conn, row["host"]):
                probe(row["method"], row["host"], hashed)
            else:
                continue
        except (UnsafeTarget, ClaimError):
            pass
        checked += 1
        if hashed.found is not None:
            verification = prepare_claim_verification(conn, row["id"], hashed.found)

            def apply():
                with db.writer(immediate=True) as writer_conn:
                    return apply_claim_verification(
                        writer_conn, row["id"], hashed.found, verification=verification)

            ok, _detail, notification = db.with_retry(apply)
            notify_claim_displaced(notification)
            if ok:
                verified += 1
                continue

        def record():
            with db.writer(immediate=True) as writer_conn:
                writer_conn.execute(
                    "UPDATE domain_ownership SET last_checked=? "
                    "WHERE id=? AND status='pending' AND token_hash=?",
                    (int(time.time()), row["id"], row["token_hash"]))

        db.with_retry(record)
    return checked, verified


# ------------------------------------------------------------- lookups

def owner_of_host(conn: sqlite3.Connection, host: str):
    if db.is_shared_host(conn, host):
        return None
    return conn.execute(
        "SELECT * FROM domain_ownership WHERE host=? AND status='verified' "
        "AND scope_path IS NULL",
        ((host or "").lower(),),
    ).fetchone()


def hosts_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
    return [
        r["host"]
        for r in conn.execute(
            "SELECT host FROM domain_ownership "
            "WHERE user_id=? AND status='verified' AND scope_path IS NULL ORDER BY host",
            (user_id,),
        )
    ]


def urls_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
    out = []
    for row in conn.execute(
            "SELECT host,scope_path,method FROM domain_ownership WHERE user_id=? "
            "AND status='verified' AND scope_path IS NOT NULL ORDER BY host,scope_path", (user_id,)):
        if row["method"] not in URL_METHODS:
            continue
        try:
            out.append(normalize_claim_url("https://%s%s" % (row["host"], row["scope_path"])))
        except UnsafeTarget:
            continue
    return out


def owner_for_url(conn: sqlite3.Connection, url: str):
    """The verified claim covering this listing URL, if any."""
    host, _ = db._host_and_path(url or "")
    if not host:
        return None
    try:
        scoped_host, scope = claim_url_scope(url)
    except UnsafeTarget:
        pass
    else:
        owner = conn.execute(
            "SELECT * FROM domain_ownership WHERE host=? AND scope_path=? "
            "AND status='verified' AND method IN (%s)"
            % ",".join("?" for _method in URL_METHODS),
            (scoped_host, scope, *URL_METHODS),
        ).fetchone()
        if owner is not None:
            return owner
    return owner_of_host(conn, host)


def may_edit(conn: sqlite3.Connection, user_id: int, url: str) -> bool:
    owner = owner_for_url(conn, url)
    return bool(owner) and owner["user_id"] == user_id


def listings_for_claim(conn: sqlite3.Connection, claim) -> list[dict]:
    if claim["scope_path"] is None:
        return db.listings_for_host(conn, claim["host"])
    out = []
    for kind, table, column, view in (
            ("x402", "services", "url", "/services/%s"),
            ("mcp", "mcp_servers", "endpoint_url", "/mcp/servers/%s"),
            ("a2a", "a2a_agents", "COALESCE(endpoint_url,card_url)", "/a2a/agents/%s")):
        rows = conn.execute(
            "SELECT slug,name,%s AS _url FROM %s WHERE lower(%s) LIKE ?"
            % (column, table, column), ("%" + claim["host"] + "%",))
        for row in rows:
            try:
                target = claim_url_scope(row["_url"] or "")
            except UnsafeTarget:
                continue
            if target != (claim["host"], claim["scope_path"]):
                continue
            if db.find_privacy_suppression_any(conn, kind, {"slug": row["slug"]}) is not None:
                continue
            out.append({"kind": kind, "slug": row["slug"], "name": row["name"],
                        "view_url": view % row["slug"],
                        "edit_url": "/api/v1/listings/%s/%s" % (kind, row["slug"])})
    return out


FRESH_PROOF_SECONDS = 60


class OwnershipBlocked(PermissionError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OwnershipReceipt:
    ownership_id: int
    user_id: int
    host: str
    method: str
    token_hash: str
    verified_at: int
    checked_at: int


def _active_whole_host_claim(conn, user_id: int, host: str):
    user = conn.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
    owner = owner_of_host(conn, host)
    if (user is None or user["status"] != "active" or owner is None
            or owner["user_id"] != user_id or owner["scope_path"] is not None
            or not owner["verified_at"] or db.is_shared_host(conn, host)):
        raise OwnershipBlocked("not_verified_owner")
    token = owner["token"]
    if (not token or not secrets.compare_digest(token_hash(token), owner["token_hash"])):
        raise OwnershipBlocked("claim_changed")
    return owner


def fresh_ownership(conn, user_id: int, host: str) -> OwnershipReceipt:
    host = normalize_host(host)
    owner = _active_whole_host_claim(conn, user_id, host)
    ok, _detail = probe(owner["method"], host, owner["token"])
    if not ok:
        raise OwnershipBlocked("control_unavailable")
    return OwnershipReceipt(
        int(owner["id"]), user_id, host, owner["method"], owner["token_hash"],
        int(owner["verified_at"]), int(time.time()))


def assert_fresh_ownership(conn, receipt: OwnershipReceipt, user_id: int,
                           host: str) -> None:
    if (not isinstance(receipt, OwnershipReceipt) or receipt.user_id != user_id
            or receipt.host != normalize_host(host)
            or not 0 <= int(time.time()) - receipt.checked_at <= FRESH_PROOF_SECONDS):
        raise OwnershipBlocked("proof_expired")
    owner = _active_whole_host_claim(conn, user_id, receipt.host)
    current = (int(owner["id"]), owner["method"], owner["token_hash"],
               int(owner["verified_at"]))
    expected = (receipt.ownership_id, receipt.method, receipt.token_hash,
                receipt.verified_at)
    if current != expected:
        raise OwnershipBlocked("claim_changed")


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
        scope_name = "service URL" if method in URL_METHODS else "host"
        subject = "Your claim on %s was transferred" % host
        text = (
            "Someone proved control of %s and now holds the verified claim "
            "on it.\n\n"
            "Account: %s\n"
            "Method: %s\n\n"
            "We verify ownership with a token published for the claimed %s, "
            "so the token we issued to them was reachable for %s. "
            "If you still control that target, sign in and claim it "
            "again -- proving control is all it takes. If you did not expect "
            "this, check who can publish on that host.\n\n"
            "Edits you already made to listings there have been kept.\n\n"
            "https://agent-tools.cloud/account\n"
        ) % (host, who, method, scope_name, host)
        html = (
            "<p>Someone proved control of <b>%s</b> and now holds the verified "
            "claim on it.</p><ul><li>Account: <b>%s</b></li>"
            "<li>Method: <code>%s</code></li></ul>"
            "<p>We verify ownership with a token published for the claimed %s, "
            "so the token we issued to them was reachable for %s. If you still control that target, "
            "<a href=\"https://agent-tools.cloud/account\">sign in and claim it "
            "again</a> &mdash; proving control is all it takes. If you did not "
            "expect this, check who can publish on that host.</p>"
            "<p>Edits you already made to listings there have been kept.</p>"
        ) % (escape(host), escape(who), escape(method), scope_name, escape(host))
        mailer._send(old["email"], subject, text, html)
    except Exception as exc:                       # notification is best-effort
        log.warning("could not notify displaced owner of %s: %s", host, exc)
