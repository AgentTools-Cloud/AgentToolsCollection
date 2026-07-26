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
from . import agenstry as agenstry_mod
from . import flows as flows_mod
from . import paygent as paygent_mod
from . import prowl as prowl_mod
from . import mcp_safety

# agenstry.com reverse-crawl: register its MCP page crawler as a source so
# `python -m directory.jobs crawl-mcp agenstry` works like any other source.
crawlers.MCP_CRAWLERS["agenstry"] = agenstry_mod.fetch_agenstry_mcp
crawlers.MCP_CRAWLERS["prowl"] = prowl_mod.fetch_prowl_mcp

# paygent.net reverse-crawl: register its x402 payment index (x402/mpp/l402)
# as an x402 source; crawl runs via ALL_CRAWLERS -> cmd_crawl -> upsert_service.
crawlers.ALL_CRAWLERS["paygent-discover"] = paygent_mod.fetch_paygent_discover
crawlers.ALL_CRAWLERS["flows-litprotocol"] = flows_mod.fetch_flows_litprotocol
crawlers.ALL_CRAWLERS["x402-fuchss"] = crawlers.fetch_x402_fuchss

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


QUARANTINE_DAYS = 30
_RESOURCE_LIST_SOURCES = {"flows-litprotocol", "x402-fuchss"}


def _resource_key(value) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _item_resource_keys(item: dict) -> set[str]:
    return {
        key
        for sample in (item.get("resource_samples") or [])
        if isinstance(sample, dict)
        if (key := _resource_key(sample.get("url")))
    }


def _filter_claimed_resource_items(items: list[dict], source: str) -> list[dict]:
    """Skip cross-source duplicates while preserving distinct same-host APIs."""
    known_source_ids: set[str] = set()
    claimed_resources: set[str] = set()
    with db.connect(read_only=True) as conn:
        for row in conn.execute(
            "SELECT source, source_id, url, resource_samples FROM services"
        ).fetchall():
            if row["source"] == source and row["source_id"]:
                known_source_ids.add(str(row["source_id"]))
            url_key = _resource_key(row["url"])
            if url_key:
                claimed_resources.add(url_key)
            try:
                samples = json.loads(row["resource_samples"] or "[]")
            except (TypeError, json.JSONDecodeError):
                samples = []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                sample_key = _resource_key(sample.get("url"))
                if sample_key:
                    claimed_resources.add(sample_key)

    kept: list[dict] = []
    skipped = 0
    for item in items:
        source_id = str(item.get("source_id") or "")
        if source_id and source_id in known_source_ids:
            kept.append(item)
            continue
        resources = _item_resource_keys(item)
        if resources:
            if resources.issubset(claimed_resources):
                skipped += 1
                continue
        elif _resource_key(item.get("url")) in claimed_resources:
            skipped += 1
            continue
        kept.append(item)
    log.info("%s resource dedup: kept=%d skipped=%d", source, len(kept), skipped)
    return kept


def _maintain_down_since(table: str) -> None:
    """Track consecutive-down start time. Clear on recovery, set on new down.

    Idempotent bulk maintenance run after each health pass. A row whose
    down_since is older than QUARANTINE_DAYS is considered "quarantined" and is
    skipped by the normal high-frequency health pass (probed once/day instead).
    """
    _now = int(time.time())

    def op():
        with db.writer() as c:
            c.execute(
                f"UPDATE {table} SET down_since=NULL "
                f"WHERE health!='down' AND down_since IS NOT NULL"
            )
            c.execute(
                f"UPDATE {table} SET down_since=? "
                f"WHERE health='down' AND down_since IS NULL",
                (_now,),
            )
    db.with_retry(op)


