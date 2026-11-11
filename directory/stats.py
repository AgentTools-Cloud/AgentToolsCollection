"""Directory-wide statistics for the public /stats page.

`compute(conn)` builds one JSON-serialisable snapshot from what the daily
probes already stored (listing tables, probe history, listing sources). The
daily job stores it in `stats_snapshots`; the page and the JSON/CSV endpoints
only read stored snapshots, so a request never scans the catalogue.

Every figure is a count over our own index at the stated time. Nothing here is
an ecosystem-wide total, a revenue figure or an endorsement.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict

VERSION = 1
KINDS = ("x402", "mcp", "a2a")
KIND_LABEL = {"x402": "x402 APIs", "mcp": "MCP servers", "a2a": "A2A agents"}
TABLE = {"x402": "services", "mcp": "mcp_servers", "a2a": "a2a_agents"}
GRADES = ("A", "B", "C", "D", "F")
REFUSED = ("NOT (COALESCE(http_status,0)=429 OR "
           "(COALESCE(http_status,0)=403 AND status!='ok'))")
MCP_REVISIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TOPICS = (
    "search", "web", "data", "database", "finance", "payment", "crypto",
    "wallet", "trading", "blockchain", "weather", "maps", "github", "code",
    "deploy", "ai", "llm", "image", "video", "audio", "pdf", "document",
    "email", "calendar", "news", "social", "automation", "security",
    "analytics", "translation", "knowledge",
)
CATALOGUE_TOPICS = 5
_TOPIC_RE = {t: re.compile(r"\b" + re.escape(t) + r"s?\b") for t in TOPICS}
CHAIN_NAMES = {
    "eip155:8453": "Base", "base": "Base", "eip155:1": "Ethereum", "ethereum": "Ethereum",
    "eip155:137": "Polygon", "polygon": "Polygon", "eip155:42161": "Arbitrum",
    "arbitrum": "Arbitrum", "eip155:10": "Optimism", "optimism": "Optimism",
    "eip155:43114": "Avalanche", "avalanche": "Avalanche", "eip155:56": "BNB Chain",
    "bsc": "BNB Chain", "eip155:196": "X Layer", "xlayer": "X Layer",
    "solana": "Solana", "eip155:5042": "Arc", "stripe": "Stripe", "sui": "Sui",
}
# Internal source ids that are our own crawls, not an outside directory.
SOURCE_LABELS = {
    "mcp": "own crawl of MCP listings", "a2a": "own crawl of A2A cards",
    "x402": "own x402 crawl", "wellknown": "own /.well-known crawl",
    "submission": "provider submissions",
}
TESTNETS = {"eip155:84532", "base-sepolia", "eip155:11155111", "sepolia",
            "eip155:80002", "solana:devnet", "solana-devnet"}


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _rows(items):
    """[(label, count)] -> [{label, count, pct}] with pct of the listed total."""
    total = sum(c for _l, c in items)
    return [{"label": l, "count": c, "pct": _pct(c, total)} for l, c in items]


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

_A2A_RPC_LIVE = ("EXISTS (SELECT 1 FROM json_each(readiness_json,'$.criteria') "
                 "WHERE json_extract(value,'$.key')='live_jsonrpc' "
                 "AND json_extract(value,'$.points')>=20)")


def funnel(conn):
    steps = {
        "x402": [
            ("Indexed", "1=1"),
            ("Has an endpoint", "COALESCE(url,'')!=''"),
            ("Reachable", "health='ok'"),
            ("Valid 402 challenge seen", "health='ok' AND x402_ok=1"),
            ("Readiness A or B", "readiness_grade IN ('A','B')"),
        ],
        "mcp": [
            ("Indexed", "1=1"),
            ("Has a remote endpoint", "COALESCE(endpoint_url,'')!=''"),
            ("Reachable", "health='ok'"),
            ("Full handshake (initialize + tools/list)", "health='ok' AND conformance='pass'"),
            ("Readiness A or B", "readiness_grade IN ('A','B')"),
        ],
        "a2a": [
            ("Indexed", "1=1"),
            ("Has an Agent Card URL", "COALESCE(card_url,'')!=''"),
            ("Card fetched", "health='ok'"),
            ("Answers JSON-RPC", "health='ok' AND " + _A2A_RPC_LIVE),
            ("Readiness A or B", "readiness_grade IN ('A','B')"),
        ],
    }
    out = {}
    for kind, items in steps.items():
        cols = ", ".join(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)" for _l, cond in items)
        row = conn.execute(f"SELECT {cols} FROM {TABLE[kind]}").fetchone()
        counts = [int(v or 0) for v in row]
        total = counts[0] or 1
        out[kind] = [{"label": label, "count": c, "pct_total": _pct(c, total),
                      "pct_prev": _pct(c, counts[i - 1]) if i else 100.0}
                     for i, ((label, _c), c) in enumerate(zip(items, counts))]
    return out


# ---------------------------------------------------------------------------
# Sources, overlap, growth
# ---------------------------------------------------------------------------

def sources(conn):
    out = {}
    for kind in KINDS:
        table = TABLE[kind]
        rows = _q(conn, f"""
            SELECT s.source, COUNT(*) n, SUM(t.health='ok') ok
            FROM listing_sources s JOIN {table} t ON t.id = s.listing_id
            WHERE s.kind=? GROUP BY s.source ORDER BY n DESC""", (kind,))
        per_listing = _q(conn, f"""
            SELECT COUNT(*) FROM listing_sources s JOIN {table} t ON t.id = s.listing_id
            WHERE s.kind=? GROUP BY s.listing_id""", (kind,))
        hist = Counter(min(r[0], 5) for r in per_listing)
        out[kind] = {
            "by_source": [{"source": r[0], "label": SOURCE_LABELS.get(r[0], r[0]), "listings": r[1], "reachable": int(r[2] or 0),
                           "reachable_pct": _pct(int(r[2] or 0), r[1])} for r in rows],
            "overlap": [{"label": f"{k}+ sources" if k == 5 else f"{k} source" + ("s" if k > 1 else ""),
                         "count": hist.get(k, 0)} for k in range(1, 6)],
            "multi_source_pct": _pct(sum(v for k, v in hist.items() if k > 1), sum(hist.values())),
        }
    return out


def growth(conn, now, days=90):
    start = now - days * 86400
    out = {}
    for kind in KINDS:
        table = TABLE[kind]
        rows = _q(conn, f"""
            SELECT date(MIN(s.first_seen),'unixepoch') d FROM listing_sources s
            JOIN {table} t ON t.id = s.listing_id
            WHERE s.kind=? GROUP BY s.listing_id HAVING MIN(s.first_seen) >= ?""", (kind, start))
        per_day = Counter(r[0] for r in rows)
        series = []
        for i in range(days, 0, -1):
            d = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400 + 86400))
            series.append([d, per_day.get(d, 0)])
        went_down = _one(conn, f"SELECT COUNT(*) FROM {table} WHERE down_since >= ?",
                         (now - 7 * 86400,)) or 0
        out[kind] = {"daily_new": series,
                     "new_7d": sum(c for _d, c in series[-7:]),
                     "new_30d": sum(c for _d, c in series[-30:]),
                     "went_down_7d": int(went_down)}
    return out


# ---------------------------------------------------------------------------
# Protocols, readiness, access, safety
# ---------------------------------------------------------------------------

def _a2a_version_bucket(v):
    v = str(v or "").strip()
    if not v:
        return "Not declared"
    m = re.match(r"v?(\d+)\.(\d+)", v)
    if not m:
        return "Other"
    major, minor = int(m.group(1)), int(m.group(2))
    if major >= 1:
        return "1.x"
    if minor == 3:
        return "0.3"
    return "0.2 and older"


def protocols(conn):
    mcp = Counter()
    for v, n in _q(conn, "SELECT protocol_version, COUNT(*) FROM mcp_servers WHERE health='ok' GROUP BY 1"):
        v = (v or "").strip()
        mcp[v if v in MCP_REVISIONS else ("Not reported" if not v else "Non-standard")] += n
    mcp_rows = [(r, mcp.get(r, 0)) for r in MCP_REVISIONS] + [
        ("Non-standard", mcp.get("Non-standard", 0)), ("Not reported", mcp.get("Not reported", 0))]

    a2a = Counter()
    for v, n in _q(conn, "SELECT protocol_version, COUNT(*) FROM a2a_agents WHERE health='ok' GROUP BY 1"):
        a2a[_a2a_version_bucket(v)] += n
    a2a_rows = [(k, a2a.get(k, 0)) for k in ("1.x", "0.3", "0.2 and older", "Other", "Not declared")]

    x402 = Counter()
    for (samples,) in _q(conn, "SELECT resource_samples FROM services WHERE health='ok' AND x402_ok=1"):
        try:
            items = json.loads(samples) if samples else []
        except (TypeError, ValueError):
            items = []
        if isinstance(items, dict):
            items = items.get("resource_samples") or []
        versions = {s.get("x402_version") for s in items if isinstance(s, dict)}
        versions = {int(v) for v in versions if isinstance(v, int) or (isinstance(v, str) and v.isdigit())}
        x402["v2" if versions and max(versions) >= 2 else ("v1" if versions else "Not recorded")] += 1
    x402_rows = [(k, x402.get(k, 0)) for k in ("v2", "v1", "Not recorded")]

    signed = {"verified": 0, "unverified": 0, "unsigned": 0}
    for (points,) in _q(conn, """
            SELECT (SELECT json_extract(value,'$.points') FROM json_each(readiness_json,'$.criteria')
                    WHERE json_extract(value,'$.key')='signature')
            FROM a2a_agents WHERE readiness_json IS NOT NULL AND health='ok'"""):
        signed["verified" if points == 10 else ("unverified" if points == 3 else "unsigned")] += 1
    return {"mcp": _rows(mcp_rows), "a2a": _rows(a2a_rows), "x402": _rows(x402_rows),
            "a2a_signatures": signed}


def readiness(conn):
    out = {}
    for kind in KINDS:
        counts = dict(_q(conn, f"SELECT readiness_grade, COUNT(*) FROM {TABLE[kind]} "
                               "WHERE readiness_grade IS NOT NULL GROUP BY 1"))
        out[kind] = _rows([(g, int(counts.get(g, 0))) for g in GRADES])
    # A2A keeps the full breakdown: which criteria lose the most points.
    lost = defaultdict(float)
    n = 0
    for (raw,) in _q(conn, "SELECT readiness_json FROM a2a_agents WHERE readiness_json IS NOT NULL AND health='ok'"):
        try:
            crits = json.loads(raw)["criteria"]
        except (TypeError, ValueError, KeyError):
            continue
        n += 1
        for c in crits:
            if c.get("applicable", True):
                lost[c.get("label") or c.get("key")] += float(c["max_points"]) - float(c["points"])
    out["a2a_points_lost"] = sorted(
        ({"label": k, "avg_lost": round(v / n, 1)} for k, v in lost.items()), key=lambda r: -r["avg_lost"]) if n else []
    return out


def access(conn):
    buckets = Counter()
    for auth, x402, n in _q(conn, "SELECT LOWER(COALESCE(auth_method,'')), COALESCE(x402_supported,0), COUNT(*) "
                                  "FROM mcp_servers WHERE health='ok' GROUP BY 1,2"):
        if x402:
            key = "x402 pay-per-call"
        elif "oauth" in auth:
            key = "OAuth"
        elif auth in ("open", "none", "public"):
            key = "Open (declared)"
        elif auth:
            key = "API key or token"
        else:
            key = "Not declared"
        buckets[key] += n
    mcp_rows = [(k, buckets.get(k, 0)) for k in
                ("Open (declared)", "API key or token", "OAuth", "x402 pay-per-call", "Not declared")]

    a2a = Counter()
    for (detail,) in _q(conn, """
            SELECT (SELECT json_extract(value,'$.detail') FROM json_each(readiness_json,'$.criteria')
                    WHERE json_extract(value,'$.key')='security')
            FROM a2a_agents WHERE readiness_json IS NOT NULL AND health='ok'"""):
        d = (detail or "").lower()
        if "mutual tls" in d:
            a2a["mTLS"] += 1
        elif "oauth2 with pkce" in d:
            a2a["OAuth2 + PKCE"] += 1
        elif "security scheme" in d:
            a2a["Other declared scheme"] += 1
        elif "no authentication" in d:
            a2a["Declared public"] += 1
        else:
            a2a["Not declared"] += 1
    a2a_rows = [(k, a2a.get(k, 0)) for k in
                ("mTLS", "OAuth2 + PKCE", "Other declared scheme", "Declared public", "Not declared")]
    return {"mcp": _rows(mcp_rows), "a2a": _rows(a2a_rows)}


def safety(conn):
    verdicts = dict(_q(conn, "SELECT COALESCE(safety_verdict,'not scanned'), COUNT(*) FROM mcp_servers GROUP BY 1"))
    rules = Counter()
    for (raw,) in _q(conn, "SELECT safety_reasons FROM mcp_servers WHERE safety_verdict IN ('suspicious','malicious')"):
        try:
            for r in json.loads(raw or "[]"):
                if isinstance(r, dict) and r.get("rule"):
                    rules[r["rule"]] += 1
        except (TypeError, ValueError):
            pass
    return {"verdicts": [{"label": k, "count": int(verdicts.get(k, 0))}
                         for k in ("clean", "suspicious", "malicious")],
            "scanned": sum(int(verdicts.get(k, 0)) for k in ("clean", "suspicious", "malicious")),
            "rules": [{"label": k.replace("_", " "), "count": v} for k, v in rules.most_common(8)]}


# ---------------------------------------------------------------------------
# Reliability and latency
# ---------------------------------------------------------------------------

def reliability(conn, now):
    window = now - 30 * 86400
    hist_sql = {
        "x402": f"SELECT service_id, SUM(status='ok'), COUNT(*) FROM health_history WHERE checked_at>=? AND {REFUSED} GROUP BY 1",
        "mcp": f"SELECT server_id, SUM(status='ok'), COUNT(*) FROM mcp_health_history WHERE checked_at>=? AND {REFUSED} GROUP BY 1",
        "a2a": f"SELECT agent_id, SUM(status='ok'), COUNT(*) FROM a2a_health_history WHERE checked_at>=? AND {REFUSED} GROUP BY 1",
    }
    out = {}
    for kind in KINDS:
        bands = Counter()
        tracked = 0
        for _id, ok, n in _q(conn, hist_sql[kind], (window,)):
            if n < 5:
                continue
            tracked += 1
            r = (ok or 0) / n
            bands["100%" if r >= 1 else "95–99%" if r >= .95 else "80–94%" if r >= .8
                  else "50–79%" if r >= .5 else "<50%"] += 1
        lat = [r[0] for r in _q(conn, f"SELECT latency_ms FROM {TABLE[kind]} "
                                       "WHERE health='ok' AND latency_ms IS NOT NULL")]
        speed = Counter("≤2.5 s" if v <= 2500 else "2.5–5 s" if v <= 5000 else "5–8 s" if v <= 8000 else ">8 s"
                        for v in lat)
        out[kind] = {
            "tracked": tracked,
            "uptime": _rows([(k, bands.get(k, 0)) for k in ("100%", "95–99%", "80–94%", "50–79%", "<50%")]),
            "latency": {"n": len(lat),
                        "p50": round(_quantile(lat, .5)) if lat else None,
                        "p95": round(_quantile(lat, .95)) if lat else None},
            "speed": _rows([(k, speed.get(k, 0)) for k in ("≤2.5 s", "2.5–5 s", "5–8 s", ">8 s")]),
        }
    return out


# ---------------------------------------------------------------------------
# x402 prices, chains, payment evidence
# ---------------------------------------------------------------------------

PRICE_BINS = ((0.001, "≤ $0.001"), (0.005, "≤ $0.005"), (0.01, "≤ $0.01"), (0.05, "≤ $0.05"),
              (0.1, "≤ $0.10"), (1.0, "≤ $1"), (float("inf"), "> $1"))


def _network(pay, chains):
    from .db import network_id
    net = ""
    if isinstance(pay, dict):
        accepts = pay.get("accepts")
        first = accepts[0] if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict) else {}
        net = network_id(pay.get("network") or first.get("network") or pay.get("networks"))
    if not net and chains:
        try:
            parsed = json.loads(chains) if isinstance(chains, str) and chains.startswith("[") else chains
        except ValueError:
            parsed = chains
        net = network_id(parsed if isinstance(parsed, (list, tuple)) else str(parsed).split(",")[0])
    return net


EVIDENCE_LABELS = {
    "x402_challenge": "Live 402 challenge", "x402_manifest": "x402 manifest",
    "agent_card": "Agent Card payment extension", "bazaar_feed": "CDP Bazaar feed",
    "catalog_listing": "Catalog listing only", "protocol_directory": "Protocol directory only",
    "none": "No evidence",
}


def _fold(counter, keep):
    """Top `keep` labels by count; the rest summed into 'Other' (kept last)."""
    other = counter.get("Other", 0)
    items = [(k, v) for k, v in counter.most_common() if k != "Other"]
    head, tail = items[:keep], items[keep:]
    other += sum(v for _k, v in tail)
    return head + ([("Other", other)] if other else [])


def x402_economy(conn):
    from . import listing_readiness as L
    prices, nets = [], Counter()
    rows = _q(conn, "SELECT id, payment, chains, x402_ok, descriptor_checked_at, well_known_url "
                    "FROM services WHERE health='ok'")
    srcs = L.sources_by_listing(conn, "x402")
    evidence = Counter()
    for sid, payment, chains, x402_ok, desc, wk in rows:
        try:
            pay = json.loads(payment) if payment else None
        except ValueError:
            pay = None
        ev = L.payment_evidence(challenge=bool(x402_ok), manifest=bool(desc) and bool(wk),
                                sources=srcs.get(sid, ()))
        evidence[ev["grade"] if ev else "none"] += 1
        if not x402_ok:
            continue
        net = _network(pay, chains)
        if net:
            label = "Testnets" if net in TESTNETS else CHAIN_NAMES.get(
                net, "Solana" if net.startswith("solana") else "Other")
            nets[label] += 1
        else:
            nets["Not recorded"] += 1
        if isinstance(pay, dict):
            # Two descriptor shapes are stored: a price range from the
            # challenge's accepts list, or a single maximum amount.
            p = pay.get("price_min_usd")
            if not isinstance(p, (int, float)) or p <= 0:
                p = pay.get("max_amount_usdc")
            if isinstance(p, (int, float)) and p > 0:
                prices.append(float(p))
    bins = Counter()
    for p in prices:
        for limit, label in PRICE_BINS:
            if p <= limit:
                bins[label] += 1
                break
    order = [g for g, _t in L.EVIDENCE_GRADES] + ["none"]
    return {
        "priced": len(prices),
        "price_quantiles": {k: (round(_quantile(prices, q), 4) if prices else None)
                            for k, q in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9))},
        "share_le_1c": _pct(sum(1 for p in prices if p <= 0.01), len(prices)),
        "price_bins": _rows([(label, bins.get(label, 0)) for _l, label in PRICE_BINS]),
        "chains": _rows(_fold(nets, 7)),
        "payment_evidence": _rows([(EVIDENCE_LABELS.get(g, g), evidence.get(g, 0)) for g in order]),
    }


# ---------------------------------------------------------------------------
# Topic coverage
# ---------------------------------------------------------------------------

def topics(conn):
    counts = {t: Counter() for t in TOPICS}
    sources = {
        "x402": "SELECT name, description FROM services WHERE health='ok'",
        "mcp": "SELECT name, description FROM mcp_servers WHERE health='ok'",
        "a2a": "SELECT name, description FROM a2a_agents WHERE health='ok'",
    }
    totals = Counter()
    skipped = Counter()
    for kind, sql in sources.items():
        for name, desc in _q(conn, sql):
            text = f"{name or ''} {desc or ''}".lower()
            hits = [t for t, rx in _TOPIC_RE.items() if rx.search(text)]
            # Gateway descriptions that list every category (one host had over
            # a thousand paths sharing one such text) say nothing about topic.
            if len(hits) > CATALOGUE_TOPICS:
                skipped[kind] += 1
                continue
            totals[kind] += 1
            for t in hits:
                counts[t][kind] += 1
    rows = []
    for t in TOPICS:
        row = {"topic": t}
        for k in KINDS:
            row[k] = counts[t].get(k, 0)
            row[k + "_pct"] = _pct(row[k], totals[k])
        rows.append(row)
    rows.sort(key=lambda r: -(r["x402"] + r["mcp"] + r["a2a"]))
    # A gap: the topic is a sizeable share of one protocol's supply but under a
    # fifth of that share in another.
    gaps = []
    for r in rows:
        best = max(KINDS, key=lambda k: r[k + "_pct"])
        if r[best + "_pct"] < 3:
            continue
        for k in KINDS:
            if k != best and r[k + "_pct"] < r[best + "_pct"] / 5:
                gaps.append({"topic": r["topic"], "kind": k, "pct": r[k + "_pct"],
                             "strong_kind": best, "strong_pct": r[best + "_pct"]})
    gaps.sort(key=lambda g: -(g["strong_pct"] - g["pct"]))
    return {"counted": dict(totals), "skipped_catalogue": dict(skipped), "rows": rows, "gaps": gaps[:10]}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def compute(conn, now: int | None = None) -> dict:
    now = int(now or time.time())
    t0 = time.monotonic()
    f = funnel(conn)
    snap = {
        "version": VERSION,
        "generated_at": now,
        "as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        "funnel": f,
        "readiness": readiness(conn),
        "protocols": protocols(conn),
        "reliability": reliability(conn, now),
        "access": access(conn),
        "safety": safety(conn),
        "x402": x402_economy(conn),
        "sources": sources(conn),
        "growth": growth(conn, now),
        "topics": topics(conn),
    }
    kpis = {}
    for kind in KINDS:
        steps = f[kind]
        kpis[kind] = {"indexed": steps[0]["count"], "reachable": steps[2]["count"],
                      "verified": steps[3]["count"], "ready": steps[4]["count"]}
    kpis["all"] = {k: sum(kpis[kind][k] for kind in KINDS) for k in ("indexed", "reachable", "verified", "ready")}
    snap["kpis"] = kpis
    snap["compute_seconds"] = round(time.monotonic() - t0, 1)
    return snap


def flatten(snap: dict) -> list[tuple[str, str, float]]:
    """(kind, metric, value) rows for the daily CSV."""
    out = []
    for kind, kp in snap.get("kpis", {}).items():
        for metric, value in kp.items():
            out.append((kind, metric, value))
    for kind in KINDS:
        for row in snap.get("readiness", {}).get(kind, []):
            out.append((kind, f"readiness_{row['label']}", row["count"]))
        rel = snap.get("reliability", {}).get(kind, {})
        lat = rel.get("latency") or {}
        for q in ("p50", "p95"):
            if lat.get(q) is not None:
                out.append((kind, f"latency_{q}_ms", lat[q]))
        g = snap.get("growth", {}).get(kind, {})
        for metric in ("new_7d", "went_down_7d"):
            if metric in g:
                out.append((kind, metric, g[metric]))
    x = snap.get("x402", {})
    for k, v in (x.get("price_quantiles") or {}).items():
        if v is not None:
            out.append(("x402", f"price_{k}_usd", v))
    return out


# ---------------------------------------------------------------------------
# Headlines: one finding sentence per section, rebuilt from the numbers.
# ---------------------------------------------------------------------------

def headlines(snap: dict, kind: str = "all") -> dict:
    f = snap["funnel"]
    mcp = f["mcp"]
    a2a = f["a2a"]
    x = snap["x402"]
    rel = snap["reliability"]
    prot = snap["protocols"]
    a2a_1x = next((r["pct"] for r in prot["a2a"] if r["label"] == "1.x"), 0)
    sig = prot["a2a_signatures"]
    signed_total = sum(sig.values()) or 1
    q = x["price_quantiles"]
    multi = snap["sources"]["mcp"]["multi_source_pct"]
    gaps = snap["topics"]["gaps"]
    out = {
        "funnel": (f"Only {mcp[3]['pct_total']}% of indexed MCP servers complete a full handshake, "
                   f"and {a2a[3]['pct_total']}% of A2A agents answer JSON-RPC."),
        "sources": f"{multi}% of MCP servers are listed by more than one directory.",
        "protocols": (f"{a2a_1x}% of reachable A2A agents declare 1.x; "
                      f"{_pct(sig['verified'], signed_total)}% carry a signature that verifies."),
        "readiness": (f"Reachable A2A agents lose the most readiness points on "
                      f"{' and '.join(r['label'] for r in snap['readiness']['a2a_points_lost'][:2])}."
                      if snap["readiness"]["a2a_points_lost"] else "Readiness grades by protocol."),
        "reliability": (f"Median response is {rel['mcp']['latency']['p50']} ms for MCP and "
                        f"{rel['x402']['latency']['p50']} ms for x402."),
        "x402": (f"The median x402 API's cheapest call costs ${q['p50']:g}; {x['share_le_1c']}% charge a cent or less."
                 if q.get("p50") is not None else "No x402 prices recorded yet."),
        "access": (f"{next((r['pct'] for r in snap['access']['mcp'] if r['label'] == 'Not declared'), 0)}% of "
                   "reachable MCP servers do not say how to authenticate."),
        "safety": (f"{snap['safety']['verdicts'][1]['count'] + snap['safety']['verdicts'][2]['count']} MCP servers "
                   f"are flagged by our static safety scan."),
        "topics": (f"'{gaps[0]['topic']}' is {gaps[0]['strong_pct']}% of reachable {KIND_LABEL[gaps[0]['strong_kind']]} "
                   f"but {gaps[0]['pct']}% of {KIND_LABEL[gaps[0]['kind']]}."
                   if gaps else "No topic is common in one protocol and rare in another."),
        "growth": (f"{snap['growth']['mcp']['new_7d'] + snap['growth']['x402']['new_7d'] + snap['growth']['a2a']['new_7d']:,} "
                   "listings were first seen in the last 7 days."),
    }
    if kind in KINDS:
        out.update(_kind_headlines(snap, kind))
    return out


VERIFIED_PHRASE = {"x402": "return a valid 402 challenge", "mcp": "complete a full handshake",
                   "a2a": "answer JSON-RPC"}


def _kind_headlines(snap, kind):
    label = KIND_LABEL[kind]
    steps = snap["funnel"][kind]
    rel = snap["reliability"][kind]
    grades = {r["label"]: r["pct"] for r in snap["readiness"][kind]}
    g = snap["growth"][kind]
    h = {
        "funnel": (f"{steps[2]['pct_total']}% of indexed {label} answer our probe, and "
                   f"{steps[3]['pct_total']}% {VERIFIED_PHRASE[kind]}."),
        "sources": f"{snap['sources'][kind]['multi_source_pct']}% of {label} are listed by more than one source.",
        "readiness": (f"{round(grades.get('A', 0) + grades.get('B', 0), 1)}% of {label} grade A or B; "
                      f"{grades.get('F', 0)}% grade F."),
        "reliability": (f"Median response for {label} is {rel['latency']['p50']} ms; p95 is {rel['latency']['p95']} ms."
                        if rel["latency"]["p50"] is not None else f"No latency recorded for {label} yet."),
        "growth": f"{g['new_7d']:,} {label} were first seen in the last 7 days; {g['went_down_7d']:,} went down.",
    }
    prot = snap["protocols"]
    if kind == "x402":
        v2 = next((r["pct"] for r in prot["x402"] if r["label"] == "v2"), 0)
        h["protocols"] = f"{v2}% of verified x402 APIs were seen speaking x402 v2."
    elif kind == "mcp":
        latest = prot["mcp"][0]
        h["protocols"] = f"{latest['pct']}% of reachable MCP servers report the latest revision, {latest['label']}."
    else:
        h["access"] = (f"{next((r['pct'] for r in snap['access']['a2a'] if r['label'] == 'Not declared'), 0)}% of "
                       "reachable A2A agents declare no security scheme.")
    return h


# ---------------------------------------------------------------------------
# Agent-facing summary (MCP tool, A2A skill, /api/v1/stats)
# ---------------------------------------------------------------------------

SITE = "https://agent-tools.cloud"
VIEWS = ("all",) + KINDS
LINKS = {
    "page": f"{SITE}/stats",
    "snapshot_json": f"{SITE}/stats.json",
    "daily_csv": f"{SITE}/stats/daily.csv",
    "methodology": f"{SITE}/stats#method",
    "license": "CC BY 4.0",
}
_latest_cache: dict = {"at": 0.0, "snap": None}


def latest(conn_factory, ttl: float = 600.0) -> dict | None:
    """The newest stored snapshot, cached in-process for `ttl` seconds.

    `conn_factory` returns a read-only connection; it is only called on a miss.
    """
    from contextlib import closing
    from . import db
    now = time.time()
    if _latest_cache["snap"] is not None and now - _latest_cache["at"] < ttl:
        return _latest_cache["snap"]
    with closing(conn_factory()) as c:
        snap = db.latest_stats_snapshot(c)
    _latest_cache.update(at=now, snap=snap)
    return snap


def summary(snap: dict | None, view: str = "all") -> dict:
    """Compact, agent-readable digest of one snapshot (a few hundred bytes)."""
    view = view if view in VIEWS else "all"
    if not snap:
        return {"available": False, "view": view, "links": LINKS,
                "note": "The first daily snapshot has not been computed yet."}
    kinds = KINDS if view == "all" else (view,)
    out = {
        "available": True,
        "view": view,
        "as_of": snap["as_of"],
        "generated_at": snap["generated_at"],
        "kpis": {k: snap["kpis"][k] for k in (("all",) + KINDS if view == "all" else (view,))},
        "definitions": {
            "indexed": "listings any source shows us",
            "reachable": "answered our latest daily probe",
            "verified": "valid 402 challenge (x402), full initialize + tools/list (MCP), "
                        "JSON-RPC reply (A2A)",
            "ready": "readiness grade A or B",
        },
        "findings": headlines(snap, view),
        "links": LINKS,
    }
    facts: dict = {}
    if "x402" in kinds:
        x = snap["x402"]
        facts["x402"] = {
            "cheapest_call_usd_quantiles": x["price_quantiles"],
            "share_cheapest_call_le_1_cent_pct": x["share_le_1c"],
            "top_networks": x["chains"][:3],
        }
    if "mcp" in kinds:
        facts["mcp"] = {
            "protocol_versions": snap["protocols"]["mcp"][:3],
            "auth_not_declared_pct": next(
                (r["pct"] for r in snap["access"]["mcp"] if r["label"] == "Not declared"), None),
            "latency_ms": snap["reliability"]["mcp"]["latency"],
        }
    if "a2a" in kinds:
        facts["a2a"] = {
            "protocol_versions": snap["protocols"]["a2a"][:3],
            "card_signatures": snap["protocols"]["a2a_signatures"],
        }
    out["facts"] = facts
    return out
