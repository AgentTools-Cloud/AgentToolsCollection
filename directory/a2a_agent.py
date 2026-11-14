"""LLM analyst behind our own A2A agent.

The model (Claude Opus via our gateway) is the agent: it reads the question,
calls data tools over our daily probe snapshot and the live directory, and
writes the analysis. Code supplies every number (tools) and checks the
numbers in the answer against the tool results.

Token discipline (the gateway does not honour prompt caching, so every input
token is paid each turn):
- one fixed system prompt; per-skill rules are one short line;
- tool results are compact JSON (short keys, numbers only, capped);
- at most MAX_TURNS tool turns, a per-request input cap, small max_tokens;
- the model writes prose only; the data part is assembled by code;
- conversation memory is a <=180-word summary the model returns with its
  answer (no extra call), not the transcript;
- answers are cached per (skill, normalised question, snapshot day);
- per-IP hourly limits and one shared daily cap on analyst runs
  (AGENT_TOOLS_A2A_DAILY_CALLS, default 1000); past them, or when the model
  fails, the request gets a JSON-RPC error (no silent downgrade). Cached
  answers do not count and are served even when the cap is reached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any

import httpx

from . import a2a_research, db

log = logging.getLogger("directory.a2a_agent")

SITE = "https://agent-tools.cloud"
MAX_TURNS = 5
TOOL_RESULT_CHARS = 4800          # ~1.2k tokens per tool result
CTX_TTL = 1800                    # conversation memory, seconds
CACHE_TTL = 6 * 3600
MAX_OUTPUT_TOKENS = 10000
SKILLS = {
    # id: (max output tokens per call, request input-token cap, hourly calls per IP)
    # No length rules in the prompts: the answer is as long as the question needs.
    # MAX_OUTPUT_TOKENS is only a ceiling against runaway output.
    "ask_analyst": (MAX_OUTPUT_TOKENS, 14000, 20),
    "compare": (MAX_OUTPUT_TOKENS, 14000, 20),
    "listing_audit": (MAX_OUTPUT_TOKENS, 14000, 20),
    "market_scan": (MAX_OUTPUT_TOKENS, 32000, 3),
    "research_report": (MAX_OUTPUT_TOKENS, 32000, 3),
}
_SKILL_RULE = {
    "ask_analyst": "",
    "compare": "Compare both sides on the same metrics; end with the single biggest difference.",
    "listing_audit": ("Call benchmark first. Give rank vs peers, the 3 biggest gaps, and one concrete fix per "
                      "gap (what to change, where)."),
    "market_scan": ("Find demand-vs-supply gaps: searched or unmet terms with few ready listings, topics strong "
                    "in one protocol and thin in another. Give 3-5 opportunities, each with its evidence numbers. "
                    "Use the report format."),
    "research_report": "Use the report format.",
}
REPORT_FORMAT = ("Report format: Title; Summary (3 sentences); Findings (3-6, each with numbers and [n]); "
                 "Method and data (1 paragraph: as_of, which tools); Limits (1-3 bullets).")

SYSTEM = """You are Agent Tools Research, the analyst agent of agent-tools.cloud. We index and probe x402 paid APIs, MCP servers and A2A agents every day. You answer research questions about this market for other agents and for people.

