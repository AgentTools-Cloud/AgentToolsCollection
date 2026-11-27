"""Aggregate search-term counts, for demand statistics.

Only the normalised term, the protocol searched, the channel (web, api, mcp),
the UTC day and two counters are kept: how often it was searched and how often
it returned nothing. No IP address, user, key, session or timestamp finer than
a day is recorded. Terms that look personal or unique (e-mail, URL, wallet
address, long numbers) are dropped before counting.

So that one script repeating a term cannot pass for demand, each search also
yields an anonymous searcher tag, and only (day, term, tag) is kept:

- browsers and MCP clients: a keyed hash of IP address and user agent under a
  random key that is replaced every UTC day. Once the day's key is replaced,
  a tag can no longer be matched to any client, and tags of different days
  cannot be linked. Such a searcher counts once per term per day.
- scripts (non-browser user agents on the site and API): a keyed hash of the
  user agent alone under a long-lived key, so a script running every day
  counts once per window, not once per day. No IP address is involved.

Search-engine crawlers following links are not counted at all.

Public output only shows terms with at least `PUBLIC_MIN` searches by at least
`MIN_SEARCHERS` searchers in the window, ranked by searchers.

Counts are buffered in-process and flushed to a small separate SQLite file, so
a search never waits on the main database writer.
"""
from __future__ import annotations

import atexit
import fcntl
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import Counter

log = logging.getLogger(__name__)

