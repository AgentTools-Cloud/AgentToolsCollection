"""A2A readiness score: nine criteria, 100 points.

Aligned with the public Agenstry conformance methodology (v1.1, CC BY 4.0) so
an operator sees the same levers on both sites. Differences, all documented in
`METHODOLOGY`:

* Live JSON-RPC is probed with a read-only `GetTask` / `tasks/get` for a random
  task id instead of `SendMessage`, so the probe never makes a third-party
  agent do work or spend money. Any well-formed JSON-RPC 2.0 reply counts.
* Verified identity uses what we can check: a declared provider plus our own
  domain-control claim. Company-registry checks are not implemented, so the
  top 2 points of that criterion are not reachable yet.
* Uptime needs five probes in 30 days before it is final; until then the
  observed ratio is shown as provisional.

The score is shown on its own and also contributes up to 15 of the 100 points
of the A2A quality score (see db._score_a2a), so it does affect
search order and leaderboards through that grade.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

VERSION = "1.0"
WEIGHTS = {
    "valid_card": 10, "live_jsonrpc": 25, "protocol_version": 10,
    "signature": 10, "uptime": 15, "skills": 10, "identity": 10,
    "freshness": 5, "security": 5,
}
LABELS = {
    "valid_card": "Valid Agent Card",
    "live_jsonrpc": "Live JSON-RPC",
    "protocol_version": "Protocol version",
    "signature": "JWS signature",
    "uptime": "Uptime (30 days)",
    "skills": "Skill declaration",
    "identity": "Verified identity",
    "freshness": "Freshness + modern flags",
    "security": "Security declaration",
}
GRADES = ((90, "A"), (75, "B"), (60, "C"), (40, "D"))
UPTIME_WINDOW = 30 * 86400
UPTIME_MIN_PROBES = 5
RPC_TIMEOUT = 8.0
_MODERN_FLAGS = ("x402", "ap2", "ucp", "mpp", "acp")
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", re.I)

METHODOLOGY = (
    "Nine criteria, 100 points, aligned with the public Agenstry conformance "
    "methodology v1.1. Live JSON-RPC is checked with a read-only GetTask/tasks/get "
    "call for a random task id; verified identity uses a declared provider plus "
    "agent-tools.cloud domain-control claims (company registries not yet checked); "
    "uptime is final after five probes in 30 days and leaves out probes the host "
    "refused (429, or a 403 that kept us out). Grades: A>=90, B>=75, C>=60, "
    "D>=40, F<40. It also feeds 15 points of the A2A quality grade."
)


def grade(score: float | None) -> str | None:
    if score is None:
        return None
    for lo, g in GRADES:
        if score >= lo:
            return g
    return "F"


def _crit(key: str, points: float, status: str, detail: str) -> dict:
    return {"key": key, "label": LABELS[key], "points": round(float(points), 2),
            "max_points": WEIGHTS[key], "status": status, "detail": detail}


def _interfaces(card: dict) -> list[dict]:
    out: list[dict] = []
    for key in ("supportedInterfaces", "additionalInterfaces", "interfaces"):
        items = card.get(key)
        if isinstance(items, list):
            out.extend(i for i in items if isinstance(i, dict))
    return out


def _version_tuple(v: str):
    m = _VERSION_RE.match(v.strip())
    return tuple(int(g or 0) for g in m.groups()) if m else None


def declared_versions(card: dict) -> list[str]:
    vs = [str(i.get("protocolVersion")).strip() for i in _interfaces(card)
          if i.get("protocolVersion")]
    if card.get("protocolVersion"):
        vs.append(str(card["protocolVersion"]).strip())
    return [v for v in vs if _version_tuple(v)]


def preferred_rpc(card: dict, card_url: str | None = None) -> tuple[str | None, str]:
    """(endpoint, 'v1'|'v03') to probe: first JSON-RPC 1.x interface, else 0.3."""
    for itf in _interfaces(card):
        binding = str(itf.get("protocolBinding") or itf.get("transport") or "JSONRPC").upper()
        vt = _version_tuple(str(itf.get("protocolVersion") or ""))
        if itf.get("url") and binding == "JSONRPC" and vt and vt[0] >= 1:
            return str(itf["url"]), "v1"
    url = card.get("url")
    if isinstance(url, str) and url:
        if card_url and url.startswith("/") and not url.startswith("//"):
            from urllib.parse import urljoin
            url = urljoin(card_url, url)
        return url, "v03"
    for itf in _interfaces(card):
        binding = str(itf.get("protocolBinding") or itf.get("transport") or "JSONRPC").upper()
        if itf.get("url") and binding == "JSONRPC":
            return str(itf["url"]), "v03"
    return None, "v03"


# --------------------------------------------------------------------------
# Card criteria
# --------------------------------------------------------------------------

def score_valid_card(card: Any) -> dict:
    if not isinstance(card, dict):
        return _crit("valid_card", 0, "fail", "No parseable Agent Card at the card URL.")
    findings = []
    for field in ("name", "description", "version"):
        if not (isinstance(card.get(field), str) and card[field].strip()):
            findings.append(f"missing {field}")
    if not (isinstance(card.get("skills"), list) and card["skills"]):
        findings.append("missing skills[]")
    if not isinstance(card.get("capabilities"), dict):
        findings.append("missing capabilities")
    for field in ("defaultInputModes", "defaultOutputModes"):
        if not (isinstance(card.get(field), list) and card[field]):
            findings.append(f"missing {field}")
    if not (_interfaces(card) or card.get("url")):
        findings.append("no endpoint (supportedInterfaces[] or url)")
    if not findings:
        return _crit("valid_card", 10, "pass", "Parseable Agent Card with the required fields.")
    pts = 10 - min(3, len(findings))
    c = _crit("valid_card", pts, "partial", f"{len(findings)} finding(s); one point each, at most three.")
    c["findings"] = findings
    return c


def score_protocol_version(card: Any) -> dict:
    if not isinstance(card, dict):
        return _crit("protocol_version", 0, "fail", "No card.")
    has_si = isinstance(card.get("supportedInterfaces"), list) and any(
        isinstance(i, dict) and i.get("protocolVersion") for i in card["supportedInterfaces"])
    vs = declared_versions(card)
    if not vs:
        return _crit("protocol_version", 0, "fail", "No A2A protocol version declared.")
    best = max(vs, key=_version_tuple)
    major = _version_tuple(best)[0]
    if major >= 1 and has_si:
        patch_form = best.count(".") >= 2
        if patch_form:
            return _crit("protocol_version", 9, "partial",
                         f"Declares A2A {best} in supportedInterfaces[]; use Major.Minor (e.g. 1.0).")
        return _crit("protocol_version", 10, "pass",
                     f"Declares A2A {best} with supportedInterfaces[] (v1 card shape).")
    if major >= 1:
        return _crit("protocol_version", 7, "partial",
                     f"Declares A2A {best} but without supportedInterfaces[] (pre-1.0 card shape).")
    return _crit("protocol_version", 5, "partial",
                 f"Declares pre-1.0 A2A {best}. Upgrade to v1.x for full points.")


def score_skills(card: Any) -> dict:
    skills = card.get("skills") if isinstance(card, dict) else None
    if not isinstance(skills, list) or not skills:
        return _crit("skills", 0, "fail", "No skills declared.")
    full = [s for s in skills if isinstance(s, dict) and s.get("id") and s.get("name")]
    if not full:
        return _crit("skills", 0, "fail", "Skills lack id/name.")
    pts = 10 if len(full) >= 3 else 5
    v1 = any((_version_tuple(v) or (0,))[0] >= 1 for v in declared_versions(card))
    untagged = [s for s in full if not s.get("tags") or not s.get("description")]
    if v1 and untagged and pts > 8:
        return _crit("skills", 8, "partial",
                     f"{len(full)} skills; {len(untagged)} lack tags or description (required in v1).")
    status = "pass" if pts == 10 else "partial"
    return _crit("skills", pts, status, f"Declares {len(full)} skill(s) with structured metadata.")


def score_security(card: Any) -> dict:
    if not isinstance(card, dict):
        return _crit("security", 0, "fail", "No card.")
    schemes = card.get("securitySchemes")
    items = list(schemes.values()) if isinstance(schemes, dict) else (
        schemes if isinstance(schemes, list) else [])
    items = [s for s in items if isinstance(s, dict)]
    text = json.dumps(items).lower()
    if items and ("mtls" in text or "mutualtls" in text):
        return _crit("security", 5, "pass", "Declares mutual TLS.")
    if items and "oauth2" in text and ("pkce" in text or "s256" in text):
        return _crit("security", 4, "pass", "Declares OAuth2 with PKCE.")
    if items:
        return _crit("security", 2, "partial", f"Declares {len(items)} security scheme(s).")
    reqs = card.get("securityRequirements", card.get("security"))
    if isinstance(reqs, list) and not reqs:
        return _crit("security", 2, "partial", "Explicitly declares no authentication (public).")
    return _crit("security", 0, "info", "Neither securitySchemes nor securityRequirements declared.")


def score_identity(card: Any, owner_verified: bool) -> dict:
    prov = card.get("provider") if isinstance(card, dict) else None
    declared = isinstance(prov, dict) and bool(prov.get("organization")) and bool(prov.get("url"))
    if declared and owner_verified:
        return _crit("identity", 8, "partial",
                     "Provider declared and domain control verified on agent-tools.cloud "
                     "(company-registry check not available yet).")
    if declared:
        return _crit("identity", 5, "partial",
                     "Provider declared; claim this listing to verify domain control.")
    if owner_verified:
        return _crit("identity", 3, "partial", "Domain control verified, but no provider in the card.")
    return _crit("identity", 0, "fail", "No provider organization/url declared.")


def score_freshness(card: Any, last_seen: int | None, x402_supported: bool, now: int) -> dict:
    pts, notes = 0, []
    if last_seen:
        age = now - int(last_seen)
        if age <= 7 * 86400:
            pts += 4; notes.append("seen in a source within 7 days")
        elif age <= 30 * 86400:
            pts += 2; notes.append("seen within 30 days")
        else:
            notes.append("not seen in a source for over 30 days")
    blob = json.dumps(card.get("capabilities") or {}).lower() if isinstance(card, dict) else ""
    blob += json.dumps(card.get("securitySchemes") or {}).lower() if isinstance(card, dict) else ""
    if x402_supported or any(f in blob for f in _MODERN_FLAGS):
        pts += 1; notes.append("declares a modern payment/commerce flag")
    pts = min(5, pts)
    status = "pass" if pts >= 4 else ("partial" if pts else "fail")
    return _crit("freshness", pts, status, ("; ".join(notes) or "no freshness signal") + ".")


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _clean_empty(value):
    if isinstance(value, dict):
        out = {k: c for k, v in value.items() if (c := _clean_empty(v)) is not None}
        return out or None
    if isinstance(value, list):
        out = [c for v in value if (c := _clean_empty(v)) is not None]
        return out or None
    if isinstance(value, str) and not value:
        return None
    return value


def canonical_card(card: dict) -> bytes:
    """Canonical JSON of the card without `signatures`: empty values removed,
    keys sorted, no whitespace (the a2a-sdk rule applied to the raw JSON)."""
    body = _clean_empty({k: v for k, v in card.items() if k != "signatures"}) or {}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sdk_canonical(card: dict) -> bytes | None:
    """Canonical payload exactly as the official a2a-sdk signer builds it
    (proto field presence + RFC 8785). None when the SDK is unavailable."""
    # The SDK logs a spec-compliance warning per card; the valid_card
    # criterion already reports those findings, so keep the job log quiet.
    logging.getLogger("a2a.utils.proto_utils").setLevel(logging.ERROR)
    try:
        from google.protobuf.json_format import ParseDict
        from a2a.types import AgentCard
        from a2a.utils import signing
        proto = ParseDict(card, AgentCard(), ignore_unknown_fields=True)
        return signing._canonicalize_agent_card(proto).encode("utf-8")
    except Exception:  # noqa: BLE001 - SDK missing or card not representable
        return None


def canonical_variants(card: dict) -> list[bytes]:
    out = []
    sdk = _sdk_canonical(card)
    if sdk:
        out.append(sdk)
    raw = canonical_card(card)
    if raw not in out:
        out.append(raw)
    return out


_SITES = None


def _site(host: str) -> str:
    """Registrable domain (eTLD+1, private PSL entries included, offline list),
    so a2a.example.com and example.com match but two tenants of a shared
    hosting suffix such as *.onrender.com do not."""
    global _SITES
    if _SITES is None:
        import tldextract
        _SITES = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(),
                                       include_psl_private_domains=True)
    return _SITES(host).top_domain_under_public_suffix or host


def _same_site(a: str, b: str) -> bool:
    ha, hb = (urlsplit(a).hostname or "").lower(), (urlsplit(b).hostname or "").lower()
    if not ha or not hb:
        return False
    return ha == hb or _site(ha) == _site(hb)


def score_signature(card: Any, card_url: str | None,
                    fetch_json: Callable[[str], Any] | None) -> dict:
    sigs = card.get("signatures") if isinstance(card, dict) else None
    if not isinstance(sigs, list) or not sigs:
        return _crit("signature", 0, "info", "Card is unsigned.")
    if fetch_json is None or not card_url:
        return _crit("signature", 3, "partial", "Card carries a signature that was not verified.")
    import jwt  # PyJWT
    payloads = canonical_variants(card)
    reasons = []
    for sig in sigs[:3]:
        if not isinstance(sig, dict) or not sig.get("protected") or not sig.get("signature"):
            reasons.append("malformed signature entry"); continue
        try:
            header = json.loads(_b64url_decode(sig["protected"]))
        except Exception:
            reasons.append("unreadable protected header"); continue
        alg, kid = header.get("alg"), header.get("kid")
        jku = header.get("jku") or (
            f"{urlsplit(card_url).scheme}://{urlsplit(card_url).netloc}/.well-known/jwks.json")
        if not _same_site(jku, card_url):
            reasons.append("jku is not on the provider's site"); continue
        if alg not in ("ES256", "ES384", "RS256", "PS256", "EdDSA"):
            reasons.append(f"unsupported alg {alg}"); continue
        try:
            jwks = fetch_json(jku)
            keys = [k for k in (jwks or {}).get("keys", []) if isinstance(k, dict)]
            key = next((k for k in keys if not kid or k.get("kid") == kid), None)
            if key is None:
                reasons.append("signing key not found in JWKS"); continue
            pub = jwt.PyJWK(key, algorithm=alg).key
        except Exception as exc:  # noqa: BLE001
            reasons.append(type(exc).__name__); continue
        unencoded = header.get("b64") is False  # RFC 7797
        for payload in payloads:
            try:
                if unencoded:
                    jwt.PyJWS().decode_complete(
                        f"{sig['protected']}..{sig['signature']}", key=pub,
                        algorithms=[alg], detached_payload=payload)
                else:
                    # Detached JWS (RFC 7515 App. F): re-attach the canonical
                    # payload base64url-encoded, as the a2a-sdk verifier does.
                    enc = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
                    jwt.PyJWS().decode_complete(
                        f"{sig['protected']}.{enc}.{sig['signature']}", key=pub,
                        algorithms=[alg])
                return _crit("signature", 10, "pass",
                             f"Signature verifies ({alg}, kid {kid or '-'}) against the provider JWKS.")
            except Exception as exc:  # noqa: BLE001 - any failure is "not verified"
                last = type(exc).__name__
        reasons.append(last)
    return _crit("signature", 3, "partial",
                 "Signature present but did not verify: " + ", ".join(reasons[:3]) + ".")


# --------------------------------------------------------------------------
# Observed criteria
# --------------------------------------------------------------------------

def probe_jsonrpc(client, endpoint: str | None, flavour: str) -> dict:
    """Read-only liveness probe: GetTask/tasks/get for a random task id.

    Returns {"state": live|auth_gated|wrong_shape|unreachable, "latency_ms",
    "http_status"}. Never sends a message, so it cannot trigger agent work.
    """
    if not endpoint:
        return {"state": "unreachable", "latency_ms": None, "http_status": None,
                "detail": "no JSON-RPC endpoint"}
    task_id = str(uuid.uuid4())
    if flavour == "v1":
        body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id}}
        headers = {"A2A-Version": "1.0", "Content-Type": "application/json"}
    else:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": task_id}}
        headers = {"Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        r = client.post(endpoint, json=body, headers=headers, timeout=RPC_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return {"state": "unreachable", "latency_ms": None, "http_status": None,
                "detail": type(exc).__name__}
    dt = int((time.monotonic() - t0) * 1000)
    sc = r.status_code
    if sc in (401, 402, 403, 407):
        return {"state": "auth_gated", "latency_ms": dt, "http_status": sc,
                "detail": f"HTTP {sc}"}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict) and data.get("jsonrpc") == "2.0" and (
            "result" in data or isinstance(data.get("error"), dict)):
        return {"state": "live", "latency_ms": dt, "http_status": sc,
                "detail": f"{body['method']} answered with JSON-RPC 2.0"}
    if sc >= 500 or sc == 404:
        return {"state": "unreachable", "latency_ms": dt, "http_status": sc,
                "detail": f"HTTP {sc}"}
    return {"state": "wrong_shape", "latency_ms": dt, "http_status": sc,
            "detail": f"HTTP {sc} without a JSON-RPC 2.0 envelope"}


_RPC_POINTS = {"live": 25, "auth_gated": 15, "wrong_shape": 5, "unreachable": 0}


def _latency_penalty(ms: int | None) -> int:
    if ms is None or ms <= 2500:
        return 0
    if ms <= 5000:
        return 1
    if ms <= 8000:
        return 2
    return 3


def score_live_jsonrpc(rpc: dict | None) -> dict:
    if not rpc:
        return _crit("live_jsonrpc", 0, "fail", "Not probed (no reachable card).")
    state = rpc.get("state", "unreachable")
    pts = _RPC_POINTS.get(state, 0)
    pen = _latency_penalty(rpc.get("latency_ms")) if pts else 0
    pts = max(0, pts - pen)
    status = {"live": "pass", "auth_gated": "partial", "wrong_shape": "partial"}.get(state, "fail")
    detail = {"live": "Endpoint answers A2A JSON-RPC",
              "auth_gated": "Endpoint requires authentication or payment",
              "wrong_shape": "Endpoint answers, but not with JSON-RPC 2.0",
              "unreachable": "Endpoint did not answer"}.get(state, state)
    if rpc.get("latency_ms") is not None:
        detail += f" ({rpc['latency_ms']} ms)"
    if pen:
        detail += f"; slow response -{pen}"
    return _crit("live_jsonrpc", pts, status, detail + ".")


def score_uptime(ok: int, total: int) -> dict:
    if total <= 0:
        return _crit("uptime", 0, "info", "No probes recorded yet.")
    ratio = ok / total
    pts = 15 * ratio
    if total < UPTIME_MIN_PROBES:
        return _crit("uptime", pts, "provisional",
                     f"{ok}/{total} probes succeeded; final after {UPTIME_MIN_PROBES} probes in 30 days.")
    status = "pass" if ratio >= 0.95 else ("partial" if ratio > 0 else "fail")
    return _crit("uptime", pts, status, f"{ok}/{total} probes succeeded over 30 days ({ratio:.0%}).")


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _card_declares_payment(card: Any) -> bool:
    if not isinstance(card, dict):
        return False
    blob = json.dumps(card.get("capabilities") or {}).lower()
    return any(f in blob for f in ("x402", "ap2", "mpp", "payment"))


def assess(card: Any, card_url: str | None, *, rpc: dict | None, uptime_ok: int,
           uptime_total: int, owner_verified: bool, last_seen: int | None,
           x402_supported: bool, fetch_json: Callable[[str], Any] | None = None,
           now: int | None = None, sources=()) -> dict:
    from . import listing_readiness as L
    now = int(now or time.time())
    criteria = [
        score_valid_card(card),
        score_live_jsonrpc(rpc),
        score_protocol_version(card),
        score_signature(card, card_url, fetch_json),
        score_uptime(uptime_ok, uptime_total),
        score_skills(card),
        score_identity(card, owner_verified),
        score_freshness(card, last_seen, x402_supported, now),
        score_security(card),
    ]
    score = round(sum(c["points"] for c in criteria), 1)
    rpc_status = (rpc or {}).get("http_status")
    declared = _card_declares_payment(card)
    evidence = None
    if x402_supported or declared or rpc_status == 402:
        evidence = L.payment_evidence(challenge=rpc_status == 402, declared=declared,
                                      sources=L.source_names(sources))
    latency = (rpc or {}).get("latency_ms")
    return {"version": VERSION, "kind": "a2a", "score": score, "grade": grade(score),
            "max_score": 100, "applicable_max": 100, "checked_at": now,
            "criteria": criteria, "methodology": METHODOLOGY,
            "responsiveness": L.responsiveness(latency),
            "payment_evidence": evidence}
