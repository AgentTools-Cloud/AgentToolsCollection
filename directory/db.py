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

CREATE TABLE IF NOT EXISTS rate_limits (
    key          TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_updated ON rate_limits(updated_at);

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
  github_stars      INTEGER,
  tags              TEXT,
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
  name, description, tags,
  content='mcp_servers', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS mcp_ai AFTER INSERT ON mcp_servers BEGIN
  INSERT INTO mcp_fts(rowid, name, description, tags)
    VALUES (new.id, new.name, new.description, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS mcp_ad AFTER DELETE ON mcp_servers BEGIN
  INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags)
    VALUES('delete', old.id, old.name, old.description, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS mcp_au AFTER UPDATE ON mcp_servers BEGIN
  INSERT INTO mcp_fts(mcp_fts, rowid, name, description, tags)
    VALUES('delete', old.id, old.name, old.description, old.tags);
  INSERT INTO mcp_fts(rowid, name, description, tags)
    VALUES (new.id, new.name, new.description, new.tags);
END;
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
            "ALTER TABLE mcp_servers ADD COLUMN conformance TEXT",
            "ALTER TABLE mcp_servers ADD COLUMN tool_count INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN latency_p95_ms INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN quality_score INTEGER",
            "ALTER TABLE a2a_agents ADD COLUMN conformance TEXT",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
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
    row["resource_samples"] = _to_json(row.get("resource_samples"))
    row["payment"] = _to_json(row.get("payment"))
    row["call_info"] = _to_json(row.get("call_info"))
    row["quality"] = _to_json(row.get("quality"))

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


def _expand_fts_query(q: str) -> str | None:
    """Turn a free-text query into an FTS5 MATCH expression with synonyms.

    Strategy: lowercase, split on whitespace, drop FTS-special chars from
    each token; per token look up the synonym group and emit `(a OR b OR c)`
    (the group always contains the original token). Groups are joined with
    space which is FTS5 AND. Single bare tokens get a trailing `*` for
    prefix match. Returns None if nothing usable remains.
    """
    if not q:
        return None
    # Remove FTS operator chars and quoting; keep CJK + word chars + space.
    cleaned = []
    for ch in q.lower():
        if ch.isalnum() or ch == " " or "\u4e00" <= ch <= "\u9fff":
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    tokens = [t for t in "".join(cleaned).split() if t]
    if not tokens:
        return None
    groups: list[str] = []
    for t in tokens:
        syns = _SYNONYM_MAP.get(t)
        if syns:
            # quote each synonym to keep CJK / multiword safe inside FTS5
            quoted = [f'"{s}"' for s in syns]
            groups.append("(" + " OR ".join(quoted) + ")")
        elif len(tokens) == 1:
            # single bare token → prefix match
            groups.append(f"{t}*")
        else:
            groups.append(f'"{t}"')
    # FTS5 only allows implicit AND between bare tokens; once any group is
    # parenthesized or quoted, the parser requires explicit AND.
    return " AND ".join(groups)


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


def find_service_by_url(conn, url: str) -> dict | None:
    """Best-effort lookup: any services row whose url or mcp_url shares the
    same canonical origin as `url`. Returns the first match or None.
    """
    origin = _canonical_origin(url)
    if not origin:
        return None
    like = origin + "%"
    row = conn.execute(
        "SELECT * FROM services WHERE url LIKE ? OR mcp_url LIKE ? LIMIT 1",
        (like, like),
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
    return d


def upsert_a2a_agent(conn: sqlite3.Connection, row: dict) -> tuple:
    """Insert/update an A2A agent. Dedup on (source, source_id) then slug.

    `skills` may be a list of dicts; we also derive `skill_names` (a flat
    text blob) so FTS can match skill ids/names/tags without parsing JSON.
    """
    now = int(time.time())
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
    if row.get("source") and row.get("source_id"):
        existing = cur.execute(
            "SELECT id, created_at, health, health_checked, last_success_at "
            "FROM a2a_agents WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
    if existing is None:
        existing = cur.execute(
            "SELECT id, created_at, health, health_checked, last_success_at "
            "FROM a2a_agents WHERE slug=?",
            (row["slug"],),
        ).fetchone()

    if existing is None:
        placeholders = ",".join(["?"] * len(_A2A_COLS))
        cur.execute(
            f"INSERT INTO a2a_agents ({','.join(_A2A_COLS)}) VALUES ({placeholders})",
            [row.get(c) for c in _A2A_COLS],
        )
        return True, cur.lastrowid

    row["created_at"] = existing["created_at"]
    # metadata-only refresh must not reset a previously probed health
    if row.get("health") is None:
        row["health"] = existing["health"]
    if row.get("health_checked") is None:
        row["health_checked"] = existing["health_checked"]
    if row.get("last_success_at") is None:
        row["last_success_at"] = existing["last_success_at"]
    set_clause = ",".join(f"{c}=?" for c in _A2A_COLS if c != "created_at")
    params = [row.get(c) for c in _A2A_COLS if c != "created_at"]
    params.append(existing["id"])
    cur.execute(f"UPDATE a2a_agents SET {set_clause} WHERE id=?", params)
    return False, int(existing["id"])


def search_a2a(conn, q=None, health=None, x402_only=False,
               limit=50, offset=0):
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
    return a2a_row_to_dict(row) if row else None


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
    return {"total": total, "healthy": healthy, "x402_capable": x402,
            "conformant": conformant}


# ---------------------------------------------------------------------------
# MCP servers: standalone directory (PulseMCP / official registry import).
# Kept separate from `services` so non-x402 MCP servers never pollute the
# x402 service search; the unified resource search (P1) unions them at read
# time alongside x402 services that also expose an mcp_url.
# ---------------------------------------------------------------------------

_MCP_COLS = [
    "slug", "name", "description", "homepage_url", "endpoint_url",
    "transport", "auth_method", "cost_hint", "source_code_url",
    "package_registry", "package_name", "github_stars", "tags",
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
    row.setdefault("created_at", now)
    row["updated_at"] = now
    row["last_seen"] = now
    row["x402_supported"] = 1 if row.get("x402_supported") else 0
    row["kind"] = "callable" if (row.get("endpoint_url") or "").strip() else "catalog"
    for k in _MCP_JSON_COLS:
        row[k] = _to_json(row.get(k))

    cur = conn.cursor()
    _sel = ("SELECT id, created_at, health, health_checked, last_success_at, "
            "source, source_id, confidence, x402_supported "
            "FROM mcp_servers ")
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
        return True, cur.lastrowid

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
    if row.get("health") is None:
        row["health"] = existing["health"]
    if row.get("health_checked") is None:
        row["health_checked"] = existing["health_checked"]
    if row.get("last_success_at") is None:
        row["last_success_at"] = existing["last_success_at"]
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
    return False, int(existing["id"])


def search_mcp(conn, q=None, health=None, x402_only=False, kind=None, limit=50, offset=0):
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
    return mcp_row_to_dict(row) if row else None


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
    _p = cur.execute(
        "SELECT AVG(latency_p95_ms) AS a FROM mcp_servers "
        "WHERE latency_p95_ms IS NOT NULL"
    ).fetchone()
    avg_p95 = int(_p["a"]) if _p and _p["a"] is not None else None
    return {"total": total, "healthy": healthy, "remote_callable": remote,
            "x402_capable": x402, "conformant": conformant, "avg_p95_ms": avg_p95}


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


def mcp_quality_score(health, conformance, p95_ms):
    """0..100 = availability(30) + conformance(30) + performance(40)."""
    avail = 30 if health == "ok" else (12 if health == "degraded" else 0)
    conf = 30 if conformance == "pass" else (12 if conformance == "partial" else 0)
    if p95_ms is None:
        perf = 0
    elif p95_ms <= 300:
        perf = 40
    elif p95_ms >= 3000:
        perf = 0
    else:
        perf = int(round(40 * (3000 - p95_ms) / (3000 - 300)))
    return avail + conf + perf


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