How you work:
- Decide what data answers the question, then call tools. Call independent tools in the same turn. Stop calling tools as soon as you can answer. At most 5 tool turns; prefer aggregate/topics over many listings calls.
- Every number in your answer must come from a tool result. You may round, and compute shares, differences and ratios from tool numbers; show the basis briefly ("x of y"). Never estimate a number no tool returned.
- Cite tool results inline as [n]; n is the index shown with each tool result.
- If the question is ambiguous in a way that changes which data to fetch, ask ONE short clarifying question instead of guessing.
- If the data cannot answer the question, say so and name what is missing.
- Finding, recommending, calling or safety-scanning a specific tool is the job of our MCP server (https://agent-tools.cloud/mcp-discovery/); say so in one sentence and still answer any market question in the message.
- Greetings or questions about what you do: answer in two sentences without tools.

Definitions: indexed = any source lists it; reachable = answered our latest probe; verified = passed a full protocol check (402 challenge / MCP initialize+tools/list / A2A JSON-RPC reply); ready = readiness grade A or B. Data refreshes daily; state the as_of date once.

Style: answer first in 1-2 sentences, then the supporting points the question needs. Plain text, no headings unless the report format applies, no filler, no marketing. Write in the user's language; keep product and protocol names in English. Our data licence: CC BY 4.0.

Final reply format (plain text, no JSON, no code fences):
<your answer with [n] citations>
SUMMARY: <running summary for your next turn: goal, entities and filters in play, conclusions with key numbers, open questions; max 100 words>
If you need a clarification instead of answering, reply with one line: CLARIFY: <one short question>"""

_KINDS = ("x402", "mcp", "a2a")
_TABLE = {"x402": "services", "mcp": "mcp_servers", "a2a": "a2a_agents"}
_VIEW_PATH = {"x402": "/services/", "mcp": "/mcp/servers/", "a2a": "/a2a/agents/"}
_ENUM_VIEW = {"type": "string", "enum": ["all", "mcp", "x402", "a2a"]}
_ENUM_KIND = {"type": "string", "enum": list(_KINDS)}
_FILTER = {
    "type": "object",
    "description": ("Optional filters: topic (word in name/description), grade (A-F), source, "
                    "protocol_version, network (x402 chain, e.g. base, solana), price_max_usd (x402), "
                    "reachable_only (default true)."),
    "properties": {"topic": {"type": "string"}, "grade": {"type": "string"}, "source": {"type": "string"},
                   "protocol_version": {"type": "string"}, "network": {"type": "string"},
                   "price_max_usd": {"type": "number"}, "reachable_only": {"type": "boolean"}},
}


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


TOOLS = [
    _fn("kpis", "Today's indexed/reachable/verified/ready counts and headline findings.", {"view": _ENUM_VIEW},
        ["view"]),
    _fn("trend", "Daily series (<=15 points) and start/end change of the counts over 7, 30 or 90 days.",
        {"view": _ENUM_VIEW, "days": {"type": "integer", "enum": [7, 30, 90]}}, ["view", "days"]),
    _fn("topic", "One topic: supply per protocol, readiness, top listings, coverage gaps, searches.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("topics", "All topics ranked: listings per protocol and share; gaps between protocols.",
        {"sort": {"type": "string", "enum": ["total", "x402", "mcp", "a2a"]},
         "limit": {"type": "integer"}}),
    _fn("usability", "Share that answers, passes a protocol check and is ready; why the rest fail; latency.",
        {"kind": {"type": "string", "enum": ["all", *_KINDS]}}),
    _fn("demand", "What agents and people searched in the last 30 days; searches with no results.",
        {"limit": {"type": "integer"}, "unmet_only": {"type": "boolean"}}),
    _fn("aggregate", ("Count listings grouped by grade|source|protocol_version|network|price_band, with filters. "
                      "Rows {k, n, ready}. Use for any how-many / what-share question."),
        {"kind": _ENUM_KIND, "group_by": {"type": "string", "enum": [
            "grade", "source", "protocol_version", "network", "price_band"]},
         "filter": _FILTER, "limit": {"type": "integer"}}, ["kind", "group_by"]),
    _fn("listings", "Up to 10 listings matching filters: name, grade, score, latency, price, url.",
        {"kind": _ENUM_KIND, "filter": _FILTER,
         "sort": {"type": "string", "enum": ["readiness", "latency", "price", "newest"]},
         "limit": {"type": "integer"}}, ["kind"]),
    _fn("benchmark", "One listing vs its peers: readiness and latency percentiles, biggest gaps with fixes.",
        {"kind": _ENUM_KIND, "slug": {"type": "string"}}, ["kind", "slug"]),
    _fn("month", "Start vs end of one month (YYYY-MM): KPI change, new listings, prices, versions.",
        {"month": {"type": "string"}}, ["month"]),
]


# --------------------------------------------------------------------------- tools

def _snap():
    from . import stats
    return stats.latest(lambda: db.connect(read_only=True))


def _r1(x):
    return round(x, 1) if isinstance(x, float) else x


def _downsample(rows, n=15):
    if len(rows) <= n:
        return rows
    step = (len(rows) - 1) / (n - 1)
    return [rows[round(i * step)] for i in range(n)]


def t_kpis(view="all"):
    d = a2a_research.ecosystem_report(view)["data"]
    keep = {k: d.get(k) for k in ("as_of", "kpis", "findings") if d.get(k)}
    if isinstance(d.get("facts"), dict) and view in ("x402", "all"):
        x = d["facts"].get("x402") or {}
        keep["x402_prices"] = {k: x.get(k) for k in ("cheapest_call_usd_quantiles",
                                                      "share_cheapest_call_le_1_cent_pct") if k in x}
    return keep


def t_trend(view="all", days=30):
    d = a2a_research.trend_report(view, days)["data"]
    if not d.get("available"):
        return {"error": "no history"}
    series = [[r["day"], r.get("indexed"), r.get("reachable"), r.get("verified"), r.get("ready")]
              for r in d.get("series") or []]
    return {"from": d["from"], "to": d["to"], "metrics": [
        {k: m[k] for k in ("metric", "start_day", "start", "end", "change", "change_pct")} for m in d["metrics"]],
        "series_cols": ["day", "indexed", "reachable", "verified", "ready"],
        "series": _downsample(series), "note": "days before the first full snapshot are rebuilt (reachable only)"}


def t_topic(name=""):
    d = a2a_research.topic_report(name)["data"]
    return a2a_research.compact("topic_report", d) if not d.get("error") else d


def t_topics(sort="total", limit=15):
    snap = _snap() or {}
    t = snap.get("topics") or {}
    rows = list(t.get("rows") or [])
    key = (lambda r: sum(r.get(k) or 0 for k in _KINDS)) if sort == "total" else (lambda r: r.get(sort) or 0)
    rows.sort(key=key, reverse=True)
    limit = max(1, min(int(limit or 15), 40))
    return {"as_of": snap.get("as_of"), "counted": t.get("counted"),
            "cols": ["topic", "x402", "x402_pct", "mcp", "mcp_pct", "a2a", "a2a_pct"],
            "rows": [[r.get(c) for c in ("topic", "x402", "x402_pct", "mcp", "mcp_pct", "a2a", "a2a_pct")]
                     for r in rows[:limit]],
            "gaps": (t.get("gaps") or [])[:8]}


def t_usability(kind="all"):
    d = a2a_research.compact("usability_report", a2a_research.usability_report()["data"])
    if kind in _KINDS:
        d["by_protocol"] = [r for r in d.get("by_protocol") or [] if r.get("kind") == kind]
        d["why_not"] = {kind: (d.get("why_not") or {}).get(kind)}
    return d


def t_demand(limit=15, unmet_only=False):
    from . import stats
    sd = stats.searches_view(_snap(), full=False) or {}
    limit = max(1, min(int(limit or 15), 30))
    out = {"from": sd.get("from"), "to": sd.get("to"), "searches": sd.get("searches"),
           "distinct_terms": sd.get("distinct_terms"), "by_kind": sd.get("by_kind"),
           "unmet": [[r.get("term"), r.get("searches")] for r in (sd.get("unmet") or [])[:limit]]}
    if not unmet_only:
        out["top"] = [[r.get("term"), r.get("searches"), r.get("no_results_pct")]
                      for r in (sd.get("top") or [])[:limit]]
    return out


def _where(kind, f):
    f = f if isinstance(f, dict) else {}
    w, a = [], []
    if f.get("reachable_only", True):
        w.append("health='ok'")
    if f.get("topic"):
        w.append("(lower(name) LIKE ? OR lower(coalesce(description,'')) LIKE ?)")
        t = "%" + str(f["topic"]).lower()[:40] + "%"
        a += [t, t]
    if f.get("grade"):
        w.append("readiness_grade=?")
        a.append(str(f["grade"]).upper()[:1])
    if f.get("source"):
        w.append("source=?")
        a.append(str(f["source"])[:40])
    if f.get("protocol_version") and kind in ("mcp", "a2a"):
        w.append("protocol_version LIKE ?")
        a.append(str(f["protocol_version"])[:20] + "%")
    if kind == "x402":
        if f.get("network"):
            w.append("lower(coalesce(chains,'')||coalesce(payment,'')) LIKE ?")
            a.append("%" + str(f["network"]).lower()[:20] + "%")
        if f.get("price_max_usd") is not None:
            try:
                w.append("price_min IS NOT NULL AND price_min<=?")
                a.append(float(f["price_max_usd"]))
            except (TypeError, ValueError):
                pass
    return (" WHERE " + " AND ".join(w)) if w else "", a


_BAND = ("CASE WHEN price_min IS NULL THEN 'unknown' WHEN price_min<=0.001 THEN '<=0.001' "
         "WHEN price_min<=0.01 THEN '<=0.01' WHEN price_min<=0.1 THEN '<=0.1' WHEN price_min<=1 THEN '<=1' "
         "ELSE '>1' END")


def t_aggregate(kind="mcp", group_by="grade", filter=None, limit=12):  # noqa: A002
    if kind not in _KINDS:
        return {"error": "kind must be mcp, x402 or a2a"}
    col = {"grade": "coalesce(readiness_grade,'-')", "source": "source",
           "protocol_version": "coalesce(protocol_version,'-')",
           "network": "coalesce(nullif(chains,''),'-')", "price_band": _BAND}.get(group_by)
    if col is None or (group_by in ("network", "price_band") and kind != "x402") or \
            (group_by == "protocol_version" and kind == "x402"):
        return {"error": f"group_by {group_by} not available for {kind}"}
    where, args = _where(kind, filter)
    limit = max(1, min(int(limit or 12), 25))
    sql = (f"SELECT {col} AS k, count(*) AS n, sum(readiness_grade IN ('A','B')) AS ready "
           f"FROM {_TABLE[kind]}{where} GROUP BY k ORDER BY n DESC LIMIT ?")
    with closing(db.connect(read_only=True)) as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
        total = conn.execute(f"SELECT count(*), sum(readiness_grade IN ('A','B')) FROM {_TABLE[kind]}{where}",
                             args).fetchone()
    return {"kind": kind, "group_by": group_by, "filter": filter or {}, "total": total[0],
            "ready": total[1] or 0, "cols": ["k", "n", "ready"],
            "rows": [[r[0], r[1], r[2] or 0] for r in rows]}


def t_listings(kind="mcp", filter=None, sort="readiness", limit=10):  # noqa: A002
    if kind not in _KINDS:
        return {"error": "kind must be mcp, x402 or a2a"}
    order = {"readiness": "readiness_score DESC", "latency": "latency_ms IS NULL, latency_ms ASC",
             "price": "price_min IS NULL, price_min ASC", "newest": "created_at DESC"}.get(sort, "readiness_score DESC")
    if sort == "price" and kind != "x402":
        order = "readiness_score DESC"
    where, args = _where(kind, filter)
    price = "price_min" if kind == "x402" else "NULL"
    limit = max(1, min(int(limit or 10), 10))
    with closing(db.connect(read_only=True)) as conn:
        rows = conn.execute(
            f"SELECT slug, name, readiness_grade, readiness_score, latency_ms, {price} "
            f"FROM {_TABLE[kind]}{where} ORDER BY {order} LIMIT ?", (*args, limit)).fetchall()
    return {"kind": kind, "cols": ["name", "grade", "score", "latency_ms", "price_usd", "url"],
            "rows": [[r[1], r[2], _r1(r[3]), r[4], r[5], SITE + _VIEW_PATH[kind] + r[0]] for r in rows]}


def t_benchmark(kind="mcp", slug=""):
    return a2a_research.compact("listing_benchmark", a2a_research.listing_benchmark(kind, slug)["data"])


def t_month(month=""):
    d = a2a_research.monthly_report(month)["data"]
    return {k: v for k, v in d.items() if k not in ("answer", "license", "methodology", "sign_in")}


_IMPL = {"kpis": t_kpis, "trend": t_trend, "topic": t_topic, "topics": t_topics, "usability": t_usability,
         "demand": t_demand, "aggregate": t_aggregate, "listings": t_listings, "benchmark": t_benchmark,
         "month": t_month}


def run_tool(name: str, args: dict) -> dict:
    fn = _IMPL.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames})
    except Exception as exc:  # noqa: BLE001 - a failing tool is reported to the model, not raised
        log.exception("a2a tool %s failed", name)
        return {"error": f"{name} failed: {type(exc).__name__}"}


# --------------------------------------------------------------------------- state

_state_lock = threading.Lock()


def _state_path() -> str:
    return os.path.join(os.path.dirname(db.DEFAULT_DB_PATH) or ".", "a2a-agent.db")


def _state() -> sqlite3.Connection:
    conn = sqlite3.connect(_state_path(), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ctx(id TEXT PRIMARY KEY, summary TEXT, updated INTEGER);
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, body TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS calls(ts INTEGER, ip TEXT, skill TEXT, tin INTEGER, tout INTEGER,
                                         usd REAL, cached INTEGER, fallback TEXT);
        CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);""")
    return conn


