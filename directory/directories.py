"""Third-party paid-API directories as discovery sources (researched 2026-10-22).

Each source here publishes a public, unauthenticated JSON listing that robots.txt
allows. We only take the endpoints they list, aggregated per origin like
cdp-bazaar; health, x402 support and payment details are still proven by our own
probes and reverify_x402. Their own measurements are kept under
``quality.<source>`` so a card can show what each directory observed.

Upstream rows that only mirror the Coinbase CDP Bazaar are skipped at the query
(we crawl Bazaar ourselves), and hosts another source already lists are dropped
in jobs._run_one, same as paygent-discover.
"""
from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import urlsplit

import httpx

from .crawlers import UA, TIMEOUT, PartialCrawl, _host_slug, _url_acceptable, network_to_chain

log = logging.getLogger("directory.directories")

MAX_SAMPLES = 50
# Quick tunnels change name on every restart; listing them only adds dead rows.
EPHEMERAL_SUFFIXES = (".trycloudflare.com", ".ngrok-free.app", ".ngrok.io", ".ngrok.app",
                      ".loca.lt", ".hostc.app", ".serveo.net", ".localhost.run")


def _origin(url: str) -> str | None:
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or host.endswith(EPHEMERAL_SUFFIXES):
        return None
    origin = f"https://{p.netloc.lower()}"
    return origin if _url_acceptable(origin) else None


def _chain(value) -> str | None:
    if not value:
        return None
    s = str(value).strip().lower()
    return network_to_chain(s) or ("lightning" if s == "lightning" else s if s.isalpha() else None)


def aggregate(source: str, resources: list[dict]) -> list[dict]:
    """Collapse resource-level entries ({url, name, description, price_usd,
    chains, protocol, quality}) into one services row per origin."""
    grouped: dict[str, dict] = {}
    for res in resources:
        origin = _origin(res.get("url"))
        if not origin:
            continue
        g = grouped.setdefault(origin, {"samples": [], "keys": set(), "prices": [],
                                        "chains": set(), "protocols": set(),
                                        "names": [], "descs": [], "quality": None})
        p = urlsplit(res["url"].strip())
        key = p._replace(netloc=p.netloc.lower(), fragment="").geturl()
        if key not in g["keys"]:
            g["keys"].add(key)
            if len(g["samples"]) < MAX_SAMPLES:
                proto = (res.get("protocol") or "x402").lower()
                g["samples"].append({"url": key, "kind": f"{proto}-resource"})
        if isinstance(res.get("price_usd"), (int, float)) and res["price_usd"] >= 0:
            g["prices"].append(float(res["price_usd"]))
        g["chains"].update(c for c in res.get("chains") or [] if c)
        if res.get("protocol"):
            g["protocols"].add(res["protocol"].lower())
        if res.get("name"):
            g["names"].append(res["name"].strip())
        if res.get("description"):
            g["descs"].append(res["description"].strip())
        if g["quality"] is None and res.get("quality"):
            g["quality"] = res["quality"]

    out = []
    for origin, g in sorted(grouped.items()):
        host = urlsplit(origin).hostname
        digest = hashlib.sha256(origin.encode()).hexdigest()[:8]
        name = g["names"][0] if len(g["keys"]) == 1 and g["names"] else host
        prices = g["prices"]
        out.append({
            "slug": f"{_host_slug(origin)}-{source}-{digest}"[:90],
            "name": name[:200],
            "url": origin,
            "description": (g["descs"][0] if g["descs"] else f"Paid API listed on {source}")[:400],
            "category": "general",
            "chains": sorted(g["chains"]),
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "well_known_url": origin + "/.well-known/x402",
            "resource_count": len(g["keys"]),
            "resource_samples": g["samples"],
            "quality": {source: g["quality"]} if g["quality"] else None,
            "source": source,
            "source_id": f"origin:{origin}",
            "tags": [source] + sorted(g["protocols"]),
            "region": "global",
        })
    return out


def _get_json(client: httpx.Client, url: str, params: dict, gap: float):
    last: Exception | None = None
    for attempt in range(4):
        time.sleep(gap)
        try:
            r = client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                wait = r.headers.get("retry-after")
                time.sleep(min(float(wait), 120.0) if wait and wait.isdigit() else 5.0 * (attempt + 1))
                last = httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, ValueError) as e:
            last = e
            time.sleep(3.0 * (attempt + 1))
    raise last or RuntimeError("request failed")


def _paginate(source: str, url: str, items_of, params_of, done, gap: float,
              max_pages: int) -> list:
    """Walk a paged listing; a mid-walk failure keeps what was read."""
    items: list = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        for page in range(max_pages):
            try:
                data = _get_json(c, url, params_of(page), gap)
            except Exception as e:  # noqa: BLE001
                raise PartialCrawl(items, f"page {page} failed: {e!r}")
            batch = items_of(data) or []
            items.extend(batch)
            if not batch or done(data, page, len(batch)):
                return items
    raise PartialCrawl(items, f"max_pages={max_pages} exhausted")