def cmd_health_quarantined() -> int:
    """Daily low-frequency probe of quarantined (down > QUARANTINE_DAYS) entries.

    Anything that recovers has its down_since cleared by _maintain_down_since and
    rejoins the normal high-frequency pool automatically.
    """
    cmd_health(quarantined_only=True)
    cmd_health_a2a(quarantined_only=True)
    cmd_health_mcp(quarantined_only=True)
    return 0


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

    if name in _RESOURCE_LIST_SOURCES:
        items = _filter_claimed_resource_items(items, name)

    if name == "paygent-discover":
        with db.connect(read_only=True) as c:
            claimed_origins = {
                db._canonical_origin(row[0])
                for row in c.execute(
                    "SELECT url FROM services WHERE source != ?",
                    ("paygent-discover",),
                ).fetchall()
            }
        items = [
            item for item in items
            if db._canonical_origin(item.get("url") or "") not in claimed_origins
        ]

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
        deleted = 0
        since = None
        kwargs: dict = {}
        if name == "mcp-registry":
            with db.writer() as c:
                since = db.get_meta(c, "mcp_registry:updated_since")
            if since:
                kwargs["updated_since"] = since
        elif name == "pulsemcp":
            # First run = full crawl (page through all ~16k newest-first); once
            # done, flip a flag and switch to incremental "recent" crawls that
            # stop after hitting a run of already-known servers. We key "known"
            # on endpoint_url across ALL sources (not source="pulsemcp"), since
            # cross-source dedup drifts the source column but endpoints are
            # stable — otherwise the early-stop never triggers.
            with db.writer() as c:
                full_done = db.get_meta(c, "pulsemcp:full_done")
                known = set(db.mcp_endpoint_urls(c)) if full_done else set()
            if full_done:
                kwargs["known_ids"] = known
                kwargs["stop_after_known"] = 60  # ~4 pages of known remotes = caught up
        try:
            items = fn(**kwargs)
        except Exception as e:
            _finish_run(run_id, 0, 0, [f"fetch failed: {e!r}"], status="error")
            log.warning("mcp crawl %s fetch failed: %r", name, e)
            continue
        max_updated = since
        if name == "mcp-registry":
            for it in items:
                u = it.get("_updated_at")
                if u and (max_updated is None or u > max_updated):
                    max_updated = u
            del_ids = [it["source_id"] for it in items
                       if it.get("_status") and it["_status"] != "active"]
            if del_ids:
                def _del(ids=del_ids):
                    n = 0
                    with db.writer() as c:
                        for sid in ids:
                            n += db.delete_mcp_by_source(c, "mcp-registry", sid)
                    return n
                deleted = db.with_retry(_del)
            items = [it for it in items
                     if not (it.get("_status") and it["_status"] != "active")]
        batch_size = 100
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            def op(batch=batch):
                b_add = b_upd = 0
                b_err: list[str] = []
                with db.writer() as c:
                    for item in batch:
                        try:
                            if not (item.get("endpoint_url") or "").strip():
                                continue
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
        if name == "mcp-registry" and max_updated:
            with db.writer() as c:
                db.set_meta(c, "mcp_registry:updated_since", max_updated)
        if name == "pulsemcp" and not errors and "known_ids" not in kwargs:
            # Completed a full crawl with no errors -> switch to incremental.
            with db.writer() as c:
                db.set_meta(c, "pulsemcp:full_done", "1")
        log.info("mcp crawl %s: added=%d updated=%d deleted=%d errors=%d",
                 name, added, updated, deleted, len(errors))
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
    try:
        g = agenstry_mod.crawl_agenstry_a2a()
        added += g["inserted"]; updated += g["updated"]
        log.info("a2a agenstry: candidates=%d resolved=%d inserted=%d updated=%d",
                 g["candidates"], g["resolved"], g["inserted"], g["updated"])
    except Exception as e:
        errors.append(f"agenstry: {e!r}"); log.warning("a2a agenstry crawl failed: %r", e)
    # chiark.ai retired its API with HTTP 410 in July 2026. Existing rows stay
    # in the directory and continue through health checks; only polling stops.
    try:
        ar = a2a_mod.crawl_a2aregistry()
        added += ar["inserted"]; updated += ar["updated"]
        log.info("a2a a2aregistry: candidates=%d resolved=%d inserted=%d updated=%d",
                 ar["candidates"], ar["resolved"], ar["inserted"], ar["updated"])
    except Exception as e:
        errors.append(f"a2aregistry: {e!r}"); log.warning("a2a a2aregistry crawl failed: %r", e)
    try:
        gh = a2a_mod.crawl_github_topic()
        added += gh["inserted"]; updated += gh["updated"]
        log.info("a2a github-topic: candidates=%d resolved=%d inserted=%d updated=%d",
                 gh["candidates"], gh["resolved"], gh["inserted"], gh["updated"])
    except Exception as e:
        errors.append(f"github-topic: {e!r}"); log.warning("a2a github-topic crawl failed: %r", e)
    _finish_run(run_id, added, updated, errors,
                status="ok" if not errors else "partial")
    if added:
        cmd_health_a2a(only_unknown=True)
    return 0


