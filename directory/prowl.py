"""Prowl public MCP directory source.

Prowl's robots policy explicitly allows ``/v1`` and its public discovery API.
Only services with a public MCP manifest are requested, and only concrete
remote endpoints declared by that manifest are returned.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .crawlers import TIMEOUT, UA, _host_slug, _slugify

log = logging.getLogger("directory.prowl")

DISCOVER_URL = "https://prowl.world/v1/discover"
SERVICE_URL = "https://prowl.world/service/{slug}"
PAGE_SIZE = 100


def _concrete_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        return None
    if "{" in value or "}" in value:
        return None
    return value


def _manifest_endpoints(manifest: dict) -> list[tuple[str, str | None]]:
    """Extract concrete remote MCP endpoints from known manifest fields."""
    out: list[tuple[str, str | None]] = []
    direct = _concrete_url(
        manifest.get("endpoint")
        or manifest.get("endpoint_url")
        or manifest.get("endpointUrl")
    )
    if direct:
        out.append((direct, manifest.get("transport")))

    for item in manifest.get("transports") or []:
        if not isinstance(item, dict):
            continue
        endpoint = _concrete_url(item.get("url") or item.get("endpoint"))
        transport = str(item.get("type") or "").lower()
        if endpoint and transport in ("http", "https", "sse", "streamable-http"):
            out.append((endpoint, transport))

    unique: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for endpoint, transport in out:
        key = endpoint.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append((endpoint, transport))
    return unique


def _transport(value: str | None, endpoint: str) -> str:
    value = (value or "").lower()
    if value == "sse" or endpoint.lower().rstrip("/").endswith("sse"):
        return "sse"
    return "streamable-http"


def _auth_method(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    if not value or value in ("false", "none", "open", "public"):
        return None
    return value


def _row(service: dict, endpoint: str, transport: str | None) -> dict:
    service_id = str(service.get("id") or endpoint)
    service_slug = str(service.get("slug") or _host_slug(endpoint))
    score = (service.get("score") or {}).get("overall")
    confidence = None
    if isinstance(score, (int, float)):
        confidence = max(0.0, min(1.0, float(score) / 100.0))
    categories = service.get("category") or []
    if isinstance(categories, str):
        categories = [categories]
    return {
        "slug": _slugify(f"{service_slug}-{_host_slug(endpoint)}-prowl")[:80],
        "name": service.get("name") or _host_slug(endpoint),
        "description": (service.get("description") or "").strip() or None,
        "homepage_url": service.get("website_url") or endpoint,
        "endpoint_url": endpoint,
        "transport": _transport(transport, endpoint),
        "auth_method": _auth_method(service.get("auth_type")),
        "tags": [str(item) for item in categories if item],
        # Source metadata is not proof. The independent reverify job owns this.
        "x402_supported": False,
        "source": "prowl",
        "source_id": f"{service_id}:{endpoint.lower().rstrip('/')}",
        "source_url": SERVICE_URL.format(slug=service_slug),
        "confidence": confidence,
    }


def fetch_prowl_mcp() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as client:
        while True:
            response = client.get(
                DISCOVER_URL,
                params={"has_mcp": "true", "limit": PAGE_SIZE, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            services = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(services, list):
                raise RuntimeError("Prowl discovery returned an unexpected payload")

            for service in services:
                if not isinstance(service, dict):
                    continue
                manifest_url = _concrete_url(service.get("mcp_manifest_url"))
                if not manifest_url or not manifest_url.endswith((".json", "/mcp.json")):
                    continue
                try:
                    manifest_response = client.get(manifest_url)
                    if manifest_response.status_code != 200:
                        continue
                    manifest = manifest_response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                if not isinstance(manifest, dict):
                    continue
                for endpoint, transport in _manifest_endpoints(manifest):
                    key = endpoint.lower().rstrip("/")
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(_row(service, endpoint, transport))

            if not payload.get("has_more") or not services:
                break
            next_offset = payload.get("next_offset")
            offset = int(next_offset) if next_offset is not None else offset + len(services)

    log.info("prowl: resolved %d concrete remote MCP endpoints", len(rows))
    return rows