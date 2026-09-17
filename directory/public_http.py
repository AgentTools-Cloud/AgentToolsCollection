"""HTTP clients for untrusted URLs, pinned to validated public IPs."""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable

import httpx

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


class UnsafeURL(ValueError):
    """A URL can reach something other than a public Internet service."""


def normalize_hostname(value: str) -> str:
    raw = (value or "").strip().rstrip(".").lower()
    try:
        host = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURL("URL host is not a valid DNS name") from exc
    if not host or len(host) > 253 or "." not in host:
        raise UnsafeURL("URL host must be a fully-qualified DNS name")
    if any(not _HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise UnsafeURL("URL host is not a valid DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeURL("IP literals are not accepted")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURL("local hostnames are not accepted")
    return host


def resolve_public(host: str, port: int) -> list[str]:
    host = normalize_hostname(host)
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeURL("URL host does not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise UnsafeURL("URL host resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafeURL("URL host resolves to a non-public address")
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise UnsafeURL("URL host has no usable public address")
    return addresses


def _parse_url(url: str) -> tuple[httpx.URL, str, int]:
    try:
        parsed = httpx.URL(str(url))
    except Exception as exc:
        raise UnsafeURL("invalid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("URL scheme must be http or https")
    if parsed.userinfo:
        raise UnsafeURL("URL credentials are not accepted")
    host = normalize_hostname(parsed.host)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise UnsafeURL("URL port is invalid")
    return parsed, host, port


def _validated_addresses(addresses) -> list[str]:
    out: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeURL("URL host resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafeURL("URL host resolves to a non-public address")
        text = str(address)
        if text not in out:
            out.append(text)
    if not out:
        raise UnsafeURL("URL host has no usable public address")
    return out


def validate_url(url: str) -> httpx.URL:
    parsed, host, port = _parse_url(url)
    resolve_public(host, port)
    return parsed


class PublicTransport(httpx.BaseTransport):
    """Resolve once, validate every address, then connect to that exact IP."""

    def __init__(self, *, resolver: Callable[[str, int], list[str]] | None = None,
                 inner: httpx.BaseTransport | None = None):
        self._resolver = resolver or resolve_public
        self._inner = inner or httpx.HTTPTransport(
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        parsed, host, port = _parse_url(str(request.url))
        addresses = _validated_addresses(self._resolver(host, port))
        # Use one validated address for this request. No keepalive means a later
        # redirect cannot inherit a connection validated for another host.
        address = addresses[0]
        headers = request.headers.copy()
        default_port = 443 if parsed.scheme == "https" else 80
        display_host = "[%s]" % host if ":" in host else host
        headers["Host"] = (display_host if port == default_port
                   else "%s:%d" % (display_host, port))
        extensions = dict(request.extensions)
        if parsed.scheme == "https":
            extensions["sni_hostname"] = host
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return self._inner.handle_request(pinned)

    def close(self) -> None:
        self._inner.close()


def client(**kwargs) -> httpx.Client:
    """An HTTPX client whose initial request and every redirect are public-only."""
    if "transport" not in kwargs:
        kwargs["transport"] = PublicTransport()
    kwargs.setdefault("max_redirects", 3)
    return httpx.Client(**kwargs)
