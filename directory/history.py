"""Long-term daily history, kept indefinitely.

The raw probe tables are pruned (x402 90 days, MCP 30 days, A2A 120 days) and
the listing tables only hold today's values. This module rolls both up into one
small row per listing per UTC day, plus daily values of the external adoption
signals, in a separate SQLite file so the main database and its backups do not
grow with it.

Not public yet. It is the basis for future per-listing history, alerts and
trend reports.
"""
from __future__ import annotations

import calendar
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_daily (
    kind TEXT NOT NULL,            -- x402 | mcp | a2a
    listing_id INTEGER NOT NULL,
    day TEXT NOT NULL,             -- UTC YYYY-MM-DD
    probes INTEGER, ok INTEGER, degraded INTEGER, down INTEGER,
    latency_avg_ms INTEGER, latency_max_ms INTEGER,
    health TEXT,                   -- listing state when the day was rolled up
    readiness REAL, grade TEXT, quality REAL,
    tx_30d INTEGER, payto_tx_30d INTEGER, payto_payers_30d INTEGER,
    PRIMARY KEY (kind, listing_id, day)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS listing_daily_day ON listing_daily(day, kind);
CREATE TABLE IF NOT EXISTS signal_daily (
    source TEXT NOT NULL,          -- github | npm | tranco
    key TEXT NOT NULL,             -- repo, package or domain
    day TEXT NOT NULL,
    value INTEGER,                 -- stars | downloads_30d | rank
    value2 INTEGER,                -- forks
    PRIMARY KEY (source, key, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_runs (
    day TEXT NOT NULL, part TEXT NOT NULL, rows INTEGER, at INTEGER,
    PRIMARY KEY (day, part)
) WITHOUT ROWID;
"""

HISTORY = {  # kind -> (raw probe table, id column)
    "x402": ("health_history", "service_id"),
    "mcp": ("mcp_health_history", "server_id"),
    "a2a": ("a2a_health_history", "agent_id"),
}
LISTING = {
    "x402": ("services", ", tx_30d, payto_tx_30d, payto_payers_30d"),
    "mcp": ("mcp_servers", ", NULL, NULL, NULL"),
    "a2a": ("a2a_agents", ", NULL, NULL, NULL"),
}


def db_path() -> str:
    explicit = os.environ.get("AGENT_TOOLS_HISTORY_DB")
    if explicit:
        return explicit
    from . import db
    return os.path.join(os.path.dirname(os.path.abspath(db.DEFAULT_DB_PATH)), "history.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _probe_rows(src, kind, since=None, until=None):
    """(listing_id, day, probes, ok, degraded, down, lat_avg, lat_max) per day."""
    table, col = HISTORY[kind]
    where, args = [], []
    if since is not None:
        where.append("checked_at >= ?")
        args.append(since)
    if until is not None:
        where.append("checked_at < ?")
        args.append(until)
    sql = (f"SELECT {col}, date(checked_at, 'unixepoch'), COUNT(*), "
           "SUM(status='ok'), SUM(status='degraded'), SUM(status NOT IN ('ok','degraded')), "
           "CAST(AVG(latency_ms) AS INTEGER), MAX(latency_ms) "
           f"FROM {table} {'WHERE ' + ' AND '.join(where) if where else ''} GROUP BY 1, 2")
    return src.execute(sql, args).fetchall()


def _write_probes(dst, kind, rows) -> int:
    dst.executemany(
        "INSERT INTO listing_daily(kind, listing_id, day, probes, ok, degraded, down, "
        "latency_avg_ms, latency_max_ms) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(kind, listing_id, day) DO UPDATE SET probes=excluded.probes, ok=excluded.ok, "
        "degraded=excluded.degraded, down=excluded.down, latency_avg_ms=excluded.latency_avg_ms, "
        "latency_max_ms=excluded.latency_max_ms",
        [(kind, *r) for r in rows])
    return len(rows)


def rollup(src: sqlite3.Connection, now: float | None = None) -> dict:
    """Roll up yesterday and today (UTC) probes, plus every listing's current state.

    Idempotent; run after the daily probe pass. `src` is a read-only
    connection to the main database.
    """
    now = now or time.time()
    day = _day(now)
    start = calendar.timegm(time.strptime(day, "%Y-%m-%d"))
    out = {"day": day}
    dst = connect()
    try:
        with dst:
            for kind in HISTORY:
                # yesterday too: probes that ran after last night's rollup
                n = _write_probes(dst, kind, _probe_rows(src, kind, since=start - 86400, until=start + 86400))
                table, extra = LISTING[kind]
                rows = src.execute(
                    f"SELECT id, health, readiness_score, readiness_grade, quality_score{extra} "
                    f"FROM {table}").fetchall()
                dst.executemany(
                    "INSERT INTO listing_daily(kind, listing_id, day, health, readiness, grade, quality, "
                    "tx_30d, payto_tx_30d, payto_payers_30d) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(kind, listing_id, day) DO UPDATE SET health=excluded.health, "
                    "readiness=excluded.readiness, grade=excluded.grade, quality=excluded.quality, "
                    "tx_30d=excluded.tx_30d, payto_tx_30d=excluded.payto_tx_30d, "
                    "payto_payers_30d=excluded.payto_payers_30d",
                    [(kind, r[0], day, *r[1:]) for r in rows])
                out[kind] = {"probed": n, "listings": len(rows)}
            sig = 0
            for source, sql in (
                    ("github", "SELECT repo, stars, forks FROM ext_github_repos WHERE stars IS NOT NULL"),
                    ("npm", "SELECT package, downloads_30d, NULL FROM ext_npm_packages "
                            "WHERE downloads_30d IS NOT NULL"),
                    ("tranco", "SELECT domain, rank, NULL FROM ext_tranco")):
                try:
                    rows = src.execute(sql).fetchall()
                except sqlite3.OperationalError:
                    continue
                dst.executemany(
                    "INSERT OR REPLACE INTO signal_daily(source, key, day, value, value2) VALUES (?,?,?,?,?)",
                    [(source, r[0], day, r[1], r[2]) for r in rows])
                sig += len(rows)
            out["signals"] = sig
            dst.execute("INSERT OR REPLACE INTO rollup_runs(day, part, rows, at) VALUES (?,?,?,?)",
                        (day, "daily", sum(out[k]["listings"] for k in HISTORY), int(now)))
    finally:
        dst.close()
    return out


def backfill(src: sqlite3.Connection) -> dict:
    """Roll up every day still present in the raw probe tables (probes only)."""
    out = {}
    dst = connect()
    try:
        for kind in HISTORY:
            rows = _probe_rows(src, kind)
            with dst:
                out[kind] = _write_probes(dst, kind, rows)
                dst.execute("INSERT OR REPLACE INTO rollup_runs(day, part, rows, at) VALUES (?,?,?,?)",
                            (_day(time.time()), "backfill:" + kind, out[kind], int(time.time())))
    finally:
        dst.close()
    return out


def summary() -> dict:
    """Coverage of the stored history (for ops and tests)."""
    if not os.path.exists(db_path()):
        return {"available": False}
    c = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, timeout=5)
    try:
        rows = c.execute("SELECT kind, MIN(day), MAX(day), COUNT(DISTINCT day), COUNT(*) "
                         "FROM listing_daily GROUP BY kind").fetchall()
        sig = c.execute("SELECT source, MIN(day), MAX(day), COUNT(*) FROM signal_daily GROUP BY source").fetchall()
    finally:
        c.close()
    return {"available": True,
            "listings": {k: {"from": a, "to": b, "days": d, "rows": n} for k, a, b, d, n in rows},
            "signals": {s: {"from": a, "to": b, "rows": n} for s, a, b, n in sig}}
