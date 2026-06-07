"""Shared malware/abuse detection orchestration for an MCP endpoint.

Used by BOTH surfaces so the logic lives in exactly one place:
  * the MCP tool   `directory.mcp_app.scan_mcp_safety`
  * the A2A skill  `directory.a2a` (`message/send` -> scan_mcp_safety)

Strategy, keyed by endpoint URL:
  * already in the directory  -> return our LATEST stored rule verdict (refreshed
    hourly by the health job).
  * not yet indexed           -> probe the endpoint live, statically scan its
    advertised tools + metadata, ADD it to the directory, and return the fresh
    verdict.

Two independent dimensions are reported:
  * `verdict`        - authoritative, from the deterministic static rules
                       (`mcp_safety`). This is what gets stored/surfaced and is
                       what the hourly health job refreshes.
  * `llm_reference`  - advisory only, a Qwen3-8B read of the same advertised
                       text. The LLM is SLOW, so it runs ON-DEMAND only (only on
                       a live user call, never in the hourly job) and is NEVER
                       persisted. Never overrides the rule verdict; when it is
                       *more* severe than the rules an `advisory` note is
                       attached as a safety-net signal.

Fully synchronous (httpx.Client + sqlite). Call it via ``asyncio.to_thread``
from async code (MCP tool) or ``run_in_threadpool`` from the A2A route.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from . import crawlers as directory_crawlers
from . import db as directory_db
from . import mcp_safety

log = logging.getLogger("mcpserver.directory.safety")

_SEVERITY = {"clean": 0, "suspicious": 1, "malicious": 2}

# ---- Qwen3-8B reference dimension --------------------------------------------

_LLM_SYSTEM = (
    "You are a security auditor for MCP (Model Context Protocol) servers. You "
    "are given the PUBLIC, ADVERTISED metadata of an MCP server — its name, "
    "description and tool docs. Decide whether that advertised text tries to "
    "trick an AI agent or a user into running malware, leaking secrets, or "
    "other abuse: e.g. pipe-to-shell install lures (curl|bash), base64/eval "
    "download cradles, PowerShell IEX cradles, bare-IP payload hosts, or "
    "prompt-injection / credential-exfiltration phrasing ('ignore previous "
    "instructions', 'send your .env / api key'). IMPORTANT: a security or "
    "defense product that merely DESCRIBES these attacks (a scanner, firewall, "
    "detector) is NOT malicious. You only ever see text; you never execute "
    "anything. Reply with strict JSON only, no markdown, no prose."
)

_LLM_USER_TMPL = (
    "MCP server advertised metadata:\n----\n__TEXT__\n----\n"
    "Return ONLY one compact JSON object with this exact shape: "
    '{"verdict":"clean|suspicious|malicious","confidence":0.0,'
    '"reason":"one short sentence"}'
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def llm_reference(name: str = "", description: str = "", tools_text: str = "",
                  timeout: float = 25.0) -> Optional[dict]:
    """Ask Qwen3-8B for an advisory verdict over advertised metadata.

    Returns ``{model, verdict, reason, confidence}`` or ``None`` on any failure
    (missing config, empty text, network/parse error). Never raises.
    """
    base_url = (
        os.getenv("AGENT_TOOLS_SAFETY_BASE_URL")
        or os.getenv("AGENT_TOOLS_ASK_BASE_URL")
        or os.getenv("UPSTREAM_BASE_URL", "")
    ).rstrip("/")
    api_key = (
        os.getenv("AGENT_TOOLS_SAFETY_API_KEY")
        or os.getenv("AGENT_TOOLS_ASK_API_KEY")
        or os.getenv("UPSTREAM_API_KEY", "")
    )
    model = (
        os.getenv("AGENT_TOOLS_SAFETY_MODEL")
        or os.getenv("AGENT_TOOLS_ASK_MODEL")
        or "Qwen/Qwen3-8B"
    )
    if not base_url or not api_key:
        return None
    text = " ".join(x for x in (name, description, tools_text) if x).strip()[:6000]
    if not text:
        return None

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": _LLM_USER_TMPL.replace("__TEXT__", text)},
        ],
        "temperature": 0.0,
        "max_tokens": 768,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        with httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=8.0, read=timeout, write=8.0, pool=8.0),
        ) as c:
            r = c.post("/v1/chat/completions", json=body)
            r.raise_for_status()
            payload = r.json()
    except Exception as e:  # noqa: BLE001 - advisory only
        log.warning("safety llm call failed: %r", e)
        return None

    choices = payload.get("choices") or []
    if not choices:
        return None
    content = _strip_think((choices[0].get("message") or {}).get("content") or "")
    data = None
    try:
        data = json.loads(content)
    except Exception:
        m = _JSON_OBJ_RE.search(content)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return None

    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in _SEVERITY:
        synonyms = {
            "safe": "clean", "benign": "clean", "ok": "clean", "harmless": "clean",
            "suspect": "suspicious", "risky": "suspicious", "warning": "suspicious",
            "danger": "malicious", "dangerous": "malicious", "unsafe": "malicious",
            "malware": "malicious", "malicous": "malicious",
        }
        verdict = synonyms.get(verdict, "")
    if verdict not in _SEVERITY:
        # The model did not return a usable verdict (empty / garbled / refused).
        # Report NO opinion rather than a misleading 'clean' — this is a safety
        # tool, so silence must never read as an all-clear.
        return None
    conf = data.get("confidence")
    try:
        conf = round(float(conf), 2)
    except (TypeError, ValueError):
        conf = None
    return {
        "model": model,
        "verdict": verdict,
        "reason": str(data.get("reason") or data.get("explanation") or "")[:400],
        "confidence": conf,
    }


def _advisory(rule_verdict: str, llm: Optional[dict]) -> Optional[str]:
    """A safety-net note when the LLM reference is more severe than the rules."""
    if not llm:
        return None
    lv = llm.get("verdict")
    if _SEVERITY.get(lv, 0) > _SEVERITY.get(rule_verdict, 0):
        return (
            f"LLM reference ({llm.get('model')}) rates this '{lv}' while static "
            f"rules rate it '{rule_verdict}'. Reason: {llm.get('reason') or 'n/a'}"
        )
    return None


# ---- helpers -----------------------------------------------------------------

def _tools_to_text(tools) -> str:
    """Flatten a list of {name, description} tool dicts into one scannable blob."""
    return " ".join(
        f"{t.get('name', '')} {t.get('description', '')}"
        for t in (tools or []) if isinstance(t, dict)
    ).strip()


# ---- public entry point ------------------------------------------------------

def scan_endpoint(endpoint_url: str, name: str = "", description: str = "",
                  tools_text: str = "", db_path: Optional[str] = None,
                  with_llm: bool = True) -> dict[str, Any]:
    """Lookup-or-(probe+scan+index) an MCP endpoint. Returns a verdict dict.

    Synchronous. See module docstring for the dimensions reported.
    """
    endpoint_url = (endpoint_url or "").strip()
    if not endpoint_url:
        return {"error": "invalid_url", "message": "endpoint_url is required"}
    if not (endpoint_url.startswith("http://") or endpoint_url.startswith("https://")):
        return {"error": "invalid_url",
                "message": "endpoint_url must start with http:// or https://"}
    if len(endpoint_url) > 500:
        return {"error": "invalid_url", "message": "endpoint_url too long (max 500)"}

    name = (name or "").strip()
    description = (description or "").strip()[:2000]
    tools_text = (tools_text or "").strip()
    ep = endpoint_url.lower().rstrip("/")
    dbp = db_path or directory_db.DEFAULT_DB_PATH

    # ---- 1. Already indexed? return latest stored verdict ------------------
    with directory_db.connect(dbp, read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM mcp_servers WHERE lower(rtrim(endpoint_url, '/'))=?",
            (ep,),
        ).fetchone()
        mcp = directory_db.mcp_row_to_dict(row) if row else None

    if mcp is not None:
        stored = mcp.get("safety_verdict")
        if not stored:
            res = mcp_safety.scan_mcp(
                name=mcp.get("name") or "", description=mcp.get("description") or "",
                tools_text=_tools_to_text(mcp.get("tools")),
            )
            stored, score, reasons = res.verdict, res.score, res.to_dict()["reasons"]
            try:
                with directory_db.writer(dbp) as wc:
                    wc.execute(
                        "UPDATE mcp_servers SET safety_verdict=?, safety_score=?, "
                        "safety_reasons=? WHERE id=?",
                        (res.verdict, res.score,
                         json.dumps(reasons, ensure_ascii=False), mcp.get("id")),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("safety persist (existing) failed: %r", e)
        else:
            score = mcp.get("safety_score")
            reasons = mcp.get("safety_reasons")
            if isinstance(reasons, str):
                try:
                    reasons = json.loads(reasons)
                except (TypeError, ValueError):
                    reasons = []
        # The LLM second opinion is slow, so it is on-demand only and never
        # persisted: run it live over the stored metadata for each user call.
        llm_ref = None
        if with_llm:
            llm_ref = llm_reference(
                mcp.get("name") or "", mcp.get("description") or "",
                _tools_to_text(mcp.get("tools")),
            )
        return {
            "verdict": stored, "score": score, "reasons": reasons or [],
            "llm_reference": llm_ref, "advisory": _advisory(stored, llm_ref),
            "slug": mcp.get("slug"), "name": mcp.get("name"),
            "endpoint_url": mcp.get("endpoint_url"),
            "source": "stored", "indexed": True,
            "last_scanned": mcp.get("health_checked"),
        }

    # ---- 2. New server: probe live, scan (rules + LLM), index -------------
    probe = directory_crawlers.probe_mcp_health(endpoint_url)
    probe_tools = probe.get("tools")
    if isinstance(probe_tools, list):
        tools_json = json.dumps(probe_tools, ensure_ascii=False)
        scan_tools_text = _tools_to_text(probe_tools) or tools_text
    else:
        tools_json = None
        scan_tools_text = tools_text

    host = urlparse(endpoint_url).hostname or "server"
    if not name:
        name = host
    res = mcp_safety.scan_mcp(name=name, description=description, tools_text=scan_tools_text)

    # LLM second opinion: on-demand only, never stored (it is slow).
    llm_ref = llm_reference(name, description, scan_tools_text) if with_llm else None

    # Only index endpoints we could actually reach OR for which the caller
    # supplied real metadata — don't pollute the directory with dead/garbage
    # URLs that carry no signal.
    reachable = probe.get("status") in ("ok", "degraded")
    has_meta = bool(description or scan_tools_text)
    slug = None
    indexed = False
    if reachable or has_meta:
        base = (directory_crawlers._host_slug(endpoint_url) + "-"
                + directory_crawlers._slugify(name)[:32]).strip("-")[:72]
        slug = f"{base or directory_crawlers._slugify(host)}-{hashlib.md5(ep.encode()).hexdigest()[:6]}"
        new_row = {
            "slug": slug, "name": name, "description": description or None,
            "endpoint_url": endpoint_url, "transport": "streamable-http",
            "source": "safety-scan", "source_id": ep, "source_url": endpoint_url,
            "health": probe.get("status") or "unknown",
            "health_checked": int(time.time()),
            "latency_ms": probe.get("latency_ms"),
            "http_status": probe.get("http_status"),
            "confidence": 0.3, "tags": [],
        }
        try:
            with directory_db.writer(dbp) as wc:
                _created, row_id = directory_db.upsert_mcp_server(wc, new_row)
                wc.execute(
                    "UPDATE mcp_servers SET conformance=?, tool_count=?, "
                    "tools_json=COALESCE(?, tools_json), "
                    "tools_text=COALESCE(?, tools_text), "
                    "safety_verdict=?, safety_score=?, safety_reasons=? WHERE id=?",
                    (probe.get("conformance"), probe.get("tool_count"),
                     tools_json, scan_tools_text or None,
                     res.verdict, res.score,
                     json.dumps(res.to_dict()["reasons"], ensure_ascii=False),
                     row_id),
                )
            indexed = True
        except Exception as e:  # noqa: BLE001
            log.warning("safety index failed: %r", e)
            slug = None

    out = {
        **res.to_dict(), "llm_reference": llm_ref,
        "advisory": _advisory(res.verdict, llm_ref),
        "slug": slug, "name": name, "endpoint_url": endpoint_url,
        "source": "new_scan", "indexed": indexed,
        "health": probe.get("status"), "tool_count": probe.get("tool_count"),
    }
    if not indexed:
        out["note"] = ("Endpoint unreachable and no metadata supplied — scanned "
                       "but not added to the directory.")
    return out


__all__ = ["scan_endpoint", "llm_reference"]
