"""Machine-readable methodology: how every score and statistic is computed.

The human version lives on /about (#grading, #readiness) and /stats#method.
Readiness weights, labels, grades and evidence grades are read from the
scoring modules, so they cannot drift; the quality-score components restate
the /about table and are pinned by tests/test_agent_content.py.
"""
from __future__ import annotations

from . import a2a_readiness as A
from . import db
from . import listing_readiness as L

VERSION = "2026-11-11"
SITE = "https://agent-tools.cloud"

QUALITY_COMPONENTS = {
    "x402": [
        {"name": "Availability", "max": 20,
         "how": "Endpoint probe: ok 100%, degraded 45%, unknown 22%, down 0."},
        {"name": "Payability", "max": 15,
         "how": "Live HTTP 402 challenge 15, /.well-known/x402 descriptor 5, payment address 5, "
                "settlement network 3, price 2; of 30, scaled to 15."},
        {"name": "Demand", "max": 35,
         "how": "Latest 30-day USDC receipts at the payTo address read from the chain; distinct "
                "payers two thirds (full at 1,000), transfer count one third (full at 10,000), "
                "both on a log scale. A wallet shared by several listings is split between them; "
                "more than 20 transfers per payer is discounted, down to half. Chains without a "
                "free indexer leave this blank rather than zero."},
        {"name": "Performance", "max": 10,
         "how": "Latency of the last reachable probe: full marks at <= 200 ms, falling linearly "
                "to 0 at >= 2000 ms."},
        {"name": "Readiness", "max": 15,
         "how": "Agent readiness score (0-100), scaled to 15; only while the endpoint is reachable."},
        {"name": "Owner verified", "max": 5,
         "how": "Operator proved control of the domain."},
    ],
    "mcp": [
        {"name": "Availability", "max": 25, "how": "ok 25, degraded 10."},
        {"name": "Conformance", "max": 25, "how": "JSON-RPC initialize + tools/list: pass 25, partial 10."},
        {"name": "Performance", "max": 30,
         "how": "Full marks at p95 <= 50 ms, falling linearly to 0 at >= 2000 ms; no latency data scores 0."},
        {"name": "Descriptor", "max": 20,
         "how": "Advertises its tools 8, tool metadata captured 5, source or package published 4, "
                "transport declared 3."},
    ],
    "a2a": [
        {"name": "Availability", "max": 30, "how": "Agent card fetched and parsed: ok 30, degraded 12."},
        {"name": "Conformance", "max": 20, "how": "pass 20, partial 8."},
        {"name": "Card detail", "max": 15,
         "how": "Skills declared 8, three or more skills 4, callable endpoint 6, capabilities 4, "
                "protocol version 3, auth scheme 3, documentation 2; of 30, scaled to 15."},
        {"name": "Readiness", "max": 15,
         "how": "Agent readiness score (0-100), scaled to 15; only while the card is reachable."},
        {"name": "Performance", "max": 20,
         "how": "Full marks at <= 50 ms, falling linearly to 0 at >= 1500 ms."},
    ],
}
QUALITY_RULES = [
    "An x402 service whose endpoint is down is held at D regardless of the other components.",
    "An MCP server flagged by the safety scan is capped: suspicious at C, malicious at E.",
    "Owner verification adds %g points to MCP servers, and to A2A agents as score x 0.97 + 3 "
    "so a claimed agent never saturates at 100." % db._OWNER_BONUS,
    "Bands are set per resource type, so grades are not comparable across types. Below the D threshold is E.",
]


def _criteria(kind: str) -> list[dict]:
    labels = A.LABELS if kind == "a2a" else L.LABELS[kind]
    weights = A.WEIGHTS if kind == "a2a" else L.WEIGHTS
    return [{"key": k, "label": labels[k], "max_points": weights[k]} for k in weights]


def build() -> dict:
    kinds = ("x402", "mcp", "a2a")
    return {
        "version": VERSION,
        "human_readable": {"grading": f"{SITE}/about#grading", "readiness": f"{SITE}/about#readiness",
                           "statistics": f"{SITE}/stats#method"},
        "probing": {
            "schedule": "Every listing is probed once a day from one server, starting 21:30 UTC.",
            "refused_probes": "Probes a host refused (HTTP 429, or a 403 that kept us out) are left "
                              "out of uptime.",
            "history_days": {"x402": 90, "mcp": 30, "a2a": 120},
        },
        "quality_score": {
            "purpose": "Search order and leaderboards.",
            "scale": "0-100, shown as 0-10 with three decimals.",
            "components": QUALITY_COMPONENTS,
            "grade_bands": {k: [{"min": lo, "grade": g} for lo, g in db._GRADE_BANDS_BY_KIND[k]]
                            for k in kinds},
            "rules": QUALITY_RULES,
        },
        "readiness_score": {
            "purpose": "Whether an agent could call the listing today. Feeds 15 points of the x402 "
                       "quality score (and of the A2A one); otherwise display only.",
            "criteria": {k: _criteria(k) for k in kinds},
            "notes": {"a2a": A.METHODOLOGY, "mcp": L.METHODOLOGY["mcp"], "x402": L.METHODOLOGY["x402"]},
            "grade_bands": [{"min": lo, "grade": g} for lo, g in A.GRADES] + [{"min": 0, "grade": "F"}],
            "decay": {"live_points_full_until_days": L.DECAY_START_DAYS,
                      "floor_reached_at_days": L.DECAY_END_DAYS, "floor": L.DECAY_FLOOR},
            "latency_bands_ms": [{"max_ms": ms, "band": band} for ms, band, _label in L.BANDS],
            "payment_evidence_grades": [{"grade": g, "meaning": t} for g, t in L.EVIDENCE_GRADES],
        },
        "statistics": {
            "snapshot": "Computed once a day after the probe run; every figure is a count over our "
                        "own index, not an ecosystem census.",
            "definitions": {
                "indexed": "listings any source shows us",
                "reachable": "answered our latest daily probe",
                "verified": "valid 402 challenge (x402), full initialize + tools/list (MCP), "
                            "JSON-RPC reply (A2A)",
                "ready": "readiness grade A or B",
            },
            "topics": "Whole-word match of a fixed word list on name and description; listings naming "
                      "more than five topics (gateway catalogues) are left out.",
            "x402_prices": "Cheapest call each reachable API advertises, in USD (USDC at par).",
            "license": "CC BY 4.0",
        },
        "safety_scan": "Static pattern rules over MCP tool names and descriptions for prompt-injection "
                       "and data-exfiltration lures; no code is executed. Advisory, not a security review.",
    }