def _daily_cap() -> int:
    try:
        return int(os.getenv("AGENT_TOOLS_A2A_DAILY_CALLS", "1000") or 1000)
    except ValueError:
        return 1000


def _limited(ip: str | None, skill: str) -> tuple[str, int] | None:
    """(reason, retry_after_seconds) when an LLM run must be refused, else None."""
    now = int(time.time())
    cap = _daily_cap()
    with closing(_state()) as s:
        # Model runs today (UTC), for everyone: answered or failed, not cached hits or refusals.
        used = s.execute("SELECT count(*) FROM calls WHERE ts>=? AND cached=0 "
                         "AND (fallback IS NULL OR fallback='llm_error')", (now - now % 86400,)).fetchone()[0]
        if used >= cap:
            return "daily_limit", 86400 - now % 86400
        if ip:
            heavy = SKILLS[skill][2] < 10
            n, oldest = s.execute("SELECT count(*), min(ts) FROM calls WHERE ts>=? AND ip=? AND cached=0 "
                                  "AND fallback IS NULL"
                                  + (" AND skill IN ('market_scan','research_report')" if heavy else ""),
                                  (now - 3600, ip)).fetchone()
            if n >= SKILLS[skill][2]:
                return "rate_limited", max(1, (oldest or now) + 3600 - now)
    return None


