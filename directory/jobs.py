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
import sys
import time
from pathlib import Path

from . import crawlers, db

log = logging.getLogger("directory.jobs")
SEED_FILE = Path(__file__).resolve().parent / "seed.json"


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
    return 0


def cmd_health() -> int:
    n_ok = n_down = n_degraded = 0
    with db.connect(read_only=True) as c:
        rows = list(c.execute("SELECT id, url, well_known_url FROM services").fetchall())

    batch_size = 50
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start:start + batch_size]
        updates = []
        for r in batch_rows:
            h = crawlers.check_health(r["url"], r["well_known_url"])
            updates.append((h, int(time.time()), r["id"]))
            if h == "ok":
                n_ok += 1
            elif h == "down":
                n_down += 1
            else:
                n_degraded += 1

        def op(updates=updates):
            with db.writer() as c:
                c.executemany(
                    "UPDATE services SET health=?, health_checked=? WHERE id=?",
                    updates,
                )
        db.with_retry(op)
        log.info("health progress: checked=%d/%d ok=%d degraded=%d down=%d",
                 min(start + batch_size, len(rows)), len(rows), n_ok, n_degraded, n_down)

    log.info("health: ok=%d degraded=%d down=%d", n_ok, n_degraded, n_down)
    return 0


def cmd_stats() -> int:
    with db.connect(read_only=True) as c:
        s = db.stats(c)
    print(json.dumps(s, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="directory.jobs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p_crawl = sub.add_parser("crawl")
    p_crawl.add_argument("source", nargs="?", default=None)
    sub.add_parser("health")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    if args.cmd == "init":
        return cmd_init()
    if args.cmd == "crawl":
        return cmd_crawl(args.source)
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "stats":
        return cmd_stats()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
