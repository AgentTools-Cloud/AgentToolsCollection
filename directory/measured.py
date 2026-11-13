"""What our own probes measured for one listing, as plain sentences.

Detail pages used to be a name, an upstream description and a fixed template,
so search engines saw thousands of near-identical pages whose only text was
copied from another directory. This module writes the part nobody else has:
our 30-day probe record, the tools the server actually listed when we called
it, and where it stands among its peers. Facts only, all from our database.
"""
from __future__ import annotations

import time

WINDOW_DAYS = 30
TOOLS_SHOWN = 60
_HISTORY = {  # kind -> (table, id column)
    "x402": ("health_history", "service_id"),
    "mcp": ("mcp_health_history", "server_id"),
    "a2a": ("a2a_health_history", "agent_id"),
}
_NOUN = {"x402": "API", "mcp": "MCP server", "a2a": "agent"}


def _pct(a, b):
    return round(100.0 * a / b) if b else 0


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def probe_record(conn, kind: str, listing_id, now: float | None = None) -> dict | None:
    table, col = _HISTORY[kind]
    since = int(now or time.time()) - WINDOW_DAYS * 86400
    rows = conn.execute(
        f"SELECT status, latency_ms, checked_at FROM {table} "
        f"WHERE {col}=? AND checked_at>=?", (listing_id, since)).fetchall()
    if not rows:
        return None
    up = [r for r in rows if r[0] in ("ok", "degraded")]
    lat = [int(r[1]) for r in up if r[1] is not None and r[1] > 0]
    days = len({int(r[2]) // 86400 for r in rows})
    return {"probes": len(rows), "answered": len(up), "answered_pct": _pct(len(up), len(rows)),
            "ok": sum(1 for r in rows if r[0] == "ok"), "days": days,
            "median_ms": _quantile(lat, 0.5), "p95_ms": _quantile(lat, 0.95),
            "last": time.strftime("%Y-%m-%d", time.gmtime(max(int(r[2]) for r in rows)))}


def _tools(row) -> list:
    tools = row.get("tools")
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


def sentences(kind: str, row: dict, record: dict | None, peer: dict | None) -> list[str]:
    noun = _NOUN[kind]
    out = []
    if record:
        s = (f"Over the last {WINDOW_DAYS} days we probed this {noun} {record['probes']:,} "
             f"time{'s' if record['probes'] != 1 else ''} on {record['days']} "
             f"day{'s' if record['days'] != 1 else ''}; it answered {record['answered']:,} "
             f"({record['answered_pct']}%).")
        if record["median_ms"] is not None:
            s += f" Median response {record['median_ms']:,} ms"
            if record["p95_ms"] is not None and record["p95_ms"] != record["median_ms"]:
                s += f", 95th percentile {record['p95_ms']:,} ms"
            s += "."
        out.append(s)
    if kind == "mcp":
        tools = _tools(row)
        n = int(row.get("tool_count") or len(tools) or 0)
        if n and tools:
            names = ", ".join(t["name"] for t in tools[:3])
            s = (f"When we called tools/list it returned {n:,} tool{'s' if n != 1 else ''}"
                 f", including {names}.")
            if row.get("protocol_version"):
                s = s[:-1] + f", over MCP protocol revision {row['protocol_version']}."
            out.append(s)
        elif row.get("conformance") == "pass":
            out.append("It completed the MCP initialize handshake when we probed it.")
    if kind == "x402":
        payers = row.get("payto_payers_30d")
        if row.get("payto_checked") and payers:
            tx = row.get("payto_tx_30d") or 0
            out.append(f"On-chain, {payers:,} distinct wallet{'s' if payers != 1 else ''} paid its "
                       f"payment address {tx:,} time{'s' if tx != 1 else ''} in the last 30 days.")
    if peer:
        bits = []
        if peer.get("latency"):
            bits.append(f"faster than {peer['latency']['faster_than_pct']:.0f}%")
        if peer.get("readiness"):
            bits.append(f"higher on readiness than {peer['readiness']['better_than_pct']:.0f}%")
        if bits:
            out.append(f"Among the {peer['peers']:,} {peer['peer_group']} in our snapshot of "
                       f"{peer['as_of']}, it is " + " and ".join(bits) + ".")
    return out


def attach(conn, kind: str, out: dict) -> None:
    """Add out['measured'] = {'record', 'sentences', 'tools', 'tools_total'}."""
    record = probe_record(conn, kind, out.get("id"))
    tools = _tools(out) if kind == "mcp" else []
    out["measured"] = {
        "record": record,
        "sentences": sentences(kind, out, record, out.get("peer")),
        "tools": tools[:TOOLS_SHOWN],
        "tools_total": max(int(out.get("tool_count") or 0), len(tools)),
    }


def meta_description(kind: str, row: dict) -> str:
    """Search-result snippet: what it does plus what we measured, ≤ 160 chars."""
    m = row.get("measured") or {}
    rec = m.get("record") or {}
    parts = []
    if kind == "mcp" and m.get("tools"):
        names = ", ".join(t["name"] for t in m["tools"][:3])
        parts.append(f"MCP server with {m['tools_total']:,} tools ({names}).")
    desc = (row.get("description") or "").strip()
    if desc:
        first = desc.split("\n")[0].strip()
        parts.append(first if first.endswith((".", "!", "?")) else first + ".")
    if rec.get("probes"):
        s = f"Answered {rec['answered_pct']}% of our probes in 30 days"
        if rec.get("median_ms") is not None:
            s += f", median {rec['median_ms']:,} ms"
        parts.append(s + ".")
    text = " ".join(parts) or row.get("name") or ""
    return text if len(text) <= 160 else text[:157].rsplit(" ", 1)[0] + "…"
