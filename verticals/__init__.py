"""Paid vertical endpoints for the agent-tools.cloud x402 relay."""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_SOFT_CAP = 4096


def cache_get(key: str, ttl: float):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > ttl:
        _CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value) -> None:
    if len(_CACHE) >= _CACHE_SOFT_CAP:
        drop = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: _CACHE_SOFT_CAP // 10]
        for k, _ in drop:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time(), value)
