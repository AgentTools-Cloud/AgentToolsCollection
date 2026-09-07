"""SQLite schema + connection helpers for the directory site.

Single-writer model: the crawler writes, the FastAPI server reads.
WAL mode for concurrent reads.
"""

from __future__ import annotations

import json
import os
import math
import sqlite3
import time
import random
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Callable
from urllib.parse import urlparse

try:
    from . import mcp_safety as _mcp_safety
except Exception:  # pragma: no cover
    try:
        import mcp_safety as _mcp_safety  # type: ignore
    except Exception:
        _mcp_safety = None


def _safety_mark(name, description, tools_text):
    """Static abuse scan -> attach a marker only for non-clean results.

    Pure advisory: never blocks ingestion or display. Any failure is swallowed.
    """
    if _mcp_safety is None:
        return None
    try:
        r = _mcp_safety.scan_mcp(name or "", description or "", tools_text or "")
    except Exception:
        return None
    if r.verdict == "clean":
        return None
    return r.to_dict()


DEFAULT_DB_PATH = os.environ.get(
    "AGENT_TOOLS_DB_PATH", "/opt/mcpserver/data/agent-tools.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at INTEGER
);

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
  confidence      REAL,
  tx_30d          INTEGER,
    resource_count  INTEGER,
    resource_samples TEXT,
    payment         TEXT,
    call_info       TEXT,
    quality         TEXT,
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

CREATE TABLE IF NOT EXISTS tool_calls (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,
  tool        TEXT NOT NULL,
  args        TEXT,
  result_n    INTEGER,
  result_slug TEXT,
  client_name TEXT,
  client_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts   ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);

CREATE TABLE IF NOT EXISTS mcp_method_stats (
  day     TEXT NOT NULL,
  method  TEXT NOT NULL,
  n       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, method)
);

