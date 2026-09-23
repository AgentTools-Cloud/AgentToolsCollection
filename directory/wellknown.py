"""Wellknown Network adapters for public MCP and A2A endpoints."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx

from . import crawlers

log = logging.getLogger("directory.wellknown")

API_URL = "https://wellknown.network/api/v1/agents"
SITE_URL = "https://wellknown.network"
UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
PAGE_LIMIT = 500
PAGE_DELAY = 2.1
MAX_PAGES = 500
MAX_ATTEMPTS = 3

_CODE_HOSTS = {
    "bitbucket.org",
    "crates.io",
    "github.com",
    "githubusercontent.com",
    "gitlab.com",
    "huggingface.co",
    "jsdelivr.net",
    "npmjs.com",
    "pkg.go.dev",
    "pypi.org",
    "unpkg.com",
}
_RECORD_CACHE: tuple[list[dict], str | None] | None = None


def _retry_delay(response, attempt: int) -> float:
    value = (response.headers.get("retry-after") or "").strip()
    try:
        return max(0.0, min(float(value), 120.0))
    except ValueError:
        return float(2 ** attempt)


def _request_page(client, params: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        response = None
        try:
            response = client.get(API_URL, params=params)
            status = int(response.status_code)
            if status == 429 or status >= 500:
                raise RuntimeError(f"HTTP {status}")
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            return payload
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            status = int(response.status_code) if response is not None else 0
            if status and status != 429 and status < 500:
                raise
            if attempt + 1 < MAX_ATTEMPTS:
                delay = (_retry_delay(response, attempt)
                         if response is not None and status == 429
                         else float(2 ** attempt))
                time.sleep(delay)
    raise RuntimeError(f"Wellknown page failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _overlap_since(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (parsed - timedelta(milliseconds=1)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _fetch_pages(client=None, *, page_limit: int = PAGE_LIMIT,
                 page_delay: float = PAGE_DELAY,
                 max_pages: int = MAX_PAGES) -> list[dict]:
    """Traverse the free list API with an exclusive updatedAt watermark.

    Wellknown's cursor currently fails on every second-page request. The
    documented ``since`` parameter is monotonic, so it is used instead. Any
    condition that could hide a page is surfaced as PartialCrawl.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )

    output: list[dict] = []
    seen: set[str] = set()
    watermark: str | None = None
    try:
        for page_number in range(1, max_pages + 1):
            params = {"limit": page_limit, "status": "live", "remote": "true"}
            if watermark:
                params["since"] = _overlap_since(watermark)
            try:
                payload = _request_page(client, params)
            except Exception as exc:
                if output:
                    raise crawlers.PartialCrawl(
                        output,
                        f"page {page_number} after {watermark or 'start'} failed: {exc}",
                    ) from exc
                raise

            rows = payload.get("agents")
            if not isinstance(rows, list):
                reason = f"page {page_number} has no agents array"
                if output:
                    raise crawlers.PartialCrawl(output, reason)
                raise RuntimeError(reason)
            if not rows:
                return output

            timestamps: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise crawlers.PartialCrawl(
                        output, f"page {page_number} contains a non-object record"
                    )
                updated_at = row.get("updatedAt")
                if not isinstance(updated_at, str) or not updated_at:
                    raise crawlers.PartialCrawl(
                        output, f"page {page_number} contains a record without updatedAt"
                    )
                timestamps.append(updated_at)

            if timestamps != sorted(timestamps):
                raise crawlers.PartialCrawl(
                    output, f"page {page_number} is not ordered by updatedAt"
                )
            next_watermark = timestamps[-1]
            if watermark and next_watermark <= watermark:
                raise crawlers.PartialCrawl(
                    output, f"page {page_number} did not advance the since watermark"
                )

            for row in rows:
                record_id = str(row.get("id") or "").strip()
                dedup_key = record_id or hashlib.sha256(
                    repr(sorted(row.items())).encode("utf-8")
                ).hexdigest()
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    output.append(row)

            if len(rows) < page_limit:
                return output
            watermark = next_watermark
            if page_delay:
                time.sleep(page_delay)
    finally:
        if own_client:
            client.close()

    raise crawlers.PartialCrawl(output, f"page limit {max_pages} exhausted")


