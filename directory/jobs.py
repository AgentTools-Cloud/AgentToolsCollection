"""CLI entry for periodic crawl + health jobs.

Usage:
  python -m directory.jobs init
  python -m directory.jobs crawl [source]
  python -m directory.jobs health
  python -m directory.jobs stats
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import a2a as a2a_mod
from . import crawlers, db, mailer

log = logging.getLogger("directory.jobs")
SEED_FILE = Path(__file__).resolve().parent / "seed.json"
A2A_SEED_FILE = Path(__file__).resolve().parent / "a2a_seed.json"


def cmd_init() -> int:
    db.init_db()
    log.info("schema initialized at %s", db.DEFAULT_DB_PATH)
    if SEED_FILE.exists():
        with SEED_FILE.open(encoding="utf-8") as f:
            seed = json.load(f)
        added = 0
        with db.writer() as c:
            for row in seed:
                row.setdefault("source", "manual")
                row.setdefault("source_id", row.get("slug") or row["url"])
                created, _ = db.upsert_service(c, dict(row))
                added += int(created)
        log.info("seeded %d new services (file has %d)", added, len(seed))
    return 0


def _start_run(name: str) -> int:
    def op():
        with db.writer() as c:
            return db.record_crawl_start(c, name)
    return db.with_retry(op)


def _finish_run(run_id: int, added: int, updated: int, errors: list[str], status: str) -> None:
    def op():
        with db.writer() as c:
            db.record_crawl_finish(c, run_id, added, updated, errors, status=status)
    db.with_retry(op)


def _run_one(name):
    fn = crawlers.ALL_CRAWLERS[name]
    errors = []
    added = updated = 0
    run_id = _start_run(name)
    try:
        items = fn()
    except Exception as e:
        errors.append(f"fetch failed: {e!r}")
        _finish_run(run_id, 0, 0, errors, status="error")
        return 0, 0, errors

    # Write in short batches instead of holding a writer transaction while a
    # full source is processed. This keeps the website responsive and avoids
    # timer jobs fighting each other for a long sqlite write lock.
    batch_size = 50
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        def op(batch=batch):
            batch_added = batch_updated = 0
            batch_errors = []
            with db.writer() as c:
                for item in batch:
                    try:
                        if not crawlers._url_acceptable(item.get("url") or ""):
                            log.info("skip non-discoverable url: %s", item.get("url"))
                            continue
                        created, _ = db.upsert_service(c, item)
                        if created:
                            batch_added += 1
                        else:
                            batch_updated += 1
                    except Exception as e:
                        batch_errors.append(f"upsert {item.get('slug')}: {e!r}")
            return batch_added, batch_updated, batch_errors
        batch_added, batch_updated, batch_errors = db.with_retry(op)
        added += batch_added
        updated += batch_updated
        errors.extend(batch_errors)

    _finish_run(run_id, added, updated, errors,
                status="ok" if not errors else "partial")
    return added, updated, errors


def cmd_crawl(only=None) -> int:
    if only and only not in crawlers.ALL_CRAWLERS:
        print(f"unknown source {only!r}. available: {list(crawlers.ALL_CRAWLERS)}", file=sys.stderr)
        return 2
    names = [only] if only else list(crawlers.ALL_CRAWLERS)
    total_added = total_updated = 0
    for n in names:
        added, updated, errors = _run_one(n)
        log.info("crawl %s: added=%d updated=%d errors=%d", n, added, updated, len(errors))
        for e in errors[:5]:
            log.warning("  %s: %s", n, e)
        total_added += added
        total_updated += updated
    log.info("done. added=%d updated=%d", total_added, total_updated)
    if total_added:
        log.info("probing %d newly added service(s) for health...", total_added)
        cmd_health(only_unknown=True)
    # The single agent-tools-crawl timer also refreshes the MCP + A2A
    # directories so all three resource types stay current together.
    if only is None:
        try:
            cmd_crawl_mcp()
        except Exception as e:
            log.warning("mcp crawl failed: %r", e)
        try:
            cmd_crawl_a2a()
        except Exception as e:
            log.warning("a2a crawl failed: %r", e)
        # Clear the submission queue automatically — there is no human
        # gate. verified -> listed, rejected -> dropped, uncertain ->
        # retried next cycle.
        try:
            cmd_auto_review()
        except Exception as e:
            log.warning("auto-review failed: %r", e)
    return 0


def cmd_crawl_mcp(only=None) -> int:
    """Import MCP servers from the standalone MCP directory sources."""
    names = [only] if only else list(crawlers.MCP_CRAWLERS)
    total_added = total_updated = 0
    for name in names:
        fn = crawlers.MCP_CRAWLERS.get(name)
        if fn is None:
            print(f"unknown mcp source {name!r}. available: {list(crawlers.MCP_CRAWLERS)}",
                  file=sys.stderr)
            return 2
        run_id = _start_run(f"mcp:{name}")
        errors: list[str] = []
        added = updated = 0
        try:
            items = fn()
        except Exception as e:
            _finish_run(run_id, 0, 0, [f"fetch failed: {e!r}"], status="error")
            log.warning("mcp crawl %s fetch failed: %r", name, e)
            continue
        batch_size = 100
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            def op(batch=batch):
                b_add = b_upd = 0
                b_err: list[str] = []
                with db.writer() as c:
                    for item in batch:
                        try:
                            created, _ = db.upsert_mcp_server(c, item)
                            b_add += int(created)
                            b_upd += int(not created)
                        except Exception as e:
                            b_err.append(f"upsert {item.get('slug')}: {e!r}")
                return b_add, b_upd, b_err
            b_add, b_upd, b_err = db.with_retry(op)
            added += b_add; updated += b_upd; errors.extend(b_err)
        _finish_run(run_id, added, updated, errors,
                    status="ok" if not errors else "partial")
        log.info("mcp crawl %s: added=%d updated=%d errors=%d",
                 name, added, updated, len(errors))
        total_added += added; total_updated += updated
    log.info("mcp crawl done. added=%d updated=%d", total_added, total_updated)
    if total_added:
        cmd_health_mcp(only_unknown=True)
    return 0


def cmd_crawl_a2a() -> int:
    """Refresh A2A agents from the hand-picked seed + awesome-a2a directories."""
    run_id = _start_run("a2a:crawl")
    added = updated = 0
    errors: list[str] = []
    try:
        if A2A_SEED_FILE.exists():
            s = a2a_mod.crawl_seeds(str(A2A_SEED_FILE))
            added += s["inserted"]; updated += s["updated"]
            log.info("a2a seed: inserted=%d updated=%d failed=%d",
                     s["inserted"], s["updated"], s["failed"])
    except Exception as e:
        errors.append(f"seed: {e!r}"); log.warning("a2a seed crawl failed: %r", e)
    try:
        d = a2a_mod.crawl_directories()
        added += d["inserted"]; updated += d["updated"]
        log.info("a2a directories: candidates=%d resolved=%d inserted=%d updated=%d",
                 d["candidates"], d["resolved"], d["inserted"], d["updated"])
    except Exception as e:
        errors.append(f"directories: {e!r}"); log.warning("a2a dir crawl failed: %r", e)
    _finish_run(run_id, added, updated, errors,
                status="ok" if not errors else "partial")
    if added:
        cmd_health_a2a(only_unknown=True)
    return 0


def cmd_health(only_unknown: bool = False) -> int:
    n_ok = n_down = n_degraded = 0
    sql = "SELECT id, url, well_known_url FROM services"
    if only_unknown:
        # crawler inserts leave health NULL (bypasses the column DEFAULT), so
        # treat NULL / '' / 'unknown' all as "never probed yet".
        sql += " WHERE health IS NULL OR health='' OR health='unknown'"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        if only_unknown:
            log.info("no new (unknown-health) services to probe")
        return 0

    batch_size = 50
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start:start + batch_size]
        updates = []
        history = []
        now = int(time.time())
        for r in batch_rows:
            res = crawlers.probe_health(r["url"], r["well_known_url"])
            h = res["status"]
            x = 1 if res["x402"] else 0
            updates.append((h, now, res["latency_ms"], res["http_status"], x, r["id"]))
            history.append((r["id"], now, h, res["latency_ms"], res["http_status"], x))
            if h == "ok":
                n_ok += 1
            elif h == "down":
                n_down += 1
            else:
                n_degraded += 1

        def op(updates=updates, history=history):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET health=?, health_checked=?, latency_ms=?, "
                    "http_status=?, x402_ok=? WHERE id=?",
                    updates,
                )
                c.executemany(
                    "INSERT INTO health_history "
                    "(service_id, checked_at, status, latency_ms, http_status, x402) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    history,
                )
        db.with_retry(op)
        log.info("health progress: checked=%d/%d ok=%d degraded=%d down=%d",
                 min(start + batch_size, len(rows)), len(rows), n_ok, n_degraded, n_down)

    # keep the time series bounded (90d window is plenty for uptime stats)
    cutoff = int(time.time()) - 90 * 86400
    def _prune():
        with db.writer() as c:
            c.execute("DELETE FROM health_history WHERE checked_at < ?", (cutoff,))
    db.with_retry(_prune)

    log.info("health: ok=%d degraded=%d down=%d", n_ok, n_degraded, n_down)
    # The single agent-tools-health timer also refreshes A2A + MCP liveness.
    if not only_unknown:
        try:
            cmd_health_a2a()
        except Exception as e:
            log.warning("a2a health failed: %r", e)
        try:
            cmd_health_mcp()
        except Exception as e:
            log.warning("mcp health failed: %r", e)
    return 0


def cmd_health_a2a(only_unknown: bool = False) -> int:
    """Liveness-probe indexed A2A agents (card / endpoint reachability)."""
    sql = "SELECT id, card_url, endpoint_url FROM a2a_agents"
    if only_unknown:
        sql += " WHERE health IS NULL OR health='' OR health='unknown'"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        return 0
    n_ok = n_deg = n_down = 0
    now = int(time.time())
    updates = []
    for r in rows:
        res = a2a_mod.probe_a2a_health(r["card_url"], r["endpoint_url"])
        h = res["status"]
        last_ok = now if h == "ok" else None
        updates.append((h, now, res["latency_ms"], last_ok, r["id"]))
        n_ok += h == "ok"; n_deg += h == "degraded"; n_down += h == "down"

    def op():
        with db.writer() as c:
            c.executemany(
                "UPDATE a2a_agents SET health=?, health_checked=?, latency_ms=?, "
                "last_success_at=COALESCE(?, last_success_at) WHERE id=?",
                updates,
            )
    db.with_retry(op)
    log.info("a2a health: ok=%d degraded=%d down=%d", n_ok, n_deg, n_down)
    return 0


def cmd_health_mcp(only_unknown: bool = False) -> int:
    """Liveness-probe indexed MCP servers via an `initialize` request."""
    sql = "SELECT id, endpoint_url FROM mcp_servers WHERE endpoint_url IS NOT NULL AND endpoint_url != ''"
    if only_unknown:
        sql += " AND (health IS NULL OR health='' OR health='unknown')"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        return 0
    n_ok = n_deg = n_down = 0
    now = int(time.time())
    batch_size = 50
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        updates = []
        for r in batch:
            res = crawlers.probe_mcp_health(r["endpoint_url"])
            h = res["status"]
            last_ok = now if h == "ok" else None
            updates.append((h, now, res["latency_ms"], res["http_status"], last_ok, r["id"]))
            n_ok += h == "ok"; n_deg += h == "degraded"; n_down += h == "down"

        def op(updates=updates):
            with db.writer() as c:
                c.executemany(
                    "UPDATE mcp_servers SET health=?, health_checked=?, latency_ms=?, "
                    "http_status=?, last_success_at=COALESCE(?, last_success_at) WHERE id=?",
                    updates,
                )
        db.with_retry(op)
        log.info("mcp health progress: checked=%d/%d ok=%d degraded=%d down=%d",
                 min(start + batch_size, len(rows)), len(rows), n_ok, n_deg, n_down)
    log.info("mcp health: ok=%d degraded=%d down=%d", n_ok, n_deg, n_down)
    return 0
    """First Base-mainnet (chainid 8453) USDC payTo address for a service."""
    if not payment_json:
        return None
    try:
        p = json.loads(payment_json) if isinstance(payment_json, str) else payment_json
    except Exception:
        return None
    for a in (p.get("accepts") or []):
        net = (a.get("network") or "").lower()
        if net in ("base", "eip155:8453"):
            pt = a.get("pay_to") or a.get("payTo")
            if pt and pt.startswith("0x"):
                return pt.lower()
    return None


def cmd_onchain(limit=None, stale_days=3, refresh_all=False, rate=0.3) -> int:
    """Signal C: measure real on-chain demand per service (Base USDC payTo)."""
    now = int(time.time())
    stale_cutoff = now - stale_days * 86400
    with db.connect(read_only=True) as c:
        rows = list(c.execute(
            "SELECT id, payment, payto_checked FROM services "
            "WHERE payment IS NOT NULL AND payment != ''"
        ).fetchall())

    payto_services = {}
    for r in rows:
        pt = _base_payto_for_row(r["payment"])
        if not pt:
            continue
        if not refresh_all and r["payto_checked"] and r["payto_checked"] > stale_cutoff:
            continue
        payto_services.setdefault(pt, []).append(r["id"])

    addrs = list(payto_services)
    if limit:
        addrs = addrs[:limit]
    if not addrs:
        log.info("onchain: no Base payTo addresses to refresh")
        return 0
    log.info("onchain: querying %d unique Base payTo addresses", len(addrs))

    done = n_active = 0
    for pt in addrs:
        act = crawlers.fetch_payto_activity(pt)
        if not act["ok"]:
            time.sleep(rate)
            continue
        if act["tx"] > 0:
            n_active += 1
        sids = payto_services[pt]
        def op(sids=sids, act=act, now=now):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET payto_tx_30d=?, payto_payers_30d=?, "
                    "payto_checked=? WHERE id=?",
                    [(act["tx"], act["payers"], now, sid) for sid in sids],
                )
        db.with_retry(op)
        done += 1
        if done % 50 == 0:
            log.info("onchain progress: %d/%d addresses (active=%d)", done, len(addrs), n_active)
        time.sleep(rate)
    log.info("onchain: refreshed %d addresses, %d with paying demand", done, n_active)
    return 0


def cmd_stats() -> int:
    with db.connect(read_only=True) as c:
        s = db.stats(c)
    print(json.dumps(s, indent=2, ensure_ascii=False))
    return 0


def cmd_submissions(status: str = "pending", limit: int = 50) -> int:
    """List submissions (pending by default) for human review."""
    with db.connect(read_only=True) as c:
        rows = db.list_submissions(c, status=status, limit=limit)
    if not rows:
        print(f"no submissions with status={status}")
        return 0
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            p = {}
        ts = r.get("created_at")
        print(f"--- submission #{r['id']} ({r['status']}) "
              f"created={ts} ip={p.get('_client_ip')} client={p.get('_client_name')} ---")
        for k in ("name", "url", "mcp_url", "category", "chains",
                 "price_min_usdc", "price_max_usdc", "description", "contact"):
            v = p.get(k)
            if v not in (None, "", []):
                print(f"  {k}: {v}")
        if r.get("note"):
            print(f"  note: {r['note']}")
    return 0


def _approve(sub_id: int, note: str | None = None) -> dict | None:
    """Core approve logic: copy a pending submission into services + mark it
    approved + health-probe + notify. Returns a dict describing the listed
    service, or None if the submission could not be approved."""
    from urllib.parse import urlparse

    def _slugify(text: str) -> str:
        import re as _re
        s = _re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return s or "unnamed"

    with db.connect(read_only=True) as c:
        rows = db.list_submissions(c, status="pending", limit=1000)
    row = next((r for r in rows if r["id"] == sub_id), None)
    if row is None:
        log.warning("approve: submission #%d not found or not pending", sub_id)
        return None
    p = row.get("payload") or {}
    if not isinstance(p, dict):
        log.warning("approve: submission #%d payload not parseable", sub_id)
        return None
    url = (p.get("url") or "").strip()
    if not url:
        log.warning("approve: submission #%d has no url", sub_id)
        return None
    host = urlparse(url).hostname or url
    name = p.get("name") or host
    service = {
        "slug": _slugify(host) + "-sub" + str(sub_id),
        "name": name,
        "url": url,
        "description": p.get("description"),
        "category": _slugify(p.get("category") or "general"),
        "chains": p.get("chains") or [],
        "price_min": p.get("price_min_usdc"),
        "price_max": p.get("price_max_usdc"),
        "mcp_url": p.get("mcp_url"),
        "source": "submission",
        "source_id": f"sub:{sub_id}",
        "tags": [p.get("category")] if p.get("category") else [],
        "region": "global",
    }
    def op():
        with db.writer() as wc:
            created, _sid = db.upsert_service(wc, service)
            db.mark_submission(wc, sub_id, "approved", note=note)
            return created
    created = db.with_retry(op)
    log.info("approved submission #%d -> service slug=%s (%s)",
             sub_id, service["slug"], "new" if created else "updated")
    # Probe once now so the new service is not stuck at health=unknown
    # until the next 6h health timer fires.
    try:
        h = crawlers.check_health(service["url"], service.get("well_known_url"))
        def _probe():
            with db.writer() as wc:
                wc.execute(
                    "UPDATE services SET health=?, health_checked=? WHERE slug=?",
                    (h, int(time.time()), service["slug"]),
                )
        db.with_retry(_probe)
        log.info("  health probe: %s", h)
    except Exception as e:
        log.warning("approve health probe failed: %r", e)
    # Notify the submitter that their service is now listed.
    try:
        contact = (p.get("contact") or "").strip()
        bits = [url]
        chains = service.get("chains") or []
        if chains:
            bits.append(" / ".join(str(c) for c in chains[:3]))
        pmin = service.get("price_min")
        pmax = service.get("price_max")
        if pmin is not None and pmax is not None and pmin != pmax:
            bits.append(f"${pmin}-${pmax}")
        elif pmin is not None:
            bits.append(f"${pmin}")
        verified_line = "  \u00b7  ".join(bits)
        mailer.send_approval_email(contact, name, service["slug"], verified_line)
    except Exception as e:
        log.warning("approve notification email failed: %r", e)
    return {"slug": service["slug"], "created": bool(created),
            "name": name, "url": url}


def cmd_approve(sub_id: int, note: str | None = None) -> int:
    """CLI manual-override approve (the normal flow is fully automatic)."""
    res = _approve(sub_id, note=note)
    if res is None:
        print(f"submission #{sub_id} could not be approved", file=sys.stderr)
        return 1
    print(f"approved submission #{sub_id} -> service slug={res['slug']} "
          f"({'new' if res['created'] else 'updated'})")
    return 0


def review_submission(sub_id: int, note_prefix: str = "auto-review") -> dict:
    """Auto-review a single pending submission via x402 verification.

    verified  -> approve + publish immediately
    rejected  -> reject
    uncertain -> leave pending (the crawl timer retries it automatically;
                 no human is ever required)

    Returns a dict: {status: listed|rejected|pending|not_pending, ...}.
    """
    with db.connect(read_only=True) as c:
        rows = db.list_submissions(c, status="pending", limit=2000)
    row = next((r for r in rows if r["id"] == sub_id), None)
    if row is None:
        return {"status": "not_pending", "submission_id": sub_id}
    p = row.get("payload") or {}
    if not isinstance(p, dict):
        p = {}
    url = (p.get("url") or "").strip()
    well_known = p.get("well_known") or p.get("well_known_url")
    if not url:
        verdict = {"status": "rejected", "evidence": ["submission has no url"]}
    else:
        try:
            verdict = crawlers.verify_x402(url, well_known)
        except Exception as e:
            verdict = {"status": "uncertain",
                       "evidence": [f"verify exception: {e!r}"]}
    vstatus = verdict.get("status")
    evidence = verdict.get("evidence") or []
    note = f"{note_prefix}: {vstatus} — {'; '.join(evidence)}"
    if vstatus == "verified":
        res = _approve(sub_id, note=note)
        if res:
            return {"status": "listed", "submission_id": sub_id,
                    "slug": res["slug"], "evidence": evidence}
        return {"status": "pending", "submission_id": sub_id,
                "evidence": ["approve failed; left pending for retry"]}
    if vstatus == "rejected":
        cmd_reject(sub_id, note=note)
        return {"status": "rejected", "submission_id": sub_id,
                "evidence": evidence}
    return {"status": "pending", "submission_id": sub_id, "evidence": evidence}


def cmd_reject(sub_id: int, note: str | None = None) -> int:
    def op():
        with db.writer() as wc:
            db.mark_submission(wc, sub_id, "rejected", note=note)
    db.with_retry(op)
    print(f"rejected submission #{sub_id}")
    return 0


def cmd_auto_review(limit: int = 100, dry_run: bool = False) -> int:
    """Automatically review every pending submission by x402 verification.

    This is the only review path — there is no human gate. verified
    submissions are published, rejected ones are dropped, and uncertain
    ones stay pending and are retried on the next run.
    """
    with db.connect(read_only=True) as c:
        rows = db.list_submissions(c, status="pending", limit=limit)
    if not rows:
        log.info("auto-review: no pending submissions")
        return 0
    n_app = n_rej = n_unc = 0
    for row in rows:
        sid = row["id"]
        if dry_run:
            p = row.get("payload") or {}
            url = (p.get("url") or "").strip() if isinstance(p, dict) else ""
            wk = p.get("well_known") or p.get("well_known_url") if isinstance(p, dict) else None
            try:
                verdict = crawlers.verify_x402(url, wk) if url else {"status": "rejected"}
            except Exception as e:
                verdict = {"status": "uncertain", "evidence": [repr(e)]}
            st = verdict.get("status")
            log.info("auto-review(dry) #%d %s -> %s", sid, url, st)
            if st == "verified":
                n_app += 1
            elif st == "rejected":
                n_rej += 1
            else:
                n_unc += 1
            continue
        out = review_submission(sid)
        st = out.get("status")
        log.info("auto-review #%d -> %s", sid, st)
        if st == "listed":
            n_app += 1
        elif st == "rejected":
            n_rej += 1
        else:
            n_unc += 1
    log.info("auto-review done: listed=%d rejected=%d left_pending=%d",
             n_app, n_rej, n_unc)
    return 0


def _load_env_file(path: str = "/opt/mcpserver/.env") -> None:
    """Best-effort .env loader so manual CLI runs see SMTP_* etc.

    systemd units already inject these via EnvironmentFile; this only fills
    in values that are not already set in the environment.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_env_file()
    p = argparse.ArgumentParser(prog="directory.jobs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p_crawl = sub.add_parser("crawl")
    p_crawl.add_argument("source", nargs="?", default=None)
    p_crawl_mcp = sub.add_parser("crawl-mcp")
    p_crawl_mcp.add_argument("source", nargs="?", default=None)
    sub.add_parser("crawl-a2a")
    sub.add_parser("health")
    sub.add_parser("health-a2a")
    sub.add_parser("health-mcp")
    sub.add_parser("stats")
    p_subs = sub.add_parser("submissions")
    p_subs.add_argument("--status", default="pending")
    p_subs.add_argument("--limit", type=int, default=50)
    p_app = sub.add_parser("approve")
    p_app.add_argument("id", type=int)
    p_app.add_argument("--note", default=None)
    p_rej = sub.add_parser("reject")
    p_rej.add_argument("id", type=int)
    p_rej.add_argument("--note", default=None)
    p_auto = sub.add_parser("auto-review")
    p_auto.add_argument("--limit", type=int, default=100)
    p_auto.add_argument("--dry-run", action="store_true")
    p_onchain = sub.add_parser("onchain")
    p_onchain.add_argument("--limit", type=int, default=None)
    p_onchain.add_argument("--stale-days", type=int, default=3)
    p_onchain.add_argument("--all", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "init":
        return cmd_init()
    if args.cmd == "crawl":
        return cmd_crawl(args.source)
    if args.cmd == "crawl-mcp":
        return cmd_crawl_mcp(args.source)
    if args.cmd == "crawl-a2a":
        return cmd_crawl_a2a()
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "health-a2a":
        return cmd_health_a2a()
    if args.cmd == "health-mcp":
        return cmd_health_mcp()
    if args.cmd == "stats":
        return cmd_stats()
    if args.cmd == "submissions":
        return cmd_submissions(status=args.status, limit=args.limit)
    if args.cmd == "approve":
        return cmd_approve(args.id, note=args.note)
    if args.cmd == "reject":
        return cmd_reject(args.id, note=args.note)
    if args.cmd == "auto-review":
        return cmd_auto_review(limit=args.limit, dry_run=args.dry_run)
    if args.cmd == "onchain":
        return cmd_onchain(limit=args.limit, stale_days=args.stale_days,
                           refresh_all=args.all)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
