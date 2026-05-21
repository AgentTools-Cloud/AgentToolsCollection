"""SQLite schema + connection helpers for the directory site.

Single-writer model: the crawler writes, the FastAPI server reads.
WAL mode for concurrent reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import random
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Callable

DEFAULT_DB_PATH = os.environ.get(
    "AGENT_TOOLS_DB_PATH", "/opt/mcpserver/data/agent-tools.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
  id              INTEGER PRIMARY KEY,
  slug            TEXT UNIQUE NOT NULL,
  name            TEXT NOT NULL,
  name_zh         TEXT,
  url             TEXT NOT NULL,
  description     TEXT,
  description_zh  TEXT,
  category        TEXT,
  chains          TEXT,
  price_min       REAL,
  price_max       REAL,
  currency        TEXT DEFAULT 'USDC',
  facilitator     TEXT,
  mcp_url         TEXT,
  openapi_url     TEXT,
  well_known_url  TEXT,
  source          TEXT NOT NULL,
  source_id       TEXT,
  tags            TEXT,
  region          TEXT,
  health          TEXT DEFAULT 'unknown',
  health_checked  INTEGER,
  last_seen       INTEGER,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_services_category ON services(category);
CREATE INDEX IF NOT EXISTS idx_services_health   ON services(health);
CREATE INDEX IF NOT EXISTS idx_services_region   ON services(region);
CREATE INDEX IF NOT EXISTS idx_services_source   ON services(source);

CREATE VIRTUAL TABLE IF NOT EXISTS services_fts USING fts5(
  name, name_zh, description, description_zh, tags, category,
  content='services', content_rowid='id', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS services_ai AFTER INSERT ON services BEGIN
  INSERT INTO services_fts(rowid, name, name_zh, description, description_zh, tags, category)
    VALUES (new.id, new.name, new.name_zh, new.description, new.description_zh, new.tags, new.category);
END;
CREATE TRIGGER IF NOT EXISTS services_ad AFTER DELETE ON services BEGIN
  INSERT INTO services_fts(services_fts, rowid, name, name_zh, description, description_zh, tags, category)
    VALUES('delete', old.id, old.name, old.name_zh, old.description, old.description_zh, old.tags, old.category);
END;
CREATE TRIGGER IF NOT EXISTS services_au AFTER UPDATE ON services BEGIN
  INSERT INTO services_fts(services_fts, rowid, name, name_zh, description, description_zh, tags, category)
    VALUES('delete', old.id, old.name, old.name_zh, old.description, old.description_zh, old.tags, old.category);
  INSERT INTO services_fts(rowid, name, name_zh, description, description_zh, tags, category)
    VALUES (new.id, new.name, new.name_zh, new.description, new.description_zh, new.tags, new.category);
END;

CREATE TABLE IF NOT EXISTS submissions (
  id          INTEGER PRIMARY KEY,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  note        TEXT,
  created_at  INTEGER NOT NULL,
  reviewed_at INTEGER
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  added       INTEGER DEFAULT 0,
  updated     INTEGER DEFAULT 0,
  errors      TEXT,
  status      TEXT NOT NULL DEFAULT 'running'
);
"""


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def connect(db_path: str = DEFAULT_DB_PATH, read_only: bool = False) -> sqlite3.Connection:
    _ensure_dir(db_path)
    # sqlite timeout handles ordinary writer contention; busy_timeout is kept for
    # older sqlite builds and PRAGMA visibility. 30s is intentionally longer
    # than one service batch commit, but short enough for systemd to fail loudly.
    uri = f"file:{db_path}?mode=ro" if read_only else db_path
    conn = sqlite3.connect(
        uri,
        uri=read_only,
        check_same_thread=False,
        timeout=30.0,
        isolation_level=None if read_only else "DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def with_retry(fn: Callable[[], Any], *, attempts: int = 6, base_delay: float = 0.2) -> Any:
    """Retry short write transactions when sqlite briefly has a lock.

    Callers should keep fn small and idempotent enough for a full retry.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not is_locked_error(e) or i == attempts - 1:
                raise
            last = e
            time.sleep(base_delay * (2 ** i) + random.uniform(0, base_delay))
    if last:
        raise last


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        c.commit()


@contextmanager
def writer(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def upsert_service(conn: sqlite3.Connection, row: dict) -> tuple:
    now = int(time.time())
    row.setdefault("created_at", now)
    row["updated_at"] = now
    row["last_seen"] = now
    row["chains"] = _to_json(row.get("chains"))
    row["tags"] = _to_json(row.get("tags"))

    cur = conn.cursor()
    existing = None
    if row.get("source") and row.get("source_id"):
        existing = cur.execute(
            "SELECT id, created_at, health, health_checked FROM services WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
    if existing is None:
        existing = cur.execute(
            "SELECT id, created_at, health, health_checked FROM services WHERE slug=?", (row["slug"],)
        ).fetchone()

    cols = [
        "slug", "name", "name_zh", "url", "description", "description_zh",
        "category", "chains", "price_min", "price_max", "currency",
        "facilitator", "mcp_url", "openapi_url", "well_known_url",
        "source", "source_id", "tags", "region",
        "health", "health_checked", "last_seen", "created_at", "updated_at",
    ]

    if existing is None:
        placeholders = ",".join(["?"] * len(cols))
        cur.execute(
            f"INSERT INTO services ({','.join(cols)}) VALUES ({placeholders})",
            [row.get(c) for c in cols],
        )
        return True, cur.lastrowid
    else:
        row["created_at"] = existing["created_at"]
        # Crawlers refresh discovery metadata; health probes own health fields.
        # Preserve health on metadata-only upserts so a crawl does not reset
        # hundreds of previously checked services back to unknown.
        if "health" not in row or row.get("health") is None:
            row["health"] = existing["health"]
        if "health_checked" not in row or row.get("health_checked") is None:
            row["health_checked"] = existing["health_checked"]
        set_clause = ",".join(f"{c}=?" for c in cols if c != "created_at")
        params = [row.get(c) for c in cols if c != "created_at"]
        params.append(existing["id"])
        cur.execute(f"UPDATE services SET {set_clause} WHERE id=?", params)
        return False, int(existing["id"])


def row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("chains", "tags"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def search(conn, q=None, category=None, chain=None, region=None, health=None, limit=50, offset=0):
    sql_parts = ["SELECT s.* FROM services s"]
    where = []
    params = []

    if q:
        safe_q = q.replace('"', " ").strip()
        if safe_q:
            sql_parts.append("JOIN services_fts f ON f.rowid = s.id")
            where.append("services_fts MATCH ?")
            params.append(f'"{safe_q}"*' if " " not in safe_q else safe_q)
    if category:
        where.append("s.category=?"); params.append(category)
    if chain:
        where.append("s.chains LIKE ?"); params.append(f'%"{chain}"%')
    if region:
        where.append("s.region=?"); params.append(region)
    if health:
        where.append("s.health=?"); params.append(health)
    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    sql_parts.append("ORDER BY (s.health='ok') DESC, s.updated_at DESC")
    sql_parts.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    rows = conn.execute(" ".join(sql_parts), params).fetchall()
    return [row_to_dict(r) for r in rows]


def get_by_slug(conn, slug):
    row = conn.execute("SELECT * FROM services WHERE slug=?", (slug,)).fetchone()
    return row_to_dict(row) if row else None


def list_categories(conn):
    rows = conn.execute(
        """SELECT category, COUNT(*) AS n
           FROM services
           WHERE category IS NOT NULL AND category != ''
           GROUP BY category
           ORDER BY n DESC"""
    ).fetchall()
    return [{"category": r["category"], "count": r["n"]} for r in rows]


def stats(conn):
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS n FROM services").fetchone()["n"]
    healthy = cur.execute("SELECT COUNT(*) AS n FROM services WHERE health='ok'").fetchone()["n"]
    by_source = {
        r["source"]: r["n"]
        for r in cur.execute("SELECT source, COUNT(*) AS n FROM services GROUP BY source").fetchall()
    }
    by_chain_rows = cur.execute("SELECT chains FROM services WHERE chains IS NOT NULL").fetchall()
    chains = {}
    for r in by_chain_rows:
        try:
            for c in json.loads(r["chains"]):
                chains[c] = chains.get(c, 0) + 1
        except Exception:
            pass
    return {"total": total, "healthy": healthy, "by_source": by_source, "by_chain": chains}


def record_crawl_start(conn, source):
    cur = conn.execute(
        "INSERT INTO crawl_runs (source, started_at, status) VALUES (?, ?, 'running')",
        (source, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_crawl_finish(conn, run_id, added, updated, errors=(), status="ok"):
    err_text = "\n".join(errors) if errors else None
    conn.execute(
        "UPDATE crawl_runs SET finished_at=?, added=?, updated=?, errors=?, status=? WHERE id=?",
        (int(time.time()), added, updated, err_text, status, run_id),
    )
    conn.commit()