def _records() -> tuple[list[dict], str | None]:
    global _RECORD_CACHE
    if _RECORD_CACHE is None:
        try:
            _RECORD_CACHE = (_fetch_pages(), None)
        except crawlers.PartialCrawl as exc:
            _RECORD_CACHE = (list(exc.items), exc.reason)
    return _RECORD_CACHE


def _protocols(record: dict) -> set[str]:
    value = record.get("protocols")
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower() for item in value
            if isinstance(item, str) and item.strip()}


def _is_online_endpoint(value) -> bool:
    if not isinstance(value, str):
        return False
    endpoint = value.strip()
    if not endpoint or any(char in endpoint for char in "{}\r\n\t "):
        return False
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (host == "localhost" or host.endswith(".localhost")
                or host.endswith(".local") or "." not in host):
            return False
        if any(host == code_host or host.endswith(f".{code_host}")
               for code_host in _CODE_HOSTS):
            return False
    else:
        if not address.is_global:
            return False
    return True


def _source_url(record: dict) -> str:
    handle = str(record.get("handle") or record.get("id") or "").strip()
    return f"{SITE_URL}/agents/{quote(handle, safe='')}"


def _agent_card_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/agent-card.json"


def _slug(record: dict, endpoint: str) -> str:
    base = str(record.get("handle") or record.get("name") or "wellknown")
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "wellknown"
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:10]
    return f"{base[:70]}-{digest}"


def _text(value) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    return str(value).strip() or None


def _live_records() -> tuple[list[dict], str | None]:
    records, reason = _records()
    return ([record for record in records
             if str(record.get("status") or "").strip().lower() == "live"
             and _is_online_endpoint(record.get("endpoint"))], reason)


def fetch_wellknown_mcp() -> list[dict]:
    records, reason = _live_records()
    rows = []
    seen_endpoints: set[str] = set()
    for record in records:
        protocols = _protocols(record)
        kind = str(record.get("kind") or "").strip().lower()
        if kind != "mcp_server" and "mcp" not in protocols:
            continue
        endpoint = record["endpoint"].strip()
        endpoint_key = endpoint.lower().rstrip("/")
        if endpoint_key in seen_endpoints:
            continue
        seen_endpoints.add(endpoint_key)
        rows.append({
            "slug": _slug(record, endpoint),
            "name": _text(record.get("name")) or _text(record.get("handle")) or "Unnamed MCP server",
            "description": _text(record.get("summary")),
            "endpoint_url": endpoint,
            "transport": None,
            "homepage_url": None,
            "package_url": None,
            "auth_method": None,
            "tags": sorted(protocols),
            "source": "wellknown",
            "source_id": _text(record.get("id")) or _source_url(record),
            "source_url": _source_url(record),
            "confidence": None,
        })
    if reason:
        raise crawlers.PartialCrawl(rows, reason)
    return rows


def fetch_wellknown_a2a() -> list[dict]:
    records, reason = _live_records()
    rows = []
    seen_endpoints: set[str] = set()
    for record in records:
        protocols = _protocols(record)
        kind = str(record.get("kind") or "").strip().lower()
        if kind != "agent" or "a2a" not in protocols:
            continue
        endpoint = record["endpoint"].strip()
        endpoint_key = endpoint.lower().rstrip("/")
        if endpoint_key in seen_endpoints:
            continue
        seen_endpoints.add(endpoint_key)
        rows.append({
            "slug": _slug(record, endpoint),
            "name": _text(record.get("name")) or _text(record.get("handle")) or "Unnamed A2A agent",
            "description": _text(record.get("summary")),
            "provider_name": None,
            "provider_url": None,
            "card_url": _agent_card_url(endpoint),
            "endpoint_url": endpoint,
            "homepage_url": None,
            "documentation_url": None,
            "protocol_version": None,
            "preferred_transport": None,
            "skills": [],
            "capabilities": {},
            "default_input_modes": None,
            "default_output_modes": None,
            "auth_schemes": [],
            "x402_supported": False,
            "price_hint_usd": None,
            "payto": None,
            "source": "wellknown",
            "source_id": _text(record.get("id")) or _source_url(record),
            "source_url": _source_url(record),
            "confidence": None,
        })
    if reason:
        raise crawlers.PartialCrawl(rows, reason)
    return rows