PUBLIC_MIN = 5
MIN_SEARCHERS = 3
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
CREATE TABLE IF NOT EXISTS search_searchers (
    day TEXT NOT NULL,
    term TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (day, term, tag)
) WITHOUT ROWID;
"""

# Search-engine and AI crawlers that follow search links found on our pages.
# They are not asking for anything, so their hits are not counted.
CRAWLER = re.compile(
    r"bot\b|bot/|crawler|spider|slurp|bingpreview|facebookexternalhit|"
    r"meta-externalagent|epochal-search|barkrowler|petalsearch|yandex|baiduspider",
    re.I)
# A user agent that names a browser engine and no script or tool.
_BROWSER = re.compile(r"^Mozilla/5\.0 \(")
_SCRIPT = re.compile(
    r"python|curl|wget|go-http|node|axios|httpx|aiohttp|java|okhttp|undici|deno|"
    r"headless|scrapy|libwww|\+https?://|compatible;", re.I)

_DROP = re.compile(
    r"@|https?:|://|www\.|/|\\"                      # e-mail, URL, path
    r"|0x[0-9a-f]{6,}|\d{4,}|[a-z0-9]{25,}"            # address, long number, opaque id
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"                    # IPv4
    r"|\beyj[a-z0-9_-]{8,}"                             # JWT
    r"|\d{3}[-. ]\d{3,4}[-. ]\d{3,4}"                  # phone number
    r"|\b(?:sk|pk|rk|ak|key|tok|token|ghp|gho|ghs|xox[a-z]?)[-_](?=[a-z0-9_-]*\d)[a-z0-9_-]{4,}"  # API key
    r"|\b(?=[a-z_-]*\d)(?=[\d_-]*[a-z])[a-z0-9_-]{24,}\b"  # long mixed token
)
_EDGE = re.compile(r"^[^\w]+|[^\w+#.]+$")
_SPACE = re.compile(r"\s+")

_lock = threading.Lock()
_pending: Counter = Counter()
_pending_tags: set = set()
_last_flush = time.monotonic()
_salt_cache: tuple[str, bytes] | None = None
_salt_lock = threading.Lock()


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
    t = _SPACE.sub(" ", unicodedata.normalize("NFKC", str(q)).strip().lower())
    t = _EDGE.sub("", t)
    if len(t) < 2 or len(t) > 60 or len(t.split()) > 6:
        return None
    if _DROP.search(t):
        return None
    return t


def _salt(day: str) -> bytes:
    """The day's random key, shared by all app processes through a small file
    next to the database. A new day overwrites the old key."""
    global _salt_cache
    cached = _salt_cache
    if cached and cached[0] == day:
        return cached[1]
    with _salt_lock:
        path = db_path() + ".key"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            parts = f.read().split()
            if len(parts) == 2 and parts[0] == day:
                salt = bytes.fromhex(parts[1])
            elif len(parts) == 2 and parts[0] > day:
                salt = secrets.token_bytes(32)  # clock behind the file: never reuse a key
            else:
                salt = secrets.token_bytes(32)
                f.seek(0)
                f.truncate()
                f.write(f"{day} {salt.hex()}\n")
                f.flush()
                os.fsync(f.fileno())
        _salt_cache = (day, salt)
        return salt


def client_key(channel: str, ip: str | None, ua: str | None) -> str | None:
    """What identifies one searcher before hashing, or None for a crawler.

    Browsers and MCP clients: IP and user agent. Scripts: the user agent only,
    because one script often runs from many addresses."""
    ua = (ua or "").strip()[:300]
    if channel != "mcp" and CRAWLER.search(ua):
        return None
    if channel == "mcp" or (_BROWSER.match(ua) and not _SCRIPT.search(ua)):
        return f"ip\n{ip or '-'}\n{ua}"
    return f"ua\n{ua}"


_script_key_cache: bytes | None = None


def _script_key() -> bytes:
    """Long-lived key for script tags (user agent only, no IP)."""
    global _script_key_cache
    if _script_key_cache is not None:
        return _script_key_cache
    with _salt_lock:
        path = db_path() + ".script-key"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            raw = f.read().strip()
            if len(raw) == 64:
                key = bytes.fromhex(raw)
            else:
                key = secrets.token_bytes(32)
                f.seek(0)
                f.truncate()
                f.write(key.hex() + "\n")
                f.flush()
                os.fsync(f.fileno())
        _script_key_cache = key
        return key


def tag_for(day: str, key: str, salt: bytes | None = None) -> str:
    """'s' + hash for scripts (stable across days), 'd' + hash otherwise
    (`salt` overrides the day's key; used for back-filling past days)."""
    data = key.encode("utf-8", "replace")
    if key.startswith("ua\n"):
        return "s" + hmac.new(_script_key(), data, hashlib.sha256).hexdigest()[:16]
    return "d" + hmac.new(salt or _salt(day), data, hashlib.sha256).hexdigest()[:16]


def request_client(request) -> tuple[str | None, str | None]:
    """(ip, user agent) of a Starlette request."""
    from .limits import client_ip_from_request
    try:
        return client_ip_from_request(request), request.headers.get("user-agent")
    except Exception:  # noqa: BLE001
        return None, None


def record(kind: str, q: str | None, results: int, channel: str,
           client: tuple[str | None, str | None] | None = None) -> None:
    """Count one search. `client` is (ip, user agent); neither is stored.
    Never raises."""
    global _last_flush
    try:
        term = normalize(q)
        if term is None or channel not in CHANNELS:
            return
        key = None
        if client is not None:
            key = client_key(channel, *client)
            if key is None:
                return  # crawler following a link
        kind = kind if kind in KINDS else "all"
        day = time.strftime("%Y-%m-%d", time.gmtime())
        tag = tag_for(day, key) if key is not None else None
        with _lock:
            _pending[(day, kind, channel, term, "n")] += 1
            if not results:
                _pending[(day, kind, channel, term, "zero")] += 1
            if tag is not None:
                _pending_tags.add((day, term, tag))
            due = (len(_pending) >= FLUSH_PENDING
                   or time.monotonic() - _last_flush >= FLUSH_SECONDS)
            if due:  # one flush thread per window, not one per request
                _last_flush = time.monotonic()
        if due:
            # Off the request thread (and the event loop for MCP tools).
            threading.Thread(target=flush, name="search-terms-flush", daemon=True).start()
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


_flush_lock = threading.Lock()


def flush() -> None:
    global _last_flush
    with _lock:
        items = dict(_pending)
        tags = list(_pending_tags)
        _pending.clear()
        _pending_tags.clear()
        _last_flush = time.monotonic()
    if not items and not tags:
        return
    with _flush_lock:
        _write(items, tags)


def _write(items: dict, tags: list | None = None) -> None:
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
            if tags:
                c.executemany("INSERT OR IGNORE INTO search_searchers(day, term, tag) VALUES (?,?,?)", tags)
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
            c.execute("DELETE FROM search_searchers WHERE day < ?", (cutoff,))
        c.close()
        return n
    except Exception:  # noqa: BLE001
        log.exception("search term prune failed")
        return 0


def summary(start_day: str, end_day: str, limit: int = 20, min_n: int = PUBLIC_MIN,
            min_searchers: int = MIN_SEARCHERS) -> dict:
    """Public aggregate for [start_day, end_day]: top terms and terms that
    mostly found nothing, ranked by searchers (distinct searchers per day,
    summed over the days). Terms below `min_n` searches or `min_searchers`
    searchers are never returned."""
    out = {"from": start_day, "to": end_day, "min_searches": min_n, "min_searchers": min_searchers,
           "searches": 0, "searchers": 0, "distinct_terms": 0, "top": [], "unmet": [],
           "by_kind": {}, "by_channel": {}}
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
        has_tags = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_searchers'"
                             ).fetchone() is not None
        if not has_tags:
            return out
        # Scripts ('s' tags) are counted once per window, others once per day.
        k_expr = ("COUNT(DISTINCT CASE WHEN tag LIKE 's%' THEN tag END) "
                  "+ SUM(CASE WHEN tag LIKE 's%' THEN 0 ELSE 1 END)")
        out["searchers"] = int(c.execute(
            f"SELECT (SELECT COUNT(DISTINCT tag) FROM search_searchers {where} AND tag LIKE 's%') + "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT day, tag FROM search_searchers {where} AND tag NOT LIKE 's%'))",
            (*args, *args)).fetchone()[0] or 0)
        rows = c.execute(
            f"WITH s AS (SELECT term, {k_expr} k FROM search_searchers {where} GROUP BY term), "
            f"t AS (SELECT term, SUM(n) n, SUM(zero) z FROM search_terms {where} GROUP BY term) "
            "SELECT t.term, t.n, t.z, s.k FROM t JOIN s ON s.term = t.term "
            "WHERE t.n >= ? AND s.k >= ? ORDER BY s.k DESC, t.n DESC, t.term LIMIT 500",
            (*args, *args, min_n, min_searchers)).fetchall()
    except sqlite3.Error:
        return out
    finally:
        c.close()
    items = [{"term": t, "searchers": int(k), "searches": int(n),
              "no_results_pct": round(100.0 * z / n, 1) if n else 0.0}
             for t, n, z, k in rows]
    out["top"] = items[:limit]
    out["unmet"] = [i for i in items if i["no_results_pct"] >= 50][:limit]
    return out