def cmd_crawl_agenstry() -> int:
    """Reverse-crawl agenstry.com: live A2A agent cards + MCP server endpoints."""
    run_id = _start_run("agenstry:crawl")
    added = updated = 0
    errors: list[str] = []
    try:
        a = agenstry_mod.crawl_agenstry_a2a()
        added += a["inserted"]; updated += a["updated"]
        log.info("agenstry a2a: candidates=%d resolved=%d inserted=%d updated=%d",
                 a["candidates"], a["resolved"], a["inserted"], a["updated"])
    except Exception as e:
        errors.append(f"a2a: {e!r}"); log.warning("agenstry a2a crawl failed: %r", e)
    _finish_run(run_id, added, updated, errors,
                status="ok" if not errors else "partial")
    # MCP via the standard MCP pipeline (upsert + health probe) for the
    # newly-registered agenstry source.
    try:
        cmd_crawl_mcp("agenstry")
    except Exception as e:
        log.warning("agenstry mcp crawl failed: %r", e)
    if added:
        cmd_health_a2a(only_unknown=True)
    return 0


def cmd_health(only_unknown: bool = False, quarantined_only: bool = False) -> int:
    n_ok = n_down = n_degraded = 0
    _qc = int(time.time()) - QUARANTINE_DAYS * 86400
    if quarantined_only:
        sql = ("SELECT id, url, well_known_url FROM services "
               f"WHERE down_since IS NOT NULL AND down_since <= {_qc}")
    else:
        sql = ("SELECT id, url, well_known_url FROM services "
               f"WHERE (down_since IS NULL OR down_since > {_qc})")
        if only_unknown:
            # crawler inserts leave health NULL (bypasses the column DEFAULT), so
            # treat NULL / '' / 'unknown' all as "never probed yet".
            sql += " AND (health IS NULL OR health='' OR health='unknown')"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        if only_unknown:
            log.info("no new (unknown-health) services to probe")
        return 0

    from concurrent.futures import ThreadPoolExecutor

    def _probe(r):
        res = crawlers.probe_health(r["url"], r["well_known_url"])
        h = res["status"]
        x = 1 if res["x402"] else 0
        now = int(time.time())
        return (h,
                (h, now, res["latency_ms"], res["http_status"], x, r["id"]),
                (r["id"], now, h, res["latency_ms"], res["http_status"], x))

    # Concurrent probes (network-I/O bound). 16 workers keeps this shared VPS
    # responsive; sqlite writes stay single-thread below.
    batch_size = 100
    workers = 16
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start:start + batch_size]
        updates = []
        history = []
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for h, upd, hist in ex.map(_probe, batch_rows):
                updates.append(upd)
                history.append(hist)
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
                    "http_status=?, x402_ok=max(COALESCE(x402_ok,0), ?) WHERE id=?",
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

    _maintain_down_since("services")
    log.info("health: ok=%d degraded=%d down=%d", n_ok, n_degraded, n_down)
    # The single agent-tools-health timer also refreshes A2A + MCP liveness.
    if not only_unknown and not quarantined_only:
        try:
            cmd_health_a2a()
        except Exception as e:
            log.warning("a2a health failed: %r", e)
        try:
            cmd_health_mcp()
        except Exception as e:
            log.warning("mcp health failed: %r", e)
    return 0


