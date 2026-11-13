"""Promotion slots on site pages.

A slot is a named place on a page (for example ``home-after-index``). Each slot
shows one item: a paid ("sponsored") item when one is configured and current,
otherwise our own ("house") item, otherwise nothing.

Rules (also in /terms#promotions):
* Sponsored items are always labelled "Sponsored" and their links carry
  ``rel="sponsored nofollow noopener"``.
* Promotions never change scores, ranking, readiness or statistics, and are
  never placed on /stats pages.
* Machine-readable output marks them with ``"sponsored": true``.

Paid items are read from the JSON file in ``AGENT_TOOLS_PROMO_FILE``::

    {"home-after-index": {"title": "...", "text": "...", "href": "https://...",
                          "cta": "...", "sponsor": "Example Inc",
                          "starts": "2026-12-01", "ends": "2026-12-31"}}

Changing the file needs no code change; it is re-read at most once a minute.
"""
from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SLOTS = ("home-after-index", "home-before-blog")

HOUSE = {
    "home-after-index": {
        "title": "Own a listing here?",
        "text": ("Claim it to fix its description, pricing and links, and see how it "
                 "compares with similar listings on readiness and latency."),
        "href": "/docs/claim",
        "cta": "Claim your listing",
    },
    "home-before-blog": {
        "title": "Give your agent the whole index",
        "text": ("Connect our MCP server to search, compare and safety-check tools from "
                 "inside your agent. No key needed to search; a free key unlocks the full statistics."),
        "href": "/about#api",
        "cta": "How to connect",
    },
}

_cache: dict = {"at": 0.0, "path": None, "items": {}}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _valid(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if not all(isinstance(item.get(k), str) and item[k].strip()
               for k in ("title", "href", "sponsor")):
        return False
    if urlparse(item["href"]).scheme != "https":
        return False
    today = _today()
    if item.get("starts") and today < str(item["starts"]):
        return False
    if item.get("ends") and today > str(item["ends"]):
        return False
    return True


def _sponsored_items() -> dict:
    path = os.environ.get("AGENT_TOOLS_PROMO_FILE") or ""
    now = time.time()
    if path == _cache["path"] and now - _cache["at"] < 60:
        return _cache["items"]
    items: dict = {}
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            items = {slot: item for slot, item in (raw or {}).items()
                     if slot in SLOTS and _valid(item)}
        except FileNotFoundError:
            items = {}
        except Exception:  # noqa: BLE001 - a bad file must not break pages
            log.warning("promo file %s unreadable; showing house items", path)
            items = {}
    _cache.update(at=now, path=path, items=items)
    return items


def slot(name: str) -> dict | None:
    """The item to show in slot `name`, or None. Adds `sponsored` and `rel`."""
    if name not in SLOTS:
        return None
    paid = _sponsored_items().get(name)
    if paid:
        return {"title": paid["title"].strip()[:80], "text": (paid.get("text") or "").strip()[:240],
                "href": paid["href"], "cta": (paid.get("cta") or "Learn more").strip()[:40],
                "sponsor": paid["sponsor"].strip()[:60], "sponsored": True,
                "rel": "sponsored nofollow noopener"}
    house = HOUSE.get(name)
    if house:
        return {**house, "sponsored": False, "rel": None}
    return None


def slots(*names: str) -> dict:
    return {n: slot(n) for n in names}
