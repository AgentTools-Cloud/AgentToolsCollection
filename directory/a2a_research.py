"""Research skills of our own A2A agent: data reports on the agent-tool ecosystem.

The MCP server (/mcp-discovery) is the tool surface: search, cards, safety
scans, submissions, claims. The A2A agent is the analyst: it answers with
reports built from our daily probe snapshots, so the two do not overlap.

Every report is `{"text": <one-paragraph answer>, "data": <JSON>}`; the data
carries `as_of`, `links` and the CC BY 4.0 licence so it can be cited.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from . import db

log = logging.getLogger("directory.a2a_research")

SITE = "https://agent-tools.cloud"
LICENSE = "CC BY 4.0"
TREND_DAYS = (7, 30, 90)
KIND_PATHS = {"x402": "/services/", "mcp": "/mcp/servers/", "a2a": "/a2a/agents/"}
SEARCH_SURFACES = {
    "mcp": f"{SITE}/mcp-discovery/",
    "rest_search": f"{SITE}/api/v1/resources/search?q=<intent>",
    "safety_scan": "MCP tool scan_mcp_safety",
}

# Card text is read by every client that discovers us: keep it short.
# Each skill: one sentence, its data-part shape, one example. Answers are
# brief by default; add "detail": "full" for the whole report.
def _skill(sid, name, desc, data, example, tags):
    return {"id": sid, "name": name, "description": f"{desc} Data part: {data}.",
            "tags": ["research", *tags], "examples": [example]}


SKILLS: list[dict] = [
    _skill("ecosystem_report", "Ecosystem report",
           "Indexed, reachable, verified and agent-ready counts for x402 APIs, MCP servers and A2A agents.",
           '{"skill":"ecosystem_report","view":"all|mcp|x402|a2a"}',
           "what is the state of agent tools today?", ["ecosystem"]),
    _skill("trend_report", "Trend report",
           "Change in those counts over 7, 30 or 90 days.",
           '{"skill":"trend_report","view":"mcp","days":30}',
           "how did MCP servers grow over the last 30 days?", ["trend"]),
    _skill("topic_report", "Topic report",
           "Supply, readiness, best listings and gaps for one topic (payment, weather, finance...).",
           '{"skill":"topic_report","topic":"payment"}',
           "topic report on payment tools", ["market"]),
    _skill("monthly_report", "Monthly report",
           "Start vs end of one month: KPIs, new listings, prices, protocol versions.",
           '{"skill":"monthly_report","month":"YYYY-MM"}',
           "monthly report for 2026-11", ["monthly"]),
    _skill("usability_report", "How many agent tools work",
           "Share that answer, pass a protocol check and are ready to call, and why the rest fail.",
           '{"skill":"usability_report"}',
           "how many agent tools actually work?", ["reliability"]),
    _skill("search_demand_report", "Search demand",
           "Top searches in the last 30 days and searches that found nothing.",
           '{"skill":"search_demand_report"}',
           "what do agents search for?", ["demand"]),
    _skill("listing_benchmark", "Listing benchmark",
           "One listing vs its peers: readiness and latency percentiles, biggest gaps and fixes.",
           '{"skill":"listing_benchmark","kind":"mcp|x402|a2a","slug":"..."} or a listing URL',
           "benchmark https://agent-tools.cloud/mcp/servers/<slug>", ["benchmark"]),
]
SKILL_IDS = tuple(s["id"] for s in SKILLS)
# Skills the old card advertised that now belong to the MCP server.
_TOOL_SKILLS = frozenset({"search_x402_services", "search_a2a_agents", "search_mcp_servers",
                          "get_service", "get_agent", "recommend_paid_service", "scan_mcp_safety"})
_LEGACY = {"directory_stats": "ecosystem_report", "stats": "ecosystem_report",
           "statistics": "ecosystem_report", "ecosystem_stats": "ecosystem_report"}

_VIEW_RE = {"mcp": re.compile(r"\bmcp\b", re.I), "x402": re.compile(r"\bx402\b", re.I),
            "a2a": re.compile(r"\ba2a\b", re.I)}
_MONTH_RE = re.compile(r"\b(20\d\d-(?:0[1-9]|1[0-2]))\b")
_DAYS_RE = re.compile(r"\b(\d{1,3})\s*days?\b", re.I)
_LISTING_URL_RE = re.compile(
    r"https?://(?:www\.)?agent-tools\.cloud(/services/|/mcp/servers/|/a2a/agents/)([A-Za-z0-9._~-]+)")
_INTENTS = (
    ("search_demand_report", re.compile(
        r"search(?:es)? demand|most searched|what (?:do|did) (?:agents|people|users) search|"
        r"\bunmet\b|in demand|missing tools?", re.I)),
    ("usability_report", re.compile(
        r"actually work|\busable\b|usability|why (?:do|does|are|is)\b.*\b(?:fail|down|broken|unreachable)|"
        r"failure|reliab|uptime", re.I)),
    ("trend_report", re.compile(
        r"\btrend|\bgrow|growth|\bgrew\b|change[ds]? (?:over|in|since)|over the (?:last|past)|"
        r"last \d+ days|past (?:week|month|quarter)|time series", re.I)),
    ("listing_benchmark", re.compile(r"benchmark|compare my|peer|how does .* (?:rank|compare)", re.I)),
    ("monthly_report", re.compile(r"monthly|month report|report for (?:january|february|march|april|"
                                  r"may|june|july|august|september|october|november|december)", re.I)),
)
# Asking us to find, recommend or vet something is the MCP server's job.
_TOOL_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:find|search(?: for)?|look(?:ing)? for|recommend|suggest|get me|"
    r"show me (?:the )?card|scan|is https?://|i need (?:an?|some) )", re.I)


def _snap():
    from . import stats
    return stats.latest(lambda: db.connect(read_only=True))


def _view_from_text(text: str) -> str:
    hits = [k for k, rx in _VIEW_RE.items() if rx.search(text or "")]
    return hits[0] if len(hits) == 1 else "all"


def _topic_from_text(text: str) -> str | None:
    from . import stats
    low = (text or "").lower()
    for t in stats.TOPICS:
        if t in ("search", "data") or t in ("ai",):
            continue  # too generic in questions ("search demand", "data report")
        if re.search(r"\b" + re.escape(t) + r"s?\b", low):
            return t
    m = re.search(r"\btopic\s+[\"']?([a-z]+)", low)
    return m.group(1) if m and m.group(1) in stats.TOPICS else None


def parse_request(message: dict, text: str) -> dict | None:
    """Map a message to {"skill": ..., params}; None when it asks for nothing."""
    for p in (message.get("parts") or []):
        if isinstance(p, dict) and isinstance(p.get("data"), dict):
            d = p["data"]
            skill = str(d.get("skill") or d.get("id") or d.get("intent") or "").strip().lower()
            if skill in _LEGACY:
                if d.get("topic"):
                    skill = "topic_report"
                elif d.get("month"):
                    skill = "monthly_report"
                else:
                    skill = _LEGACY[skill]
            if skill in _TOOL_SKILLS or "scan" in skill or "safety" in skill:
                return {"skill": "tool_request", "requested": skill[:60]}
            if skill in SKILL_IDS:
                req = {"skill": skill}
                for key in ("view", "topic", "month", "days", "kind", "slug", "url", "detail"):
                    if d.get(key) not in (None, ""):
                        req[key] = d[key]
                return req
            if skill:
                return {"skill": "unknown", "requested": skill[:60]}
    if not text:
        return None
    req = _parse_text(text)
    if _FULL_RE.search(text) and req.get("skill") in SKILL_IDS:
        req["detail"] = "full"
    return req


def _parse_text(text: str) -> dict:
    m = _LISTING_URL_RE.search(text)
    if m:
        kind = {"/services/": "x402", "/mcp/servers/": "mcp", "/a2a/agents/": "a2a"}[m.group(1)]
        return {"skill": "listing_benchmark", "kind": kind, "slug": m.group(2)}
    if _TOOL_REQUEST_RE.search(text):
        return {"skill": "tool_request"}
    for skill, rx in _INTENTS:
        if rx.search(text):
            req: dict[str, Any] = {"skill": skill, "view": _view_from_text(text)}
            if skill == "trend_report":
                dm = _DAYS_RE.search(text)
                req["days"] = (int(dm.group(1)) if dm else
                               7 if re.search(r"week", text, re.I) else
                               90 if re.search(r"quarter|90", text, re.I) else 30)
            if skill == "monthly_report":
                mm = _MONTH_RE.search(text)
                if mm:
                    req["month"] = mm.group(1)
            return req
    mm = _MONTH_RE.search(text)
    if mm:
        return {"skill": "monthly_report", "month": mm.group(1)}
    topic = _topic_from_text(text)
    if topic:
        return {"skill": "topic_report", "topic": topic}
    return {"skill": "ecosystem_report", "view": _view_from_text(text)}


def _view(v) -> str:
    v = str(v or "all").strip().lower()
    return v if v in ("all", "mcp", "x402", "a2a") else "all"


def _cite(data: dict) -> dict:
    data.setdefault("license", LICENSE)
    data.setdefault("methodology", f"{SITE}/api/v1/methodology")
    return data


def ecosystem_report(view="all") -> dict:
    from . import stats as stats_mod
    view = _view(view)
    out = stats_mod.summary(_snap(), view)
    if not out.get("available"):
        return {"text": out.get("note") or "No statistics yet.", "data": out}
    k = out["kpis"][out["view"]]
    scope = "x402 APIs, MCP servers and A2A agents" if view == "all" else stats_mod.KIND_LABEL[view]
    lines = [f"As of {out['as_of']}, across {scope}: {k['indexed']:,} indexed, "
             f"{k['reachable']:,} reachable, {k['verified']:,} protocol-verified, "
             f"{k['ready']:,} graded A or B for readiness."]
    for key in ("funnel", "x402", "protocols", "access"):
        if out["findings"].get(key) and (view == "all" or key != "x402" or view == "x402"):
            lines.append(out["findings"][key])
    lines.append(f"Full data: {stats_mod.LINKS['snapshot_json']} ({LICENSE}).")
    return {"text": " ".join(lines), "data": _cite(out)}


def trend_report(view="all", days=30) -> dict:
    from . import stats as stats_mod
    view = _view(view)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = min(TREND_DAYS, key=lambda d: abs(d - days))
    with db.connect(read_only=True) as conn:
        hist = db.stats_history(conn, days + 1)
    def scoped(k):
        if isinstance(k.get(view), dict):
            return k[view]
        kinds = [k.get(x) for x in stats_mod.KINDS]
        if view == "all" and all(isinstance(x, dict) for x in kinds):
            return {m: sum(x[m] for x in kinds) for m in ("indexed", "reachable", "verified", "ready")
                    if all(x.get(m) is not None for x in kinds)}
        return None

    rows = [(d, scoped(k), bool(k.get("backfill"))) for d, k in hist if scoped(k) is not None]
    if len(rows) < 2:
        return {"text": "Not enough daily snapshots yet for a trend.",
                "data": _cite({"available": False, "view": view, "days": days})}
    metrics = []
    for m in ("indexed", "reachable", "verified", "ready"):
        have = [(d, k[m], bf) for d, k, bf in rows if k.get(m) is not None]
        if len(have) < 2:
            continue
        (sd, a, sbf), (ed, b, _) = have[0], have[-1]
        metrics.append({"metric": m, "start_day": sd, "start": a, "end_day": ed, "end": b,
                        "change": b - a, "start_rebuilt": sbf,
                        "change_pct": round(100.0 * (b - a) / a, 1) if a else None})
    if not metrics:
        return {"text": "Not enough daily snapshots yet for a trend.",
                "data": _cite({"available": False, "view": view, "days": days})}
    d0 = min(x["start_day"] for x in metrics)
    d1 = max(x["end_day"] for x in metrics)
    series = [{"day": d, "rebuilt": bf,
               **{m: k.get(m) for m in ("indexed", "reachable", "verified", "ready")}}
              for d, k, bf in rows]
    scope = "all listings" if view == "all" else stats_mod.KIND_LABEL[view]
    parts = [f"{x['metric']} {x['start']:,} → {x['end']:,} ({x['change']:+,}"
             + (f", {x['change_pct']:+}%" if x["change_pct"] is not None else "")
             + (f" since {x['start_day']}" if x["start_day"] != d0 else "") + ")"
             for x in metrics]
    text = f"From {d0} to {d1} for {scope}: " + "; ".join(parts) + "."
    if any(x["start_rebuilt"] for x in metrics):
        text += " Days before our first full snapshot are rebuilt from raw probes (reachable only)."
    return {"text": text, "data": _cite({
        "available": True, "view": view, "days": days, "from": d0, "to": d1,
        "metrics": metrics, "series": series,
        "links": {"page": f"{SITE}/stats", "daily_csv": f"{SITE}/stats/daily.csv"}})}


def topic_report(topic=None) -> dict:
    from . import stats as stats_mod
    topic = str(topic or "").strip().lower()
    out = stats_mod.topic_page(_snap(), topic) if topic else None
    if out is None:
        return {"text": f"Unknown topic '{topic}'. Topics: {', '.join(stats_mod.TOPICS)}.",
                "data": {"error": "unknown_topic", "topics": list(stats_mod.TOPICS)}}
    return {"text": out["answer"], "data": _cite(out)}


def monthly_report(month=None) -> dict:
    from . import stats as stats_mod
    with db.connect(read_only=True) as conn:
        available = stats_mod.months(conn)
        month = str(month or (available[0] if available else "")).strip()[:7]
        try:
            out = stats_mod.monthly_report(conn, month)
        except Exception:  # noqa: BLE001
            log.exception("a2a research: monthly report %s failed", month)
            out = None
    if out is None:
        return {"text": f"No report for '{month}'. Months: {', '.join(available) or 'none yet'}.",
                "data": {"error": "unknown_month", "months": available,
                         "reports": f"{SITE}/stats/reports.json"}}
    return {"text": out["answer"], "data": _cite(out)}


def usability_report() -> dict:
    from . import stats as stats_mod
    out = stats_mod.usable(_snap())
    if not out.get("available"):
        return {"text": "No statistics yet.", "data": out}
    return {"text": out["answer"], "data": _cite(out)}


def search_demand_report() -> dict:
    from . import stats as stats_mod
    snap = _snap()
    sd = stats_mod.searches_view(snap, full=False)
    if not sd or sd.get("available") is False:
        return {"text": "No search statistics yet.", "data": {"available": False}}
    top = ", ".join(f"{i['term']} ({i['searches']})" for i in (sd.get("top") or [])[:5] if "term" in i)
    unmet = ", ".join(i["term"] for i in (sd.get("unmet") or [])[:5] if "term" in i)
    text = (f"Most searched over the last {sd.get('days', 30)} days: {top or 'nothing above the threshold'}."
            + (f" Searches that found nothing: {unmet}." if unmet else ""))
    data = {"as_of": (snap or {}).get("as_of"), **sd,
            "links": {"page": f"{SITE}/stats/searches", "json": f"{SITE}/stats/searches.json"}}
    return {"text": text, "data": _cite(data)}


def listing_benchmark(kind=None, slug=None, url=None) -> dict:
    if url and not slug:
        m = _LISTING_URL_RE.search(str(url))
        if m:
            kind = {"/services/": "x402", "/mcp/servers/": "mcp", "/a2a/agents/": "a2a"}[m.group(1)]
            slug = m.group(2)
    kind = str(kind or "").strip().lower()
    slug = str(slug or "").strip()
    if kind not in KIND_PATHS or not slug:
        return {"text": "Give kind (mcp, x402 or a2a) and slug, or a listing URL on agent-tools.cloud.",
                "data": {"error": "listing_required"}}
    getter = {"x402": db.get_by_slug, "mcp": db.get_mcp_by_slug, "a2a": db.get_a2a_by_slug}[kind]
    with db.connect(read_only=True) as conn:
        row = getter(conn, slug)
    if not row:
        return {"text": f"No {kind} listing '{slug}'.", "data": {"error": "not_found"}}
    peer = row.get("peer")
    readiness = row.get("readiness") if isinstance(row.get("readiness"), dict) else {}
    grade = readiness.get("grade") or row.get("readiness_grade")
    score = readiness.get("score", row.get("readiness_score"))
    name = row.get("name") or slug
    if not peer:
        text = f"{name}: no peer statistics yet."
    else:
        bits = []
        r = peer.get("readiness")
        if r:
            bits.append(f"readiness {r['score']} (peer median {r['peer_median']}), "
                        f"better than {r['better_than_pct']}% of {peer['peers']:,} {peer['peer_group']}")
        lat = peer.get("latency")
        if lat:
            bits.append(f"latency {lat['ms']} ms (median {lat['peer_median_ms']} ms), "
                        f"faster than {lat['faster_than_pct']}%")
        gaps = "; ".join(f"{g['label']}: {g['fix']}" for g in peer.get("biggest_gaps") or [] if g.get("fix"))
        text = f"{name}: " + ("; ".join(bits) or "no comparable measurements") + "."
        if gaps:
            text += f" Biggest gaps — {gaps}"
    listing_url = SITE + KIND_PATHS[kind] + slug
    return {"text": text, "data": _cite({
        "kind": kind, "slug": slug, "name": name, "readiness_grade": grade,
        "readiness_score": score, "peer": peer,
        "links": {"page": listing_url, "readiness": listing_url + "#readiness"}})}


def _menu() -> list[dict]:
    return [{"id": s["id"], "example": s["examples"][0]} for s in SKILLS]


# --- Response budget --------------------------------------------------------
# Callers are agents paying for every token they read. The default ("brief")
# answer is the one-paragraph text plus only the numbers behind it and one or
# two links (~100-400 tokens). {"detail": "full"} returns the whole report;
# the full JSON is always one GET away via data.links.
_DROP_BRIEF = ("answer", "question", "definitions", "sign_in", "generated_at", "available",
               "full", "more_with_account", "methodology", "facts", "findings", "series",
               "by_channel", "searches")
_FULL_RE = re.compile(r"\b(?:full|detailed|complete|all)\s+(?:data|report|details?|json)\b", re.I)


def _pick(row: dict, keys) -> dict:
    return {k: row[k] for k in keys if k in row}


def _links(links, keep=("page", "json", "snapshot_json")) -> dict | None:
    if not isinstance(links, dict):
        return None
    out = {k: links[k] for k in keep if k in links}
    return out or None


def compact(skill: str, data: dict) -> dict:
    """Trim a report's data part to what backs its text (see _DROP_BRIEF)."""
    if not isinstance(data, dict) or data.get("error"):
        return data
    out = {k: v for k, v in data.items() if k not in _DROP_BRIEF}
    if skill == "ecosystem_report":
        kpis = out.get("kpis") or {}
        view = out.get("view") or "all"
        if view != "all" and view in kpis:
            out["kpis"] = {view: kpis[view]}
    elif skill == "topic_report":
        out["top"] = [_pick(r, ("kind", "name", "grade", "readiness")) for r in (out.get("top") or [])[:5]]
        out["by_kind"] = [_pick(r, ("kind", "reachable", "ready", "pct"))
                          for r in out.get("by_kind") or []]
        if data.get("searches"):
            out["searched"] = sum(int(s.get("searches") or 0) for s in data["searches"])
    elif skill == "usability_report":
        out["by_protocol"] = [_pick(r, ("kind", "reachable_pct", "verified_pct", "ready_pct",
                                        "median_latency_ms")) for r in out.get("by_protocol") or []]
        out["why_not"] = {k: [_pick(r, ("label", "pct")) for r in v
                              if not str(r.get("label", "")).startswith("Answered without")][:3]
                          for k, v in (out.get("why_not") or {}).items() if isinstance(v, list)}
    elif skill == "search_demand_report":
        out["searches"] = data.get("searches")
        out["top"] = [[r.get("term"), r.get("searches")] for r in (out.get("top") or [])[:10]]
        out["unmet"] = [r.get("term") for r in (out.get("unmet") or [])[:10]]
    elif skill == "listing_benchmark" and isinstance(out.get("peer"), dict):
        peer = dict(out["peer"])
        peer.pop("as_of", None)
        peer["biggest_gaps"] = [_pick(g, ("label", "fix")) for g in (peer.get("biggest_gaps") or [])[:3]]
        out["peer"] = peer
    links = _links(data.get("links"))
    if links:
        out["links"] = links
    else:
        out.pop("links", None)
    out["more"] = {"detail": "full"}
    return out


