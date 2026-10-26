"""Capkey public MCP catalog source (researched 2026-10-26)."""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlsplit

import httpx

from .crawlers import TIMEOUT, UA
from .ingestion import validate_endpoint

log = logging.getLogger("directory.capkey")

CATALOG_URL = "https://capkey.dev/api/v1/catalog.json"
SOURCE_URL = "https://capkey.dev/catalog"
_PLACEHOLDER = re.compile(
    r"(?:\{[^{}]+\}|<[^<>]+>|%(?:7b|3c)[^&?#]*(?:%7d|%3e)|"
    r"(?:^|[?&])(?:token|key|api[_-]?key|access[_-]?token)=(?:your|replace|example|xxx|placeholder))",
    re.IGNORECASE,
)
_CREDENTIAL_QUERY_KEYS = re.compile(
    r"(?:^|[_-])(?:auth|authorization|key|password|secret|token)$",
    re.IGNORECASE,
)


def _slug(item: dict) -> str:
    host = (urlsplit(str(item.get("url") or "")).hostname or "server").lower()
    base = re.sub(r"[^a-z0-9]+", "-", str(item.get("name") or host).lower()).strip("-")
    digest = hashlib.sha256(str(item.get("url") or "").encode()).hexdigest()[:8]
    return f"{base or 'mcp'}-{digest}-capkey"[:80]


def _row(item: dict) -> dict | None:
    if not isinstance(item, dict) or item.get("kind") != "mcp_http":
        return None
    if item.get("badge") != "live" or item.get("status") != "active" or item.get("is_template"):
        return None
    endpoint = str(item.get("url") or "").strip()
    if not endpoint or _PLACEHOLDER.search(endpoint):
        return None
    if any(_CREDENTIAL_QUERY_KEYS.search(key)
           for key, _ in parse_qsl(urlsplit(endpoint).query, keep_blank_values=True)):
        return None
    try:
        validate_endpoint(endpoint)
    except Exception:
        return None
    host = urlsplit(endpoint).hostname or ""
    return {
        "slug": _slug(item),
        "name": str(item.get("name") or host)[:200],
        "description": str(item.get("description") or "").strip()[:1000] or None,
        "homepage_url": f"https://{host}" if host else None,
        "endpoint_url": endpoint,
        "transport": "streamable-http",
        "auth_method": None,
        "cost_hint": None,
        "tags": ["capkey"],
        "x402_supported": False,
        "source": "capkey",
        "source_id": str(item.get("id") or endpoint),
        "source_url": SOURCE_URL,
        "confidence": 0.6,
    }


def fetch_capkey_mcp() -> list[dict]:
    with httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as client:
        response = client.get(CATALOG_URL)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Capkey catalog response has no items list")
    rows = [row for item in payload["items"] if (row := _row(item))]
    log.info(
        "capkey mcp: %d catalog items -> %d live concrete MCP endpoints",
        len(payload["items"]), len(rows),
    )
    return rows
