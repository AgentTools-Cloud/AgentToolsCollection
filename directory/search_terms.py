"""Aggregate search-term counts, for demand statistics.

Only the normalised term, the protocol searched, the channel (web, api, mcp),
the UTC day and two counters are kept: how often it was searched and how often
it returned nothing. No IP address, user, key, session or timestamp finer than
a day is recorded. Terms that look personal or unique (e-mail, URL, wallet
address, long numbers) are dropped before counting. Public output only shows
terms searched at least `PUBLIC_MIN` times in the window.

Counts are buffered in-process and flushed to a small separate SQLite file, so
a search never waits on the main database writer.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter

log = logging.getLogger(__name__)

PUBLIC_MIN = 5
RETENTION_DAYS = 400
FLUSH_SECONDS = 30
FLUSH_PENDING = 200
CHANNELS = ("web", "api", "mcp")
KINDS = ("x402", "mcp", "a2a", "all")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_terms (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    term TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    zero INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, channel, term)
);
CREATE INDEX IF NOT EXISTS search_terms_term ON search_terms(term);
"""

_DROP = re.compile(r"@|https?:|://|www\.|0x[0-9a-f]{6,}|\d{4,}|[a-z0-9]{25,}")
_EDGE = re.compile(r"^[^\w]+|[^\w+#.]+$")
_SPACE = re.compile(r"\s+")

_lock = threading.Lock()
_pending: Counter = Counter()
_last_flush = time.monotonic()


def db_path() -> str:
    explicit = os.environ.get("AGENT_TOOLS_SEARCH_TERMS_DB")
    if explicit:
        return explicit
    from . import db
    return os.path.join(os.path.dirname(os.path.abspath(db.DEFAULT_DB_PATH)), "search-terms.db")


def normalize(q: str | None) -> str | None:
    """Lower-cased, trimmed term, or None when it should not be counted."""
    if not q:
        return None
    t = _SPACE.sub(" ", str(q).strip().lower())
    t = _EDGE.sub("", t)
    if len(t) < 2 or len(t) > 60 or len(t.split()) > 6:
        return None
    if _DROP.search(t):
        return None
    return t


def record(kind: str, q: str | None, results: int, channel: str) -> None:
    """Count one search. Never raises."""
    try:
        term = normalize(q)
        if term is None or channel not in CHANNELS:
            return
        kind = kind if kind in KINDS else "all"
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with _lock:
            _pending[(day, kind, channel, term, "n")] += 1
            if not results:
                _pending[(day, kind, channel, term, "zero")] += 1
            due = (len(_pending) >= FLUSH_PENDING
                   or time.monotonic() - _last_flush >= FLUSH_SECONDS)
        if due:
            flush()
    except Exception:  # noqa: BLE001 - statistics must never break a search
        log.exception("search term record failed")


def _connect(read_only: bool = False) -> sqlite3.Connection:
    path = db_path()
    if read_only:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        c = sqlite3.connect(path, timeout=5)
        c.executescript(_SCHEMA)
    return c


def flush() -> None:
    global _last_flush
    with _lock:
        items = dict(_pending)
        _pending.clear()
        _last_flush = time.monotonic()
    if not items:
        return
    rows: dict = {}
    for (day, kind, channel, term, field), v in items.items():
        rows.setdefault((day, kind, channel, term), {"n": 0, "zero": 0})[field] += v
    try:
        c = _connect()
        with c:
            c.executemany(
                "INSERT INTO search_terms(day, kind, channel, term, n, zero) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(day, kind, channel, term) DO UPDATE SET "
                "n = n + excluded.n, zero = zero + excluded.zero",
                [(*k, v["n"], v["zero"]) for k, v in rows.items()])
        c.close()
    except Exception:  # noqa: BLE001
        log.exception("search term flush failed (%d rows dropped)", len(rows))


atexit.register(flush)


def prune(now: float | None = None) -> int:
    cutoff = time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) - RETENTION_DAYS * 86400))
    try:
        c = _connect()
        with c:
            n = c.execute("DELETE FROM search_terms WHERE day < ?", (cutoff,)).rowcount
        c.close()
        return n
    except Exception:  # noqa: BLE001
        log.exception("search term prune failed")
        return 0


def summary(start_day: str, end_day: str, limit: int = 20, min_n: int = PUBLIC_MIN) -> dict:
    """Public aggregate for [start_day, end_day]: top terms and terms that
    mostly found nothing. Terms below `min_n` searches are never returned."""
    out = {"from": start_day, "to": end_day, "min_searches": min_n, "searches": 0,
           "distinct_terms": 0, "top": [], "unmet": [], "by_kind": {}, "by_channel": {}}
    if not os.path.exists(db_path()):
        return out
    try:
        c = _connect(read_only=True)
    except sqlite3.Error:
        return out
    try:
        args = (start_day, end_day)
        where = "WHERE day BETWEEN ? AND ?"
        total = c.execute(f"SELECT COALESCE(SUM(n),0), COUNT(DISTINCT term) FROM search_terms {where}",
                          args).fetchone()
        out["searches"], out["distinct_terms"] = int(total[0]), int(total[1])
        out["by_kind"] = {k: int(n) for k, n in c.execute(
            f"SELECT kind, SUM(n) FROM search_terms {where} GROUP BY kind", args)}
        out["by_channel"] = {k: int(n) for k, n in c.execute(
            f"SELECT channel, SUM(n) FROM search_terms {where} GROUP BY channel", args)}
        rows = c.execute(
            f"SELECT term, SUM(n) n, SUM(zero) z FROM search_terms {where} "
            "GROUP BY term HAVING SUM(n) >= ? ORDER BY n DESC, term LIMIT 500",
            (*args, min_n)).fetchall()
    except sqlite3.Error:
        return out
    finally:
        c.close()
    items = [{"term": t, "searches": int(n), "no_results_pct": round(100.0 * z / n, 1) if n else 0.0}
             for t, n, z in rows]
    out["top"] = items[:limit]
    out["unmet"] = sorted((i for i in items if i["no_results_pct"] >= 50),
                          key=lambda i: -i["searches"])[:limit]
    return out


def matching(start_day: str, end_day: str, word: str, min_n: int = PUBLIC_MIN, limit: int = 10) -> list:
    """Public terms containing `word` as a whole word (for topic pages)."""
    s = summary(start_day, end_day, limit=500, min_n=min_n)
    rx = re.compile(r"\b" + re.escape(word) + r"s?\b")
    return [i for i in s["top"] if rx.search(i["term"])][:limit]
