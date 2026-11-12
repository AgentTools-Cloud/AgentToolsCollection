"""Readiness score for MCP servers and x402 paid APIs, plus the extras the
A2A readiness score shares with them.

Same nine criteria and grade bands as the A2A readiness score
(`a2a_readiness`). A criterion with no counterpart for a kind is marked
not applicable and the remaining points are scaled to 100:

* no MCP or x402 standard defines a signed descriptor, so "signature" is n/a
  for both instead of docking every listing 10 points;
* an x402 resource is access-controlled by the payment itself, so "security"
  is n/a for x402;
* an x402 version we have not observed yet is n/a rather than 0: it is a gap
  in our measurement, not a fact about the seller.

Everything is computed from what the directory already measures (probe
columns, probe history, listing sources); no extra network traffic. The
score is display-only: search order and leaderboards keep using
`quality_score`.

Shared extras:

* `responsiveness()` -- latency band (responsive / slow / very slow /
  near-timeout) behind the live-criterion penalty;
* `apply_age_decay()` -- a live result older than 30 days decays linearly to
  40% of its points by 120 days, applied when a stored report is read;
* `payment_evidence()` -- how we learned that a listing accepts payment, six
  grades strongest first; qualifies the claim, never adds points.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from . import a2a_readiness as A

VERSION = "1.0"
WEIGHTS = {
    "valid_card": 10, "live": 25, "protocol_version": 10, "signature": 10,
    "uptime": 15, "skills": 10, "identity": 10, "freshness": 5, "security": 5,
}
ORDER = tuple(WEIGHTS)
LABELS = {
    "mcp": {
        "valid_card": "Valid MCP handshake",
        "live": "Live MCP endpoint",
        "protocol_version": "Protocol revision",
        "signature": "Signed descriptor",
        "uptime": "Uptime (30 days)",
        "skills": "Tool declaration",
        "identity": "Verified identity",
        "freshness": "Freshness + modern flags",
        "security": "Auth declaration",
    },
    "x402": {
        "valid_card": "Valid payment requirements",
        "live": "Live 402 challenge",
        "protocol_version": "x402 version",
        "signature": "Signed descriptor",
        "uptime": "Uptime (30 days)",
        "skills": "Resource declaration",
        "identity": "Verified identity",
        "freshness": "Freshness + modern flags",
        "security": "Security declaration",
    },
}

# MCP specification revisions, oldest first. The last entry must equal
# crawlers.MCP_LATEST_REVISION (a test keeps them in step).
MCP_REVISIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
AUTH_GATED = (401, 402, 403, 407)
DECAY_START_DAYS = 30
DECAY_END_DAYS = 120
DECAY_FLOOR = 0.4

METHODOLOGY = {
    "mcp": (
        "Nine criteria, the same as the A2A readiness score, read from our daily "
        "MCP probe (initialize + tools/list) and 30-day probe history. No MCP "
        "standard defines a signed server descriptor, so that criterion is n/a "
        "and the other 90 points are scaled to 100. Grades: A>=90, B>=75, C>=60, "
        "D>=40, F<40. Display only: search order uses the quality grade."
    ),
    "x402": (
        "Nine criteria, the same as the A2A readiness score, read from our daily "
        "probe of the paid endpoint, its payment requirements and 30-day probe "
        "history. Signed descriptor and security declaration are n/a for x402 "
        "(payment is the access control), as is an x402 version we have not "
        "observed; the remaining points are scaled to 100. Grades: A>=90, B>=75, "
        "C>=60, D>=40, F<40. It also feeds 15 points of the x402 quality grade."
    ),
}

# ---------------------------------------------------------------------------
# Shared extras
# ---------------------------------------------------------------------------

BANDS = (
    (2500, "responsive", "Responsive"),
    (5000, "slow", "Slow"),
    (8000, "very_slow", "Very slow"),
)


def responsiveness(latency_ms) -> dict:
    """Latency band behind the live-criterion penalty (penalty only, no bonus)."""
    if latency_ms is None:
        return {"band": "unmeasured", "label": "Not measured", "latency_ms": None,
                "penalty": 0}
    ms = int(latency_ms)
    for limit, band, label in BANDS:
        if ms <= limit:
            break
    else:
        band, label = "near_timeout", "Near timeout"
    return {"band": band, "label": label, "latency_ms": ms,
            "penalty": A._latency_penalty(ms)}


def decay_factor(age_seconds: float) -> float:
    days = max(0.0, age_seconds / 86400.0)
    if days <= DECAY_START_DAYS:
        return 1.0
    if days >= DECAY_END_DAYS:
        return DECAY_FLOOR
    span = DECAY_END_DAYS - DECAY_START_DAYS
    return 1.0 - (1.0 - DECAY_FLOOR) * (days - DECAY_START_DAYS) / span


_LIVE_KEYS = ("live", "live_jsonrpc")


def _total(criteria: list[dict]) -> tuple[float, float]:
    applicable = [c for c in criteria if c.get("applicable", True)]
    got = sum(float(c["points"]) for c in applicable)
    cap = sum(float(c["max_points"]) for c in applicable)
    return got, cap


def finish(criteria: list[dict], *, kind: str, measured_at: int | None, now: int,
           methodology: str, **extra) -> dict:
    got, cap = _total(criteria)
    score = round(got * 100.0 / cap, 1) if cap else 0.0
    report = {"version": VERSION, "kind": kind, "score": score,
              "grade": A.grade(score), "max_score": 100,
              "applicable_max": cap, "checked_at": int(measured_at or now),
              "criteria": criteria, "methodology": methodology}
    report.update(extra)
    return apply_age_decay(report, now)


def apply_age_decay(report: dict | None, now: int | None = None) -> dict | None:
    """Scale the live criterion of an old measurement; recompute score/grade."""
    if not isinstance(report, dict) or not report.get("criteria"):
        return report
    now = int(now or time.time())
    checked = report.get("checked_at")
    if not checked:
        return report
    age = now - int(checked)
    factor = decay_factor(age)
    report = dict(report)
    report["age_days"] = int(age // 86400)
    if factor >= 1.0:
        return report
    crits = []
    for c in report["criteria"]:
        c = dict(c)
        if c.get("key") in _LIVE_KEYS and c.get("points"):
            base = float(c.get("undecayed_points", c["points"]))
            c["undecayed_points"] = base
            c["points"] = round(base * factor, 2)
            c["detail"] = (str(c.get("detail") or "").rstrip(".") +
                           f". Measured {age // 86400} days ago; live points decay "
                           f"to {int(DECAY_FLOOR * 100)}% by {DECAY_END_DAYS} days.")
        crits.append(c)
    report["criteria"] = crits
    got, cap = _total(crits)
    cap = report.get("applicable_max") or cap or 100
    report["score"] = round(got * 100.0 / cap, 1)
    report["grade"] = A.grade(report["score"])
    report["decay_factor"] = round(factor, 3)
    return report


EVIDENCE_GRADES = (
    ("x402_challenge", "We sent a request and got a live HTTP 402 challenge."),
    ("x402_manifest", "We read the seller's own /.well-known/x402 descriptor."),
    ("agent_card", "The listing's own card or descriptor declares payment."),
    ("bazaar_feed", "A facilitator's discovery feed (CDP Bazaar) lists it."),
    ("catalog_listing", "A third-party catalog says it accepts payment."),
    ("protocol_directory", "An official protocol directory lists it."),
)
_EVIDENCE_TEXT = dict(EVIDENCE_GRADES)
BAZAAR_SOURCES = frozenset({"cdp-bazaar"})
PROTOCOL_DIRECTORY_SOURCES: frozenset = frozenset()
# Our own discovery paths and cross-kind mirrors: not third-party evidence.
FIRST_PARTY_SOURCES = frozenset({
    "submission", "manual", "manual-seed", "owner-migration", "wellknown",
    "safety-scan", "mcp-keepalive", "x402", "a2a", "mcp",
})


def payment_evidence(*, challenge: bool = False, manifest: bool = False,
                     declared: bool = False, sources: Iterable[str] = ()) -> dict | None:
    """Strongest-first evidence that a listing accepts payment, or None."""
    found = []
    if challenge:
        found.append("x402_challenge")
    if manifest:
        found.append("x402_manifest")
    if declared:
        found.append("agent_card")
    srcs = {str(s) for s in sources or ()}
    if srcs & BAZAAR_SOURCES:
        found.append("bazaar_feed")
    if srcs - BAZAAR_SOURCES - PROTOCOL_DIRECTORY_SOURCES - FIRST_PARTY_SOURCES:
        found.append("catalog_listing")
    if srcs & PROTOCOL_DIRECTORY_SOURCES:
        found.append("protocol_directory")
    if not found:
        return None
    best = found[0]
    return {"grade": best, "detail": _EVIDENCE_TEXT[best],
            "self_declared_live": best in ("x402_challenge", "x402_manifest"),
            "evidence": [{"grade": g, "detail": _EVIDENCE_TEXT[g]} for g in found]}


def source_names(sources) -> set[str]:
    out = set()
    for s in sources or ():
        name = s.get("source") if isinstance(s, dict) else s
        if name:
            out.add(str(name))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _crit(kind: str, key: str, points: float, status: str, detail: str, **extra) -> dict:
    c = {"key": key, "label": LABELS[kind][key], "points": round(float(points), 2),
         "max_points": WEIGHTS[key], "status": status, "detail": detail}
    c.update(extra)
    return c


def _na(kind: str, key: str, detail: str) -> dict:
    return _crit(kind, key, 0, "n/a", detail, applicable=False)


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _uptime(kind: str, ok: int, total: int) -> dict:
    c = A.score_uptime(int(ok or 0), int(total or 0))
    c["key"] = "uptime"
    c["label"] = LABELS[kind]["uptime"]
    return c


def _freshness(kind: str, last_seen, flag: bool, flag_note: str, now: int) -> dict:
    pts, notes = 0, []
    if last_seen:
        age = now - int(last_seen)
        if age <= 7 * 86400:
            pts += 4; notes.append("seen in a source within 7 days")
        elif age <= 30 * 86400:
            pts += 2; notes.append("seen within 30 days")
        else:
            notes.append("not seen in a source for over 30 days")
    if flag:
        pts += 1; notes.append(flag_note)
    pts = min(5, pts)
    status = "pass" if pts >= 4 else ("partial" if pts else "fail")
    return _crit(kind, "freshness", pts, status, ("; ".join(notes) or "no freshness signal") + ".")


_NO_PUBLISHER = {
    "mcp": "No source repository, package or homepage declared.",
    "x402": "No /.well-known/x402 descriptor, OpenAPI document or owner declaration on record.",
}


def _identity(kind: str, *, declared: bool, declared_note: str, owner_verified: bool,
              registry_note: str | None = None) -> dict:
    if declared and owner_verified:
        return _crit(kind, "identity", 8, "partial",
                     f"{declared_note} and domain control verified on agent-tools.cloud "
                     "(company-registry check not available yet).")
    if registry_note:
        return _crit(kind, "identity", 7, "partial", registry_note)
    if declared:
        return _crit(kind, "identity", 5, "partial",
                     f"{declared_note}; claim this listing to verify domain control.")
    if owner_verified:
        return _crit(kind, "identity", 3, "partial", "Domain control verified, but no publisher declared.")
    return _crit(kind, "identity", 0, "fail", _NO_PUBLISHER[kind])


def _live(kind: str, state: str, detail: str, latency_ms) -> dict:
    pts = {"live": 25, "seen_before": 20, "auth_gated": 15, "refused": 15,
           "wrong_shape": 5, "unreachable": 0}.get(state, 0)
    band = responsiveness(latency_ms)
    pen = band["penalty"] if pts else 0
    pts = max(0, pts - pen)
    status = {"live": "pass", "seen_before": "partial", "auth_gated": "partial",
              "refused": "partial", "wrong_shape": "partial"}.get(state, "fail")
    if latency_ms is not None:
        detail += f" ({int(latency_ms)} ms)"
    if pen:
        detail += f"; {band['label'].lower()} response -{pen}"
    return _crit(kind, "live", pts, status, detail + ".", state=state)


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

def mcp_revision_points(rev) -> tuple[int, str, str]:
    if not rev:
        return 0, "fail", "No protocol revision reported by initialize."
    rev = str(rev).strip()
    if rev not in MCP_REVISIONS:
        return 3, "partial", f"Reports non-standard revision {rev!r}; use a dated MCP spec revision."
    behind = len(MCP_REVISIONS) - 1 - MCP_REVISIONS.index(rev)
    if behind == 0:
        return 10, "pass", f"Speaks the current MCP revision {rev}."
    pts = {1: 8, 2: 6}.get(behind, 5)
    return pts, "partial", f"Speaks MCP {rev}, {behind} revision(s) behind {MCP_REVISIONS[-1]}."


def _mcp_security(auth) -> dict:
    a = str(auth or "").strip().lower()
    if not a or a in ("none", "null", "unknown"):
        return _crit("mcp", "security", 0, "info", "How to authenticate is not stated.")
    if "mtls" in a or "mutual" in a:
        return _crit("mcp", "security", 5, "pass", "Declares mutual TLS.")
    if "oauth" in a:
        # OAuth 2.1 with PKCE is mandatory in the MCP authorization spec.
        return _crit("mcp", "security", 4, "pass", "Declares OAuth (MCP authorization requires PKCE).")
    if a in ("open", "public"):
        return _crit("mcp", "security", 2, "partial", "Declared open: no credentials needed.")
    return _crit("mcp", "security", 2, "partial", f"Declares credential scheme: {auth}.")


def assess_mcp(row: dict, *, uptime_ok: int = 0, uptime_total: int = 0,
               sources=(), now: int | None = None) -> dict:
    now = int(now or time.time())
    d = dict(row)
    h, sc, conf = d.get("health"), d.get("http_status"), d.get("conformance")
    names = source_names(sources)

    # 1. handshake
    if h == "down" or not conf:
        valid = _crit("mcp", "valid_card", 0, "fail", "No MCP handshake completed.")
    elif conf == "pass":
        valid = _crit("mcp", "valid_card", 10, "pass", "initialize and tools/list both answered.")
    elif conf == "partial":
        valid = _crit("mcp", "valid_card", 7, "partial", "initialize answered; tools/list failed.",
                      findings=["tools/list did not return a tools array"])
    elif sc in AUTH_GATED:
        valid = _crit("mcp", "valid_card", 0, "info",
                      f"Handshake needs credentials (HTTP {sc}); not readable anonymously.")
    else:
        valid = _crit("mcp", "valid_card", 0, "fail",
                      "initialize did not answer cleanly" + (f" (HTTP {sc})" if sc else "") + ".")

    # 2. live
    if h == "ok" and sc in AUTH_GATED:
        state, text = "auth_gated", "Endpoint requires authentication or payment"
    elif h == "ok":
        state, text = "live", "Endpoint answers the MCP initialize handshake"
    elif h == "degraded" and sc == 429:
        state, text = "refused", "Endpoint rate-limited our probe"
    elif h == "degraded":
        state, text = "wrong_shape", "Endpoint answers, but not with MCP JSON-RPC"
    else:
        state, text = "unreachable", "Endpoint did not answer"
    latency = d.get("latency_p95_ms") or d.get("latency_ms")
    live = _live("mcp", state, text, latency if h in ("ok", "degraded") else None)

    # 3. revision
    pts, st, txt = mcp_revision_points(d.get("protocol_version"))
    proto = _crit("mcp", "protocol_version", pts, st, txt)

    # 5. uptime
    up = _uptime("mcp", uptime_ok, uptime_total)

    # 6. tools
    tools = d.get("tools")
    if tools is None:
        tools = _json(d.get("tools_json"))
    tools = [t for t in tools if isinstance(t, dict) and t.get("name")] if isinstance(tools, list) else []
    n = len(tools) or int(d.get("tool_count") or 0)
    if n == 0:
        skills = _crit("mcp", "skills", 0, "fail", "No tools listed by tools/list.")
    else:
        pts = 10 if n >= 3 else 5
        bare = [t for t in tools if not str(t.get("description") or "").strip()]
        if bare and pts > 8:
            skills = _crit("mcp", "skills", 8, "partial",
                           f"{n} tools; {len(bare)} lack a description.")
        else:
            skills = _crit("mcp", "skills", pts, "pass" if pts == 10 else "partial",
                           f"Lists {n} tool(s) with descriptions.")

    # 7. identity
    declared = bool(d.get("source_code_url") or d.get("package_name") or d.get("homepage_url"))
    registry = ("Namespace verified by the official MCP Registry (GitHub or DNS "
                "proof at publish time).") if "mcp-registry" in names else None
    ident = _identity("mcp", declared=declared,
                      declared_note="Publisher declared (repository, package or homepage)",
                      owner_verified=bool(d.get("owner_verified")), registry_note=registry)

    # 8. freshness
    pricing = d.get("pricing")
    priced = bool(d.get("x402_supported")) or (isinstance(pricing, dict) and bool(pricing)) or (
        isinstance(pricing, str) and pricing.strip() not in ("", "null", "{}"))
    fresh = _freshness("mcp", d.get("last_seen"), priced,
                       "declares x402 or machine-readable pricing", now)

    criteria = [
        valid, live, proto,
        _na("mcp", "signature", "No MCP standard defines a signed server descriptor."),
        up, skills, ident, fresh, _mcp_security(d.get("auth_method")),
    ]
    evidence = None
    if d.get("x402_supported") or sc == 402:
        evidence = payment_evidence(challenge=sc == 402, declared=priced, sources=names)
    return finish(criteria, kind="mcp", measured_at=d.get("health_checked"), now=now,
                  methodology=METHODOLOGY["mcp"],
                  responsiveness=responsiveness(latency if h in ("ok", "degraded") else None),
                  payment_evidence=evidence)


# ---------------------------------------------------------------------------
# x402 paid APIs
# ---------------------------------------------------------------------------

def _samples(value) -> list[dict]:
    v = _json(value)
    if isinstance(v, dict):
        v = v.get("resource_samples")
    return [s for s in v if isinstance(s, dict)] if isinstance(v, list) else []


def assess_x402(row: dict, *, uptime_ok: int = 0, uptime_total: int = 0,
                last_402_at: int | None = None, sources=(), now: int | None = None) -> dict:
    from . import db as _db
    now = int(now or time.time())
    d = dict(row)
    h, sc = d.get("health"), d.get("http_status")
    names = source_names(sources)
    pay = _json(d.get("payment"))

    # 1. payment requirements
    if not isinstance(pay, dict) or not pay:
        valid = _crit("x402", "valid_card", 0, "fail", "No payment requirements on record.")
    else:
        parts = _db._payability_parts(d)
        wanted = ("Payment address on record", "Settlement network declared", "Price declared")
        findings = [f"missing: {label.lower()}" for label, _w, ok in parts
                    if label in wanted and not ok]
        if findings:
            valid = _crit("x402", "valid_card", 10 - min(3, len(findings)), "partial",
                          "Findings: " + "; ".join(findings) + ".", findings=findings)
        else:
            valid = _crit("x402", "valid_card", 10, "pass",
                          "Payment requirements carry a payee, a mainnet network and a price.")

    # 2. live
    recent_402 = bool(last_402_at) and now - int(last_402_at) <= 30 * 86400
    if h == "ok" and (sc == 402 or recent_402):
        state, text = "live", "Paid endpoint answers with an HTTP 402 challenge"
    elif h == "ok" and d.get("x402_ok"):
        state, text = "seen_before", "Endpoint reachable; its 402 challenge was seen earlier"
    elif h == "ok":
        state, text = "wrong_shape", "Endpoint answers, but we have not seen a 402 challenge"
    elif h == "degraded" and sc == 429:
        state, text = "refused", "Endpoint rate-limited our probe"
    elif h == "degraded" and sc in AUTH_GATED:
        state, text = "auth_gated", "Endpoint requires credentials beyond payment"
    elif h == "degraded":
        state, text = "wrong_shape", "Endpoint answers with an error"
    else:
        state, text = "unreachable", "Endpoint did not answer"
    latency = d.get("latency_ms") if h in ("ok", "degraded") else None
    live = _live("x402", state, text, latency)

    # 3. x402 version
    versions = {s.get("x402_version") for s in _samples(d.get("resource_samples"))}
    versions = {int(v) for v in versions if isinstance(v, int) or (isinstance(v, str) and v.isdigit())}
    if not versions:
        proto = _na("x402", "protocol_version", "x402 version not observed yet.")
    elif max(versions) >= 2:
        proto = _crit("x402", "protocol_version", 10, "pass", "Speaks x402 v2.")
    else:
        proto = _crit("x402", "protocol_version", 5, "partial", "Speaks x402 v1; v2 is current.")

    # 5. uptime
    up = _uptime("x402", uptime_ok, uptime_total)

    # 6. resources
    paid = [s for s in _samples(d.get("resource_samples"))
            if str(s.get("kind") or "x402-resource").startswith("x402")]
    n = int(d.get("resource_count") or 0) or len(paid) or (1 if isinstance(pay, dict) and pay else 0)
    if n == 0:
        skills = _crit("x402", "skills", 0, "fail", "No paid resource declared.")
    else:
        pts = 10 if n >= 3 else 5
        bare = [s for s in paid if not str(s.get("description") or "").strip()]
        if bare and pts > 8:
            skills = _crit("x402", "skills", 8, "partial",
                           f"{n} paid resources; {len(bare)} sampled lack a description.")
        else:
            skills = _crit("x402", "skills", pts, "pass" if pts == 10 else "partial",
                           f"Declares {n} paid resource(s).")

    # 7. identity
    manifest = bool(d.get("descriptor_checked_at")) and bool(d.get("well_known_url"))
    declared = manifest or bool(d.get("openapi_url")) or bool(d.get("owner_declaration"))
    ident = _identity("x402", declared=declared,
                      declared_note="Publisher descriptor (/.well-known/x402 or OpenAPI) on record",
                      owner_verified=bool(d.get("owner_verified")))

    # 8. freshness
    fresh = _freshness("x402", d.get("last_seen"), True, "declares x402", now)

    criteria = [
        valid, live, proto,
        _na("x402", "signature", "No x402 standard defines a signed descriptor."),
        up, skills, ident, fresh,
        _na("x402", "security", "Payment is the access control for an x402 resource."),
    ]
    evidence = payment_evidence(challenge=bool(d.get("x402_ok")) or sc == 402,
                                manifest=manifest, sources=names)
    return finish(criteria, kind="x402", measured_at=d.get("health_checked"), now=now,
                  methodology=METHODOLOGY["x402"], responsiveness=responsiveness(latency),
                  payment_evidence=evidence)


# ---------------------------------------------------------------------------
# Probe history (uptime) and per-row entry points
# ---------------------------------------------------------------------------

# A probe the host refused describes our crawler's access, not the listing's
# availability: leave out 429s, and 403s that did not count as up.
REFUSED_SQL = "NOT (COALESCE(http_status,0)=429 OR (COALESCE(http_status,0)=403 AND status!='ok'))"


def is_refused(status: str | None, http_status) -> bool:
    return http_status == 429 or (http_status == 403 and status != "ok")


def history_mcp(conn, server_id=None, now: int | None = None) -> dict:
    now = int(now or time.time())
    sql = ("SELECT server_id, SUM(status='ok') ok, COUNT(*) n FROM mcp_health_history "
           f"WHERE checked_at >= ? AND {REFUSED_SQL}")
    args: list = [now - A.UPTIME_WINDOW]
    if server_id is not None:
        sql += " AND server_id=?"; args.append(int(server_id))
    sql += " GROUP BY server_id"
    return {r[0]: (r[1] or 0, r[2] or 0) for r in conn.execute(sql, args)}


def history_x402(conn, service_id=None, now: int | None = None) -> dict:
    now = int(now or time.time())
    sql = ("SELECT service_id, SUM(status='ok') ok, COUNT(*) n, "
           "MAX(CASE WHEN x402=1 THEN checked_at END) last402 FROM health_history "
           f"WHERE checked_at >= ? AND {REFUSED_SQL}")
    args: list = [now - A.UPTIME_WINDOW]
    if service_id is not None:
        sql += " AND service_id=?"; args.append(int(service_id))
    sql += " GROUP BY service_id"
    return {r[0]: (r[1] or 0, r[2] or 0, r[3]) for r in conn.execute(sql, args)}


def for_mcp_row(conn, row: dict, now: int | None = None) -> dict:
    ok, n = history_mcp(conn, row["id"], now).get(row["id"], (0, 0))
    sources = row.get("sources")
    if sources is None:
        sources = [r[0] for r in conn.execute(
            "SELECT source FROM listing_sources WHERE kind='mcp' AND listing_id=?", (row["id"],))]
    return assess_mcp(row, uptime_ok=ok, uptime_total=n, sources=sources, now=now)


def for_x402_row(conn, row: dict, now: int | None = None) -> dict:
    ok, n, last402 = history_x402(conn, row["id"], now).get(row["id"], (0, 0, None))
    sources = row.get("sources")
    if sources is None:
        sources = [r[0] for r in conn.execute(
            "SELECT source FROM listing_sources WHERE kind='x402' AND listing_id=?", (row["id"],))]
    return assess_x402(row, uptime_ok=ok, uptime_total=n, last_402_at=last402,
                       sources=sources, now=now)


def sources_by_listing(conn, kind: str) -> dict:
    out: dict = {}
    for lid, src in conn.execute(
            "SELECT listing_id, source FROM listing_sources WHERE kind=?", (kind,)):
        out.setdefault(lid, set()).add(src)
    return out