def _record(ip, skill, tin, tout, usd, cached=False, fallback=None):
    try:
        with _state_lock, closing(_state()) as s:
            s.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)",
                      (int(time.time()), ip, skill, tin, tout, usd, int(cached), fallback))
            s.commit()
    except sqlite3.Error:
        log.exception("a2a: recording usage failed")


# Conversation ids (contextId). Memory is keyed so one caller can never read
# another caller's conversation:
# - with a valid API key the caller may choose its own contextId; memory is
#   stored under (account, contextId), so equal ids from different accounts
#   never meet;
# - without a key we ignore caller-chosen ids and issue an unguessable one
#   ("atc-ctx-" + 32 hex) in the reply; only an id we issued and still hold
#   continues a conversation.
ISSUED_PREFIX = "atc-ctx-"
_ISSUED_RE = re.compile(r"^atc-ctx-[0-9a-f]{32}$")


def account_for_key(raw: str | None) -> int | None:
    """Active account id for an API key, else None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        with closing(db.connect(read_only=True)) as conn:
            key = db.api_key_row(conn, raw)
            if key is None:
                return None
            user = conn.execute("SELECT status FROM users WHERE id=?", (key["user_id"],)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable key is treated as anonymous
        log.exception("a2a: API key lookup failed")
        return None
    return int(key["user_id"]) if user is not None and user[0] == "active" else None


def resolve_context(client_cid, account: int | None) -> dict:
    """{"id": contextId to return, "key": memory key, "scope", "continued"}."""
    cid = client_cid.strip()[:200] if isinstance(client_cid, str) else ""
    if account is not None:
        if not cid:
            cid = ISSUED_PREFIX + secrets.token_hex(16)
        key = f"u{account}:{cid}"
        return {"id": cid, "key": key, "scope": "account", "continued": _ctx_exists(key)}
    if cid and _ISSUED_RE.match(cid) and _ctx_exists("anon:" + cid):
        return {"id": cid, "key": "anon:" + cid, "scope": "issued", "continued": True}
    cid = ISSUED_PREFIX + secrets.token_hex(16)
    return {"id": cid, "key": "anon:" + cid, "scope": "issued", "continued": False,
            "replaced": bool(client_cid)}


def _ctx_exists(key) -> bool:
    with closing(_state()) as s:
        row = s.execute("SELECT updated FROM ctx WHERE id=?", (key,)).fetchone()
    return bool(row and row[0] > time.time() - CTX_TTL)


def _get_ctx(key):
    if not key:
        return None
    with closing(_state()) as s:
        row = s.execute("SELECT summary, updated FROM ctx WHERE id=?", (key,)).fetchone()
    return (row[0] or None) if row and row[1] > time.time() - CTX_TTL else None


def _put_ctx(key, summary):
    """Store (or refresh) a conversation; an empty summary still registers an issued id."""
    if not key:
        return
    with _state_lock, closing(_state()) as s:
        s.execute("INSERT OR REPLACE INTO ctx VALUES(?,?,?)", (key[:260], str(summary or "")[:1200], int(time.time())))
        s.execute("DELETE FROM ctx WHERE updated<?", (int(time.time()) - CTX_TTL,))
        s.commit()


def _cache_key(skill, question, as_of, detail):
    norm = " ".join(re.findall(r"[\w./:-]+", question.lower()))
    return hashlib.sha256(f"{skill}|{as_of}|{detail}|{norm}".encode()).hexdigest()


def _cache_get(key):
    with closing(_state()) as s:
        row = s.execute("SELECT body, created FROM cache WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[1] > time.time() - CACHE_TTL else None


def _cache_put(key, body):
    with _state_lock, closing(_state()) as s:
        s.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, json.dumps(body), int(time.time())))
        s.execute("DELETE FROM cache WHERE created<?", (int(time.time()) - CACHE_TTL,))
        s.commit()


# --------------------------------------------------------------------------- llm

def _backend():
    base = (os.getenv("AGENT_TOOLS_A2A_BASE_URL") or os.getenv("AGENT_TOOLS_SAFETY_BASE_URL") or "").rstrip("/")
    key = os.getenv("AGENT_TOOLS_A2A_API_KEY") or os.getenv("AGENT_TOOLS_SAFETY_API_KEY") or ""
    model = os.getenv("AGENT_TOOLS_A2A_MODEL") or "Anthropic/Claude-Opus-5.5"
    return base, key, model


def configured() -> bool:
    base, key, _ = _backend()
    return bool(base and key) and os.getenv("AGENT_TOOLS_A2A_LLM", "1") != "0"


def _chat(messages, max_tokens, tools=True, deadline=None):
    base, key, model = _backend()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if tools:
        body["tools"] = TOOLS
    timeout = 60.0 if deadline is None else max(5.0, min(60.0, deadline - time.time()))
    r = httpx.post(base + "/v1/chat/completions", json=body, timeout=timeout,
                   headers={"Authorization": f"Bearer {key}", "User-Agent": "agent-tools-a2a"})
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- number check

_NUM_RE = re.compile(r"(?<![\w\[])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(%?)")


def _numbers_in(obj, out):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUM_RE.finditer(obj):
            out.add(float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else "")))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out)


def unsupported_numbers(text: str, results: list) -> list[str]:
    """Numbers in text not found in, or derivable (share/difference) from, tool results."""
    known: set[float] = set()
    _numbers_in(results, known)
    base = sorted(v for v in known if v > 0)[:400]
    bad = []
    for m in _NUM_RE.finditer(re.sub(r"\[\d+\]", "", text)):
        whole = m.group(1).replace(",", "")
        val = float(whole + ("." + m.group(2) if m.group(2) else ""))
        if val <= 10 and not m.group(3) and not m.group(2):
            continue  # small counts ("3 gaps", "top 5") are wording, not data
        if 1900 <= val <= 2100 and not m.group(3):
            continue  # years / dates
        tol = max(0.06 * abs(val), 0.51) if m.group(3) else max(0.006 * abs(val), 0.51)
        if any(abs(val - k) <= tol for k in known):
            continue
        if m.group(3) and any(abs(100.0 * a / b - val) <= 0.6 or abs(100.0 * (a - b) / b - val) <= 0.6
                              for a in base for b in base if b and a != b and a <= 50 * b):
            continue
        if not m.group(3) and any(abs((a - b) - val) <= tol for a in base for b in base if a > b):
            continue
        bad.append(m.group(0))
    return bad[:8]


# --------------------------------------------------------------------------- errors

_ERRORS = {
    # reason: (JSON-RPC code, A2A ErrorInfo reason, message)
    "rate_limited": (-32029, "RATE_LIMITED", "Too many analyst requests from this client"),
    "daily_limit": (-32029, "DAILY_LIMIT_REACHED", "Today's analyst requests are used up; they reset at 00:00 UTC"),
    "unavailable": (-32603, "ANALYST_UNAVAILABLE", "The analyst model is unavailable; retry later"),
    "empty_question": (-32602, "INVALID_PARAMS", "params.message must contain a question"),
}


def error(reason: str, retry_after: int | None = None, skill: str | None = None) -> dict:
    code, info_reason, message = _ERRORS[reason]
    meta: dict[str, Any] = {}
    if retry_after:
        meta["retryAfterSeconds"] = int(retry_after)
        message += f" (retry after {int(retry_after)} s)"
    if reason == "rate_limited" and skill:
        meta["limitPerHour"] = SKILLS[skill][2]
    if reason == "daily_limit":
        meta["limitPerDay"] = _daily_cap()
    if reason != "empty_question":
        meta["rawReports"] = ",".join(a2a_research.SKILL_IDS)
    return {"error": (code, message, info_reason, meta)}


# --------------------------------------------------------------------------- agent

_LISTING_URL = re.compile(r"agent-tools\.cloud/(?:services|mcp/servers|a2a/agents)/[\w.~-]+")


def pick_skill(text: str, data: dict | None) -> str:
    s = str((data or {}).get("skill") or "").strip().lower()
    if s in SKILLS:
        return s
    t = (text or "").lower()
    if _LISTING_URL.search(t) or re.search(r"\baudit\b|my (listing|server|agent|api)|改进|评分为什么", t):
        return "listing_audit"
    if re.search(r"opportunit|gap|white ?space|underserved|机会|空白|缺口", t):
        return "market_scan"
    if re.search(r"\breport\b|\bstudy\b|研究报告|调研报告|报告", t):
        return "research_report"
    if re.search(r"\bvs\.?\b|versus|compare|comparison|对比|比较", t):
        return "compare"
    return "ask_analyst"


def _parse_final(content: str) -> dict:
    """Plain-text final reply: answer, then optional SUMMARY:/CLARIFY: lines."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?|\n?```$", "", text).strip()
    clarify = summary = None
    m = re.search(r"^\s*CLARIFY:\s*(.+)$", text, re.M)
    if m:
        clarify = m.group(1).strip()
        text = text[:m.start()].strip()
    m = re.search(r"^\s*SUMMARY:\s*(.*)\Z", text, re.M | re.S)
    if m:
        summary = m.group(1).strip()
        text = text[:m.start()].strip()
    return {"answer": text, "clarify": clarify, "summary": summary}


