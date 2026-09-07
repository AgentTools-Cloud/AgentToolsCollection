"""Authoritative x402 tagging for MCP servers and A2A agents.

Instead of flagging x402 support by string-matching "x402" in metadata, this
module probes each endpoint with crawlers.verify_x402() and:
  * sets mcp_servers.x402_supported / a2a_agents.x402_supported from the verdict
  * mirrors every *verified* paid endpoint into the services (x402) table,
    de-duplicated by normalized URL, tagged with its delivery channel
    (mcp / a2a) so the catalog can surface "agent-ready" x402 services.

Run: python -m directory.reverify_x402 [--targets mcp,a2a] [--workers 24]
                                       [--limit N] [--only-unverified]
"""
from __future__ import annotations

import json
import os
import argparse
import re as _re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from . import crawlers, db

# Map payment network identifiers to friendly chain names.
_NETWORKS = {
    "sui": "sui", "sui:mainnet": "sui",
    "eip155:8453": "base", "8453": "base", "base": "base",
    "eip155:84532": "base-sepolia", "84532": "base-sepolia",
    "eip155:1": "ethereum", "1": "ethereum", "ethereum": "ethereum",
    "eip155:137": "polygon", "137": "polygon", "polygon": "polygon",
    "solana": "solana",
}


# How long a verified listing's descriptor may go unread. Operators fix their
# manifests and nothing here noticed until they emailed.
_DESCRIPTOR_TTL = int(os.environ.get("AGENT_TOOLS_DESCRIPTOR_TTL", 7 * 86400))


def _price_span(samples):
    """(min, max) USD across a descriptor's advertised resources.

    A multi-tier service declares one amount per tier; the single accept that
    verify_x402 returns only ever sees the first of them.
    """
    if not isinstance(samples, list):
        return None, None
    prices = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        for acc in (s.get("accepts") or []):
            if isinstance(acc, dict):
                v = crawlers._bazaar_price_usd(acc)
                if v is not None:
                    prices.append(v)
    return (min(prices), max(prices)) if prices else (None, None)


