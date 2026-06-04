"""LLM-assisted service recommendation over directory candidates."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from . import cards, db

log = logging.getLogger("directory.ask")

_STOPWORDS = {
    "a", "an", "and", "api", "apis", "by", "current", "find", "for", "from",
    "get", "give", "i", "in", "me", "need", "of", "on", "please", "service",
    "services", "that", "the", "to", "tool", "tools", "use", "with",
}


def _allowed_model() -> str:
    explicit = os.getenv("AGENT_TOOLS_ASK_MODEL")
    if explicit:
        return explicit
    allowed = [m.strip() for m in os.getenv("ALLOWED_MODELS", "").split(",") if m.strip()]
    return allowed[0] if allowed else "Qwen/Qwen3.6-35B-A3B"


def _fallback(query: str, candidates: list[dict[str, Any]], limit: int, reason: str) -> dict[str, Any]:
    recs = []
    for card in candidates[:limit]:
        recs.append({
            "slug": card.get("slug"),
            "name": card.get("name"),
            "url": card.get("url"),
            "why": "; ".join((card.get("match_reason") or [])[:3]) or "Top directory match.",
            "confidence": card.get("confidence") or 0.35,
            "next_step": "Call get_service for the full service card and x402 call hints.",
            "service_card": card,
        })
    return {
        "query": query,
        "answer": (
            "LLM ranking was unavailable, so these are the highest-ranked "
            "directory matches. Inspect each service card before calling."
        ),
        "recommendations": recs,
        "follow_up_questions": [],
        "llm_used": False,
        "fallback_reason": reason,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", text):
        try:
            data, _end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            candidates.append(data)
    for data in reversed(candidates):
        if "recommendations" in data or "answer" in data:
            return data
    return candidates[-1] if candidates else None


async def _call_llm(prompt: str) -> dict[str, Any] | None:
    base_url = (
        os.getenv("AGENT_TOOLS_ASK_BASE_URL")
        or os.getenv("UPSTREAM_BASE_URL", "")
    ).rstrip("/")
    api_key = os.getenv("AGENT_TOOLS_ASK_API_KEY") or os.getenv("UPSTREAM_API_KEY", "")
    if not base_url or not api_key:
        return None
    body = {
        "model": _allowed_model(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You rank x402 services for autonomous agents. Use only "
                    "the supplied candidates. Do not invent services, prices, "
                    "schemas, URLs, or claims. Return strict JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "reasoning_effort": "off",
    }
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=20.0, pool=20.0),
        ) as client:
            response = await client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning("ask llm call failed: %r", exc)
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or msg.get("reasoning") or "").strip()
    return _extract_json(content)


def _prompt(query: str, candidates: list[dict[str, Any]], limit: int) -> str:
    compact = [cards.brief_for_llm(c) for c in candidates[:10]]
    return (
        "User/agent intent:\n"
        f"{query}\n\n"
        "Candidate services JSON:\n"
        f"{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Return ONLY one compact JSON object, no prose and no markdown. Shape:\n"
        "{\"answer\":\"short paragraph\",\"recommendations\":[],"
        "\"follow_up_questions\":[]}\n"
        "Each recommendation item must be: "
        "{\"slug\":\"candidate slug\",\"why\":\"why it matches\","
        "\"confidence\":0.0,\"next_step\":\"what to do next\"}.\n"
        f"Select at most {limit} recommendations. If no candidate is suitable, "
        "return an empty recommendations array and explain why. Prefer healthy "
        "services with explicit resources/payment metadata and real tx_30d."
    )


def _intent_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{1,}", query.lower()):
        if token in _STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:8]


def _retrieve_rows(
    conn,
    query: str,
    *,
    candidate_limit: int,
    category: str | None,
    chain: str | None,
    health: str | None,
    min_confidence: float | None,
    has_mcp: bool,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []

    def add_batch(q: str | None, limit: int) -> None:
        nonlocal rows
        for row in db.search(
            conn,
            q=q,
            category=category,
            chain=chain,
            health=health,
            min_confidence=min_confidence,
            has_mcp=has_mcp,
            limit=limit,
        ):
            slug = row.get("slug")
            if slug and slug not in seen:
                seen.add(slug)
                rows.append(row)
            if len(rows) >= candidate_limit:
                return

    add_batch(query, candidate_limit)
    if len(rows) >= candidate_limit:
        return rows
    for term in _intent_terms(query):
        add_batch(term, max(5, candidate_limit // 2))
        if len(rows) >= candidate_limit:
            break
    return rows[:candidate_limit]


def _sanitize_llm_result(
    query: str,
    raw: dict[str, Any],
    candidates: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    by_slug = {c.get("slug"): c for c in candidates if c.get("slug")}
    recs = []
    for item in raw.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if slug not in by_slug:
            continue
        card = by_slug[slug]
        recs.append({
            "slug": slug,
            "name": card.get("name"),
            "url": card.get("url"),
            "why": str(item.get("why") or "Selected from provided candidates."),
            "confidence": item.get("confidence"),
            "next_step": str(item.get("next_step") or "Call get_service for details."),
            "service_card": card,
        })
        if len(recs) >= limit:
            break
    questions = raw.get("follow_up_questions") or []
    if not isinstance(questions, list):
        questions = []
    answer = raw.get("answer") or "I ranked the provided candidate services."
    return {
        "query": query,
        "answer": str(answer),
        "recommendations": recs,
        "follow_up_questions": [str(q) for q in questions[:3]],
        "llm_used": True,
        "candidate_count": len(candidates),
    }


async def answer_query(
    query: str,
    *,
    db_path: str = db.DEFAULT_DB_PATH,
    limit: int = 5,
    candidate_limit: int = 30,
    category: str | None = None,
    chain: str | None = None,
    max_price_usd: float | None = None,
    health: str | None = None,
    min_confidence: float | None = None,
    has_mcp: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Retrieve candidates, optionally LLM-rerank, and return recommendations."""
    query = (query or "").strip()
    if not query:
        return {"error": "empty_query", "message": "query is required"}
    limit = max(1, min(int(limit), 10))
    candidate_limit = max(limit, min(int(candidate_limit), 50))

    with db.connect(db_path, read_only=True) as conn:
        rows = _retrieve_rows(
            conn,
            query,
            candidate_limit=candidate_limit,
            category=category,
            chain=chain,
            health=health,
            min_confidence=min_confidence,
            has_mcp=has_mcp,
        )
    if max_price_usd is not None:
        rows = [
            r for r in rows
            if r.get("price_min") is None or float(r["price_min"]) <= float(max_price_usd)
        ]
    candidates = [cards.build_service_card(r) for r in rows]
    if not candidates:
        return {
            "query": query,
            "answer": "No matching x402 service was found in the current directory.",
            "recommendations": [],
            "follow_up_questions": ["Try a broader intent or remove price/chain filters."],
            "llm_used": False,
            "candidate_count": 0,
        }
    if not use_llm:
        out = _fallback(query, candidates, limit, "use_llm=false")
        out["candidate_count"] = len(candidates)
        return out
    raw = await _call_llm(_prompt(query, candidates, limit))
    if raw is None:
        out = _fallback(query, candidates, limit, "llm_unavailable")
        out["candidate_count"] = len(candidates)
        return out
    out = _sanitize_llm_result(query, raw, candidates, limit)
    if not out.get("recommendations"):
        out = _fallback(query, candidates, limit, "llm_returned_no_valid_recommendations")
        out["candidate_count"] = len(candidates)
    return out