def cmd_health_a2a(only_unknown: bool = False, quarantined_only: bool = False) -> int:
    """Liveness-probe indexed A2A agents (card / endpoint reachability)."""
    _qc = int(time.time()) - QUARANTINE_DAYS * 86400
    if quarantined_only:
        sql = ("SELECT id, card_url, endpoint_url FROM a2a_agents "
               f"WHERE down_since IS NOT NULL AND down_since <= {_qc}")
    else:
        sql = ("SELECT id, card_url, endpoint_url FROM a2a_agents "
               f"WHERE (down_since IS NULL OR down_since > {_qc})")
        if only_unknown:
            sql += " AND (health IS NULL OR health='' OR health='unknown')"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        return 0
    n_ok = n_deg = n_down = 0
    now = int(time.time())
    updates = []

    n_conf = 0

    def _probe(r):
        res = a2a_mod.probe_a2a_health(r["card_url"], r["endpoint_url"])
        h = res["status"]
        last_ok = now if h == "ok" else None
        return h, res.get("conformance"), (h, now, res["latency_ms"],
                                           res.get("conformance"), last_ok, r["id"])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = min(32, max(4, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_probe, r) for r in rows]
        for fut in as_completed(futs):
            try:
                h, conf, upd = fut.result()
            except Exception:
                continue
            updates.append(upd)
            n_ok += h == "ok"; n_deg += h == "degraded"; n_down += h == "down"
            n_conf += conf == "pass"

    def op():
        with db.writer() as c:
            c.executemany(
                "UPDATE a2a_agents SET health=?, health_checked=?, latency_ms=?, "
                "conformance=?, last_success_at=COALESCE(?, last_success_at) WHERE id=?",
                updates,
            )
    db.with_retry(op)
    _maintain_down_since("a2a_agents")
    log.info("a2a health: ok=%d degraded=%d down=%d conformant=%d",
             n_ok, n_deg, n_down, n_conf)
    return 0


def cmd_health_mcp(only_unknown: bool = False, quarantined_only: bool = False) -> int:
    """Liveness-probe indexed MCP servers via an `initialize` request."""
    _qc = int(time.time()) - QUARANTINE_DAYS * 86400
    sql = ("SELECT id, endpoint_url, name, description, tools_text FROM mcp_servers "
           "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''")
    if quarantined_only:
        sql += f" AND (down_since IS NOT NULL AND down_since <= {_qc})"
    else:
        sql += f" AND (down_since IS NULL OR down_since > {_qc})"
        if only_unknown:
            sql += " AND (health IS NULL OR health='' OR health='unknown')"
    with db.connect(read_only=True) as c:
        rows = list(c.execute(sql).fetchall())
    if not rows:
        return 0
    n_ok = n_deg = n_down = n_conf = 0
    n_flagged = 0
    now = int(time.time())
    from concurrent.futures import ThreadPoolExecutor

    def _probe(r):
        res = crawlers.probe_mcp_health(r["endpoint_url"])
        h = res["status"]
        conf = res.get("conformance")
        last_ok = now if h == "ok" else None
        tools = res.get("tools")
        # Only overwrite tool data when we actually got a list back (a failed
        # tools/list returns None and must not wipe a prior good capture).
        if isinstance(tools, list):
            tools_json = json.dumps(tools, ensure_ascii=False)
            tools_text = " ".join(
                f"{t.get('name', '')} {t.get('description', '')}" for t in tools
            ).strip()
        else:
            tools_json = None
            tools_text = None
        # Static malware/abuse scan over advertised metadata (no network, no
        # exec). Scan the freshest tools_text we have (this probe's, else the
        # stored one) together with name+description.
        scan = mcp_safety.scan_mcp(
            name=r["name"] or "",
            description=r["description"] or "",
            tools_text=(tools_text if tools_text is not None else (r["tools_text"] or "")),
        )
        safety_reasons = json.dumps(scan.to_dict()["reasons"], ensure_ascii=False)
        upd = (h, now, res["latency_ms"], res["http_status"],
               conf, res.get("tool_count"), tools_json, tools_text,
               scan.verdict, scan.score, safety_reasons, last_ok, r["id"])
        return r["id"], h, conf, res["latency_ms"], scan, upd

    # Concurrent probes (network-I/O bound). 16 workers keeps this VPS — which
    # is shared with other sites — responsive; sqlite writes stay single-thread.
    batch_size = 100
    workers = 16
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        updates = []
        hist = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sid, h, conf, lat, scan, upd in ex.map(_probe, batch):
                updates.append(upd)
                hist.append((sid, now, h, lat))
                n_ok += h == "ok"; n_deg += h == "degraded"; n_down += h == "down"
                n_conf += conf == "pass"
                if scan.verdict != "clean":
                    n_flagged += 1
                    log.warning("mcp safety: server id=%s verdict=%s score=%d rules=%s",
                                sid, scan.verdict, scan.score,
                                ",".join(f.rule for f in scan.reasons))

        def op(updates=updates, hist=hist):
            with db.writer() as c:
                c.executemany(
                    "UPDATE mcp_servers SET health=?, health_checked=?, latency_ms=?, "
                    "http_status=?, conformance=?, tool_count=?, "
                    "tools_json=COALESCE(?, tools_json), "
                    "tools_text=COALESCE(?, tools_text), "
                    "safety_verdict=?, safety_score=?, safety_reasons=?, "
                    "last_success_at=COALESCE(?, last_success_at) WHERE id=?",
                    updates,
                )
                c.executemany(
                    "INSERT INTO mcp_health_history(server_id, checked_at, status, latency_ms) "
                    "VALUES(?,?,?,?)",
                    hist,
                )
        db.with_retry(op)

        # P95 + 0..100 quality score per server (over the just-written history)
        def op2(batch=batch):
            with db.writer() as c:
                for r in batch:
                    p95 = db.mcp_p95_latency(c, r["id"])
                    srow = c.execute(
                        "SELECT health, conformance, confidence FROM mcp_servers WHERE id=?",
                        (r["id"],)).fetchone()
                    score = db.mcp_quality_score(
                        srow["health"], srow["conformance"], p95, srow["confidence"])
                    c.execute(
                        "UPDATE mcp_servers SET latency_p95_ms=?, quality_score=? WHERE id=?",
                        (p95, score, r["id"]))
        db.with_retry(op2)
        log.info("mcp health progress: checked=%d/%d ok=%d degraded=%d down=%d conformant=%d",
                 min(start + batch_size, len(rows)), len(rows), n_ok, n_deg, n_down, n_conf)

    # prune history older than 30 days
    def prune():
        with db.writer() as c:
            c.execute("DELETE FROM mcp_health_history WHERE checked_at < ?",
                      (now - 30 * 86400,))
    db.with_retry(prune)
    _maintain_down_since("mcp_servers")
    log.info("mcp health: ok=%d degraded=%d down=%d conformant=%d safety_flagged=%d",
             n_ok, n_deg, n_down, n_conf, n_flagged)
    return 0


