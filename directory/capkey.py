"""Capkey public MCP catalog source (researched 2026-10-26)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlsplit

import httpx

from .crawlers import PartialCrawl, UA
from .ingestion import validate_endpoint

log = logging.getLogger("directory.capkey")

CATALOG_URL = "https://capkey.dev/api/v1/catalog.json"
SEARCH_URL = "https://capkey.dev/api/v1/search"
SOURCE_URL = "https://capkey.dev/catalog"
PAGE_LIMIT = 50
MAX_PAGES = 500
MAX_PAGE_BYTES = 1024 * 1024
MAX_SECONDS = 300
PAGE_DELAY = 0.2
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


def _row(item: dict, *, public_search: bool = False) -> dict | None:
    expected_kind = "MCP" if public_search else "mcp_http"
    if not isinstance(item, dict) or item.get("kind") != expected_kind:
        return None
    inactive = (item.get("status") not in (None, "active") if public_search
                else item.get("status") != "active")
    template = item.get("isTemplate") if public_search else item.get("is_template")
    if item.get("badge") != "live" or inactive or template:
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
    row = {
        "slug": _slug(item),
        "name": str(item.get("name") or host)[:200],
        "description": str(item.get("description") or "").strip()[:1000] or None,
        "homepage_url": f"https://{host}" if host else None,
        "endpoint_url": endpoint,
        "transport": None if public_search else "streamable-http",
        "auth_method": None,
        "cost_hint": None,
        "tags": ["capkey"],
        "x402_supported": False,
        "source": "capkey",
        "source_id": str(item.get("id") or endpoint),
        "source_url": SOURCE_URL,
        "confidence": 0.6,
    }
    if public_search:
        row["_partial_metadata"] = True
    return row


def _request_page(client, page: int, deadline: float) -> dict:
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Capkey crawl deadline reached")
        response = None
        try:
            with client.stream("GET", SEARCH_URL, params={
                    "kind": "mcp", "badge": "live", "sort": "score",
                    "page": page, "limit": PAGE_LIMIT},
                    timeout=httpx.Timeout(min(remaining, 30.0), connect=min(remaining, 10.0))) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        raise RuntimeError("Capkey page exceeds size limit")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Capkey crawl deadline reached")
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise RuntimeError("Capkey search response has no items list")
            return payload
        except httpx.HTTPError:
            status = response.status_code if response is not None else 0
            if attempt == 2 or (status and status != 429 and status < 500):
                raise
            delay = float(2 ** attempt)
            if response is not None and status == 429:
                try:
                    delay = min(120.0, max(0.0, float(response.headers.get("retry-after", delay))))
                except ValueError:
                    pass
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
    raise RuntimeError("Capkey page unavailable")


def fetch_capkey_mcp() -> list[dict]:
    rows = []
    seen = set()
    total = None
    deadline = time.monotonic() + MAX_SECONDS
    with httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as client:
        try:
            for page in range(1, MAX_PAGES + 1):
                payload = _request_page(client, page, deadline)
                current_total = payload.get("total")
                if (type(current_total) is not int or not 0 <= current_total <= PAGE_LIMIT * MAX_PAGES
                        or payload.get("page") != page or payload.get("pageSize") != PAGE_LIMIT):
                    raise RuntimeError("Capkey pagination metadata is invalid")
                if total is None:
                    total = current_total
                elif current_total != total:
                    raise RuntimeError("Capkey live catalog changed during pagination")
                pages = max(1, (total + PAGE_LIMIT - 1) // PAGE_LIMIT)
                expected_size = max(0, min(PAGE_LIMIT, total - (page - 1) * PAGE_LIMIT))
                if len(payload["items"]) != expected_size:
                    raise RuntimeError("Capkey page is incomplete")
                for item in payload["items"]:
                    identifier = str(item.get("id") or "") if isinstance(item, dict) else ""
                    if not identifier or identifier in seen:
                        raise RuntimeError("Capkey page has missing or duplicate identities")
                    seen.add(identifier)
                    row = _row(item, public_search=True)
                    if row is not None:
                        rows.append(row)
                if page >= pages:
                    break
                time.sleep(min(PAGE_DELAY, max(0.0, deadline - time.monotonic())))
            if len(seen) != total:
                raise RuntimeError("Capkey catalog traversal is incomplete")
        except Exception as exc:
            if rows:
                raise PartialCrawl(rows, f"Capkey pagination incomplete: {exc}") from exc
            raise
    log.info(
        "capkey mcp: %d public live MCP items -> %d concrete endpoints",
        len(seen), len(rows),
    )
    return rows
