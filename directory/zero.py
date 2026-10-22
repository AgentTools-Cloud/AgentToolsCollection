"""Zero (zero.xyz) source via its public API (docs: 平台API/zero-xyz.md).

  GET /v1/capabilities/hosts?limit&offset   -> {total, hosts:[{host,count,contentUpdatedAt}]}
  GET /v1/capabilities/by-host?host&limit&offset -> {host, total, capabilities:[...]}
  GET /v1/commerce/overview?window=30d&seller=<host>&limit=1 -> {headline:{onchainCalls,...}}

Rows are origin-level, like cdp-bazaar. Zero's own outcome numbers (calls
made through Zero) and its estimated on-chain seller volume go into
``quality.zero``; health and x402 support stay with our own probes. Hosts
already listed by another source are skipped. Each run fetches at most
MAX_HOSTS hosts: changed ones first, then the stalest.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import db
from .crawlers import UA, TIMEOUT, PartialCrawl, _host_slug, _url_acceptable

log = logging.getLogger("directory.zero")

API = "https://api.zero.xyz/v1"
SITE = "https://www.zero.xyz"
HOSTS_PAGE = 500
CAPS_PAGE = 100
MAX_HOSTS = 1000
MAX_CAPS_PER_HOST = 1000
REFRESH_SECS = 7 * 86400
REQUEST_GAP = 0.3
MAX_FAILURES = 25
SEEN_META = "zero:hosts"
_MAX_SAMPLES = 50
_USD_ASSETS = {"USD", "USDC", "USDT"}


def _norm_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _strip_tracking(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_row(host: str, caps: list[dict], total: int | None,
              commerce: dict | None) -> dict | None:
    host = _norm_host(host)
    origin = "https://" + host
    if not caps or not _url_acceptable(origin):
        return None
    prices, samples, protocols = [], [], set()
    statuses = {"healthy": 0, "down": 0, "unknown": 0}
    activations = reviews = 0
    rated_weight = rated_sum = 0.0
    first_party = False
    description = ""
    for cap in caps:
        url = cap.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if _norm_host(urlsplit(url).hostname or "") != host:
            continue
        protocol = str(cap.get("protocol") or "").lower()
        if protocol in ("x402", "mpp"):
            protocols.add(protocol)
        cost = cap.get("cost") or {}
        amount = _num(cost.get("amount"))
        # Zero reports unpriced/dynamic listings as amount "0"; that is not "free".
        if (amount is not None and amount > 0
                and str(cost.get("asset") or "").upper() in _USD_ASSETS):
            prices.append(amount)
        status = cap.get("availabilityStatus")
        statuses[status if status in statuses else "unknown"] += 1
        act = int(cap.get("activationCount") or 0)
        activations += act
        rating = cap.get("rating") or {}
        reviews += int(rating.get("reviews") or 0)
        rate = _num(rating.get("successRate"))
        if rating.get("state") == "rated" and rate is not None and act > 0:
            rated_weight += act
            rated_sum += rate * act
        first_party = first_party or bool(cap.get("isFirstParty"))
        if not description:
            description = (cap.get("whatItDoes") or cap.get("description") or "").strip()
        if len(samples) < _MAX_SAMPLES:
            sample = {"url": _strip_tracking(url),
                      "kind": f"{protocol or 'x402'}-resource",
                      "name": cap.get("name")}
            if cap.get("method"):
                sample["method"] = cap["method"]
            samples.append(sample)
    if not samples:
        return None
    zero_q = {
        "services": total if isinstance(total, int) else len(caps),
        "healthy": statuses["healthy"],
        "down": statuses["down"],
        "unknown": statuses["unknown"],
        "activations": activations,
        "reviews": reviews,
        "success_rate": round(rated_sum / rated_weight, 4) if rated_weight else None,
        "page": f"{SITE}/host/{host}",
    }
    headline = (commerce or {}).get("headline") or {}
    if headline:
        zero_q["onchain_calls_30d"] = headline.get("onchainCalls")
        zero_q["onchain_volume_usdc_30d"] = _num(headline.get("onchainVolumeUsdc"))
        zero_q["commerce_computed_at"] = (commerce or {}).get("computedAt")
    zero_q = {k: v for k, v in zero_q.items() if v is not None}
    count = zero_q["services"]
    tags = ["zero"] + sorted(protocols)
    if first_party:
        tags.append("zero-first-party")
    return {
        "slug": _host_slug(origin) + "-zero",
        "name": host,
        "url": origin,
        "description": (description or f"{count} paid API services listed on Zero")[:400],
        "category": "general",
        "chains": [],
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "well_known_url": origin + "/.well-known/x402",
        "resource_count": count,
        "resource_samples": samples,
        "quality": {"zero": zero_q},
        "source": "zero",
        "source_id": host,
        "tags": tags,
        "region": "global",
    }


def select_hosts(hosts: list[dict], seen: dict, claimed: set, now: float,
                 limit: int) -> tuple[list[dict], int]:
    """Changed hosts first (newest content first), then stale refreshes."""
    changed, stale = [], []
    for h in hosts:
        name = _norm_host(h.get("host") or "")
        if not name or name in claimed or not h.get("count"):
            continue
        prev = seen.get(name) or {}
        if prev.get("u") != h.get("contentUpdatedAt"):
            changed.append(h)
        elif now - float(prev.get("t") or 0) >= REFRESH_SECS:
            stale.append((float(prev.get("t") or 0), h))
    changed.sort(key=lambda h: h.get("contentUpdatedAt") or "", reverse=True)
    stale.sort(key=lambda x: x[0])
    pending = changed + [h for _, h in stale]
    return pending[:limit], len(pending)


class _Api:
    def __init__(self, client: httpx.Client):
        self.c = client

    def get(self, path: str, params: dict) -> dict:
        resp = None
        for attempt in range(4):
            time.sleep(REQUEST_GAP)
            try:
                resp = self.c.get(API + path, params=params)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _num(resp.headers.get("retry-after")
                            or resp.headers.get("x-ratelimit-reset"))
                time.sleep(min(wait if wait is not None else 5.0 * (attempt + 1), 90.0))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        raise RuntimeError(f"zero: {path} kept failing")

    def hosts(self) -> list[dict]:
        out, offset, total = [], 0, None
        while total is None or offset < total:
            data = self.get("/capabilities/hosts", {"limit": HOSTS_PAGE, "offset": offset})
            page = data.get("hosts") or []
            total = int(data.get("total") or 0)
            out.extend(page)
            if len(page) < HOSTS_PAGE:
                break
            offset += HOSTS_PAGE
        return out

    def caps(self, host: str) -> tuple[list[dict], int | None]:
        out, offset, total = [], 0, None
        while offset < MAX_CAPS_PER_HOST:
            data = self.get("/capabilities/by-host",
                            {"host": host, "limit": CAPS_PAGE, "offset": offset})
            page = data.get("capabilities") or []
            total = data.get("total") if isinstance(data.get("total"), int) else total
            out.extend(page)
            if len(page) < CAPS_PAGE or (total is not None and len(out) >= total):
                break
            offset += CAPS_PAGE
        return out, total

    def commerce(self, host: str) -> dict | None:
        try:
            return self.get("/commerce/overview",
                            {"window": "30d", "seller": host, "limit": 1})
        except Exception as e:  # noqa: BLE001
            log.info("zero: commerce for %s unavailable: %r", host, e)
            return None


def _claimed_hosts() -> set:
    hosts = set()
    with db.connect(read_only=True) as c:
        for r in c.execute("SELECT url FROM services WHERE source != 'zero' "
                           "AND url IS NOT NULL AND url != ''"):
            h = _norm_host(urlsplit(r["url"]).hostname or "")
            if h:
                hosts.add(h)
    return hosts


def _load_seen() -> dict:
    try:
        with db.connect(read_only=True) as c:
            raw = db.get_meta(c, SEEN_META)
        return json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        log.warning("zero: seen-map load failed: %r", e)
        return {}


def _save_seen(seen: dict) -> None:
    with db.writer() as c:
        db.set_meta(c, SEEN_META, json.dumps(seen, separators=(",", ":")))


def fetch_zero(max_hosts: int = MAX_HOSTS) -> list:
    seen = _load_seen()
    claimed = _claimed_hosts()
    now = time.time()
    out: list = []
    failures = 0
    done = 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA}) as client:
        api = _Api(client)
        hosts = api.hosts()
        if not hosts:
            raise RuntimeError("zero: host list empty")
        batch, pending = select_hosts(hosts, seen, claimed, now, max_hosts)
        log.info("zero: %d hosts, %d claimed by other sources, %d due, fetching %d",
                 len(hosts), len(claimed & {_norm_host(h.get("host") or "") for h in hosts}),
                 pending, len(batch))
        for h in batch:
            host = _norm_host(h["host"])
            try:
                caps, total = api.caps(host)
                row = build_row(host, caps, total, api.commerce(host) if caps else None)
                if row:
                    out.append(row)
                seen[host] = {"u": h.get("contentUpdatedAt"), "t": int(now)}
                done += 1
            except Exception as e:  # noqa: BLE001
                failures += 1
                log.warning("zero: host %s failed: %r", host, e)
                if failures >= MAX_FAILURES:
                    break
    try:
        _save_seen(seen)
    except Exception as e:  # noqa: BLE001
        log.warning("zero: seen-map save failed: %r", e)
    log.info("zero: fetched %d hosts -> %d rows, %d failures, %d still due",
             done, len(out), failures, pending - done)
    if failures >= MAX_FAILURES:
        raise PartialCrawl(out, f"{failures} host fetch failures")
    return out
