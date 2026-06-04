"""Small abuse-control helpers for public directory endpoints."""

from __future__ import annotations

import os
from typing import Iterable

from fastapi import HTTPException, Request

from . import db


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
    db_path: str = db.DEFAULT_DB_PATH,
) -> dict | None:
    """Apply one or more fixed-window limits for an IP.

    windows items are (name, limit, seconds). Returns the first exceeded
    window, otherwise None. Limits <= 0 are treated as disabled.
    """
    identity = (ip or "unknown").strip() or "unknown"
    for name, limit, seconds in windows:
        if int(limit) <= 0:
            continue
        key = f"{scope}:{name}:{identity}"
        with db.writer(db_path) as conn:
            state = db.hit_rate_limit(conn, key, int(limit), int(seconds))
        if not state["allowed"]:
            return {"window": name, "ip": identity, **state}
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