def _slugify(text: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "unnamed"


def _norm_url(url: str) -> str:
    """Normalized dedup key: host + path, scheme/query stripped, trailing
    slash and a trailing /mcp|/sse segment removed (so an origin entry and its
    /mcp endpoint collapse to the same service)."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    if "//" not in u:
        u = "https://" + u
    p = urlparse(u)
    path = p.path.rstrip("/")
    path = _re.sub(r"/(mcp|sse)$", "", path)
    return (p.hostname or "") + path


def _chain_from_payment(payment: dict | None) -> list:
    if not payment:
        return []
    net = db.network_id(payment.get("network"))
    return [_NETWORKS.get(net, net)] if net else []


def _load_existing_service_keys(conn) -> set:
    keys = set()
    for r in conn.execute("SELECT url, mcp_url FROM services").fetchall():
        for v in (r["url"], r["mcp_url"]):
            k = _norm_url(v)
            if k:
                keys.add(k)
    return keys


# Friendly catalog category per delivery channel.
_CATEGORY = {"mcp": "model-context-protocol-mcp", "a2a": "a2a-agent"}


def _build_service(t: dict, verdict: dict) -> dict:
    """Turn a probed (verified) endpoint into a services-table row dict."""
    pay = verdict.get("payment") or {}
    host = urlparse(t["endpoint"] if "//" in (t["endpoint"] or "")
                    else "https://" + (t["endpoint"] or "")).hostname or ""
    origin_src = t.get("source") or t["delivery"]
    origin_sid = t.get("source_id") or t.get("slug")
    amount = pay.get("max_amount_usdc")
    fac = pay.get("facilitator")
    if isinstance(fac, dict):
        fac = fac.get("url") or fac.get("name") or None
    elif fac is not None and not isinstance(fac, str):
        fac = str(fac)
    return {
        "slug": _slugify(t.get("slug") or host) + "-x402",
        "name": t.get("name") or host,
        "url": t["endpoint"],
        "description": t.get("description"),
        "category": t.get("category") or _CATEGORY.get(t["delivery"], t["delivery"]),
        "chains": _chain_from_payment(pay),
        "price_min": amount,
        "price_max": amount,
        "facilitator": fac,
        "mcp_url": t["endpoint"] if t["delivery"] == "mcp" else None,
        "well_known_url": (f"https://{host}/.well-known/x402" if host else None),
        "source": t["delivery"],            # "mcp" or "a2a"
        "source_id": f"{origin_src}:{origin_sid}",
        "tags": [t["delivery"], "x402"],
        "region": "global",
        "payment": pay or None,
        "confidence": 0.8,                  # machine-verified
    }


def _mirror_service(service: dict) -> bool:
    """Upsert a verified paid endpoint into services + mark x402_ok. Returns
    True if a new row was created."""
    def op(service=service):
        with db.writer() as c:
            created, _ = db.upsert_service(c, dict(service))
            # the verify already proved a 402 challenge -> mark x402_ok
            c.execute(
                "UPDATE services SET x402_ok=1 WHERE source=? AND source_id=?",
                (service["source"], service["source_id"]))
            return created
    return bool(db.with_retry(op))


def verify_and_mirror(endpoint: str, *, slug: str, name: str | None = None,
                      description: str | None = None, homepage: str | None = None,
                      delivery: str = "mcp", source: str | None = None,
                      source_id: str | None = None,
                      verdict: dict | None = None) -> dict:
    """Probe a single endpoint for x402 and, if verified, mirror it into the
    services (x402) table — the live equivalent of one reverify() iteration.

    Safe to call from a request handler (does its own read + write txns).
    Returns {x402, status, payment, service_slug, created, duplicate}.
    """
    if verdict is None:
        try:
            verdict = crawlers.verify_x402(endpoint)
        except Exception as e:
            verdict = {"status": "error", "evidence": [repr(e)], "payment": None}
    is_ver = verdict.get("status") == "verified"
    out = {"x402": is_ver, "status": verdict.get("status"),
           "payment": verdict.get("payment"), "service_slug": None,
           "created": False, "duplicate": False}
    if not is_ver:
        return out

    t = {"slug": slug, "name": name, "description": description,
         "endpoint": endpoint, "homepage": homepage, "delivery": delivery,
         "source": source, "source_id": source_id,
         "category": _CATEGORY.get(delivery, delivery)}
    service = _build_service(t, verdict)
    out["service_slug"] = service["slug"]

    with db.connect(read_only=True) as c:
        existing_keys = _load_existing_service_keys(c)
    key = _norm_url(endpoint) or _norm_url(homepage)
    if key and key in existing_keys:
        # already catalogued under some source — refresh it but report dupe
        out["duplicate"] = True
        _mirror_service(service)
        return out
    out["created"] = _mirror_service(service)
    return out


def _gather(conn, targets, limit, only_unverified):
    """Return list of probe tasks: dicts with table/id/endpoint + display meta."""
    tasks = []
    if "mcp" in targets:
        sql = ("SELECT id, slug, name, description, endpoint_url, homepage_url, "
               "source, source_id, x402_supported FROM mcp_servers "
               "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''")
        if only_unverified:
            sql += " AND COALESCE(x402_supported,0)=0"
        for r in conn.execute(sql).fetchall():
            tasks.append({
                "table": "mcp_servers", "id": r["id"], "slug": r["slug"],
                "name": r["name"], "description": r["description"],
                "endpoint": r["endpoint_url"], "homepage": r["homepage_url"],
                "category": "model-context-protocol-mcp",
                "source": r["source"], "source_id": r["source_id"],
                "delivery": "mcp", "was": r["x402_supported"] or 0,
            })
    if "a2a" in targets:
        sql = ("SELECT id, slug, name, description, endpoint_url, homepage_url, "
               "card_url, source, source_id, x402_supported FROM a2a_agents "
               "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''")
        if only_unverified:
            sql += " AND COALESCE(x402_supported,0)=0"
        for r in conn.execute(sql).fetchall():
            tasks.append({
                "table": "a2a_agents", "id": r["id"], "slug": r["slug"],
                "name": r["name"], "description": r["description"],
                "endpoint": r["endpoint_url"], "homepage": r["homepage_url"],
                "category": "a2a-agent",
                "source": r["source"], "source_id": r["source_id"],
                "delivery": "a2a", "was": r["x402_supported"] or 0,
            })
    if "services" in targets:
        # Native x402 submissions live directly in `services`; the mcp/a2a
        # mirror never touches them, so verify them in place. Three reasons to
        # probe: never verified, no payTo on file, or the descriptor has not
        # been re-read in a while -- operators fix their manifests and nothing
        # here noticed until they emailed.
        stale_before = int(time.time()) - _DESCRIPTOR_TTL
        sql = ("SELECT id, slug, name, url, well_known_url FROM services "
               "WHERE url IS NOT NULL AND url != '' AND health='ok' "
               "AND (COALESCE(x402_ok,0)=0 OR payment IS NULL OR payment='' "
               "     OR COALESCE(updated_at,0) < ?) "
               "ORDER BY COALESCE(updated_at,0) ASC")
        rows = conn.execute(sql, (stale_before,)).fetchall()
        for r in rows:
            tasks.append({
                "table": "services", "id": r["id"], "slug": r["slug"],
                "name": r["name"], "description": None,
                "endpoint": r["url"], "homepage": None,
                "well_known": r["well_known_url"],
                "category": None, "source": None, "source_id": None,
                "delivery": "x402", "was": 0,
            })
    if limit:
        tasks = tasks[:limit]
    return tasks


def _probe(task):
    try:
        verdict = crawlers.verify_x402(task["endpoint"], task.get("well_known"))
    except Exception as e:  # never let one bad host kill the pool
        verdict = {"status": "error", "evidence": [repr(e)], "payment": None}
    task["verdict"] = verdict
    # How many resources a descriptor advertises is independent of whether the
    # payment probe succeeded, and fetch_well_known is memoised, so this costs
    # a cache hit rather than another request.
    if task["table"] == "services":
        try:
            task["resources"] = crawlers.fetch_wellknown_resources(
                task["endpoint"], task.get("well_known")) or {}
        except Exception:
            task["resources"] = {}
    return task


def reverify(targets=("mcp", "a2a"), workers=24, limit=None,
             only_unverified=False) -> dict:
    with db.connect(read_only=True) as c:
        tasks = _gather(c, targets, limit, only_unverified)
        existing_keys = _load_existing_service_keys(c)

    total = len(tasks)
    print(f"[reverify] probing {total} endpoints "
          f"(targets={','.join(targets)}, workers={workers})", flush=True)

    verified = []   # mcp/a2a tasks whose verdict == verified (mirrored below)
    flag_updates = {"mcp_servers": [], "a2a_agents": []}
    service_ok_ids = []   # native x402 services proven this pass
    service_payments = []  # (payment_json, id) for rows missing payment
    service_resources = []  # (count, samples, ts, id) regardless of payment
    done = 0
    n_ver = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_probe, t) for t in tasks]
        for fut in as_completed(futs):
            t = fut.result()
            done += 1
            is_ver = t["verdict"]["status"] == "verified"
            if t["table"] == "services":
                # sticky badge: only ever set x402_ok, never revoke
                if is_ver:
                    service_ok_ids.append(t["id"])
                    n_ver += 1
                _res = t.get("resources") or {}
                if _res.get("resource_count") is not None:
                    service_resources.append((
                        _res.get("resource_count"),
                        json.dumps(_res.get("resource_samples"), ensure_ascii=False)
                        if _res.get("resource_samples") else None,
                        int(time.time()), t["id"]))
                pay = t["verdict"].get("payment") or None
                if pay and pay.get("pay_to"):
                    res = t.get("resources") or {}
                    lo, hi = _price_span(res.get("resource_samples"))
                    if lo is None:
                        lo = hi = pay.get("max_amount_usdc")
                    service_payments.append((
                        json.dumps(pay, ensure_ascii=False),
                        res.get("resource_count"),
                        json.dumps(res.get("resource_samples"), ensure_ascii=False)
                        if res.get("resource_samples") else None,
                        lo, hi, int(time.time()), t["id"]))
            else:
                flag_updates[t["table"]].append((1 if is_ver else 0, t["id"]))
                if is_ver:
                    n_ver += 1
                    verified.append(t)
            if done % 500 == 0 or done == total:
                print(f"[reverify] {done}/{total} probed, verified={n_ver}",
                      flush=True)

    # 1. truthful x402_supported flag on source tables
    for table, ups in flag_updates.items():
        if not ups:
            continue
        for start in range(0, len(ups), 500):
            chunk = ups[start:start + 500]

            def op(chunk=chunk, table=table):
                with db.writer() as c:
                    c.executemany(
                        f"UPDATE {table} SET x402_supported=? WHERE id=?", chunk)
            db.with_retry(op)
    print(f"[reverify] flags updated: "
          f"mcp={len(flag_updates['mcp_servers'])} "
          f"a2a={len(flag_updates['a2a_agents'])}", flush=True)

    # 1b. native x402 services: sticky x402_ok=1 on verified (set-only)
    for start in range(0, len(service_ok_ids), 500):
        chunk = service_ok_ids[start:start + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET x402_ok=1 WHERE id=?",
                    [(i,) for i in chunk])
        db.with_retry(op)
    if service_ok_ids:
        print(f"[reverify] native x402 services tagged: "
              f"{len(service_ok_ids)}", flush=True)

    # 1b2. descriptor resource counts, independent of payment. Gating this
    # on a payTo left most listings frozen at whatever the first crawl saw.
    for start in range(0, len(service_resources), 500):
        chunk = service_resources[start:start + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET resource_count=?, "
                    "resource_samples=COALESCE(?, resource_samples), "
                    "updated_at=? WHERE id=?", chunk)
        db.with_retry(op)
    if service_resources:
        print(f"[reverify] resource counts refreshed: "
              f"{len(service_resources)}", flush=True)

    # 1c. write back what the descriptor says now. This refreshes rather than
    # fills: a listing approved with a mistyped price kept showing it forever,
    # because nothing ever re-read the manifest that contradicted it.
    for start in range(0, len(service_payments), 500):
        chunk = service_payments[start:start + 500]

        def op(chunk=chunk):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET payment=?, "
                    "resource_count=COALESCE(?, resource_count), "
                    "resource_samples=COALESCE(?, resource_samples), "
                    "price_min=COALESCE(?, price_min), "
                    "price_max=COALESCE(?, price_max), "
                    "updated_at=? WHERE id=?", chunk)
        db.with_retry(op)
    if service_payments:
        print(f"[reverify] payment metadata recorded: "
              f"{len(service_payments)}", flush=True)

    # 2. mirror verified endpoints into services (x402) table
    svc_new = svc_skip = svc_bad = 0
    for t in verified:
        key = _norm_url(t["endpoint"]) or _norm_url(t["homepage"])
        if key and key in existing_keys:
            svc_skip += 1
            continue
        if key:
            existing_keys.add(key)
        try:
            service = _build_service(t, t["verdict"])
            svc_new += int(_mirror_service(service))
        except Exception as exc:
            # One malformed descriptor must not discard a step that already
            # spent ~45min of CPU. Report it and keep mirroring the rest.
            svc_bad += 1
            print(f"[reverify] mirror_failed slug={t.get('slug')!r} "
                  f"{type(exc).__name__}: {exc}", flush=True)

    print(f"[reverify] services mirrored: new/updated_paid={len(verified)} "
          f"inserted={svc_new} skipped_dupe={svc_skip} failed={svc_bad}",
          flush=True)

    return {"probed": total, "verified": n_ver,
            "services_inserted": svc_new, "services_skipped": svc_skip,
            "services_failed": svc_bad,
            "native_services_tagged": len(service_ok_ids)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="mcp,a2a")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-unverified", action="store_true")
    args = ap.parse_args(argv)
    targets = tuple(s.strip() for s in args.targets.split(",") if s.strip())
    res = reverify(targets=targets, workers=args.workers, limit=args.limit,
                   only_unverified=args.only_unverified)
    print("[reverify] done:", res, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