def respond(req: dict, detail: str | None = None) -> dict:
    """answer() plus the response budget: brief unless detail == "full"."""
    out = answer(req)
    full = str(detail or req.get("detail") or "").lower() == "full"
    if not full and req.get("skill") in SKILL_IDS and isinstance(out.get("data"), dict):
        out = {**out, "data": compact(req["skill"], out["data"])}
    return out


def answer(req: dict) -> dict:
    skill = req.get("skill")
    try:
        if skill == "ecosystem_report":
            return ecosystem_report(req.get("view"))
        if skill == "trend_report":
            return trend_report(req.get("view"), req.get("days", 30))
        if skill == "topic_report":
            return topic_report(req.get("topic"))
        if skill == "monthly_report":
            return monthly_report(req.get("month"))
        if skill == "usability_report":
            return usability_report()
        if skill == "search_demand_report":
            return search_demand_report()
        if skill == "listing_benchmark":
            return listing_benchmark(req.get("kind"), req.get("slug"), req.get("url"))
    except Exception:  # noqa: BLE001 - one failing report must not break the endpoint
        log.exception("a2a research: %s failed", skill)
        return {"text": "This report is unavailable right now.", "data": {"error": "report_failed"}}
    if skill == "tool_request":
        return {"text": ("I write data reports on the agent-tool ecosystem. To find, compare or "
                         f"safety-scan a tool, use our MCP server at {SEARCH_SURFACES['mcp']} or "
                         f"GET {SEARCH_SURFACES['rest_search']}."),
                "data": {"reports": _menu(), "tools": SEARCH_SURFACES}}
    return {"text": f"Unknown skill '{req.get('requested', '')}'. Skills: {', '.join(SKILL_IDS)}.",
            "data": {"error": "unknown_skill", "reports": _menu()}}