def _finish(source: str, resources: list[dict], partial: str | None) -> list[dict]:
    rows = aggregate(source, resources)
    log.info("%s: %d resources -> %d origins%s", source, len(resources), len(rows),
             f" (partial: {partial})" if partial else "")
    if partial:
        raise PartialCrawl(rows, partial)
    return rows


# --- 402index.io -------------------------------------------------------------
# 165k endpoints, but ~145k are its copy of the CDP Bazaar. The rest are its own
# self-registrations plus the L402 / MPP directories it syncs (satring,
# l402apps, sponge, discovery, mpp.dev). Its `source` filter is a substring
# match, so the protocol slices overlap the source slices; dedup by id.
INDEX402_API = "https://402index.io/api/v1/services"
INDEX402_SLICES = ({"source": "self-registered"}, {"source": "satring"},
                   {"source": "sponge"}, {"source": "discovery"},
                   {"protocol": "MPP"}, {"protocol": "L402"})


def _index402_resource(s: dict) -> dict | None:
    if (s.get("health_status") or "") == "down":
        return None
    quality = {k: v for k, v in {
        "health": s.get("health_status"),
        "reliability": s.get("reliability_score"),
        "uptime_30d": s.get("uptime_30d"),
        "latency_p50_ms": s.get("latency_p50_ms"),
        "payment_valid": s.get("x402_payment_valid"),
        "last_checked": s.get("last_checked"),
    }.items() if v is not None}
    chain = _chain(s.get("payment_network"))
    return {"url": s.get("url"), "name": s.get("provider") or s.get("name"),
            "description": s.get("description") or s.get("name"),
            "price_usd": s.get("price_usd"), "chains": [chain] if chain else [],
            "protocol": s.get("protocol"), "quality": quality or None}


def fetch_402index(page_size: int = 200, gap: float = 0.7) -> list:
    seen: set = set()
    resources: list = []
    partial = None
    for flt in INDEX402_SLICES:
        try:
            rows = _paginate(
                "402index", INDEX402_API, lambda d: d.get("services"),
                lambda p, flt=flt: {**flt, "limit": page_size, "offset": p * page_size},
                lambda d, p, n: (p + 1) * page_size >= (d.get("total") or 0),
                gap, max_pages=400)
        except PartialCrawl as e:
            rows, partial = e.items, f"{flt}: {e.reason}"
        for s in rows:
            if not isinstance(s, dict) or s.get("id") in seen:
                continue
            seen.add(s.get("id"))
            if "bazaar" in (s.get("source") or "").split(",") and s.get("protocol") == "x402":
                continue
            res = _index402_resource(s)
            if res:
                resources.append(res)
        if partial:
            break
    return _finish("402index", resources, partial)


def _price_atomic(amount, decimals) -> float | None:
    try:
        return int(amount) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return None


def _drop_none(d: dict) -> dict | None:
    out = {k: v for k, v in d.items() if v is not None}
    return out or None


# --- nohumans.directory -------------------------------------------------------
# Probes every listing every few minutes and buys from some (paid_verified).
NOHUMANS_API = "https://nohumans.directory/v1/listings"


def fetch_nohumans(page_size: int = 200, gap: float = 0.7) -> list:
    partial = None
    try:
        rows = _paginate("nohumans", NOHUMANS_API, lambda d: d.get("results"),
                         lambda p: {"limit": page_size, "offset": p * page_size},
                         lambda d, p, n: d.get("next_offset") is None, gap, max_pages=500)
    except PartialCrawl as e:
        rows, partial = e.items, e.reason
    resources = []
    for s in rows:
        if not isinstance(s, dict) or s.get("status") == "failing":
            continue
        price = s.get("price_amount") if (s.get("price_currency") or "").upper() in ("USDC", "USD") else None
        resources.append({
            "url": s.get("endpoint_url"), "name": s.get("name"),
            "description": s.get("description"), "price_usd": price,
            "chains": [c for c in map(_chain, s.get("chains") or []) if c],
            "protocol": "x402",
            "quality": _drop_none({
                "status": s.get("status"), "paid_verified": s.get("paid_verified"),
                "distinct_payers": s.get("distinct_payers"),
                "probes": s.get("probe_count"), "probes_passed": s.get("probes_passed"),
                "p50_latency_ms": s.get("p50_latency_ms"),
                "evidence_tier": s.get("evidence_tier"),
                "last_probed": s.get("last_probed_at_iso"),
            }),
        })
    return _finish("nohumans", resources, partial)


