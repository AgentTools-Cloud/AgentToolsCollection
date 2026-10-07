from __future__ import annotations

from urllib.parse import urlparse

import idna


def exact_endpoint(url: str | None) -> str:
    """Canonical HTTP endpoint identity without changing path semantics."""
    value = (url or "").strip()
    if not value:
        return ""
    if "//" not in value:
        value = "https://" + value
    try:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if scheme not in ("http", "https") or not host:
            return ""
        host = idna.encode(host, uts46=True).decode("ascii")
        port = parsed.port
    except (idna.IDNAError, UnicodeError, ValueError):
        return ""
    if ":" in host:
        host = "[%s]" % host
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    authority = host if port is None or default_port else "%s:%d" % (host, port)
    path = parsed.path.rstrip("/")
    params = ";" + parsed.params if parsed.params else ""
    query = "?" + parsed.query if parsed.query else ""
    return "%s://%s%s%s%s" % (scheme, authority, path, params, query)