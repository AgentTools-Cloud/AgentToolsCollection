"""Agenstry.com reverse-crawl source.

agenstry.com is a competing A2A/MCP directory. It advertises ~80k "indexed"
MCP servers but its own /api/stats shows ~97% are metadata-only GitHub/registry
rows with no live endpoint; only ~2.3k are actually alive. We therefore pull
its inventory through its JSON API rather than its (top-5000-truncated) sitemap:

  GET /api/mcp-servers?alive=true&limit=100&offset=N
       -> live, responding MCP servers (name/title/description/primary_url/...)
  /agents/<domain>  (sitemap)
       -> a live A2A agent (slug == its domain); we fetch its own Agent Card.

We treat agenstry as a *discovery* source only: we take the endpoint it lists,
then verify/probe it ourselves. x402 support is never inferred from text — it is
proven by directory.reverify_x402 probing each endpoint for an HTTP 402 / a
/.well-known/x402 descriptor.

skills/* are agenstry's auto-generated taxonomy (e.g. payments.crypto.*), not
URLs, so they do not map onto our concrete x402 `services` table.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from . import a2a as a2a_mod
from . import db
from . import ingestion
from . import public_http

log = logging.getLogger("directory.agenstry")

SITEMAP = "https://agenstry.com/sitemap.xml"
API_BASE = "https://agenstry.com/api"
UA = "agent-tools.cloud-crawler/0.1 (+https://agent-tools.cloud)"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

# Anonymous clients are limited to 50 rows; larger pages require an API key.
_API_PAGE = 50
_API_RETRY_DELAYS = (5.0, 20.0, 60.0)
_API_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "item"


def _host_slug(url: str) -> str:
    host = urlparse(url).netloc.lower() if "://" in (url or "") else (url or "").lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "host"


def _fetch_sitemap_paths() -> list[str]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(SITEMAP)
        r.raise_for_status()
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    return [u.strip() for u in locs]


# ---------------------------------------------------------------------------
# A2A agents
# ---------------------------------------------------------------------------
_A2A_WRITE_BATCH = 100


def _write_a2a_batch(rows: list[dict]) -> dict:
    """Commit one batch; shared ingestion isolates classified row rejections."""
    # Start an outer transaction explicitly: releasing an outermost savepoint
    # would otherwise commit a row before the whole batch has succeeded.
    with db.writer(immediate=True) as conn:
        return ingestion.ingest_batch(conn, "a2a", "agenstry", rows, db.upsert_a2a_agent)


def crawl_agenstry_a2a(max_hosts: int = 4000, workers: int = 12) -> dict:
    """Discover live cards and commit bounded batches.

    Permanent metadata/retirement rejections are counted separately from source
    and probe errors. An absent card is still an ordinary discovery miss.
    Unexpected exceptions propagate; previously committed batches remain intact.
    """
    def start_run():
        with db.writer() as conn:
            return db.record_crawl_start(conn, "a2a:agenstry")

    run_id = db.with_retry(start_run)
    inserted = updated = rejected = 0
    errors: list[str] = []

    def finish_run(status):
        def op():
            with db.writer() as conn:
                db.record_crawl_finish(
                    conn, run_id, inserted, updated, errors, status=status,
                )
        db.with_retry(op)

    def _probe(dom: str) -> tuple[dict | None, str | None]:
        base = f"https://{dom}"
        try:
            with public_http.client(
                    timeout=TIMEOUT, follow_redirects=True,
                    headers={"User-Agent": UA, "Accept": "application/json"}) as c:
                card, card_url = a2a_mod.fetch_agent_card(
                    base, client=c, include_invalid_for_audit=True,
                )
            if card and card_url:
                return a2a_mod.card_to_row(
                    card, card_url, source="agenstry", source_id=dom,
                ), None
        except (httpx.HTTPError, public_http.UnsafeURL) as e:
            log.warning("agenstry a2a: skipping %s: %r", dom, e)
            return None, f"{dom}: {e!r}"
        return None, None

    try:
        paths = _fetch_sitemap_paths()
        domains: list[str] = []
        seen: set[str] = set()
        for u in paths:
            path = urlparse(u).path
            if not path.startswith("/agents/"):
                continue
            dom = path[len("/agents/"):].strip("/").lower()
            if not dom or "/" in dom or "." not in dom or dom in seen:
                continue
            seen.add(dom)
            domains.append(dom)
        domains = domains[:max_hosts]
        log.info("agenstry a2a: %d candidate domains", len(domains))

        rows: list[dict] = []
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for row, error in ex.map(_probe, domains):
                if row:
                    rows.append(row)
                if error:
                    errors.append(error)

        log.info("agenstry a2a: resolved %d live cards / %d domains", len(rows), len(domains))
        if domains and not rows:
            errors.append(f"agenstry a2a: no live cards resolved from {len(domains)} candidates")

        for offset in range(0, len(rows), _A2A_WRITE_BATCH):
            batch = rows[offset:offset + _A2A_WRITE_BATCH]
            result = db.with_retry(lambda batch=batch: _write_a2a_batch(batch))
            inserted += result["added"]
            updated += result["updated"]
            rejected += result["rejected"]
            errors.extend(result["errors"])
            if result.get('warnings'):
                log.warning('source endpoint mismatch: a2a:agenstry new_pairs=%d; current primary retained',
                            result['warnings'])
    except Exception as exc:
        errors.append(f"agenstry a2a: {exc!r}")
        try:
            finish_run("error")
        except Exception:
            # A failed status write must not mask the original source/DB error.
            log.exception("agenstry a2a: failed to record crawl error")
        raise

    finish_run("partial" if errors else "ok")
    return {"candidates": len(domains), "resolved": len(rows),
            "inserted": inserted, "updated": updated,
            "rejected": rejected, "errors": errors}


# ---------------------------------------------------------------------------
# MCP servers  (registered into crawlers.MCP_CRAWLERS as "agenstry")
# ---------------------------------------------------------------------------
# Hosts whose primary_url is a catalog page rather than a callable endpoint.
# Not every directory belongs here: server.smithery.ai, mcp.apify.com and mcp.so
# are directories too, but they answer the protocol (84% / 98% / 89% reachable),
# whereas glama.ai/mcp/servers/{owner}/{repo} is HTML -- all 4,199 rows it put in
# the table failed conformance and none ever handshook.
_NOT_AN_ENDPOINT = {"agenstry.com", "www.agenstry.com", "glama.ai", "www.glama.ai"}


def _api_row_to_mcp(rec: dict) -> dict | None:
    """Map one /api/mcp-servers record to our mcp_servers upsert row."""
    endpoint = (rec.get("primary_url") or "").strip()
    if not endpoint:
        return None
    endpoint = endpoint.rstrip("/")
    host = urlparse(endpoint).netloc.lower()
    if not host or host in _NOT_AN_ENDPOINT or host.endswith(".agenstry.com"):
        return None
    aid = rec.get("name") or endpoint                 # e.g. "gvzq/flight-mcp"
    name = rec.get("title") or rec.get("name") or _host_slug(endpoint)
    transport = (rec.get("transport") or "").lower() or None
    if transport == "http" and endpoint.lower().endswith("/mcp"):
        transport = "streamable-http"
    out_slug = f"{_slugify(str(aid).replace('/', '-'))}-{_host_slug(endpoint)}"[:80]
    return {
        "slug": out_slug,
        "name": name,
        "description": (rec.get("description") or "").strip() or None,
        "homepage_url": f"https://{urlparse(endpoint).netloc}",
        "endpoint_url": endpoint,
        "transport": transport,
        "auth_method": None,
        # x402 is proven by reverify_x402 probing, never inferred here.
        "x402_supported": False,
        "source": "agenstry",
        "source_id": str(aid),
        "source_url": f"https://agenstry.com/mcp/{aid}",
        "confidence": 0.5,
    }


def fetch_agenstry_mcp(max_pages: int = 6000, workers: int = 12) -> list:
    """Pull Agenstry's anonymous window of live MCP servers.

    Matches the MCP_CRAWLERS contract (returns a list of row dicts; jobs
    upserts them via db.upsert_mcp_server, which dedups by endpoint across
    sources so overlap with existing servers just refreshes them).

    Agenstry now limits anonymous requests to 50 rows and ignores offset/page
    parameters. Fetch the public window once instead of repeatedly requesting
    the same page until its rate limiter returns 429. Existing rows remain in
    our catalog and continue through independent health checks.
    """
    out: list[dict] = []
    seen_eps: set[str] = set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        last_error = None
        for attempt in range(len(_API_RETRY_DELAYS) + 1):
            try:
                r = c.get(f"{API_BASE}/mcp-servers",
                          params={"alive": "true", "limit": _API_PAGE})
                if r.status_code in _API_TRANSIENT_STATUSES:
                    last_error = RuntimeError(f"HTTP {r.status_code}")
                    if attempt < len(_API_RETRY_DELAYS):
                        delay = _API_RETRY_DELAYS[attempt]
                        log.warning(
                            "agenstry mcp api returned %d, retrying in %.0fs",
                            r.status_code, delay,
                        )
                        time.sleep(delay)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.TransportError, ValueError) as e:
                last_error = e
                if attempt < len(_API_RETRY_DELAYS):
                    delay = _API_RETRY_DELAYS[attempt]
                    log.warning("agenstry mcp api failed, retrying in %.0fs: %r",
                                delay, e)
                    time.sleep(delay)
                    continue
            except Exception as e:
                raise RuntimeError(f"agenstry mcp api failed: {e!r}") from e
        else:
            raise RuntimeError(
                f"agenstry mcp api failed after retries: {last_error!r}"
            ) from last_error
    results = data.get("results") or []
    for rec in results:
        row = _api_row_to_mcp(rec)
        if not row:
            continue
        ep = row["endpoint_url"].lower()
        if ep in seen_eps:
            continue
        seen_eps.add(ep)
        out.append(row)
    log.info("agenstry mcp: refreshed %d servers from anonymous 50-row window", len(out))
    return out
