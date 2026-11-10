"""Capkey public MCP catalog source (researched 2026-10-26)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
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
MAX_CATALOG_BYTES = 512 * 1024 * 1024
MAX_CATALOG_RECORDS = 200000
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


def _fetch_search_pages() -> list[dict]:
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


class _CatalogReader:
    def __init__(self, response, deadline):
        self.chunks = response.iter_bytes(chunk_size=65536)
        self.deadline = deadline
        self.buffer = b""
        self.total_bytes = 0
        self.finished = False

    def read(self, size=-1):
        if size == 0:
            return b""
        target = min(65536, size) if size > 0 else 65536
        output = bytearray()
        while len(output) < target:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Capkey catalog deadline reached")
            if not self.buffer:
                if self.finished:
                    break
                try:
                    self.buffer = next(self.chunks)
                except StopIteration:
                    self.finished = True
                    break
                self.total_bytes += len(self.buffer)
                if self.total_bytes > MAX_CATALOG_BYTES:
                    raise RuntimeError("Capkey catalog exceeds size limit")
            take = min(target - len(output), len(self.buffer))
            output.extend(self.buffer[:take])
            self.buffer = self.buffer[take:]
        return bytes(output)


def _fetch_catalog() -> list[dict]:
    import ijson

    fields = {"id", "kind", "url", "name", "description", "status", "badge", "is_template"}
    header = {}
    rows = []
    seen = set()
    current = None
    total_records = 0
    root_complete = False
    items_started = False
    deadline = time.monotonic() + MAX_SECONDS
    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                      follow_redirects=True, headers={"User-Agent": UA, "Accept": "application/json"}) as client:
        with client.stream("GET", CATALOG_URL) as response:
            response.raise_for_status()
            for prefix, event, value in ijson.parse(_CatalogReader(response, deadline)):
                if prefix in ("version", "generated_at", "total_count"):
                    if event not in ("string", "number"):
                        raise RuntimeError("Capkey catalog metadata is not scalar")
                    if prefix in header:
                        raise RuntimeError("Capkey catalog repeated metadata")
                    header[prefix] = value
                elif prefix == "items":
                    if event == "start_array" and not items_started:
                        items_started = True
                    elif event != "end_array":
                        raise RuntimeError("Capkey catalog items is not a unique array")
                elif prefix == "items.item" and event == "start_map":
                    current = {}
                elif prefix.startswith("items.item.") and current is not None:
                    field = prefix[len("items.item."):]
                    if field in fields:
                        if event not in ("string", "number", "boolean", "null"):
                            raise RuntimeError("Capkey catalog item field is not scalar")
                        if field in current:
                            raise RuntimeError("Capkey catalog repeated item field")
                        if isinstance(value, str) and len(value) > MAX_PAGE_BYTES:
                            raise RuntimeError("Capkey catalog field exceeds size limit")
                        current[field] = value
                elif prefix == "items.item" and event == "end_map":
                    total_records += 1
                    if total_records > MAX_CATALOG_RECORDS:
                        raise RuntimeError("Capkey catalog record limit reached")
                    if (current.get("kind") == "mcp_http" and current.get("status") == "active"
                            and current.get("badge") == "live" and not current.get("is_template")):
                        identifier = str(current.get("id") or "")
                        if not identifier or identifier in seen:
                            raise RuntimeError("Capkey catalog has missing or duplicate MCP identities")
                        seen.add(identifier)
                    row = _row(current)
                    if row is not None:
                        row["transport"] = None
                        row["_partial_metadata"] = True
                        rows.append(row)
                    current = None
                elif prefix == "items.item" and event not in ("map_key",):
                    raise RuntimeError("Capkey catalog item is not an object")
                elif prefix == "" and event == "end_map":
                    root_complete = True
    if (not root_complete or not items_started or header.get("version") != "0.1"
            or type(header.get("total_count")) is not int
            or header["total_count"] != total_records or total_records == 0):
        raise RuntimeError("Capkey catalog traversal or metadata is incomplete")
    generated = header.get("generated_at")
    if not isinstance(generated, str):
        raise RuntimeError("Capkey catalog timestamp missing")
    stamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    if stamp.tzinfo is None or not -300 <= time.time() - stamp.timestamp() <= 86400:
        raise RuntimeError("Capkey catalog snapshot is not fresh")
    log.info("capkey complete catalog: %d records, %d live MCP identities, %d concrete endpoints",
             total_records, len(seen), len(rows))
    return rows


def fetch_capkey_mcp() -> list[dict]:
    try:
        return _fetch_search_pages()
    except Exception as search_error:
        try:
            return _fetch_catalog()
        except Exception as catalog_error:
            if isinstance(search_error, PartialCrawl):
                raise PartialCrawl(search_error.items,
                                   "Capkey search remains incomplete; full catalog unavailable: "
                                   + type(catalog_error).__name__) from catalog_error
            raise RuntimeError("Capkey public search and full catalog unavailable") from catalog_error