def _paytos_for_row(payment_json):
    """All supported-chain USDC (chain, payTo) pairs for a service.

    Returns a list of (chain_key, payto) tuples for every accepts[] entry on a
    chain we have a free indexer for (crawlers.EVM_USDC_INDEXERS). Previously
    this was Base-only and its def line was lost in a prior edit, which broke the
    on-chain job (NameError); restored here as multi-chain.
    """
    if not payment_json:
        return []
    try:
        p = json.loads(payment_json) if isinstance(payment_json, str) else payment_json
    except Exception:
        return []
    out, seen = [], set()
    for a in (p.get("accepts") or []):
        chain = crawlers.network_to_chain(a.get("network"))
        if not chain:
            continue
        pt = a.get("pay_to") or a.get("payTo")
        if not (pt and pt.startswith("0x")):
            continue
        key = (chain, pt.lower())
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def cmd_onchain(limit=None, stale_days=3, refresh_all=False, rate=0.3) -> int:
    """Signal C: measure real on-chain USDC demand per service across every
    supported chain (Base, Ethereum, Optimism, Polygon, Arbitrum, Gnosis)."""
    now = int(time.time())
    stale_cutoff = now - stale_days * 86400
    with db.connect(read_only=True) as c:
        rows = list(c.execute(
            "SELECT id, payment, payto_checked FROM services "
            "WHERE payment IS NOT NULL AND payment != ''"
        ).fetchall())

    # service_id -> [(chain, payto)]; (chain, payto) -> [service_id]
    service_keys = {}
    key_services = {}
    for r in rows:
        if not refresh_all and r["payto_checked"] and r["payto_checked"] > stale_cutoff:
            continue
        keys = _paytos_for_row(r["payment"])
        if not keys:
            continue
        service_keys[r["id"]] = keys
        for k in keys:
            key_services.setdefault(k, []).append(r["id"])

    work = list(key_services)
    if limit:
        work = work[:limit]
        allowed = set(work)
        # only write services fully covered by the limited work set
        service_keys = {sid: ks for sid, ks in service_keys.items()
                        if all(k in allowed for k in ks)}
    if not work:
        log.info("onchain: no payTo addresses to refresh")
        return 0
    by_chain = {}
    for ch, _pt in work:
        by_chain[ch] = by_chain.get(ch, 0) + 1
    log.info("onchain: querying %d unique (chain,payTo) over %d chains: %s",
             len(work), len(by_chain), by_chain)

    # 1) query each unique (chain, payto) once
    results = {}
    done = 0
    for key in work:
        ch, pt = key
        results[key] = crawlers.fetch_payto_activity(pt, chain=ch)
        done += 1
        if done % 50 == 0:
            log.info("onchain progress: %d/%d addresses", done, len(work))
        time.sleep(rate)

    # 2) aggregate per service across its chains, then write once
    n_active = n_written = 0
    for sid, keys in service_keys.items():
        accs = [results[k] for k in keys if k in results and results[k]["ok"]]
        if not accs:
            continue  # every query failed -> skip, avoid false zero
        tx = sum(a["tx"] for a in accs)
        payers = sum(a["payers"] for a in accs)
        if tx > 0:
            n_active += 1
        def op(sid=sid, tx=tx, payers=payers, now=now):
            with db.writer() as c:
                c.execute(
                    "UPDATE services SET payto_tx_30d=?, payto_payers_30d=?, "
                    "payto_checked=? WHERE id=?",
                    (tx, payers, now, sid),
                )
        db.with_retry(op)
        n_written += 1
    log.info("onchain: wrote %d services, %d with paying demand", n_written, n_active)
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