def run(question: str, *, skill: str = "ask_analyst", context_id: str | None = None,
        ip: str | None = None, detail: str | None = None, budget_s: float = 55.0,
        account: int | None = None) -> dict | None:
    """Answer with the LLM analyst: {text, data}, or {"error": ...} when it cannot run.

    Refusals are errors, never a silent downgrade: the caller decides whether to
    retry later or ask for a raw report.
    """
    if not question.strip():
        return error("empty_question")
    if not configured():
        # No model in this deployment (dev/tests, or AGENT_TOOLS_A2A_LLM=0):
        # the caller serves the template reports. Production always has one.
        log.warning("a2a: LLM analyst not configured; serving template reports")
        return None
    skill = skill if skill in SKILLS else "ask_analyst"
    snap = _snap() or {}
    as_of = snap.get("as_of") or ""
    ctx = resolve_context(context_id, account)
    memory = _get_ctx(ctx["key"]) if ctx["continued"] else None
    key = None if memory else _cache_key(skill, question, as_of, detail or "")
    if key:
        hit = _cache_get(key)
        if hit:  # free: served before any limit
            _record(ip, skill, 0, 0, 0.0, cached=True)
            _put_ctx(ctx["key"], hit.get("summary"))
            return _with_context({"text": hit["text"], "data": dict(hit["data"])}, ctx)
    lim = _limited(ip, skill)
    if lim:
        _record(ip, skill, 0, 0, 0.0, fallback=lim[0])
        return error(lim[0], lim[1], skill)
    max_out, in_cap, _ = SKILLS[skill]
    rule = _SKILL_RULE[skill] + (" " + REPORT_FORMAT if skill in ("research_report", "market_scan") else "")
    user = (f"skill: {skill}.{' ' + rule if rule else ''}\ntoday: {time.strftime('%Y-%m-%d', time.gmtime())}; data as_of: {as_of}\n"
            + (f"conversation so far: {memory}\n" if memory else "") + f"question: {question[:2000]}")
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    results: list[dict] = []        # [{"n", "tool", "args", "result"}]
    tin = tout = 0
    usd = 0.0
    deadline = time.time() + budget_s
    final = None
    try:
        for turn in range(MAX_TURNS + 1):
            last = turn == MAX_TURNS or tin > in_cap or time.time() > deadline - 12
            resp = _chat(messages, max_out, tools=not last, deadline=deadline)
            u = resp.get("usage") or {}
            tin += int(u.get("prompt_tokens") or 0)
            tout += int(u.get("completion_tokens") or 0)
            usd += float(u.get("total_cost") or 0)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls or last:
                final = _parse_final(msg.get("content") or "")
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls[:6]:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = run_tool(c["function"]["name"], args)
                n = len(results) + 1
                results.append({"n": n, "tool": c["function"]["name"], "args": args, "result": out})
                body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
                if len(body) > TOOL_RESULT_CHARS:
                    body = body[:TOOL_RESULT_CHARS] + '..."(truncated)"'
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": f"[{n}] {body}"})
        if final is None:
            raise RuntimeError("no final answer")
        answer = str(final.get("answer") or "").strip()
        clarify = final.get("clarify")
        if clarify and not answer:
            answer = str(clarify)
        if not answer:
            raise RuntimeError(f"empty answer (finish={resp['choices'][0].get('finish_reason')})")
        bad = unsupported_numbers(answer, [r["result"] for r in results]) if results and not clarify else []
        if bad and time.time() < deadline - 8:
            fix = _chat(messages + [{"role": "assistant", "content": answer}, {"role": "user", "content": (
                "These numbers in your answer are not in the tool results: " + ", ".join(bad)
                + ". Rewrite the answer with them corrected or removed. Return only the answer text and the "
                "SUMMARY line; do not mention this correction.")}], max_out, tools=False, deadline=deadline)
            u = fix.get("usage") or {}
            tin += int(u.get("prompt_tokens") or 0)
            tout += int(u.get("completion_tokens") or 0)
            usd += float(u.get("total_cost") or 0)
            fixed = _parse_final(fix["choices"][0]["message"].get("content") or "")
            if fixed.get("answer"):
                final, answer = fixed, str(fixed["answer"]).strip()
                bad = unsupported_numbers(answer, [r["result"] for r in results])
    except Exception:  # noqa: BLE001 - any LLM/transport failure is reported as an error
        log.exception("a2a agent failed (%s)", skill)
        _record(ip, skill, tin, tout, usd, fallback="llm_error")
        return error("unavailable", 60)
    _record(ip, skill, tin, tout, usd)
    _put_ctx(ctx["key"], final.get("summary"))
    cited = sorted({int(x) for x in re.findall(r"\[(\d+)\]", answer)})
    used = [r for r in results if r["n"] in cited] or results
    data: dict[str, Any] = {
        "skill": skill, "as_of": as_of, "license": a2a_research.LICENSE,
        "sources": [{"n": r["n"], "tool": r["tool"], "args": r["args"]} for r in used],
        "usage": {"input_tokens": tin, "output_tokens": tout, "model": _backend()[2]},
    }
    if clarify:
        data["clarify"] = True
    if bad:
        data["unverified_numbers"] = bad
    if str(detail or "").lower() == "full":
        data["results"] = {str(r["n"]): r["result"] for r in used}
    out = {"text": answer, "data": data}
    if key and not clarify:
        _cache_put(key, {**out, "summary": final.get("summary")})
    return _with_context(out, ctx)


def _with_context(out: dict, ctx: dict) -> dict:
    """Attach the conversation id to return and say how memory was handled."""
    info = {"contextId": ctx["id"], "memory": "continued" if ctx["continued"] else "new",
            "scope": ctx["scope"]}
    if ctx.get("replaced"):
        info["note"] = ("contextId replaced: without an API key only ids issued by us continue a "
                        "conversation; send this contextId on your next message")
    out["data"]["context"] = info
    out["context_id"] = ctx["id"]
    return out