# --- x402-list.com (CC BY 4.0, attribution required) --------------------------
X402LIST_API = "https://x402-list.com/api/v1/services"


def fetch_x402_list(page_size: int = 100, gap: float = 1.0) -> list:
    partial = None
    try:
        rows = _paginate("x402-list", X402LIST_API, lambda d: d.get("data"),
                         lambda p: {"per_page": page_size, "page": p + 1},
                         lambda d, p, n: p + 1 >= ((d.get("meta") or {}).get("total_pages") or 0),
                         gap, max_pages=200)
    except PartialCrawl as e:
        rows, partial = e.items, e.reason
    resources = []
    for s in rows:
        if not isinstance(s, dict) or s.get("status") == "offline":
            continue
        if s.get("source") == "imported:bazaar":
            continue
        a = s.get("assessment") or {}
        t = a.get("traction") or {}
        resources.append({
            "url": s.get("base_url"), "name": s.get("name"),
            "description": s.get("description"), "price_usd": s.get("min_price_usd"),
            "chains": [c for c in map(_chain, s.get("networks_caip2") or []) if c],
            "protocol": "x402",
            "quality": _drop_none({
                "status": s.get("status"), "payment_ready": s.get("payment_ready"),
                "verified": s.get("verified"), "uptime_24h": s.get("uptime_24h"),
                "uptime_30d": a.get("reliability_uptime_30d"),
                "compliance_grade": a.get("compliance_grade"),
                "risk_level": a.get("risk_level"),
                "volume_usd_30d": t.get("volume_usd_30d"),
                "unique_buyers_30d": t.get("unique_buyers_30d"),
                "last_checked": s.get("last_checked_at"),
                "attribution": "Data: x402-list.com (CC BY 4.0)",
            }),
        })
    return _finish("x402-list", resources, partial)


# --- aegis.borisinc.com --------------------------------------------------------
AEGIS_API = "https://aegis.borisinc.com/discover"


def fetch_aegis(page_size: int = 50, gap: float = 1.0) -> list:
    partial = None
    try:
        rows = _paginate("aegis", AEGIS_API, lambda d: d.get("results"),
                         lambda p: {"limit": page_size, "offset": p * page_size},
                         lambda d, p, n: (p + 1) * page_size >= (d.get("count") or 0),
                         gap, max_pages=600)
    except PartialCrawl as e:
        rows, partial = e.items, e.reason
    resources = []
    for s in rows:
        if not isinstance(s, dict) or s.get("tier") == "flagged":
            continue
        chain = _chain(s.get("network"))
        resources.append({
            "url": s.get("url"), "name": s.get("name"),
            "description": s.get("description"), "price_usd": s.get("price_usd"),
            "chains": [chain] if chain else [], "protocol": "x402",
            "quality": _drop_none({"tier": s.get("tier"), "trust_score": s.get("trust_score")}),
        })
    return _finish("aegis", resources, partial)


# --- agentgates.ai -------------------------------------------------------------
# Mirrors CDP Bazaar (skipped, we crawl it) and the PayAI facilitator list.
AGENTGATES_API = "https://agentgates.ai/api/directory/resources"


def fetch_agentgates(page_size: int = 100, gap: float = 0.7) -> list:
    partial = None
    try:
        rows = _paginate("agentgates", AGENTGATES_API, lambda d: d.get("items"),
                         lambda p: {"limit": page_size, "offset": p * page_size},
                         lambda d, p, n: (p + 1) * page_size >= ((d.get("pagination") or {}).get("total") or 0),
                         gap, max_pages=1000)
    except PartialCrawl as e:
        rows, partial = e.items, e.reason
    resources = []
    for s in rows:
        if not isinstance(s, dict) or s.get("delisted") or s.get("source") == "cdp":
            continue
        legs = s.get("legs") or []
        prices = [p for p in (_price_atomic(l.get("amountAtomic"), l.get("decimals")) for l in legs) if p is not None]
        traffic = s.get("traffic") or {}
        resources.append({
            "url": s.get("resource"), "name": s.get("displayName") or s.get("name"),
            "description": s.get("description"),
            "price_usd": min(prices) if prices else None,
            "chains": [c for c in (_chain(l.get("chain") or l.get("network")) for l in legs) if c],
            "protocol": "x402",
            "quality": _drop_none({"upstream": s.get("source"),
                                   "calls_30d": traffic.get("calls30d"),
                                   "payers_30d": traffic.get("payers30d"),
                                   "last_called": traffic.get("lastCalledAt"),
                                   "last_seen": s.get("lastSeenAt")}),
        })
    return _finish("agentgates", resources, partial)


# --- the402.dev ----------------------------------------------------------------
# Re-probes Bazaar (skipped) plus direct submissions.
THE402_API = "https://the402.dev/api/listings"
THE402_OK = {"payable", "needs-params"}