# ---------------------------------------------------------------------------
# One-off: searcher tags for days before tagging existed, from the web
# server's access log (which already holds IP and user agent).
# ---------------------------------------------------------------------------

_LOG_LINE = re.compile(r'^(\S+) \S+ \S+ \[(\d+)/(\w+)/(\d+):[^\]]+\] "GET (\S+) [^"]*" (\d{3}) .*?ua="([^"]*)"')
_MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_WEB_PATHS = {"/x402", "/mcp", "/mcp/code", "/a2a"}
_API_PATHS = {"/api/v1/search", "/api/v1/a2a/search", "/api/v1/mcp/search", "/api/v1/resources/search"}


def tags_from_log(lines, start_day: str, end_day: str) -> set:
    """(day, term, tag) for searches in access-log `lines`, using the same
    rules as `record`. Past days get a throw-away random key; the current day
    uses the live key so later live searches are not counted twice."""
    from urllib.parse import parse_qs, urlsplit
    today = time.strftime("%Y-%m-%d", time.gmtime())
    keys: dict = {}
    out: set = set()
    for line in lines:
        m = _LOG_LINE.match(line)
        if not m:
            continue
        ip, d, mon, y, target, status, ua = m.groups()
        if status != "200" or mon not in _MON:
            continue
        day = f"{y}-{_MON[mon]:02d}-{int(d):02d}"
        if not (start_day <= day <= end_day):
            continue
        u = urlsplit(target)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)
        if path in _WEB_PATHS:
            if "ref" in qs:
                continue
            channel = "web"
        elif path in _API_PATHS:
            if (qs.get("offset") or ["0"])[0] not in ("", "0"):
                continue
            channel = "api"
        else:
            continue
        term = normalize((qs.get("q") or [""])[0])
        key = client_key(channel, ip, ua) if term else None
        if key is None:
            continue
        if day not in keys:
            keys[day] = _salt(day) if day == today else secrets.token_bytes(32)
        out.add((day, term, tag_for(day, key, keys[day])))
    keys.clear()
    return out


def backfill_from_logs(paths, start_day: str, end_day: str) -> int:
    """Run once, right after tagging is deployed: replaces the tags of past
    days in range (which would otherwise double up on a second run, as each
    run uses new keys) and adds today's without duplicates."""
    import gzip
    today = time.strftime("%Y-%m-%d", time.gmtime())
    end_day = min(end_day, today)
    found: set = set()
    for p in paths:
        opener = gzip.open if p.endswith(".gz") else open
        try:
            with opener(p, "rt", errors="replace") as fh:
                found |= tags_from_log(fh, start_day, end_day)
        except OSError:
            log.warning("cannot read %s", p)
    c = _connect()
    with c:
        c.execute("DELETE FROM search_searchers WHERE day >= ? AND day <= ? AND day < ?",
                  (start_day, end_day, today))
        c.executemany("INSERT OR IGNORE INTO search_searchers(day, term, tag) VALUES (?,?,?)",
                      sorted(found))
    c.close()
    return len(found)


def matching(start_day: str, end_day: str, word: str, min_n: int = PUBLIC_MIN, limit: int = 10) -> list:
    """Public terms containing `word` as a whole word (for topic pages)."""
    s = summary(start_day, end_day, limit=500, min_n=min_n)
    rx = re.compile(r"\b" + re.escape(word) + r"s?\b")
    return [i for i in s["top"] if rx.search(i["term"])][:limit]


if __name__ == "__main__":  # python -m directory.search_terms backfill START END LOG...
    import sys
    if len(sys.argv) >= 5 and sys.argv[1] == "backfill":
        print(backfill_from_logs(sys.argv[4:], sys.argv[2], sys.argv[3]), "searcher tags written")
    else:
        sys.exit("usage: python -m directory.search_terms backfill START_DAY END_DAY LOGFILE...")
