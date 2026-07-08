"""Lit Protocol Flows reverse-crawl source (robots-compliant).

flows.litprotocol.com ("Flows") is an agent-payment platform where developers
deploy "flows"/"skills" (code that can move money, governed by a Lit policy),
exposed as x402-payable resources. Its `flows-crawler/0.1` bot indexes
agent-tools.cloud; this source mirrors its public catalogue back into the x402
`services` table.

robots.txt DISALLOWS /api/, so we never touch /api/flows. We use ONLY the
robots-ALLOWED discovery surfaces:
  - /sitemap.xml       -> canonical list of /f/<slug> flow pages
  - /.well-known/x402  -> x402 payable resource (invoke) URLs
  - /f/<slug>          -> public flow page, OpenGraph title/description
x402 support is still proven by directory.reverify_x402 (never inferred).
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

import httpx

from .crawlers import UA, TIMEOUT

log = logging.getLogger("directory.flows")

_BASE = "https://flows.litprotocol.com"
_SITEMAP = _BASE + "/sitemap.xml"
_WELLKNOWN = _BASE + "/.well-known/x402"
_MAX_FLOWS = 300  # safety cap

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_FPATH_RE = re.compile(r"^/f/([^/]+)/?$")
_INVOKE_RE = re.compile(r"/api/flows/([^/]+)/invoke")


def _og(text: str, prop: str) -> str:
    m = re.search(r'<meta[^>]*\b(?:property|name)=["\']og:' + prop + r'["\'][^>]*>', text, re.I)
    if not m:
        return ""
    cm = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
    return html.unescape(cm.group(1)).strip() if cm else ""


def _slugs_from_sitemap(c: httpx.Client) -> list:
    r = c.get(_SITEMAP)
    r.raise_for_status()
    slugs = []
    for loc in _LOC_RE.findall(r.text):
        m = _FPATH_RE.match(urlparse(loc).path)
        if m:
            slugs.append(m.group(1))
    return slugs


def _resource_map(c: httpx.Client) -> dict:
    out = {}
    try:
        r = c.get(_WELLKNOWN)
        r.raise_for_status()
        for res in (r.json().get("resources") or []):
            m = _INVOKE_RE.search(res or "")
            if m:
                out[m.group(1)] = res
    except Exception as e:  # noqa: BLE001
        log.warning("flows .well-known/x402 failed: %r", e)
    return out


def _map(slug: str, res_map: dict, c: httpx.Client) -> dict:
    name = desc = ""
    try:
        r = c.get(_BASE + "/f/" + slug)
        if r.status_code == 200:
            name = re.sub(r"\s*[\u2014\u2013-]\s*Flows\s*$", "", _og(r.text, "title")).strip()
            desc = _og(r.text, "description")
    except Exception as e:  # noqa: BLE001
        log.debug("flows /f/%s meta failed: %r", slug, e)
    if not name:
        name = re.sub(r"[-_]+", " ", slug).strip().title()
    resource = res_map.get(slug)
    tags = ["flows", "lit-protocol", "x402", "skill"]
    if resource:
        tags.append("x402-resource")
    return {
        "slug": ("flows-litprotocol-" + slug)[:80],
        "name": name[:200],
        "url": _BASE + "/f/" + slug,
        "description": (desc or "Lit Protocol Flows skill (code that can move money, governed by a policy).")[:500],
        "category": "general",
        "chains": [],
        "currency": None,
        "well_known_url": _WELLKNOWN,
        "confidence": None,
        "resource_samples": ([{"url": resource, "kind": "x402-resource"}] if resource else []),
        "source": "flows-litprotocol",
        "source_id": slug,
        "tags": tags,
        "region": "global",
    }


def fetch_flows_litprotocol() -> list:
    out: list = []
    seen: set = set()
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True) as c:
            slugs = _slugs_from_sitemap(c)
            res_map = _resource_map(c)
            for slug in slugs:
                if slug in seen or slug.startswith("e2e-echo"):
                    continue
                seen.add(slug)
                out.append(_map(slug, res_map, c))
                if len(out) >= _MAX_FLOWS:
                    break
    except Exception as e:  # noqa: BLE001
        log.warning("flows-litprotocol fetch failed: %r", e)
        return out
    log.info("flows-litprotocol: %d flows ingested (skipped e2e-echo tests)", len(out))
    return out