CREATE TABLE IF NOT EXISTS mcp_client_seen (
  ip           TEXT PRIMARY KEY,
  client_name  TEXT,
  ts           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS page_views (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  slug        TEXT NOT NULL,
  ref         TEXT,
  client_ip   TEXT,
  ua          TEXT
);
CREATE INDEX IF NOT EXISTS idx_page_views_ts   ON page_views(ts);
CREATE INDEX IF NOT EXISTS idx_page_views_slug ON page_views(kind, slug);

CREATE TABLE IF NOT EXISTS rate_limits (
    key          TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_updated ON rate_limits(updated_at);

CREATE TABLE IF NOT EXISTS service_paytos (
    service_id  INTEGER NOT NULL,
    chain       TEXT    NOT NULL,
    address     TEXT    NOT NULL,
    first_seen  INTEGER,
    last_seen   INTEGER,
    PRIMARY KEY (service_id, chain, address)
);
CREATE INDEX IF NOT EXISTS idx_service_paytos_last
    ON service_paytos(last_seen);

CREATE TABLE IF NOT EXISTS health_history (
  id          INTEGER PRIMARY KEY,
  service_id  INTEGER NOT NULL,
  checked_at  INTEGER NOT NULL,
  status      TEXT NOT NULL,
  latency_ms  INTEGER,
  http_status INTEGER,
  x402        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_health_history_svc ON health_history(service_id, checked_at);

CREATE TABLE IF NOT EXISTS a2a_agents (
  id                  INTEGER PRIMARY KEY,
  slug                TEXT UNIQUE NOT NULL,
  name                TEXT NOT NULL,
  description         TEXT,
  provider_name       TEXT,
  provider_url        TEXT,
  card_url            TEXT,
  endpoint_url        TEXT,
  homepage_url        TEXT,
  documentation_url   TEXT,
  protocol_version    TEXT,
  preferred_transport TEXT,
  skills              TEXT,
  skill_names         TEXT,
  capabilities        TEXT,
  default_input_modes  TEXT,
  default_output_modes TEXT,
  auth_schemes        TEXT,
  x402_supported      INTEGER DEFAULT 0,
  price_hint_usd      REAL,
  payto               TEXT,
  source              TEXT NOT NULL,
  source_id           TEXT,
  health              TEXT DEFAULT 'unknown',
  health_checked      INTEGER,
  latency_ms          INTEGER,
  last_seen           INTEGER,
  last_success_at     INTEGER,
  confidence          REAL,
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_source ON a2a_agents(source);
CREATE INDEX IF NOT EXISTS idx_a2a_health ON a2a_agents(health);

CREATE VIRTUAL TABLE IF NOT EXISTS a2a_fts USING fts5(
  name, description, skill_names, provider_name,
  content='a2a_agents', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS a2a_ai AFTER INSERT ON a2a_agents BEGIN
  INSERT INTO a2a_fts(rowid, name, description, skill_names, provider_name)
    VALUES (new.id, new.name, new.description, new.skill_names, new.provider_name);
END;
CREATE TRIGGER IF NOT EXISTS a2a_ad AFTER DELETE ON a2a_agents BEGIN
  INSERT INTO a2a_fts(a2a_fts, rowid, name, description, skill_names, provider_name)
    VALUES('delete', old.id, old.name, old.description, old.skill_names, old.provider_name);
END;
CREATE TRIGGER IF NOT EXISTS a2a_au AFTER UPDATE ON a2a_agents BEGIN
  INSERT INTO a2a_fts(a2a_fts, rowid, name, description, skill_names, provider_name)
    VALUES('delete', old.id, old.name, old.description, old.skill_names, old.provider_name);
  INSERT INTO a2a_fts(rowid, name, description, skill_names, provider_name)
    VALUES (new.id, new.name, new.description, new.skill_names, new.provider_name);
END;

CREATE TABLE IF NOT EXISTS mcp_servers (
  id                INTEGER PRIMARY KEY,
  slug              TEXT UNIQUE NOT NULL,
  name              TEXT NOT NULL,
  description       TEXT,
  homepage_url      TEXT,
  endpoint_url      TEXT,
  transport         TEXT,
  auth_method       TEXT,
  cost_hint         TEXT,
  source_code_url   TEXT,
  package_registry  TEXT,
  package_name      TEXT,
  package_download_count INTEGER,
  github_stars      INTEGER,
  tags              TEXT,
  tools_json        TEXT,
  tools_text        TEXT,
  x402_supported    INTEGER DEFAULT 0,
  source            TEXT NOT NULL,
  source_id         TEXT,
  source_url        TEXT,
  health            TEXT DEFAULT 'unknown',
  health_checked    INTEGER,
  latency_ms        INTEGER,
  http_status       INTEGER,
  last_seen         INTEGER,
  last_success_at   INTEGER,
  confidence        REAL,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_source ON mcp_servers(source);
CREATE INDEX IF NOT EXISTS idx_mcp_health ON mcp_servers(health);

CREATE VIRTUAL TABLE IF NOT EXISTS mcp_fts USING fts5(
  name, description, tags, tools_text,
  content='mcp_servers', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS mcp_ai AFTER INSERT ON mcp_servers BEGIN
  INSERT INTO mcp_fts(rowid, name, description, tags, tools_text)
    VALUES (new.id, new.name, new.description, new.tags, new.tools_text);
END;
CREATE TRIGGER IF NOT EXISTS mcp_ad AFTER DELETE ON mcp_servers BEGIN
  INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags, tools_text)
    VALUES('delete', old.id, old.name, old.description, old.tags, old.tools_text);
END;
CREATE TRIGGER IF NOT EXISTS mcp_au AFTER UPDATE ON mcp_servers BEGIN
  INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags, tools_text)
    VALUES('delete', old.id, old.name, old.description, old.tags, old.tools_text);
  INSERT INTO mcp_fts(rowid, name, description, tags, tools_text)
    VALUES (new.id, new.name, new.description, new.tags, new.tools_text);
END;

-- Every directory that vouched for a listing. The row's own `source` column is
-- only the first/owning one; a server indexed by several upstreams keeps one
-- row per upstream here so the public card can list them all.
CREATE TABLE IF NOT EXISTS listing_sources (
  kind        TEXT NOT NULL,          -- 'x402' | 'mcp' | 'a2a'
  listing_id  INTEGER NOT NULL,
  source      TEXT NOT NULL,
  source_id   TEXT,
  source_url  TEXT,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  PRIMARY KEY (kind, listing_id, source)
);
CREATE INDEX IF NOT EXISTS idx_listing_sources_listing
  ON listing_sources(kind, listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_sources_source
  ON listing_sources(source);

-- Identity and authorisation are deliberately separate: a GitHub login proves
-- who you are, only a token published on the listed host proves you control
-- the endpoint. Edit rights come from domain_ownership, never from users.
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY,
  provider       TEXT NOT NULL,          -- 'github'
  provider_uid   TEXT NOT NULL,          -- stable numeric id; logins can be renamed
  login          TEXT,
  avatar_url     TEXT,
  email          TEXT,                   -- provider-verified address, contact only
  email_verified INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'blocked'
  created_at     INTEGER NOT NULL,
  last_login_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_provider
  ON users(provider, provider_uid);

CREATE TABLE IF NOT EXISTS domain_ownership (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  host         TEXT NOT NULL,
  scope_path   TEXT,                     -- NULL = whole host
  method       TEXT NOT NULL,            -- 'inline' | 'wellknown_file' | 'dns_txt'
  token_hash   TEXT NOT NULL,            -- sha256 of the issued token
  status       TEXT NOT NULL,            -- 'pending' | 'verified' | 'revoked'
  created_at   INTEGER NOT NULL,
  verified_at  INTEGER,
  last_checked INTEGER,
  fail_count   INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_domain_scope
  ON domain_ownership(host, IFNULL(scope_path, ''))
  WHERE status = 'verified';
CREATE INDEX IF NOT EXISTS idx_domain_user ON domain_ownership(user_id, status);
CREATE INDEX IF NOT EXISTS idx_domain_host ON domain_ownership(host, status);

-- Public audit trail. Owner edits and our own corrections both land here.
CREATE TABLE IF NOT EXISTS listing_edits (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,            -- 'x402' | 'mcp' | 'a2a'
  listing_id   INTEGER NOT NULL,
  user_id      INTEGER,                  -- NULL = staff edit
  ownership_id INTEGER,
  field        TEXT NOT NULL,
  old_value    TEXT,
  new_value    TEXT,
  applied_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_edits_listing
  ON listing_edits(kind, listing_id, applied_at);

-- Hosts whose paths belong to different operators (shared gateways). Whole-host
-- verification must be refused there: the author cannot change the response and
-- the platform that can is not the author.
CREATE TABLE IF NOT EXISTS shared_hosts (
  host          TEXT PRIMARY KEY,
  path_count    INTEGER NOT NULL,
  listing_count INTEGER NOT NULL,
  -- 'shared'  = paths belong to unrelated authors, refuse whole-host claims
  -- 'single_operator' = one operator owns every path, whole-host claims are fine
  -- 'unreviewed' = auto-detected, not judged yet; treated as 'shared'
  verdict       TEXT NOT NULL DEFAULT 'unreviewed',
  updated_at    INTEGER NOT NULL
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
        # idempotent migrations for older DBs that pre-date these columns
        c.execute(
            """CREATE TABLE IF NOT EXISTS mcp_health_history (
                 id INTEGER PRIMARY KEY,
                 server_id INTEGER NOT NULL,
                 checked_at INTEGER NOT NULL,
                 status TEXT NOT NULL,
                 latency_ms INTEGER
               )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_mcp_hist_server "
            "ON mcp_health_history(server_id, checked_at)"
        )
        for ddl in (
            "ALTER TABLE services ADD COLUMN confidence REAL",
            "ALTER TABLE services ADD COLUMN tx_30d INTEGER",
            "ALTER TABLE services ADD COLUMN resource_count INTEGER",
            "ALTER TABLE services ADD COLUMN resource_samples TEXT",
            "ALTER TABLE services ADD COLUMN payment TEXT",
            "ALTER TABLE services ADD COLUMN call_info TEXT",
            "ALTER TABLE services ADD COLUMN quality TEXT",
            "ALTER TABLE services ADD COLUMN latency_ms INTEGER",
            "ALTER TABLE services ADD COLUMN http_status INTEGER",
            "ALTER TABLE services ADD COLUMN x402_ok INTEGER",
            "ALTER TABLE services ADD COLUMN payto_tx_30d INTEGER",
            "ALTER TABLE services ADD COLUMN payto_payers_30d INTEGER",
            "ALTER TABLE services ADD COLUMN payto_checked INTEGER",
            "ALTER TABLE services ADD COLUMN quality_score REAL",
            "ALTER TABLE a2a_agents ADD COLUMN quality_score REAL",
            "ALTER TABLE mcp_servers ADD COLUMN conformance TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN tool_count INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN latency_p95_ms INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN quality_score INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN package_download_count INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN tools_json TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN tools_text TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN safety_verdict TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN safety_score INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN safety_reasons TEXT",
        "ALTER TABLE mcp_servers ADD COLUMN protocol_version TEXT",
            "ALTER TABLE a2a_agents ADD COLUMN conformance TEXT",
            "ALTER TABLE services ADD COLUMN down_since INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN down_since INTEGER",
            "ALTER TABLE a2a_agents ADD COLUMN down_since INTEGER",
            "ALTER TABLE shared_hosts ADD COLUMN verdict TEXT "
            "NOT NULL DEFAULT 'unreviewed'",
            "ALTER TABLE domain_ownership ADD COLUMN token TEXT",
            "ALTER TABLE services ADD COLUMN owner_verified INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN owner_verified INTEGER",
            "ALTER TABLE a2a_agents ADD COLUMN owner_verified INTEGER",
            "ALTER TABLE services ADD COLUMN owner_edited TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN owner_edited TEXT",
            "ALTER TABLE a2a_agents ADD COLUMN owner_edited TEXT",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        _migrate_mcp_fts_tools(c)
        c.commit()


def _migrate_mcp_fts_tools(c: sqlite3.Connection) -> None:
    """Idempotently extend the MCP FTS index to cover per-tool text.

    The original mcp_fts indexed name/description/tags only, so a server's
    individual tool names were not searchable. This rebuilds the external
    content FTS table (and its sync triggers) to add a `tools_text` column,
    then repopulates from mcp_servers. Safe to run on every startup: it is a
    no-op once the column is present.
    """
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='mcp_fts'"
    ).fetchone()
    if row and "tools_text" in (row[0] or ""):
        return  # already migrated
    c.executescript(
        """
        DROP TRIGGER IF EXISTS mcp_ai;
        DROP TRIGGER IF EXISTS mcp_ad;
        DROP TRIGGER IF EXISTS mcp_au;
        DROP TABLE IF EXISTS mcp_fts;
        CREATE VIRTUAL TABLE mcp_fts USING fts5(
          name, description, tags, tools_text,
          content='mcp_servers', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER mcp_ai AFTER INSERT ON mcp_servers BEGIN
          INSERT INTO mcp_fts(rowid, name, description, tags, tools_text)
            VALUES (new.id, new.name, new.description, new.tags, new.tools_text);
        END;
        CREATE TRIGGER mcp_ad AFTER DELETE ON mcp_servers BEGIN
          INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags, tools_text)
            VALUES('delete', old.id, old.name, old.description, old.tags, old.tools_text);
        END;
        CREATE TRIGGER mcp_au AFTER UPDATE ON mcp_servers BEGIN
          INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags, tools_text)
            VALUES('delete', old.id, old.name, old.description, old.tags, old.tools_text);
          INSERT INTO mcp_fts(rowid, name, description, tags, tools_text)
            VALUES (new.id, new.name, new.description, new.tags, new.tools_text);
        END;
        INSERT INTO mcp_fts(mcp_fts) VALUES('rebuild');
        """
    )


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


def _record_source(conn: sqlite3.Connection, kind: str, listing_id, src: tuple) -> None:
    """Remember that `src` vouched for this listing. Additive: never removes."""
    source, source_id, source_url = src
    if not source or not listing_id:
        return
    now = int(time.time())
    try:
        conn.execute(
            "INSERT INTO listing_sources "
            "(kind, listing_id, source, source_id, source_url, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, listing_id, source) DO UPDATE SET "
            "last_seen=excluded.last_seen, "
            "source_id=COALESCE(excluded.source_id, listing_sources.source_id), "
            "source_url=COALESCE(excluded.source_url, listing_sources.source_url)",
            (kind, int(listing_id), str(source), source_id, source_url, now, now))
    except sqlite3.Error:
        # Provenance is additive metadata; never fail a listing write over it.
        pass


def sources_for(conn: sqlite3.Connection, kind: str, listing_id) -> list:
    """Every source that vouched for one listing, earliest first."""
    rows = conn.execute(
        "SELECT source, source_id, source_url, first_seen, last_seen "
        "FROM listing_sources WHERE kind=? AND listing_id=? "
        "ORDER BY first_seen, source",
        (kind, int(listing_id))).fetchall()
    return [dict(r) for r in rows]


def sources_for_many(conn: sqlite3.Connection, kind: str, ids) -> dict:
    """Bulk variant for list pages: {listing_id: [sources, ...]}."""
    wanted = [int(i) for i in ids if i is not None]
    if not wanted:
        return {}
    out: dict = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT listing_id, source, source_id, source_url, first_seen, last_seen "
            f"FROM listing_sources WHERE kind=? AND listing_id IN ({marks}) "
            "ORDER BY first_seen, source", [kind] + chunk):
            d = dict(r)
            out.setdefault(d.pop("listing_id"), []).append(d)
    return out


def listing_sources(conn: sqlite3.Connection, kind: str, listing_id: int) -> list:
    """All directories this listing was found in, oldest first."""
    rows = conn.execute(
        "SELECT source, source_url, first_seen, last_seen FROM listing_sources "
        "WHERE kind=? AND listing_id=? ORDER BY first_seen, source",
        (kind, listing_id),
    ).fetchall()
    return [{"source": r["source"], "source_url": r["source_url"],
             "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows]


def attach_sources(conn: sqlite3.Connection, kind: str, rows: list) -> list:
    """Attach the public `sources` list to each row (listing provenance)."""
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            row["sources"] = listing_sources(conn, kind, int(row["id"]))
    return rows


def upsert_service(conn: sqlite3.Connection, row: dict) -> tuple:
    now = int(time.time())
    # Capture provenance before dedup logic can rewrite it (first-source-wins).
    _src = (row.get("source"), row.get("source_id"), row.get("source_url"))
    row.setdefault("created_at", now)
    row["updated_at"] = now
    row["last_seen"] = now
    row["chains"] = _to_json(row.get("chains"))
    row["tags"] = _to_json(row.get("tags"))
    row["resource_samples"] = _to_json(row.get("resource_samples"))
    row["payment"] = _to_json(row.get("payment"))
    row["call_info"] = _to_json(row.get("call_info"))
    row["quality"] = _to_json(row.get("quality"))
    # facilitator is a scalar identifier (preset name / URL); some pay-skills
    # PAY.md frontmatter carries a nested x402 facilitator object that sqlite
    # cannot bind. Coerce any stray dict/list to a string.
    _fac = row.get("facilitator")
    if isinstance(_fac, dict):
        row["facilitator"] = _fac.get("url") or _fac.get("name") or _fac.get("id") or _to_json(_fac)
    elif isinstance(_fac, (list, tuple)):
        row["facilitator"] = _to_json(_fac)

    cur = conn.cursor()
    existing = None
    if row.get("source") and row.get("source_id"):
        existing = cur.execute(
            "SELECT * FROM services WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
    if existing is None:
        existing = cur.execute(
            "SELECT * FROM services WHERE slug=?", (row["slug"],)
        ).fetchone()
    if existing is None and row.get("source") == "paygent-discover" and row.get("url"):
        existing = cur.execute(
            "SELECT * FROM services "
            "WHERE source=? AND rtrim(lower(url), '/')=? ORDER BY id LIMIT 1",
            ("paygent-discover", row["url"].rstrip("/").lower()),
        ).fetchone()

    cols = [
        "slug", "name", "name_zh", "url", "description", "description_zh",
        "category", "chains", "price_min", "price_max", "currency",
        "facilitator", "mcp_url", "openapi_url", "well_known_url",
        "source", "source_id", "tags", "region",
        "health", "health_checked", "last_seen",
        "confidence", "tx_30d", "resource_count", "resource_samples",
        "payment", "call_info", "quality",
        "created_at", "updated_at",
    ]

    if existing is None:
        placeholders = ",".join(["?"] * len(cols))
        cur.execute(
            f"INSERT INTO services ({','.join(cols)}) VALUES ({placeholders})",
            [row.get(c) for c in cols],
        )
        new_id = cur.lastrowid
        _record_source(conn, "x402", new_id, _src)
        return True, new_id
    else:
        row["created_at"] = existing["created_at"]
        # Crawlers refresh discovery metadata; probes own the measured fields.
        # A crawl that simply has nothing to say about a field must not blank
        # what a probe established -- that is how 252 on-chain tx counts and,
        # worse, freshly backfilled payTo addresses were being wiped every
        # six hours.
        for field in ("health", "health_checked", "tx_30d", "payment",
                      "resource_count", "resource_samples", "well_known_url",
                      "confidence"):
            if row.get(field) is None:
                row[field] = existing[field]
        _restore_owner_edits(row, existing)
        set_clause = ",".join(f"{c}=?" for c in cols if c != "created_at")
        params = [row.get(c) for c in cols if c != "created_at"]
        params.append(existing["id"])
        cur.execute(f"UPDATE services SET {set_clause} WHERE id=?", params)
        _record_source(conn, "x402", existing["id"], _src)
        return False, int(existing["id"])


def row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("chains", "tags", "resource_samples", "payment", "call_info", "quality"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


# ---------------------------------------------------------------------------
# Query expansion (P1-3)
#
# Agents say "twitter" but services index "X" or "推特"; agents say "巨鲸"
# but the listing reads "whale". Build a small, hand-curated synonym map and
# OR-expand each token at query time. Keep it deliberately small — false
# friends in a synonym set are worse than a missed recall.
# ---------------------------------------------------------------------------
_SYNONYM_GROUPS: list[list[str]] = [
    # social / twitter
    ["twitter", "x", "推特", "tweet", "tweets"],
    # crypto market roles
    ["whale", "whales", "巨鲸", "大户"],
    ["airdrop", "空投"],
    ["nft", "non-fungible", "藏品"],
    ["wallet", "钱包"],
    ["price", "quote", "报价", "价格"],
    ["swap", "交换", "兑换"],
    ["balance", "余额"],
    ["transfer", "转账", "send"],
    ["dex", "去中心化交易所"],
    ["cex", "中心化交易所", "exchange", "交易所"],
    ["onchain", "on-chain", "链上"],
    # chains
    ["solana", "sol"],
    ["ethereum", "eth"],
    ["base", "basechain"],
    # search / discovery
    ["search", "find", "查找", "搜索"],
    ["news", "新闻", "资讯"],
    ["weather", "天气"],
    ["image", "picture", "图片", "图像"],
    ["video", "视频"],
    ["translate", "translation", "翻译"],
    ["llm", "language model", "大模型"],
]
_SYNONYM_MAP: dict[str, list[str]] = {}
for _g in _SYNONYM_GROUPS:
    for _t in _g:
        _SYNONYM_MAP[_t.lower()] = _g


_FTS_STOPWORDS = {
    "a", "an", "and", "api", "for", "in", "key", "of", "pack",
    "quota", "the", "to", "with", "mini", "compatible", "service",
}


def _query_tokens(q: str) -> list[str]:
    if not q:
        return []
    cleaned = []
    for ch in q.lower():
        if ch.isalnum() or ch == " " or "一" <= ch <= "鿿":
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    return [t for t in "".join(cleaned).split() if t]


def _expand_fts_query(q: str) -> str | None:
    """Turn a free-text query into a strict FTS5 MATCH expression.

    Tokens are AND-ed for precision; callers can fall back to
    _expand_fts_query_relaxed() when a long natural-language query returns 0.
    """
    tokens = _query_tokens(q)
    if not tokens:
        return None
    groups: list[str] = []
    for t in tokens:
        syns = _SYNONYM_MAP.get(t)
        if syns:
            quoted = [f'"{s}"' for s in syns]
            groups.append("(" + " OR ".join(quoted) + ")")
        elif len(tokens) == 1:
            groups.append(f"{t}*")
        else:
            groups.append(f'"{t}"')
    return " AND ".join(groups)


def _expand_fts_query_relaxed(q: str) -> str | None:
    """Recall-oriented FTS query for long agent intent strings.

    Keeps distinctive tokens, expands known synonyms, and ORs terms so one
    wrong model/version word does not zero out otherwise relevant services.
    """
    tokens = _query_tokens(q)
    terms: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if len(t) < 3 and not any(ch.isdigit() for ch in t):
            continue
        if t in _FTS_STOPWORDS:
            continue
        syns = _SYNONYM_MAP.get(t) or [t]
        for s in syns:
            sl = str(s).lower().strip()
            if not sl or sl in seen:
                continue
            seen.add(sl)
            if " " in sl:
                terms.append(f'"{sl}"')
            elif len(sl) >= 4:
                terms.append(f"{sl}*")
            else:
                terms.append(f'"{sl}"')
        if len(terms) >= 12:
            break
    if len(terms) < 2:
        return None
    return " OR ".join(terms)


def _match_reason(row: dict, q: str | None) -> list[str]:
    """Human-readable ranking signals so agents see WHY a row was returned."""
    reasons: list[str] = []
    tx = row.get("tx_30d") or 0
    if row.get("health") == "ok" and tx > 0:
        reasons.append("popular+healthy")
    if row.get("health") == "ok":
        reasons.append("health=ok")
    elif row.get("health"):
        reasons.append(f"health={row['health']}")
    if tx:
        reasons.append(f"tx_30d={int(tx)}")
    conf = row.get("confidence")
    if conf is not None:
        reasons.append(f"confidence={conf:.2f}")
    if row.get("category"):
        reasons.append(f"category={row['category']}")
    snip = row.get("match_snippet")
    if snip:
        reasons.append(f"matched: {snip}")
    elif q:
        # No FTS hit (shouldn't happen when q is set & joined) — still show
        # the user-issued query for debugging.
        reasons.append(f"query={q!r}")
    return reasons


def search(conn, q=None, category=None, chain=None, region=None, health=None,
           min_confidence=None, has_mcp: bool = False,
           limit=50, offset=0):
    """Search the directory.

    Returns a list of dict rows. Each row gets two extra synthetic fields:
      - match_snippet : FTS5 snippet of the matched text (only when q given)
      - match_reason  : list[str] of human-readable ranking signals
                        (e.g. ["health=ok", "tx_30d=7500", "matched: ..."])
    """
    # Select s.* plus an FTS snippet when we have a query. The snippet
    # uses [[ and ]] as match markers (less likely to collide with code).
    select_cols = ["s.*"]
    sql_parts: list[str]
    where: list[str] = []
    params: list = []

    fts_query = None
    if q:
        fts_query = _expand_fts_query(q)

    if fts_query is not None:
        # Use snippet() with a moderate token window — column -1 = any column.
        select_cols.append(
            "snippet(services_fts, -1, '[[', ']]', '…', 12) AS match_snippet"
        )
        sql_parts = ["SELECT " + ", ".join(select_cols) + " FROM services s"]
        sql_parts.append("JOIN services_fts f ON f.rowid = s.id")
        where.append("services_fts MATCH ?")
        params.append(fts_query)
    else:
        sql_parts = ["SELECT " + ", ".join(select_cols) + " FROM services s"]

    if category:
        where.append("s.category=?"); params.append(category)
    if chain:
        where.append("s.chains LIKE ?"); params.append(f'%"{chain}"%')
    if region:
        where.append("s.region=?"); params.append(region)
    if health:
        where.append("s.health=?"); params.append(health)
    if min_confidence is not None:
        where.append("s.confidence >= ?"); params.append(float(min_confidence))
    if has_mcp:
        where.append("s.mcp_url IS NOT NULL AND s.mcp_url != ''")
    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    # Quality-aware ordering (P1-4):
    #   1. healthy AND has real 30-day traffic — the strongest "this thing
    #      actually works" signal we have. Bumped above bare health=ok so
    #      a popular service outranks an idle-but-up one.
    #   2. healthy at all
    #   3. has a confidence score (means x402scan saw it)
    #   4. confidence value
    #   5. tx_30d value
    #   6. recency
    sql_parts.append(
        "ORDER BY "
        # primary tier: ok -> degraded -> unknown -> down (dead links sink)
        "CASE s.health WHEN 'ok' THEN 0 WHEN 'degraded' THEN 1 "
        "WHEN 'unknown' THEN 2 WHEN 'down' THEN 3 ELSE 4 END ASC, "
        "(s.health='ok' AND COALESCE(s.tx_30d,0) > 0) DESC, "
        "(s.confidence IS NOT NULL) DESC, "
        "s.confidence DESC, "
        "COALESCE(s.tx_30d, 0) DESC, "
        "s.updated_at DESC"
    )
    sql_parts.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    rows = conn.execute(" ".join(sql_parts), params).fetchall()
    if not rows and q and fts_query is not None:
        relaxed = _expand_fts_query_relaxed(q)
        if relaxed:
            select_cols = [
                "s.*",
                "snippet(services_fts, -1, '[[', ']]', '…', 12) AS match_snippet",
            ]
            sql_parts = ["SELECT " + ", ".join(select_cols) + " FROM services s"]
            sql_parts.append("JOIN services_fts f ON f.rowid = s.id")
            where = ["services_fts MATCH ?"]
            params = [relaxed]
            if category:
                where.append("s.category=?"); params.append(category)
            if chain:
                where.append("s.chains LIKE ?"); params.append(f'%"{chain}"%')
            if region:
                where.append("s.region=?"); params.append(region)
            if health:
                where.append("s.health=?"); params.append(health)
            if min_confidence is not None:
                where.append("s.confidence >= ?"); params.append(float(min_confidence))
            if has_mcp:
                where.append("s.mcp_url IS NOT NULL AND s.mcp_url != ''")
            sql_parts.append("WHERE " + " AND ".join(where))
            sql_parts.append(
                "ORDER BY bm25(services_fts), "
                "CASE s.health WHEN 'ok' THEN 0 WHEN 'degraded' THEN 1 "
                "WHEN 'unknown' THEN 2 WHEN 'down' THEN 3 ELSE 4 END ASC, "
                "(s.health='ok' AND COALESCE(s.tx_30d,0) > 0) DESC, "
                "(s.confidence IS NOT NULL) DESC, "
                "s.confidence DESC, "
                "COALESCE(s.tx_30d, 0) DESC, "
                "s.updated_at DESC"
            )
            sql_parts.append("LIMIT ? OFFSET ?")
            params.extend([limit, offset])
            rows = conn.execute(" ".join(sql_parts), params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r)
        # Strip empty/None snippet so it doesn't pollute output
        if d.get("match_snippet") in (None, "", "…"):
            d.pop("match_snippet", None)
        d["match_reason"] = _match_reason(d, q)
        out.append(d)
    return out


def get_by_slug(conn, slug):
    row = conn.execute("SELECT * FROM services WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    out = row_to_dict(row)
    out["sources"] = listing_sources(conn, "x402", int(out["id"]))
    return out


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
    by_chain_rows = cur.execute("SELECT chains FROM services WHERE chains IS NOT NULL").fetchall()
    chains = {}
    for r in by_chain_rows:
        try:
            for c in json.loads(r["chains"]):
                chains[c] = chains.get(c, 0) + 1
        except Exception:
            pass
    return {"total": total, "healthy": healthy, "by_chain": chains}


def record_crawl_start(conn, source):
    cur = conn.execute(
        "INSERT INTO crawl_runs (source, started_at, status) VALUES (?, ?, 'running')",
        (source, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_tool_call(conn, tool, args=None, result_n=None, result_slug=None,
                  client_name=None, client_ip=None):
    conn.execute(
        "INSERT INTO tool_calls (ts, tool, args, result_n, result_slug, "
        "client_name, client_ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), tool, _to_json(args) if args is not None else None,
         result_n, result_slug, client_name, client_ip),
    )
    conn.commit()


def bump_mcp_method(conn, method, day=None, n=1):
    """Increment the daily counter for an MCP JSON-RPC method seen at the
    /mcp-discovery entry. Lightweight telemetry: bounded rows (days x methods),
    no per-request row growth. Caller commits (via writer())."""
    if not method:
        return
    if day is None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
    conn.execute(
        "INSERT INTO mcp_method_stats (day, method, n) VALUES (?, ?, ?) "
        "ON CONFLICT(day, method) DO UPDATE SET n = n + excluded.n",
        (day, str(method)[:64], int(n)),
    )


_CLIENT_SEEN_TTL = 86400.0


def remember_mcp_client(conn, ip, name):
    """Associate an MCP clientInfo name with a peer IP.

    The stateless transport hands us clientInfo on `initialize` and nothing on
    the `tools/call` that follows, so the link is kept here instead of in the
    session. Rows expire, keeping the table at roughly one row per active peer.
    Caller commits (via writer())."""
    if not ip or not name:
        return
    now = time.time()
    conn.execute(
        "INSERT INTO mcp_client_seen (ip, client_name, ts) VALUES (?, ?, ?) "
        "ON CONFLICT(ip) DO UPDATE SET client_name = excluded.client_name, "
        "ts = excluded.ts",
        (str(ip)[:64], str(name)[:128], now),
    )
    conn.execute("DELETE FROM mcp_client_seen WHERE ts < ?", (now - _CLIENT_SEEN_TTL,))


def recent_mcp_client(conn, ip, max_age=_CLIENT_SEEN_TTL):
    """Best-guess clientInfo name last seen from `ip`. Shared NAT can mislabel."""
    if not ip:
        return None
    row = conn.execute(
        "SELECT client_name, ts FROM mcp_client_seen WHERE ip = ?", (str(ip)[:64],)
    ).fetchone()
    if row is None or (time.time() - (row[1] or 0)) > max_age:
        return None
    return row[0] or None


def log_page_view(conn, kind, slug, ref=None, client_ip=None, ua=None):
    """Record a real-browser detail-page view (fired by a JS beacon, so bots
    that don't execute JS never reach here)."""
    conn.execute(
        "INSERT INTO page_views (ts, kind, slug, ref, client_ip, ua) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), kind, slug, ref, client_ip, ua),
    )
    conn.commit()


def hit_rate_limit(conn, key: str, limit: int, window_seconds: int,
                   now: int | None = None) -> dict:
    """Increment a fixed-window counter and return the rate-limit state."""
    now = int(time.time() if now is None else now)
    limit = int(limit)
    window_seconds = max(1, int(window_seconds))
    window_start = now - (now % window_seconds)
    reset_at = window_start + window_seconds
    if limit <= 0:
        return {
            "allowed": True,
            "limit": limit,
            "remaining": None,
            "reset_at": reset_at,
            "retry_after": 0,
        }

    # Keep the table small without doing expensive maintenance on every row.
    conn.execute(
        "DELETE FROM rate_limits WHERE updated_at < ?",
        (now - max(86400 * 7, window_seconds * 2),),
    )
    row = conn.execute(
        "SELECT count FROM rate_limits WHERE key=? AND window_start=?",
        (key, window_start),
    ).fetchone()
    current = int(row["count"] if row else 0)
    if current >= limit:
        return {
            "allowed": False,
            "limit": limit,
            "remaining": 0,
            "reset_at": reset_at,
            "retry_after": max(1, reset_at - now),
        }

    new_count = current + 1
    if row:
        conn.execute(
            "UPDATE rate_limits SET count=?, updated_at=? WHERE key=? AND window_start=?",
            (new_count, now, key, window_start),
        )
    else:
        conn.execute(
            "INSERT INTO rate_limits (key, window_start, count, updated_at) VALUES (?, ?, ?, ?)",
            (key, window_start, new_count, now),
        )
    return {
        "allowed": True,
        "limit": limit,
        "remaining": max(0, limit - new_count),
        "reset_at": reset_at,
        "retry_after": 0,
    }


def record_crawl_finish(conn, run_id, added, updated, errors=(), status="ok"):
    err_text = "\n".join(errors) if errors else None
    conn.execute(
        "UPDATE crawl_runs SET finished_at=?, added=?, updated=?, errors=?, status=? WHERE id=?",
        (int(time.time()), added, updated, err_text, status, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Submissions (P3-3): self-service service registration via MCP `register`.
# The HTTP /api/v1/submit endpoint also lands here. payload is opaque JSON.
# ---------------------------------------------------------------------------

def _canonical_origin(url: str) -> str | None:
    """Return scheme://host (lowercased) — used to dedup near-identical URLs."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return None
        return f"{p.scheme.lower()}://{p.netloc.lower()}"
    except Exception:
        return None


def _canonical_endpoint(url: str) -> str | None:
    """scheme://host/path (lowercased host, trailing slash trimmed).

    The dedup key is the endpoint, not the origin: one provider commonly sells
    several paid endpoints under the same domain, and each is its own service.
    """
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url.strip())
        if not p.scheme or not p.netloc:
            return None
        path = (p.path or "").rstrip("/")
        return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
    except Exception:
        return None


def find_service_by_url(conn, url: str) -> dict | None:
    """Best-effort lookup: a services row registering this exact endpoint."""
    key = _canonical_endpoint(url)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM services WHERE "
        "rtrim(lower(url), '/')=? OR rtrim(lower(mcp_url), '/')=? LIMIT 1",
        (key, key),
    ).fetchone()
    return row_to_dict(row) if row else None


def count_recent_submissions(conn, client_ip: str | None,
                             since_seconds: int = 86400) -> int:
    """Count pending submissions from `client_ip` in the last N seconds.

    Uses json_extract on the payload — we store client_ip inside the payload
    JSON so we don't need a schema change.
    """
    if not client_ip:
        return 0
    cutoff = int(time.time()) - since_seconds
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions "
        "WHERE created_at >= ? AND status = 'pending' "
        "AND json_extract(payload, '$._client_ip') = ?",
        (cutoff, client_ip),
    ).fetchone()
    return int(row["n"] if row else 0)


def find_pending_submission(conn, url: str) -> dict | None:
    """Return a pending submission with the same canonical origin, if any."""
    origin = _canonical_origin(url)
    if not origin:
        return None
    row = conn.execute(
        "SELECT id, payload, created_at FROM submissions "
        "WHERE status = 'pending' "
        "AND json_extract(payload, '$.url') LIKE ? "
        "ORDER BY id DESC LIMIT 1",
        (origin + "%",),
    ).fetchone()
    return dict(row) if row else None


def create_submission(conn, payload: dict) -> int:
    """Insert a submission row. payload is stored verbatim as JSON."""
    cur = conn.execute(
        "INSERT INTO submissions (payload, status, created_at) "
        "VALUES (?, 'pending', ?)",
        (json.dumps(payload, ensure_ascii=False), int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_submissions(conn, status: str = "pending", limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, payload, status, note, created_at, reviewed_at "
        "FROM submissions WHERE status = ? "
        "ORDER BY id DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        out.append(d)
    return out


def mark_submission(conn, sub_id: int, status: str, note: str | None = None):
    conn.execute(
        "UPDATE submissions SET status=?, note=?, reviewed_at=? WHERE id=?",
        (status, note, int(time.time()), sub_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# A2A agents (P0): separate table from `services`. Lifecycle, health probe
# and schema differ from x402 services, so we never merge them; the unified
# resource search (P1) is a read-time union instead.
# ---------------------------------------------------------------------------

_A2A_COLS = [
    "slug", "name", "description", "provider_name", "provider_url",
    "card_url", "endpoint_url", "homepage_url", "documentation_url",
    "protocol_version", "preferred_transport",
    "skills", "skill_names", "capabilities",
    "default_input_modes", "default_output_modes", "auth_schemes",
    "x402_supported", "price_hint_usd", "payto",
    "source", "source_id",
    "health", "health_checked", "latency_ms",
    "last_seen", "last_success_at", "confidence",
    "created_at", "updated_at",
]

_A2A_JSON_COLS = (
    "skills", "capabilities", "default_input_modes",
    "default_output_modes", "auth_schemes",
)


def a2a_row_to_dict(row) -> dict:
    d = dict(row)
    for k in _A2A_JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    _sf = _safety_mark(d.get("name"), d.get("description"), d.get("skill_names"))
    if _sf:
        d["safety"] = _sf
    return d


def upsert_a2a_agent(conn: sqlite3.Connection, row: dict) -> tuple:
    """Insert/update an A2A agent. Dedup on (source, source_id) then slug.

    `skills` may be a list of dicts; we also derive `skill_names` (a flat
    text blob) so FTS can match skill ids/names/tags without parsing JSON.
    """
    now = int(time.time())
    _src = (row.get("source"), row.get("source_id"), row.get("source_url"))
    row.setdefault("created_at", now)
    row["updated_at"] = now
    row["last_seen"] = now

    # derive skill_names blob for FTS if not explicitly supplied
    if not row.get("skill_names"):
        names: list[str] = []
        skills = row.get("skills")
        if isinstance(skills, list):
            for sk in skills:
                if isinstance(sk, dict):
                    for key in ("name", "id", "description"):
                        v = sk.get(key)
                        if v:
                            names.append(str(v))
                    tags = sk.get("tags")
                    if isinstance(tags, list):
                        names.extend(str(t) for t in tags)
                elif sk:
                    names.append(str(sk))
        row["skill_names"] = " ".join(names) if names else None

    row["x402_supported"] = 1 if row.get("x402_supported") else 0
    for k in _A2A_JSON_COLS:
        row[k] = _to_json(row.get(k))

    cur = conn.cursor()
    existing = None
    existing_by_source_id = False
    if row.get("source") and row.get("source_id"):
        existing = cur.execute(
            "SELECT * FROM a2a_agents WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
        existing_by_source_id = existing is not None
    if existing is None:
        existing = cur.execute(
            "SELECT * FROM a2a_agents WHERE slug=?",
            (row["slug"],),
        ).fetchone()

    if existing is None:
        placeholders = ",".join(["?"] * len(_A2A_COLS))
        cur.execute(
            f"INSERT INTO a2a_agents ({','.join(_A2A_COLS)}) VALUES ({placeholders})",
            [row.get(c) for c in _A2A_COLS],
        )
        new_id = cur.lastrowid
        _record_source(conn, "a2a", new_id, _src)
        return True, new_id

    row["created_at"] = existing["created_at"]
    # A stable (source, source_id) identity should keep its public slug. Some
    # crawlers derive slug from mutable Agent Card names; changing it can collide
    # with another indexed agent and break the whole crawl batch.
    if existing_by_source_id:
        row["slug"] = existing["slug"]
    # A metadata-only refresh has nothing to say about the last probe, and
    # "nothing to say" is not "wipe it". latency_ms feeds the perf component of
    # the score, so losing it silently cost verified-looking agents 20 points.
    for _keep in ("health", "health_checked", "last_success_at", "latency_ms"):
        if row.get(_keep) is None:
            row[_keep] = existing[_keep]
    _restore_owner_edits(row, existing)
    set_clause = ",".join(f"{c}=?" for c in _A2A_COLS if c != "created_at")
    params = [row.get(c) for c in _A2A_COLS if c != "created_at"]
    params.append(existing["id"])
    cur.execute(f"UPDATE a2a_agents SET {set_clause} WHERE id=?", params)
    _record_source(conn, "a2a", existing["id"], _src)
    return False, int(existing["id"])


def search_a2a(conn, q=None, health=None, x402_only=False,
               access=None, limit=50, offset=0):
    """Search A2A agents. Ranks healthy + x402-capable + confident first."""
    select_cols = ["a.*"]
    where: list[str] = []
    params: list = []
    fts_query = _expand_fts_query(q) if q else None

    if fts_query is not None:
        select_cols.append(
            "snippet(a2a_fts, -1, '[[', ']]', '…', 12) AS match_snippet"
        )
        sql = ["SELECT " + ", ".join(select_cols) + " FROM a2a_agents a"]
        sql.append("JOIN a2a_fts f ON f.rowid = a.id")
        where.append("a2a_fts MATCH ?")
        params.append(fts_query)
    else:
        sql = ["SELECT " + ", ".join(select_cols) + " FROM a2a_agents a"]

    if health:
        where.append("a.health=?"); params.append(health)
    if x402_only:
        where.append("a.x402_supported=1")
    if access in ("open", "key", "x402"):
        where.append(_access_case("a", "a2a") + "=?"); params.append(access)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append(
        "ORDER BY "
        "CASE a.health WHEN 'ok' THEN 0 WHEN 'degraded' THEN 1 "
        "WHEN 'unknown' THEN 2 WHEN 'down' THEN 3 ELSE 4 END ASC, "
        "a.x402_supported DESC, "
        "(a.confidence IS NOT NULL) DESC, "
        "a.confidence DESC, "
        "a.updated_at DESC"
    )
    sql.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    rows = conn.execute(" ".join(sql), params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = a2a_row_to_dict(r)
        if d.get("match_snippet") in (None, "", "…"):
            d.pop("match_snippet", None)
        out.append(d)
    return out


def get_a2a_by_slug(conn, slug):
    row = conn.execute("SELECT * FROM a2a_agents WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    out = a2a_row_to_dict(row)
    out["sources"] = listing_sources(conn, "a2a", int(out["id"]))
    return out


def find_a2a_by_card_url(conn, card_url: str) -> dict | None:
    if not card_url:
        return None
    row = conn.execute(
        "SELECT * FROM a2a_agents WHERE card_url=? LIMIT 1", (card_url,)
    ).fetchone()
    return a2a_row_to_dict(row) if row else None


def a2a_stats(conn):
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS n FROM a2a_agents").fetchone()["n"]
    healthy = cur.execute(
        "SELECT COUNT(*) AS n FROM a2a_agents WHERE health='ok'"
    ).fetchone()["n"]
    x402 = cur.execute(
        "SELECT COUNT(*) AS n FROM a2a_agents WHERE x402_supported=1"
    ).fetchone()["n"]
    conformant = cur.execute(
        "SELECT COUNT(*) AS n FROM a2a_agents WHERE conformance='pass'"
    ).fetchone()["n"]
    new_7d = cur.execute(
        "SELECT COUNT(*) AS n FROM a2a_agents "
        "WHERE created_at >= strftime('%s', 'now', '-7 days')"
    ).fetchone()["n"]

    def _host(url):
        h = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return h[4:] if h.startswith("www.") else h
    doms, dom_healthy, dom_conf, dom_x402 = set(), set(), set(), set()
    for r in cur.execute(
        "SELECT endpoint_url, card_url, homepage_url, provider_url, "
        "health, conformance, x402_supported FROM a2a_agents"
    ).fetchall():
        host = None
        for u in (r["endpoint_url"], r["card_url"], r["homepage_url"], r["provider_url"]):
            if u:
                host = _host(u if "//" in u else "https://" + u)
                if host:
                    break
        if not host:
            continue
        doms.add(host)
        if r["health"] == "ok":
            dom_healthy.add(host)
        if r["conformance"] == "pass":
            dom_conf.add(host)
        if r["x402_supported"] == 1:
            dom_x402.add(host)
    _acc = {r["t"]: r["n"] for r in cur.execute(
        "SELECT " + _access_case("a2a_agents", "a2a") + " AS t, COUNT(*) AS n "
        "FROM a2a_agents GROUP BY t").fetchall()}
    return {"total": total, "healthy": healthy, "x402_capable": x402,
            "conformant": conformant, "new_7d": new_7d,
            "access_open": _acc.get("open", 0), "access_key": _acc.get("key", 0),
            "access_x402": _acc.get("x402", 0),
            "domains": len(doms), "healthy_domains": len(dom_healthy),
            "conformant_domains": len(dom_conf), "x402_domains": len(dom_x402)}


# ---------------------------------------------------------------------------
# MCP servers: standalone directory (PulseMCP / official registry import).
# Kept separate from `services` so non-x402 MCP servers never pollute the
# x402 service search; the unified resource search (P1) unions them at read
# time alongside x402 services that also expose an mcp_url.
# ---------------------------------------------------------------------------

_MCP_COLS = [
    "slug", "name", "description", "homepage_url", "endpoint_url",
    "transport", "auth_method", "cost_hint", "source_code_url",
    "package_registry", "package_name", "package_download_count",
    "github_stars", "tags",
    "x402_supported", "source", "source_id", "source_url", "kind",
    "health", "health_checked", "latency_ms", "http_status",
    "last_seen", "last_success_at", "confidence",
    "created_at", "updated_at",
]

_MCP_JSON_COLS = ("tags",)


def mcp_row_to_dict(row) -> dict:
    d = dict(row)
    for k in _MCP_JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    # tools_json holds the server's advertised tools [{name, description}];
    # parsed here for display but kept out of _MCP_JSON_COLS so the crawl
    # upsert path never serializes/overwrites it (only health probing does).
    if d.get("tools_json"):
        try:
            d["tools"] = json.loads(d["tools_json"])
        except (TypeError, json.JSONDecodeError):
            pass
    _sf = _safety_mark(d.get("name"), d.get("description"), d.get("tools_text"))
    if _sf:
        d["safety"] = _sf
    d.pop("tools_text", None)
    return d


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (key, value, int(time.time())),
    )


def delete_mcp_by_source(conn: sqlite3.Connection, source: str, source_id: str) -> int:
    cur = conn.execute(
        "DELETE FROM mcp_servers WHERE source=? AND source_id=?",
        (source, source_id),
    )
    return cur.rowcount


def mcp_source_ids(conn: sqlite3.Connection, source: str) -> list:
    """Return all source_id values currently stored for a given MCP source."""
    rows = conn.execute(
        "SELECT source_id FROM mcp_servers WHERE source=? AND source_id IS NOT NULL",
        (source,),
    ).fetchall()
    return [r[0] for r in rows]


def mcp_endpoint_urls(conn: sqlite3.Connection) -> list:
    """Return all non-empty endpoint_url values across all MCP servers.

    Used as the "known" set for incremental crawls: endpoints are stable
    primary keys even when cross-source dedup drifts the source column.
    """
    rows = conn.execute(
        "SELECT endpoint_url FROM mcp_servers "
        "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''",
    ).fetchall()
    return [r[0] for r in rows]


def upsert_mcp_server(conn: sqlite3.Connection, row: dict) -> tuple:
    """Insert/update an MCP server. Dedup on (source, source_id) then slug."""
    now = int(time.time())
    # Must be read before the cross-source branch below rewrites row["source"]
    # to the first-seen owner -- that rewrite is what used to discard the fact
    # that a second directory also indexes this endpoint.
    _src = (row.get("source"), row.get("source_id"), row.get("source_url"))
    row.setdefault("created_at", now)
    row["updated_at"] = now
    row["last_seen"] = now
    row["x402_supported"] = 1 if row.get("x402_supported") else 0
    row["kind"] = "callable" if (row.get("endpoint_url") or "").strip() else "catalog"
    for k in _MCP_JSON_COLS:
        row[k] = _to_json(row.get(k))

    cur = conn.cursor()
    _sel = "SELECT * FROM mcp_servers "
    existing = None
    cross_source = False
    # 1) same source identity
    if row.get("source") and row.get("source_id"):
        existing = cur.execute(
            _sel + "WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
    # 2) same callable endpoint, regardless of source (normalised: lower + no
    #    trailing slash). Lets the same server discovered on multiple
    #    directories collapse onto one row.
    if existing is None and (row.get("endpoint_url") or "").strip():
        ep = row["endpoint_url"].strip().lower().rstrip("/")
        existing = cur.execute(
            _sel + "WHERE lower(rtrim(endpoint_url, '/'))=?",
            (ep,),
        ).fetchone()
        if existing is not None:
            cross_source = True
    # 3) same slug fallback
    if existing is None:
        existing = cur.execute(
            _sel + "WHERE slug=?",
            (row["slug"],),
        ).fetchone()

    if existing is None:
        placeholders = ",".join(["?"] * len(_MCP_COLS))
        cur.execute(
            f"INSERT INTO mcp_servers ({','.join(_MCP_COLS)}) VALUES ({placeholders})",
            [row.get(c) for c in _MCP_COLS],
        )
        new_id = cur.lastrowid
        _record_source(conn, "mcp", new_id, _src)
        return True, new_id

    row["created_at"] = existing["created_at"]
    # When the match was by endpoint across a different source, keep the
    # original row's source identity stable (so re-crawls don't ping-pong the
    # owning source) and only adopt the incoming metadata if it is at least as
    # confident as what we already stored.
    if cross_source:
        # First-source-wins: a server discovered earlier under source A keeps
        # source=A even when source B later finds the same endpoint, so the
        # owning source does not ping-pong between crawlers on every run.
        # We still enrich: adopt the higher confidence signal either way.
        new_conf = row.get("confidence") or 0.0
        old_conf = existing["confidence"] or 0.0
        row["source"] = existing["source"]
        row["source_id"] = existing["source_id"]
        row["confidence"] = max(new_conf, old_conf)
        row["slug"] = None  # keep existing slug (set below)
    # metadata-only refresh must not reset a previously probed health
    for _keep in ("health", "health_checked", "last_success_at",
                  "latency_ms", "http_status"):
        if row.get(_keep) is None:
            row[_keep] = existing[_keep]
    _restore_owner_edits(row, existing)
    # package_download_count is only provided by pulsemcp; don't let a re-crawl
    # from another source (which never carries it) wipe a stored value.
    if row.get("package_download_count") is None:
        row["package_download_count"] = existing["package_download_count"]
    # x402_supported is owned by directory.reverify_x402 (real 402 probe).
    # A plain re-crawl must never downgrade a verified flag back to 0.
    row["x402_supported"] = 1 if (row.get("x402_supported")
                                  or existing["x402_supported"]) else 0
    cols = [c for c in _MCP_COLS if c != "created_at"]
    if row.get("slug") is None:
        cols = [c for c in cols if c != "slug"]
    set_clause = ",".join(f"{c}=?" for c in cols)
    params = [row.get(c) for c in cols]
    params.append(existing["id"])
    cur.execute(f"UPDATE mcp_servers SET {set_clause} WHERE id=?", params)
    _record_source(conn, "mcp", existing["id"], _src)
    return False, int(existing["id"])


def _access_case(alias, kind):
    """SQL CASE -> access tier: 'x402' (agent self-pays per call) / 'key'
    (a human must provision an API key/OAuth first) / 'open' (no credentials).
    Encodes the directory's 'who provisions access' axis."""
    a = alias
    if kind == "mcp":
        return (
            f"CASE WHEN {a}.x402_supported=1 THEN 'x402' "
            f"WHEN {a}.auth_method IS NOT NULL "
            f"AND lower(trim({a}.auth_method)) NOT IN "
            f"('','open','none','public','public-discovery') THEN 'key' "
            f"ELSE 'open' END"
        )
    return (
        f"CASE WHEN {a}.x402_supported=1 OR ({a}.auth_schemes IS NOT NULL "
        f"AND lower({a}.auth_schemes) LIKE '%x402%') THEN 'x402' "
        f"WHEN {a}.auth_schemes IS NOT NULL AND ("
        f"lower({a}.auth_schemes) LIKE '%key%' OR lower({a}.auth_schemes) LIKE '%bearer%' "
        f"OR lower({a}.auth_schemes) LIKE '%oauth%' OR lower({a}.auth_schemes) LIKE '%token%' "
        f"OR lower({a}.auth_schemes) LIKE '%auth%' OR lower({a}.auth_schemes) LIKE '%secret%' "
        f"OR lower({a}.auth_schemes) LIKE '%credential%') THEN 'key' ELSE 'open' END"
    )


def search_mcp(conn, q=None, health=None, x402_only=False, kind=None,
               access=None, limit=50, offset=0):
    """Search standalone MCP servers. Ranks healthy + remotely callable first."""
    select_cols = ["m.*"]
    where: list[str] = []
    params: list = []
    fts_query = _expand_fts_query(q) if q else None

    if fts_query is not None:
        select_cols.append("snippet(mcp_fts, -1, '[[', ']]', '…', 12) AS match_snippet")
        sql = ["SELECT " + ", ".join(select_cols) + " FROM mcp_servers m"]
        sql.append("JOIN mcp_fts f ON f.rowid = m.id")
        where.append("mcp_fts MATCH ?")
        params.append(fts_query)
    else:
        sql = ["SELECT " + ", ".join(select_cols) + " FROM mcp_servers m"]

    if health:
        where.append("m.health=?"); params.append(health)
    if x402_only:
        where.append("m.x402_supported=1")
    if access in ("open", "key", "x402"):
        where.append(_access_case("m", "mcp") + "=?"); params.append(access)
    if kind:
        where.append("m.kind=?"); params.append(kind)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append(
        "ORDER BY "
        "CASE m.health WHEN 'ok' THEN 0 WHEN 'degraded' THEN 1 "
        "WHEN 'unknown' THEN 2 WHEN 'down' THEN 3 ELSE 4 END ASC, "
        "(m.endpoint_url IS NOT NULL AND m.endpoint_url != '') DESC, "
        "(m.confidence IS NOT NULL) DESC, "
        "m.confidence DESC, "
        "(m.github_stars IS NOT NULL) DESC, m.github_stars DESC, "
        "m.updated_at DESC"
    )
    sql.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    rows = conn.execute(" ".join(sql), params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = mcp_row_to_dict(r)
        if d.get("match_snippet") in (None, "", "…"):
            d.pop("match_snippet", None)
        out.append(d)
    return out


def get_mcp_by_slug(conn, slug):
    row = conn.execute("SELECT * FROM mcp_servers WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    out = mcp_row_to_dict(row)
    out["sources"] = listing_sources(conn, "mcp", int(out["id"]))
    return out


def mcp_stats(conn):
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS n FROM mcp_servers").fetchone()["n"]
    healthy = cur.execute(
        "SELECT COUNT(*) AS n FROM mcp_servers WHERE health='ok'"
    ).fetchone()["n"]
    remote = cur.execute(
        "SELECT COUNT(*) AS n FROM mcp_servers "
        "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''"
    ).fetchone()["n"]
    x402 = cur.execute(
        "SELECT COUNT(*) AS n FROM mcp_servers WHERE x402_supported=1"
    ).fetchone()["n"]
    conformant = cur.execute(
        "SELECT COUNT(*) AS n FROM mcp_servers WHERE conformance='pass'"
    ).fetchone()["n"]
    new_7d = cur.execute(
        "SELECT COUNT(*) AS n FROM mcp_servers "
        "WHERE created_at >= strftime('%s', 'now', '-7 days')"
    ).fetchone()["n"]

    # Distinct host domains per metric: a single domain can expose several MCP
    # services (different paths), so every stat carries both a server count and
    # a domain count. We bucket each metric's distinct domains in one pass.
    def _host(url: str) -> str:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host[4:] if host.startswith("www.") else host

    dom_all, dom_healthy, dom_conf, dom_x402 = set(), set(), set(), set()
    for r in cur.execute(
        "SELECT endpoint_url AS u, health, conformance, x402_supported "
        "FROM mcp_servers "
        "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''"
    ).fetchall():
        host = _host(r["u"])
        if not host:
            continue
        dom_all.add(host)
        if r["health"] == "ok":
            dom_healthy.add(host)
        if r["conformance"] == "pass":
            dom_conf.add(host)
        if r["x402_supported"] == 1:
            dom_x402.add(host)

    _p = cur.execute(
        "SELECT AVG(latency_p95_ms) AS a FROM mcp_servers "
        "WHERE latency_p95_ms IS NOT NULL"
    ).fetchone()
    avg_p95 = int(_p["a"]) if _p and _p["a"] is not None else None
    _acc = {r["t"]: r["n"] for r in cur.execute(
        "SELECT " + _access_case("mcp_servers", "mcp") + " AS t, COUNT(*) AS n "
        "FROM mcp_servers GROUP BY t").fetchall()}
    return {"total": total, "healthy": healthy, "remote_callable": remote,
            "conformant": conformant, "x402_capable": x402, "new_7d": new_7d,
            "access_open": _acc.get("open", 0), "access_key": _acc.get("key", 0),
            "access_x402": _acc.get("x402", 0),
            "domains": len(dom_all),
            "healthy_domains": len(dom_healthy),
            "conformant_domains": len(dom_conf),
            "x402_domains": len(dom_x402),
            "avg_p95_ms": avg_p95}


def mcp_p95_latency(conn, server_id: int, days: int = 14):
    """P95 of recent successful-probe latencies for one MCP server, or None."""
    cutoff = int(time.time()) - days * 86400
    vals = [r[0] for r in conn.execute(
        "SELECT latency_ms FROM mcp_health_history "
        "WHERE server_id=? AND checked_at>=? AND latency_ms IS NOT NULL "
        "AND status='ok'", (server_id, cutoff)).fetchall()]
    if not vals:
        return None
    vals.sort()
    idx = max(0, math.ceil(0.95 * len(vals)) - 1)
    return int(vals[idx])


def _mcp_descriptor_parts(row) -> list:
    """What the server tells an agent about itself, as (label, weight, present).

    Replaces the old `confidence` term, which was 100% re-exported from
    whichever registry happened to list the server.
    """
    def _filled(key, blanks=("", "[]", "{}")):
        v = row.get(key)
        return v is not None and str(v).strip() not in blanks

    return [
        ("Advertises its tools", 8.0, (row.get("tool_count") or 0) > 0),
        ("Tool metadata captured", 5.0, _filled("tools_json")),
        ("Source or package published", 4.0,
         _filled("source_code_url") or _filled("package_name")),
        ("Transport declared", 3.0, _filled("transport")),
    ]


def mcp_quality_score(row, p95_ms=None):
    """0..100 = availability(25) + conformance(25) + performance(30)
    + descriptor(20).

    Performance is continuous so the many healthy sub-300ms servers no longer
    all tie at full marks: full 30 at p95<=50ms, linearly down to 0 at
    >=2000ms (no p95 data => 0).
    """
    d = row if isinstance(row, dict) else dict(row)
    if p95_ms is None:
        p95_ms = d.get("latency_p95_ms")
    avail = 25 if d.get("health") == "ok" else (
        10 if d.get("health") == "degraded" else 0)
    conf = 25 if d.get("conformance") == "pass" else (
        10 if d.get("conformance") == "partial" else 0)
    if p95_ms is None:
        perf = 0.0
    elif p95_ms <= 50:
        perf = 30.0
    elif p95_ms >= 2000:
        perf = 0.0
    else:
        perf = 30.0 * (2000 - p95_ms) / (2000 - 50)
    desc = sum(w for _l, w, ok in _mcp_descriptor_parts(d) if ok)
    score = _with_owner_bonus(avail + conf + perf + desc, d)
    # A server our own scanner flagged must not sit in the top grades, whatever
    # else it scores. Held to C when suspicious, to E when malicious.
    verdict = d.get("safety_verdict")
    if verdict == "malicious":
        score = min(score, _band_ceiling("mcp", "E"))
    elif verdict == "suspicious":
        score = min(score, _band_ceiling("mcp", "C"))
    return round(score, 2)


# ---------------------------------------------------------------------------
# Unified rating: one 0-100 score + letter grade across all three resource
# types, so every page can show a "Top Rated" leaderboard and grade column.
#   MCP  -> existing quality_score (availability + conformance + performance)
#   A2A  -> availability(40) + conformance(30) + trust/confidence(30)
#   x402 -> availability(40) + trust/confidence(30) + on-chain demand(30)
# All three are availability-first and reuse signals we already probe/compute,
# mirroring the chiark-style quality model. No new crawl.
# ---------------------------------------------------------------------------
# Cut at percentiles of each type's live distribution: A ~5%, B+ ~15%, B ~35%,
# C+ ~50%, C ~70%, D ~80%. Bands are per-type because the three scorers measure
# different things -- one shared ladder left MCP unable to reach A at all while
# 62% of A2A agents sat there. "A" means top of its own kind.
_GRADE_BANDS_BY_KIND = {
    "x402": ((88, "A"), (80, "B+"), (62, "B"), (53, "C+"), (43, "C"), (12, "D")),
    "mcp": ((95, "A"), (88, "B+"), (72, "B"), (56, "C+"), (40, "C"), (20, "D")),
    "a2a": ((98, "A"), (96, "B+"), (92, "B"), (85, "C+"), (66, "C"), (60, "D")),
}
_GRADE_BANDS = _GRADE_BANDS_BY_KIND["x402"]


def grade_letter(score100, kind: str = "x402") -> str:
    s = score100 or 0
    for lo, g in _GRADE_BANDS_BY_KIND.get(kind, _GRADE_BANDS):
        if s >= lo:
            return g
    return "E"


def _a2a_card_parts(row) -> list:
    """Agent-card completeness as (label, weight, present).

    Replaces the old `confidence` term, which was a hardcoded 0.60 for 95.8%
    of agents -- a constant 18 points that separated nobody from anybody.
    """
    def _filled(key, blanks=("", "[]", "{}")):
        v = row.get(key)
        return v is not None and str(v).strip() not in blanks

    skills = row.get("skills")
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (TypeError, json.JSONDecodeError):
            skills = None
    n_skills = len(skills) if isinstance(skills, list) else 0

    return [
        ("Skills declared", 8.0, n_skills > 0),
        ("Three or more skills", 4.0, n_skills >= 3),
        ("Callable endpoint published", 6.0, _filled("endpoint_url")),
        ("Capabilities declared", 4.0, _filled("capabilities")),
        ("Protocol version identified", 3.0, _filled("protocol_version")),
        ("Auth scheme declared", 3.0, _filled("auth_schemes")),
        ("Documentation published", 2.0, _filled("documentation_url")),
    ]


def _a2a_perf(latency_ms) -> float:
    """0..20. Card completeness is a checklist most agents pass, so without a
    continuous term the top three quarters of the catalogue all tie."""
    if latency_ms is None:
        return 0.0
    if latency_ms <= 150:
        return 20.0
    if latency_ms >= 1500:
        return 0.0
    return 20.0 * (1500 - latency_ms) / (1500 - 150)


def _score_a2a(row) -> float:
    d = row if isinstance(row, dict) else dict(row)
    avail = 35 if d.get("health") == "ok" else (
        14 if d.get("health") == "degraded" else 0)
    conf = 25 if d.get("conformance") == "pass" else (
        10 if d.get("conformance") == "partial" else 0)
    card = sum(w for _l, w, ok in _a2a_card_parts(d) if ok) * (20.0 / 30.0)
    score = avail + conf + card + _a2a_perf(d.get("latency_ms"))
    return round(_with_owner_bonus(score, d), 1)


# A dead endpoint cannot be recommended today whatever it earned before, so
# its grade is held to the top of the D band.
_DOWN_SCORE_CAP = 39.9

# A claimed listing has someone accountable for it, which is worth a nudge but
# not a promotion: most of the catalogue was crawled and its operators have
# never heard of us, so this has to stay small enough to be an incentive rather
# than a penalty on everyone else. Applied before the down/safety caps.
_OWNER_BONUS = 3.0


def _with_owner_bonus(score: float, row: dict) -> float:
    if not row.get("owner_verified"):
        return score
    return min(100.0, score + _OWNER_BONUS)

# "Top-graded" everywhere means A or B+, so the counters read the band instead
# of a copy of its value that goes stale when the bands move.
def _bplus(kind: str) -> int:
    return next(lo for lo, g in _GRADE_BANDS_BY_KIND[kind] if g == "B+")


def _band_ceiling(kind: str, grade: str) -> float:
    """Highest score that still lands in `grade` -- derived from the bands so
    caps follow when the bands move."""
    bands = _GRADE_BANDS_BY_KIND[kind]
    for i, (lo, g) in enumerate(bands):
        if g == grade:
            return 100.0 if i == 0 else bands[i - 1][0] - 0.1
    return bands[-1][0] - 0.1   # "E"



# availability / payability / demand. Demand outweighs payability now that
# on-chain coverage is real: metadata completeness is table stakes, paying
# customers are the scarce signal.
_W_AVAIL, _W_PAY, _W_DEMAND = 40.0, 20.0, 40.0


def _payability_parts(row) -> list:
    """The five payability signals as (label, weight, present) triples.

    Replaces the former `confidence` term, which was 62% another directory's
    score, 11% a hardcoded 0.8 and 27% absent -- it measured which crawler
    found the listing, not the service.
    """
    pay = row.get("payment")
    if isinstance(pay, str):
        try:
            pay = json.loads(pay)
        except (TypeError, json.JSONDecodeError):
            pay = None
    if not isinstance(pay, dict):
        pay = {}
    accepts = pay.get("accepts")
    first = (accepts[0] if isinstance(accepts, list) and accepts
             and isinstance(accepts[0], dict) else {})
    return [
        ("Answers a real HTTP 402 challenge", 15.0, bool(row.get("x402_ok"))),
        ("Publishes a /.well-known/x402 descriptor", 5.0,
         bool(row.get("well_known_url"))),
        ("Payment address on record", 5.0,
         bool(pay.get("pay_to") or pay.get("payTo")
              or first.get("payTo") or first.get("pay_to"))),
        ("Settlement network declared", 3.0,
         bool(pay.get("network") or pay.get("chains") or pay.get("networks")
              or first.get("network"))),
        ("Price declared", 2.0, any(v is not None for v in (
            pay.get("max_amount_usdc"), pay.get("price_min_usd"),
            first.get("maxAmountRequired"), first.get("price")))),
    ]


def _payability(row) -> float:
    """0..1 for "can an agent actually pay this", from our own probes only."""
    return sum(w for _l, w, ok in _payability_parts(row) if ok) / 30.0


def _demand(row) -> float:
    """0..1 from on-chain USDC receipts, weighted 2:1 toward distinct payers.

    Payer count is the harder number to fake, which is why the design note
    ranks it above raw transfer count.
    """
    import math
    tx = row.get("tx_30d") or row.get("payto_tx_30d") or 0
    payers = row.get("payto_payers_30d") or 0
    f = 0.0
    if payers > 0:
        f += (2 / 3) * min(1.0, math.log10(payers + 1) / 3.0)
    if tx > 0:
        f += (1 / 3) * min(1.0, math.log10(tx + 1) / 4.0)
    return f


def _score_x402(row) -> float:
    d = row if isinstance(row, dict) else dict(row)
    avail = {"ok": 1.0, "degraded": 0.45, "unknown": 0.22}.get(d.get("health"), 0.0)
    score = _with_owner_bonus(
        _W_AVAIL * avail + _W_PAY * _payability(d) + _W_DEMAND * _demand(d), d)
    if d.get("health") == "down":
        score = min(score, _DOWN_SCORE_CAP)
    return round(score, 1)


def score_breakdown(row) -> dict:
    """Per-signal explanation of an x402 grade, for the public detail page."""
    d = row if isinstance(row, dict) else dict(row)
    health = d.get("health")
    avail_f = {"ok": 1.0, "degraded": 0.45, "unknown": 0.22}.get(health, 0.0)

    http_status = d.get("http_status")
    avail_note = {
        "ok": "Endpoint responded" + (f" (HTTP {http_status})" if http_status else ""),
        "degraded": "Reachable but returned "
                    + (f"HTTP {http_status}" if http_status else "an error"),
        "down": "Endpoint did not respond",
    }.get(health, "Not probed yet")

    pay_parts = _payability_parts(d)
    payers = d.get("payto_payers_30d")
    tx = d.get("tx_30d") or d.get("payto_tx_30d")
    if d.get("payto_checked"):
        demand_note = (f"{payers or 0} distinct payers, {tx or 0} USDC transfers "
                       "in the last 30 days")
    elif any(ok for lbl, _w, ok in pay_parts if lbl == "Payment address on record"):
        demand_note = "Payment address on record, not yet queried on-chain"
    else:
        demand_note = "No payment address on record, so demand cannot be measured"

    def _ago(ts):
        if not ts:
            return None
        mins = max(0, int((time.time() - ts) / 60))
        if mins < 60:
            return f"{mins} min ago"
        if mins < 60 * 48:
            return f"{mins // 60} h ago"
        return f"{mins // 1440} d ago"

    return {
        "score": _score_x402(d),
        "grade": grade_letter(_score_x402(d)),
        "capped": health == "down",
        "components": [
            {"name": "Availability", "max": _W_AVAIL,
             "points": round(_W_AVAIL * avail_f, 1), "note": avail_note},
            {"name": "Payability", "max": _W_PAY,
             "points": round(_W_PAY * _payability(d), 1), "note": None,
             "parts": [{"label": l, "ok": ok} for l, _w, ok in pay_parts]},
            {"name": "Demand", "max": _W_DEMAND,
             "points": round(_W_DEMAND * _demand(d), 1), "note": demand_note},
            {"name": "Owner verified", "max": _OWNER_BONUS, "bonus": True,
             "points": _OWNER_BONUS if d.get("owner_verified") else 0.0,
             "note": ("Operator proved control of this domain"
                      if d.get("owner_verified")
                      else "Nobody has claimed this listing yet")},
        ],
        "health_checked": _ago(d.get("health_checked")),
        "payto_checked": _ago(d.get("payto_checked")),
    }


def rate_row(kind: str, row) -> tuple:
    """Return (score_0_100, grade_letter) for a resource row dict/Row."""
    d = row if isinstance(row, dict) else dict(row)
    if kind == "mcp":
        s = float(d.get("quality_score") or 0)
    elif kind == "a2a":
        s = d.get("quality_score")
        s = _score_a2a(d) if s is None else float(s)
    else:  # x402
        s = _score_x402(d)
    return s, grade_letter(s, kind)


_EXPORT_SPECS = {
    "x402": ("services", row_to_dict),
    "mcp": ("mcp_servers", mcp_row_to_dict),
    "a2a": ("a2a_agents", a2a_row_to_dict),
}


def export_listings(conn: sqlite3.Connection, kind: str, after_id: int = 0,
                    limit: int = 500, with_sources: bool = True) -> list[dict]:
    """Keyset-paginated bulk export ordered by id.

    search() ranks rows with a multi-tier ORDER BY that no index can satisfy,
    so LIMIT/OFFSET degrades to O(offset) as callers page deeper. Bulk mirrors
    do not need ranking, so this walks the primary key and stays index-backed
    at any depth.
    """
    spec = _EXPORT_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind: {kind!r}")
    table, to_dict = spec
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
        (int(after_id), int(limit)),
    ).fetchall()
    out = [to_dict(r) for r in rows]
    if with_sources and out:
        attach_sources(conn, kind, out)
    return out


def record_service_paytos(conn: sqlite3.Connection, observed: dict,
                         ts: int) -> int:
    """Remember which payTo addresses each service advertised, and when.

    ``observed`` maps service_id -> [(chain, address), ...] as parsed from
    that service's own verified payment descriptor.
    """
    rowsn = 0
    for sid, keys in observed.items():
        for chain, addr in keys:
            conn.execute(
                "INSERT INTO service_paytos(service_id, chain, address, "
                "first_seen, last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(service_id, chain, address) DO UPDATE SET "
                "last_seen=excluded.last_seen",
                (sid, chain, addr, ts, ts),
            )
            rowsn += 1
    return rowsn


def rescore_services(conn) -> int:
    """Recompute and store services.quality_score.

    The homepage counter and the leaderboard read this column instead of
    re-deriving the formula in SQL, which is how the two used to drift apart.
    """
    rows = conn.execute(
        "SELECT id, health, x402_ok, well_known_url, payment, tx_30d, "
        "payto_tx_30d, payto_payers_30d, owner_verified FROM services").fetchall()
    updates = [(_score_x402(dict(r)), r["id"]) for r in rows]
    for start in range(0, len(updates), 1000):
        conn.executemany("UPDATE services SET quality_score=? WHERE id=?",
                         updates[start:start + 1000])
    return len(updates)


def rescore_a2a(conn) -> int:
    rows = conn.execute("SELECT * FROM a2a_agents").fetchall()
    ups = [(_score_a2a(dict(r)), r["id"]) for r in rows]
    for i in range(0, len(ups), 1000):
        conn.executemany("UPDATE a2a_agents SET quality_score=? WHERE id=?",
                         ups[i:i + 1000])
    return len(ups)


def rescore_mcp(conn) -> int:
    rows = conn.execute(
        "SELECT id, health, conformance, latency_p95_ms, tool_count, tools_json, "
        "source_code_url, package_name, transport, safety_verdict, "
        "owner_verified FROM mcp_servers").fetchall()
    ups = [(mcp_quality_score(dict(r)), r["id"]) for r in rows]
    for i in range(0, len(ups), 1000):
        conn.executemany("UPDATE mcp_servers SET quality_score=? WHERE id=?",
                         ups[i:i + 1000])
    return len(ups)


def attach_ratings(kind: str, rows: list) -> list:
    """Annotate each row dict in-place with score (0-10, 1dp) + grade letter."""
    for d in rows:
        s100, g = rate_row(kind, d)
        d["score"] = round(s100 / 10.0, 2)
        d["score100"] = s100
        d["grade"] = g
    return rows


def grade_mix_all(conn) -> dict:
    """A/B+ share per type, read from each table's stored quality_score so the
    counter cannot drift away from the Python scorer."""
    cur = conn.cursor()
    out = {}
    # MCP: quality_score already stored. A/B+ == score >= 70.
    r = cur.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN quality_score>=? THEN 1 ELSE 0 END) ap "
        "FROM mcp_servers WHERE health='ok'", (_bplus("mcp"),)).fetchone()
    n, ap = (r["n"] or 0), (r["ap"] or 0)
    out["mcp"] = {"a_plus": ap, "share": round(100 * ap / n) if n else 0}
    r = cur.execute(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN COALESCE(quality_score,0)>=? THEN 1 ELSE 0 END) ap "
        "FROM a2a_agents WHERE health='ok'", (_bplus("a2a"),)).fetchone()
    n, ap = (r["n"] or 0), (r["ap"] or 0)
    out["a2a"] = {"a_plus": ap, "share": round(100 * ap / n) if n else 0}
    r = cur.execute(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN COALESCE(quality_score,0)>=? THEN 1 ELSE 0 END) ap "
        "FROM services WHERE health='ok'", (_bplus("x402"),)).fetchone()
    n, ap = (r["n"] or 0), (r["ap"] or 0)
    out["x402"] = {"a_plus": ap, "share": round(100 * ap / n) if n else 0}
    return out


def top_rated(conn, kind: str, limit: int = 10) -> list:
    """Highest-rated healthy resources of a type, annotated with score+grade."""
    if kind == "mcp":
        raw = conn.execute(
            "SELECT * FROM mcp_servers WHERE health='ok' "
            "ORDER BY quality_score DESC, confidence DESC, updated_at DESC "
            "LIMIT ?", (limit,)).fetchall()
        rows = [mcp_row_to_dict(r) for r in raw]
        return attach_ratings("mcp", rows)
    if kind == "a2a":
        raw = conn.execute(
            "SELECT * FROM a2a_agents WHERE health='ok' ORDER BY "
            "COALESCE(quality_score,0) DESC, updated_at DESC LIMIT ?",
            (limit,)).fetchall()
        return attach_ratings("a2a", [a2a_row_to_dict(r) for r in raw])
    # x402
    raw = conn.execute(
        "SELECT * FROM services WHERE health='ok' ORDER BY "
        "COALESCE(quality_score,0) DESC, COALESCE(payto_payers_30d,0) DESC, "
        "COALESCE(tx_30d,0) DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()
    return attach_ratings("x402", [dict(r) for r in raw])


# Curated topic keywords used to bucket MCP servers (which carry no tags).
# Each keyword is also a working full-text search term, so a category link
# (/mcp?q=<keyword>) returns exactly the servers counted here.
_MCP_TOPIC_KEYWORDS = (
    "search", "web", "data", "database", "finance", "payment", "crypto",
    "wallet", "trading", "blockchain", "weather", "maps", "github", "code",
    "deploy", "ai", "llm", "image", "video", "audio", "pdf", "document",
    "email", "calendar", "news", "social", "api", "automation", "security",
    "analytics", "translation", "knowledge",
)


def mcp_categories(conn, min_count=3):
    """Bucket MCP servers by curated topic keyword (name + description).

    Returns [{category, count}] sorted by count desc. Each `category` is a
    searchable keyword: link to /mcp?q=<category> to see the matching servers.
    """
    rows = conn.execute(
        "SELECT name, description FROM mcp_servers"
    ).fetchall()
    counts = {k: 0 for k in _MCP_TOPIC_KEYWORDS}
    for r in rows:
        text = ((r["name"] or "") + " " + (r["description"] or "")).lower()
        for k in _MCP_TOPIC_KEYWORDS:
            if k in text:
                counts[k] += 1
    out = [{"category": k, "count": c} for k, c in counts.items() if c >= min_count]
    out.sort(key=lambda d: (-d["count"], d["category"]))
    return out


def a2a_categories(conn, min_count=1):
    """Aggregate A2A agents by skill tag.

    Returns [{category, count}] where count is the number of agents that
    expose at least one skill carrying that tag. Link to /a2a?q=<category>.
    """
    rows = conn.execute(
        "SELECT skills FROM a2a_agents WHERE skills IS NOT NULL AND skills != ''"
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            skills = json.loads(r["skills"])
        except (TypeError, json.JSONDecodeError):
            continue
        seen: set[str] = set()
        for s in (skills or []):
            if not isinstance(s, dict):
                continue
            for t in (s.get("tags") or []):
                t = str(t).strip().lower()
                if t:
                    seen.add(t)
        for t in seen:
            counts[t] = counts.get(t, 0) + 1
    out = [{"category": t, "count": c} for t, c in counts.items() if c >= min_count]
    out.sort(key=lambda d: (-d["count"], d["category"]))
    return out


SHARED_HOST_MIN_PATHS = 20


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        u = urlparse(url or "")
    except ValueError:
        return "", ""
    host = (u.hostname or "").lower()
    if not host:
        return "", ""
    return host, (u.path or "/")


def refresh_shared_hosts(
    conn: sqlite3.Connection, min_paths: int = SHARED_HOST_MIN_PATHS
) -> int:
    """Rebuild the list of hosts that multiplex unrelated operators.

    Whole-host domain verification is refused on these: the listing author
    cannot change what the gateway returns, and the platform that can is not
    the author.
    """
    paths: dict[str, set] = {}
    listings: dict[str, int] = {}
    for sql in (
        "SELECT url FROM services WHERE url IS NOT NULL AND url != ''",
        "SELECT endpoint_url FROM mcp_servers "
        "WHERE endpoint_url IS NOT NULL AND endpoint_url != ''",
        "SELECT COALESCE(endpoint_url, card_url) FROM a2a_agents "
        "WHERE COALESCE(endpoint_url, card_url) IS NOT NULL",
    ):
        for row in conn.execute(sql):
            host, path = _host_and_path(row[0])
            if not host:
                continue
            paths.setdefault(host, set()).add(path)
            listings[host] = listings.get(host, 0) + 1

    # Path count only finds candidates. Whether the paths belong to unrelated
    # authors is a judgement call, so a human verdict outlives every refresh.
    verdicts = {
        r["host"]: r["verdict"]
        for r in conn.execute("SELECT host, verdict FROM shared_hosts")
    }
    now = int(time.time())
    rows = [
        (h, len(p), listings[h], verdicts.get(h, "unreviewed"), now)
        for h, p in paths.items()
        if len(p) >= min_paths
    ]
    conn.execute("DELETE FROM shared_hosts")
    conn.executemany(
        "INSERT INTO shared_hosts(host, path_count, listing_count, verdict, "
        "updated_at) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def is_shared_host(conn: sqlite3.Connection, host: str) -> bool:
    """Whether whole-host verification must be refused for this host.

    Unreviewed candidates count as shared: over-refusing costs an email,
    under-refusing hands one party control over other people's listings.
    """
    if not host:
        return False
    row = conn.execute(
        "SELECT verdict FROM shared_hosts WHERE host = ?", (host.lower(),)
    ).fetchone()
    return bool(row) and row["verdict"] != "single_operator"


def sync_owner_verified(conn: sqlite3.Connection) -> dict[str, int]:
    """Project verified domain claims onto the three listing tables.

    Stored rather than joined so the scorers keep taking a plain row. Rebuilt
    wholesale on every run: a partial update is how a projection starts lying.
    Rows whose flag moved are rescored here too -- a badge that appears hours
    before the score it earns reads like a bug.
    """
    hosts = {
        r["host"]
        for r in conn.execute(
            "SELECT host FROM domain_ownership WHERE status='verified'")
    }
    out: dict[str, int] = {}
    for table, url_expr, score_fn in (
        ("services", "url", _score_x402),
        ("mcp_servers", "endpoint_url", mcp_quality_score),
        ("a2a_agents", "COALESCE(endpoint_url, card_url)", _score_a2a),
    ):
        marked: list[int] = []
        changed: list[int] = []
        for r in conn.execute(
                "SELECT id, %s AS _u, COALESCE(owner_verified,0) AS _v FROM %s"
                % (url_expr, table)):
            host, _ = _host_and_path(r["_u"] or "")
            flag = 1 if host in hosts else 0
            if flag:
                marked.append(r["id"])
            if flag != r["_v"]:
                changed.append(r["id"])

        conn.execute(
            "UPDATE %s SET owner_verified=0 WHERE COALESCE(owner_verified,0)=1"
            % table)
        for i in range(0, len(marked), 500):
            chunk = marked[i:i + 500]
            conn.execute(
                "UPDATE %s SET owner_verified=1 WHERE id IN (%s)"
                % (table, ",".join("?" * len(chunk))), chunk)

        for i in range(0, len(changed), 500):
            chunk = changed[i:i + 500]
            rows = conn.execute(
                "SELECT * FROM %s WHERE id IN (%s)"
                % (table, ",".join("?" * len(chunk))), chunk).fetchall()
            conn.executemany(
                "UPDATE %s SET quality_score=? WHERE id=?" % table,
                [(score_fn(dict(r)), r["id"]) for r in rows])

        out[table] = len(marked)
    conn.commit()
    return out

def upsert_user(conn: sqlite3.Connection, provider: str, provider_uid: str,
                login: str | None = None, avatar_url: str | None = None,
                email: str | None = None, email_verified: bool = False) -> int:
    """Insert/refresh an OAuth identity, keyed on the provider's stable id.

    Not keyed on email: GitHub handles and addresses both change, the numeric
    id does not. `email` is contact information only -- it never authorises an
    edit.
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT id FROM users WHERE provider=? AND provider_uid=?",
        (provider, provider_uid)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users(provider, provider_uid, login, avatar_url, "
            "email, email_verified, status, created_at, last_login_at) "
            "VALUES (?,?,?,?,?,?,'active',?,?)",
            (provider, provider_uid, login, avatar_url, email,
             1 if email_verified else 0, now, now))
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE users SET login=?, avatar_url=?, email=COALESCE(?, email), "
        "email_verified=?, last_login_at=? WHERE id=?",
        (login, avatar_url, email, 1 if email_verified else 0, now, row["id"]))
    conn.commit()
    return int(row["id"])


_EDITABLE_FIELDS = {
    "x402": ("name", "description", "category", "url", "mcp_url"),
    "mcp": ("name", "description", "homepage_url", "endpoint_url"),
    "a2a": ("name", "description", "homepage_url", "documentation_url",
            "provider_name", "endpoint_url", "card_url"),
}

# 改这些字段等于改这条 listing 指向谁，只接受该账号已验证过的 host。
_ENDPOINT_FIELDS = {
    "x402": ("url", "mcp_url"),
    "mcp": ("endpoint_url",),
    "a2a": ("endpoint_url", "card_url"),
}

# endpoint 一变，实测结论就不再适用于新地址，全部清空等待重测。
_MEASURED_RESET = {
    "x402": ("health", "health_checked", "latency_ms", "http_status", "x402_ok",
             "quality_score", "resource_count", "resource_samples", "tx_30d",
             "payto_tx_30d", "payto_payers_30d", "payto_checked", "down_since",
             "last_seen", "latency_ms"),
    "mcp": ("health", "health_checked", "latency_ms", "http_status", "tool_count",
            "tools_json", "tools_text", "latency_p95_ms", "quality_score",
            "conformance", "down_since", "last_success_at", "safety_verdict",
            "safety_score", "safety_reasons"),
    "a2a": ("health", "health_checked", "latency_ms", "conformance",
            "quality_score", "down_since", "last_success_at"),
}
_KIND_TABLE = {"x402": "services", "mcp": "mcp_servers", "a2a": "a2a_agents"}


def _restore_owner_edits(row: dict, existing) -> None:
    """Keep fields an owner corrected by hand.

    Upstream metadata is exactly what they were fixing, so a re-crawl must not
    undo it. Measured columns are untouched by this -- owners cannot edit them.
    """
    try:
        fields = json.loads(existing["owner_edited"] or "[]")
    except Exception:
        return
    for field in fields:
        row[field] = existing[field]


def apply_listing_edits(conn: sqlite3.Connection, kind: str, listing_id: int,
                        user_id: int, ownership_id: int,
                        changes: dict,
                        verified_hosts=None) -> tuple[list[str], list[str]]:
    """Write owner edits and record each one in the public audit trail.

    Returns (applied, rejected_messages). An endpoint change is only accepted on
    a host this account already verified, and it wipes the measured columns so a
    listing cannot carry its reputation over to a different address.
    """
    table = _KIND_TABLE[kind]
    allowed = _EDITABLE_FIELDS[kind]
    endpoints = _ENDPOINT_FIELDS.get(kind, ())
    hosts = {str(h).lower() for h in (verified_hosts or ())}
    current = conn.execute("SELECT * FROM %s WHERE id=?" % table,
                           (listing_id,)).fetchone()
    if current is None:
        return [], []
    now = int(time.time())
    applied: list[str] = []
    rejected: list[str] = []
    endpoint_changed = False
    for field, value in changes.items():
        if field not in allowed:
            continue
        new = (value or "").strip() or None
        old = current[field]
        if (old or None) == new:
            continue
        if field in endpoints:
            if new is None:
                rejected.append("%s cannot be empty" % field)
                continue
            host, _ = _host_and_path(new)
            if not host:
                rejected.append("%s is not a valid URL" % field)
                continue
            if host not in hosts:
                rejected.append(
                    "%s: you have not verified control of %s" % (field, host))
                continue
            endpoint_changed = True
        conn.execute("UPDATE %s SET %s=? WHERE id=?" % (table, field),
                     (new, listing_id))
        conn.execute(
            "INSERT INTO listing_edits(kind, listing_id, user_id, ownership_id, "
            "field, old_value, new_value, applied_at) VALUES (?,?,?,?,?,?,?,?)",
            (kind, listing_id, user_id, ownership_id, field, old, new, now))
        applied.append(field)
    if endpoint_changed:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        resets = sorted({c for c in _MEASURED_RESET.get(kind, ()) if c in cols})
        if resets:
            conn.execute(
                "UPDATE %s SET %s WHERE id=?"
                % (table, ", ".join("%s=NULL" % c for c in resets)),
                (listing_id,))
    if applied:
        try:
            marked = set(json.loads(current["owner_edited"] or "[]"))
        except Exception:
            marked = set()
        marked.update(applied)
        conn.execute("UPDATE %s SET owner_edited=? WHERE id=?" % table,
                     (json.dumps(sorted(marked)), listing_id))
    # 不在此提交：调用方的 db.writer() 负责，内部提交会让调用方无法组合事务。
    return applied, rejected


def listing_edit_history(conn: sqlite3.Connection, kind: str,
                         listing_id: int, limit: int = 50) -> list[dict]:
    """Public record of who changed what. Transparency is the anti-abuse."""
    return [dict(r) for r in conn.execute(
        "SELECT e.field, e.old_value, e.new_value, e.applied_at, u.login "
        "FROM listing_edits e LEFT JOIN users u ON u.id = e.user_id "
        "WHERE e.kind=? AND e.listing_id=? "
        "ORDER BY e.applied_at DESC LIMIT ?",
        (kind, listing_id, limit))]