def fetch_the402(page_size: int = 500, gap: float = 0.7) -> list:
    partial = None
    try:
        rows = _paginate("the402", THE402_API, lambda d: d.get("items"),
                         lambda p: {"limit": page_size, "offset": p * page_size},
                         lambda d, p, n: (p + 1) * page_size >= (d.get("total") or 0),
                         gap, max_pages=200)
    except PartialCrawl as e:
        rows, partial = e.items, e.reason
    resources = []
    for s in rows:
        if not isinstance(s, dict) or s.get("source") == "bazaar" or s.get("verdict") not in THE402_OK:
            continue
        chain = _chain(s.get("network"))
        resources.append({
            "url": s.get("resource"), "name": s.get("name"),
            "description": s.get("description"),
            "price_usd": s.get("price_usd") or None,
            "chains": [chain] if chain else [], "protocol": "x402",
            "quality": _drop_none({"verdict": s.get("verdict"), "http_status": s.get("http_status"),
                                   "latency_ms": s.get("latency_ms"), "verified_at": s.get("verified_at")}),
        })
    return _finish("the402", resources, partial)


# --- mpp.dev (official MPP service registry) -----------------------------------
MPPDEV_API = "https://mpp.dev/api/services"


def fetch_mppdev(gap: float = 0.5) -> list:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        data = _get_json(c, MPPDEV_API, {}, gap)
    resources = []
    for s in data.get("services") or []:
        if not isinstance(s, dict) or (s.get("status") or "active") != "active":
            continue
        base = (s.get("serviceUrl") or s.get("url") or "").rstrip("/")
        q = _drop_none({"methods": sorted((s.get("methods") or {}).keys()) or None,
                        "integration": s.get("integration"),
                        "docs": (s.get("docs") or {}).get("apiReference")})
        eps = s.get("endpoints") or [{}]
        for ep in eps:
            pay = ep.get("payment") or {}
            resources.append({
                "url": base + (ep.get("path") or ""), "name": s.get("name"),
                "description": s.get("description"),
                "price_usd": _price_atomic(pay.get("amount"), pay.get("decimals")),
                "chains": [], "protocol": "mpp", "quality": q,
            })
    return _finish("mppdev", resources, None)


# --- verantis.ai ---------------------------------------------------------------
# Host-level entities with per-chain rails and 90-day on-chain buyer counts.
VERANTIS_API = "https://api.verantis.ai/v1/entities"


def fetch_verantis(gap: float = 0.5) -> list:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        data = _get_json(c, VERANTIS_API, {"limit": 10000}, gap)
    resources = []
    for e in data.get("entities") or []:
        if not isinstance(e, dict) or set(e.get("sources") or []) <= {"cdp-bazaar"}:
            continue
        rails = e.get("rails") or []
        url = next((r.get("sample_url") for r in rails if r.get("sample_url")), None) or f"https://{e.get('host')}"
        resources.append({
            "url": url, "name": e.get("name"), "description": e.get("category"),
            "price_usd": e.get("min_price_usd"),
            "chains": [c for c in (_chain(r.get("chain")) for r in rails) if c],
            "protocol": (e.get("protocols") or ["x402"])[0],
            "quality": _drop_none({"trust_tier": e.get("trust_tier"),
                                   "classification": e.get("classification"),
                                   "buyers_90d": e.get("buyers"), "repeat": e.get("repeat"),
                                   "probed_at": e.get("probed_at")}),
        })
    return _finish("verantis", resources, None)


FETCHERS = {
    "402index": fetch_402index,
    "nohumans": fetch_nohumans,
    "x402-list": fetch_x402_list,
    "aegis": fetch_aegis,
    "agentgates": fetch_agentgates,
    "the402": fetch_the402,
    "mppdev": fetch_mppdev,
    "verantis": fetch_verantis,
}


# --- agenstry.com paid_api records (shared full export feed) -------------------
def fetch_agenstry_paid() -> list:
    from . import agenstry
    resources = []
    for r in agenstry.fetch_feed():
        if r.get("kind") != "paid_api" or not r.get("payment_protocol"):
            continue
        chain = _chain(r.get("payment_network"))
        resources.append({
            "url": r.get("documentation_url") or r.get("provider_url") or r.get("url"),
            "name": r.get("provider") or r.get("name"),
            "description": r.get("description"), "price_usd": r.get("min_price_usd"),
            "chains": [chain] if chain else [],
            "protocol": str(r.get("payment_protocol")).lower(),
            "quality": _drop_none({"payment_evidence": r.get("payment_evidence"),
                                   "domain_verified": r.get("domain_verified") or None,
                                   "first_seen": r.get("first_seen")}),
        })
    return _finish("agenstry-paid", resources, None)


FETCHERS["agenstry-paid"] = fetch_agenstry_paid
