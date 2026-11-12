"""Synchronize free BotsRated MCP/x402 measurements into quality.botsrated."""
from __future__ import annotations

from datetime import datetime
import json
import logging
import re
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from . import db
from .crawlers import TIMEOUT, UA

log = logging.getLogger("directory.botsrated")

BASE = "https://botsrated.com"
SITEMAP_INDEX = BASE + "/sitemap.xml"
PRELIGHT = BASE + "/v1/preflight"
MAX_HOSTS_PER_RUN = 1000
REQUEST_GAP_SECONDS = 0.3
# Wait in-process only for short Retry-After values. BotsRated answers a spent
# daily quota with Retry-After of ~13 h; capping that at 120 s and retrying
# turned one hourly run into a 10-hour loop of 429s that blocked the timer.
MAX_INLINE_WAIT_SECONDS = 120
DEFAULT_PAUSE_SECONDS = 3600
PAUSE_META_KEY = "botsrated_paused_until"


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> int | None:
    value = (response.headers.get("retry-after") or "").strip()
    return int(value) if value.isdigit() else None


def _parse_time(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _get(client: httpx.Client, url: str, *, params: dict | None = None) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = client.get(url, params=params)
            if response.status_code == 429:
                wait = _retry_after(response)
                if wait is not None and wait > MAX_INLINE_WAIT_SECONDS:
                    raise RateLimited(wait)
                if attempt == 3:
                    raise RateLimited(wait or DEFAULT_PAUSE_SECONDS)
                time.sleep(wait if wait is not None else 3.0 * (attempt + 1))
                continue
            if response.status_code >= 500:
                time.sleep(3.0 * (attempt + 1))
                continue
            return response
        except httpx.TransportError as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    if last:
        raise last
    raise RuntimeError("BotsRated request failed after retries")


def fetch_covered_hosts() -> set[str]:
    headers = {"User-Agent": UA, "Accept": "application/xml,text/xml"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        index = _get(client, SITEMAP_INDEX)
        index.raise_for_status()
        root = ElementTree.fromstring(index.content)
        maps = [node.text for node in root.findall("{*}sitemap/{*}loc")
                if node.text and ("/sellers-" in node.text or "/mcp-" in node.text)]
        hosts: set[str] = set()
        for url in maps:
            response = _get(client, url)
            response.raise_for_status()
            sitemap = ElementTree.fromstring(response.content)
            for node in sitemap.findall("{*}url/{*}loc"):
                if not node.text:
                    continue
                match = re.match(r"https://botsrated\.com/s/([^/?#]+)", node.text)
                if match:
                    hosts.add(match.group(1).lower())
    return hosts


def _mcp_quality(server: dict, served_at: str | None) -> dict:
    measured = server.get("measured") if isinstance(server.get("measured"), dict) else {}
    recent = measured.get("recent_window") if isinstance(measured.get("recent_window"), dict) else {}
    fresh = server.get("freshness") if isinstance(server.get("freshness"), dict) else {}
    return {key: value for key, value in {
        "type": "mcp",
        "state": measured.get("last_state"),
        "last_http_status": measured.get("last_http_status"),
        "last_error": measured.get("last_error"),
        "last_probe_at": measured.get("last_probe_at") or fresh.get("last_probe_at"),
        "ok_rate": recent.get("ok_rate"),
        "probes": measured.get("probes"),
        "tool_count": measured.get("tool_count"),
        "latency_p50_ms": measured.get("latency_p50_ms"),
        "latency_p90_ms": measured.get("latency_p90_ms"),
        "auth_required_count": measured.get("auth_required_count"),
        "protocol_version": measured.get("protocol_version"),
        "tls": measured.get("tls"),
        "stale": fresh.get("stale"),
        "receipt_url": server.get("latest_receipt_url"),
        "served_at": served_at,
    }.items() if value is not None}


def _x402_quality(payload: dict) -> dict:
    latest = payload.get("latest_probe") if isinstance(payload.get("latest_probe"), dict) else {}
    recent = payload.get("recent_window") if isinstance(payload.get("recent_window"), dict) else {}
    fresh = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    return {key: value for key, value in {
        "type": "x402",
        "valid_402": payload.get("valid_402"),
        "terms_consistent": payload.get("terms_consistent"),
        "last_status": latest.get("status"),
        "last_error": latest.get("error"),
        "last_probe_at": latest.get("ts") or fresh.get("last_probe_at"),
        "latency_ms": latest.get("latency_ms"),
        "valid_402_rate": recent.get("valid_402_rate"),
        "served_at": payload.get("served_at"),
    }.items() if value is not None}


def summarize(payload: dict, endpoint: str | None, kind: str) -> dict | None:
    if not isinstance(payload, dict) or payload.get("found") is not True:
        return None
    if kind == "mcp":
        wanted = db._exact_endpoint(endpoint or "")
        servers = payload.get("servers") if isinstance(payload.get("servers"), list) else []
        matches = [server for server in servers if db._exact_endpoint(str(server.get("url") or "")) == wanted]
        if not matches:
            return None
        return _mcp_quality(matches[0], payload.get("served_at"))
    return _x402_quality(payload)


def _merge_quality(raw, summary: dict) -> str:
    try:
        quality = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        quality = {}
    if not isinstance(quality, dict):
        quality = {}
    quality["botsrated"] = summary
    return json.dumps(quality, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _interleave(queues: dict[str, list[tuple]], limit: int) -> list[tuple]:
    candidates = []
    wanted = max(0, int(limit))
    while len(candidates) < wanted and any(queues.values()):
        for kind in ("mcp", "x402"):
            if queues[kind] and len(candidates) < wanted:
                candidates.append(queues[kind].pop(0))
    return candidates


def sync_botsrated_quality(*, limit: int = MAX_HOSTS_PER_RUN,
                            stale_hours: int = 24) -> dict:
    db.init_db()
    now = int(time.time())
    with db.connect(read_only=True) as conn:
        paused = int(db.get_meta(conn, PAUSE_META_KEY, "0") or 0)
    if paused > now:
        result = {"skipped": "rate_limited", "paused_until": paused}
        log.info("botsrated quality sync: %s", result)
        return result

    def pause(seconds: int) -> int:
        until = int(time.time()) + max(60, int(seconds))
        def op():
            with db.writer(immediate=True) as conn:
                db.set_meta(conn, PAUSE_META_KEY, str(until))
        db.with_retry(op)
        return until

    try:
        covered = fetch_covered_hosts()
    except RateLimited as exc:
        result = {"skipped": "rate_limited", "paused_until": pause(exc.retry_after)}
        log.warning("botsrated quality sync: %s", result)
        return result
    cutoff = now - max(1, stale_hours) * 3600
    with db.connect(read_only=True) as conn:
        groups: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
        for kind, table, column in (("x402", "services", "url"),
                                    ("mcp", "mcp_servers", "endpoint_url")):
            rows = conn.execute(
                f"SELECT id,{column} endpoint,quality FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column}!='' ORDER BY id"
            ).fetchall()
            for row in rows:
                host = (urlsplit(row["endpoint"]).hostname or "").lower()
                if host not in covered:
                    continue
                try:
                    current = json.loads(row["quality"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    current = {}
                checked = _parse_time(((current.get("botsrated") or {}).get("served_at")
                                       if isinstance(current, dict) else None))
                if checked is None or checked < cutoff:
                    groups.setdefault((kind, host), []).append(
                        (checked if checked is not None else -1,
                         int(row["id"]), row["endpoint"]))
        queues: dict[str, list[tuple]] = {"mcp": [], "x402": []}
        for (kind, host), rows in groups.items():
            queues[kind].append(
                (min(item[0] for item in rows), kind, host, rows))
        for queue in queues.values():
            queue.sort(key=lambda item: (item[0], item[2]))
        candidates = _interleave(queues, limit)

    def fetch(client: httpx.Client, item):
        _, kind, host, rows = item
        try:
            time.sleep(REQUEST_GAP_SECONDS)
            response = _get(client, PRELIGHT, params={"url": rows[0][2]})
            if response.status_code == 404:
                return item, [], None
            response.raise_for_status()
            payload = response.json()
            summaries = [
                (listing_id, summary)
                for _, listing_id, endpoint in rows
                if (summary := summarize(payload, endpoint, kind)) is not None
            ]
            return item, summaries, None
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return item, [], type(exc).__name__

    updates: list[tuple[str, int, dict]] = []
    errors = 0
    attempted = 0
    paused_until = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        for candidate in candidates:
            try:
                item, summaries, error = fetch(client, candidate)
            except RateLimited as exc:
                # Stop the whole run; keep what was already fetched.
                paused_until = pause(exc.retry_after)
                log.warning("botsrated: rate limited after %d groups; paused until %d",
                            attempted, paused_until)
                break
            attempted += 1
            if error:
                errors += 1
            else:
                updates.extend((item[1], listing_id, summary)
                               for listing_id, summary in summaries)

    def write():
        with db.writer(immediate=True) as conn:
            for kind, listing_id, summary in updates:
                table = "services" if kind == "x402" else "mcp_servers"
                row = conn.execute(f"SELECT quality FROM {table} WHERE id=?", (listing_id,)).fetchone()
                if row is None:
                    continue
                conn.execute(
                    f"UPDATE {table} SET quality=? WHERE id=?",
                    (_merge_quality(row["quality"], summary), listing_id),
                )
    if updates:
        db.with_retry(write)
    done = candidates[:attempted]
    candidate_rows = sum(len(item[3]) for item in done)
    result = {"covered_hosts": len(covered), "candidate_groups": len(candidates),
              "attempted_groups": attempted,
              "candidate_rows": candidate_rows, "updated": len(updates),
              "not_found_or_unmatched": max(0, candidate_rows - len(updates) - errors),
              "errors": errors}
    if paused_until:
        result["paused_until"] = paused_until
    log.info("botsrated quality sync: %s", result)
    return result
