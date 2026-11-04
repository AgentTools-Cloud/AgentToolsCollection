"""Small abuse-control helpers for public directory endpoints."""

from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import logging
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Iterable

from fastapi import HTTPException, Request

from . import db

log = logging.getLogger(__name__)
RATE_STORE_TIMEOUT = 0.5
RATE_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_updated ON rate_limits(updated_at);
"""


def rate_limit_db_path(primary_db: str | None = None) -> str:
    primary = Path(primary_db or db.DEFAULT_DB_PATH).resolve()
    suffix = primary.suffix or ".db"
    return str(primary.with_name(primary.stem + "-rate-limits" + suffix))


def _marker_path(primary_db: str) -> Path:
    return Path(rate_limit_db_path(primary_db) + ".active")


def _store_active(primary_db: str) -> bool:
    try:
        mode = _marker_path(primary_db).lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise OSError("Invalid rate-limit activation marker")
    return True


@contextmanager
def _migration_lock(primary_db: str, *, exclusive: bool, timeout: float = RATE_STORE_TIMEOUT):
    path = Path(rate_limit_db_path(primary_db) + ".lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Invalid rate-limit migration lock")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _counter_connection(path: Path, *, create: bool = False) -> sqlite3.Connection:
    if create:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("Invalid rate-limit database")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    elif not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Invalid rate-limit database")
    conn = sqlite3.connect(
        path.as_uri() + "?mode=rw", uri=True, timeout=RATE_STORE_TIMEOUT,
        check_same_thread=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(RATE_STORE_TIMEOUT * 1000)}")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(RATE_STORE_SCHEMA)
        return conn
    except BaseException:
        conn.close()
        raise


def _merge_counters(conn, rows) -> None:
    conn.executemany(
        "INSERT INTO rate_limits(key,window_start,count,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(key,window_start) DO UPDATE SET "
        "count=MAX(rate_limits.count,excluded.count), "
        "updated_at=MAX(rate_limits.updated_at,excluded.updated_at)",
        rows,
    )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def activate_rate_limit_store(primary_db: str | None = None) -> dict:
    """Switch only after every serving instance understands the migration fence."""
    primary = str(Path(primary_db or db.DEFAULT_DB_PATH).resolve())
    rate_path = Path(rate_limit_db_path(primary))
    marker = _marker_path(primary)
    with _migration_lock(primary, exclusive=True, timeout=10.0):
        if _store_active(primary):
            with closing(_counter_connection(rate_path)):
                pass
            return {"active": True, "rows_copied": 0, "rate_db_path": str(rate_path)}
        with closing(db.connect(primary, read_only=True)) as conn:
            rows = [tuple(row) for row in conn.execute(
                "SELECT key,window_start,count,updated_at FROM rate_limits")]
        with closing(_counter_connection(rate_path, create=True)) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                _merge_counters(conn, rows)
        pending = marker.with_name(marker.name + ".next")
        descriptor = os.open(
            pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("active\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, marker)
        _sync_directory(marker.parent)
    return {"active": True, "rows_copied": len(rows), "rate_db_path": str(rate_path)}


def deactivate_rate_limit_store(primary_db: str | None = None) -> dict:
    """Merge live counters back before rolling any instance back to old code."""
    primary = str(Path(primary_db or db.DEFAULT_DB_PATH).resolve())
    with _migration_lock(primary, exclusive=True, timeout=10.0):
        if not _store_active(primary):
            return {"active": False, "rows_copied": 0}
        with closing(_counter_connection(Path(rate_limit_db_path(primary)))) as conn:
            rows = [tuple(row) for row in conn.execute(
                "SELECT key,window_start,count,updated_at FROM rate_limits")]
        with db.writer(primary, immediate=True, timeout=3.0) as conn:
            _merge_counters(conn, rows)
        _marker_path(primary).unlink()
        _sync_directory(Path(primary).parent)
    return {"active": False, "rows_copied": len(rows)}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def client_ip_from_request(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def check_ip_limits(
    ip: str | None,
    scope: str,
    windows: Iterable[tuple[str, int, int]],
    *,
    db_path: str | None = None,
) -> dict | None:
    """Apply one or more fixed-window limits for an IP.

    windows items are (name, limit, seconds). Returns the first exceeded
    window, otherwise None. Limits <= 0 are treated as disabled.
    """
    primary = str(Path(db_path or db.DEFAULT_DB_PATH).resolve())
    identity = (ip or "unknown").strip() or "unknown"
    try:
        with _migration_lock(primary, exclusive=False):
            isolated = _store_active(primary)
            for name, limit, seconds in windows:
                if int(limit) <= 0:
                    continue
                key = f"{scope}:{name}:{identity}"
                if isolated:
                    with closing(_counter_connection(Path(rate_limit_db_path(primary)))) as conn:
                        with conn:
                            conn.execute("BEGIN IMMEDIATE")
                            state = db.hit_rate_limit(conn, key, int(limit), int(seconds))
                else:
                    with db.writer(primary, timeout=RATE_STORE_TIMEOUT) as conn:
                        state = db.hit_rate_limit(conn, key, int(limit), int(seconds))
                if not state["allowed"]:
                    return {"window": name, "ip": identity, **state}
    except (sqlite3.Error, OSError) as exc:
        log.warning("rate-limit store unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"error": "rate_limit_unavailable", "message": "Please retry shortly."},
            headers={"Retry-After": "2", "Cache-Control": "private, no-store"},
        ) from exc
    return None


def raise_rate_limited(state: dict, message: str = "rate limited") -> None:
    retry_after = str(max(1, int(state.get("retry_after") or 1)))
    raise HTTPException(
        status_code=429,
        detail={
            "error": "rate_limited",
            "message": message,
            "window": state.get("window"),
            "limit": state.get("limit"),
            "retry_after_seconds": int(retry_after),
        },
        headers={"Retry-After": retry_after},
    )