def _approve(sub_id: int, note: str | None = None,
             payment: dict | None = None) -> dict | None:
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
    fixed_price = p.get("price_usdc")
    detected_price = (payment or {}).get("max_amount_usdc")
    price_min = p.get("price_min_usdc")
    price_max = p.get("price_max_usdc")
    if price_min is None:
        price_min = fixed_price if fixed_price is not None else detected_price
    if price_max is None:
        price_max = fixed_price if fixed_price is not None else detected_price
    service = {
        "slug": _slugify(host) + "-sub" + str(sub_id),
        "name": name,
        "url": url,
        "description": p.get("description"),
        "category": _slugify(p.get("category") or "general"),
        "chains": p.get("chains") or [],
        "price_min": price_min,
        "price_max": price_max,
        "currency": "USDC" if price_min is not None or price_max is not None else None,
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


def review_submission(sub_id: int, note_prefix: str = "auto-review", notify_pending: bool = False) -> dict:
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

    # admin notification — only on terminal verdicts (listed/rejected) or the
    # first review (on-submit / mcp-register). A pending submission is retried
    # every 30 min by the timer; re-notifying each retry floods the admin
    # inbox, so suppress notifications for repeated "pending" verdicts.
    sname = p.get("name") or p.get("title") or url or f"submission #{sub_id}"
    _verdict_label = {"verified": "listed", "rejected": "rejected"}.get(
        vstatus, "pending")
    if vstatus in ("verified", "rejected") or notify_pending:
        mailer.send_admin_notification(
            "x402 submission", sname, _verdict_label,
            [("URL", url or "—"),
             ("Contact", p.get("contact") or "—"),
             ("Category", p.get("category") or "—"),
             ("Description", p.get("description") or "—"),
             ("Source", p.get("_source") or "rest-submit"),
             ("Evidence", "; ".join(evidence) or "—"),
             ("Submission", f"#{sub_id}")])

    if vstatus == "verified":
        res = _approve(sub_id, note=note, payment=verdict.get("payment"))
        if res:
            return {"status": "listed", "submission_id": sub_id,
                    "slug": res["slug"], "evidence": evidence}
        return {"status": "pending", "submission_id": sub_id,
                "evidence": ["approve failed; left pending for retry"]}
    if vstatus == "rejected":
        cmd_reject(sub_id, note=note)
        contact = (p.get("contact") or "").strip()
        if contact and "@" in contact:
            try:
                mailer.send_rejection_email(
                    contact, sname, "; ".join(evidence) or None)
            except Exception as e:
                log.warning("rejection notification email failed: %r", e)
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
    sub.add_parser("crawl-agenstry")
    sub.add_parser("health")
    sub.add_parser("health-quarantined")
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
    if args.cmd == "crawl-agenstry":
        return cmd_crawl_agenstry()
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "health-quarantined":
        return cmd_health_quarantined()
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
