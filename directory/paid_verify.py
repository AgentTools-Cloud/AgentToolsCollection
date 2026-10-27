"""Sponsored paid verification: a user pays a listed x402 endpoint, we call it once.

Non-custodial: the user's wallet signs an EIP-3009 authorization whose recipient
is the listing's own payTo. We only relay that signature in a single request and
record evidence. We never hold keys, balances or reusable authorizations.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any

import httpx

from . import cards, db, public_http

BASE_NETWORK = "eip155:8453"
BASE_CHAIN_ID = 8453
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_DOMAIN = {"name": "USD Coin", "version": "2"}
MAX_AMOUNT_ATOMIC = 1_000_000  # $1.00 in 6-decimal USDC
VALID_SECONDS = 300
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_REQUEST_SECONDS = 45.0
SETTLEMENT_RETRY_SECONDS = 15
RETENTION_DAYS = 90
TIMEOUT = httpx.Timeout(connect=8.0, read=30.0, write=10.0, pool=5.0)
UA = "agent-tools.cloud paid-verification/1 (+https://agent-tools.cloud/about#paid-verification)"
EVIDENCE_DAYS = 30
BASE_RPC_URL = os.getenv("PAID_VERIFY_BASE_RPC_URL", "https://base.gateway.tenderly.co")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
AUTHORIZATION_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"

TERMINAL = {"succeeded", "delivery_failed", "payment_rejected",
            "failed_before_dispatch", "indeterminate", "expired"}

TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_verifications (
  id              TEXT PRIMARY KEY,
  service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  user_id         INTEGER NOT NULL,
  status          TEXT NOT NULL,
  endpoint_url    TEXT NOT NULL,
  endpoint_hash   TEXT NOT NULL,
  challenge_hash  TEXT NOT NULL,
  requirements    TEXT NOT NULL,
  payer           TEXT NOT NULL,
  pay_to          TEXT NOT NULL,
  amount_atomic   TEXT NOT NULL,
  nonce           TEXT NOT NULL,
  valid_after     INTEGER NOT NULL,
  valid_before    INTEGER NOT NULL,
  http_status     INTEGER,
  latency_ms      INTEGER,
  response_type   TEXT,
  response_bytes  INTEGER,
  response_sha256 TEXT,
  settled         INTEGER,
  tx_hash         TEXT,
  score           INTEGER,
  checks          TEXT,
  failure_code    TEXT,
  created_at      INTEGER NOT NULL,
  dispatched_at   INTEGER,
  completed_at    INTEGER,
  UNIQUE (nonce)
);
CREATE INDEX IF NOT EXISTS idx_paid_verifications_service
    ON paid_verifications(service_id, completed_at);
CREATE INDEX IF NOT EXISTS idx_paid_verifications_user
    ON paid_verifications(user_id, created_at);
"""


class VerifyError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _canonical_body(body: Any) -> bytes:
    if not isinstance(body, dict):
        raise VerifyError("missing_probe_body", "POST verification requires a public JSON probe_body")
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise VerifyError("invalid_probe_body", "The public probe_body is not valid JSON") from exc
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise VerifyError("request_too_large", "The public probe_body exceeds the size limit")
    return encoded


def _request(method: Any, body: Any = None) -> dict:
    normalized = str(method or "GET").strip().upper()
    if normalized == "GET":
        if body is not None:
            raise VerifyError("invalid_request_template", "GET verification cannot include a request body")
        return {"method": "GET", "body": None, "body_sha256": None}
    if normalized != "POST":
        raise VerifyError("unsupported_method", "Only GET and declared POST endpoints can be verified")
    encoded = _canonical_body(body)
    return {"method": "POST", "body": body, "body_sha256": _sha(encoded)}


def request_template(service: dict) -> dict:
    service = db.row_to_dict(service)
    endpoint = str(service.get("url") or "")
    info = service.get("call_info") if isinstance(service.get("call_info"), dict) else {}
    advertised = str(info.get("primary_url") or endpoint)
    if advertised.rstrip("/") != endpoint.rstrip("/"):
        raise VerifyError("request_mismatch", "The listing request template names a different endpoint")
    if info and not info.get("primary_url"):
        service = {**service, "call_info": {**info, "primary_url": endpoint}}
    explicit_method = str(info.get("method") or "").strip().upper()
    if explicit_method and explicit_method not in {"GET", "POST"}:
        raise VerifyError("unsupported_method", "Only GET and declared POST endpoints can be verified")
    request = cards.primary_http_request(service)
    method = explicit_method or str(request.get("method") or "GET").upper()
    body = None
    if method == "POST":
        encoded = cards.primary_json_body(service, endpoint)
        if encoded is None:
            raise VerifyError("missing_probe_body", "POST verification requires one unambiguous public JSON probe_body")
        try:
            body = json.loads(encoded)
        except (TypeError, ValueError, RecursionError) as exc:
            raise VerifyError("invalid_probe_body", "The public probe_body is not valid JSON") from exc
    return _request(method, body)


def _stored_requirements(value: str) -> tuple[dict, dict]:
    try:
        stored = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise VerifyError("invalid_verification", "Stored verification data is invalid", 409) from exc
    if not isinstance(stored, dict):
        raise VerifyError("invalid_verification", "Stored verification data is invalid", 409)
    if isinstance(stored.get("payment"), dict) and isinstance(stored.get("request"), dict):
        req = stored["payment"]
        raw_request = stored["request"]
        request = _request(raw_request.get("method"), raw_request.get("body"))
        if raw_request.get("body_sha256") != request["body_sha256"]:
            raise VerifyError("invalid_verification", "Stored request template hash is invalid", 409)
        return req, request
    return stored, _request("GET")


def _payment_fingerprint(value: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    extra = value.get("extra") if isinstance(value.get("extra"), dict) else {}
    amount = value.get("amount") if value.get("amount") is not None else value.get("amount_raw")
    pay_to = value.get("payTo") if value.get("payTo") is not None else value.get("pay_to")
    timeout = value.get("maxTimeoutSeconds")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError, OverflowError):
        return None
    asset = _addr(value.get("asset"))
    pay_to = _addr(pay_to)
    if not asset or not pay_to or not str(amount or "").isascii() or not str(amount or "").isdecimal():
        return None
    return {
        "scheme": str(value.get("scheme") or "").lower(),
        "network": str(value.get("network") or ""),
        "asset": asset,
        "amount": str(amount),
        "payTo": pay_to,
        "maxTimeoutSeconds": timeout,
        "extra": {
            "name": extra.get("name"),
            "version": extra.get("version"),
            "assetTransferMethod": extra.get("assetTransferMethod"),
        },
    }


def _payment_matches(service: dict, req: dict) -> bool:
    service = db.row_to_dict(service)
    payment = service.get("payment")
    if not isinstance(payment, dict):
        return False
    accepts = payment.get("accepts")
    candidates = ([item for item in accepts if isinstance(item, dict)]
                  if isinstance(accepts, list) else [payment])
    expected = _payment_fingerprint(req)
    return expected is not None and any(_payment_fingerprint(item) == expected for item in candidates)


def _addr(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) == 42 and text.startswith("0x"):
        try:
            int(text[2:], 16)
            return text
        except ValueError:
            return None
    return None


def _tx_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 66 or not text.startswith("0x"):
        return None
    try:
        int(text[2:], 16)
    except ValueError:
        return None
    return text


def _topic_address(value: str) -> str:
    return "0x" + "0" * 24 + value[2:].lower()


def _rpc(method: str, params: list) -> Any:
    if not BASE_RPC_URL.startswith("https://"):
        raise ValueError("Base RPC must use https")
    with public_http.client(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=False,
            headers={"User-Agent": UA, "Content-Type": "application/json"}) as client:
        response = client.post(BASE_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
        raise ValueError("Base RPC returned an invalid response")
    return payload["result"]


def verify_onchain_settlement(req: dict, payer: str, nonce: str,
                              tx_hash: str) -> tuple[bool, str]:
    try:
        chain_id = _rpc("eth_chainId", [])
        if int(str(chain_id or "0"), 16) != BASE_CHAIN_ID:
            return False, "wrong_chain"
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            return False, "receipt_pending"
        if (_tx_hash(receipt.get("transactionHash")) or "").lower() != tx_hash.lower():
            return False, "transaction_mismatch"
        if int(str(receipt.get("status") or "0"), 16) != 1 or not receipt.get("blockNumber"):
            return False, "transaction_failed"
        payer_topic = _topic_address(payer)
        pay_to_topic = _topic_address(_addr(req.get("payTo")) or "0x" + "0" * 40)
        nonce_topic = str(nonce or "").lower()
        amount = int(str(req.get("amount") or ""))
        authorization_used = False
        transferred = False
        for log in receipt.get("logs") or []:
            if not isinstance(log, dict) or _addr(log.get("address")) != _addr(req.get("asset")):
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics:
                continue
            normalized = [str(topic).lower() for topic in topics]
            if (len(normalized) >= 3 and normalized[0] == AUTHORIZATION_USED_TOPIC
                    and normalized[1] == payer_topic and normalized[2] == nonce_topic):
                authorization_used = True
            if (len(normalized) >= 3 and normalized[0] == TRANSFER_TOPIC
                    and normalized[1] == payer_topic and normalized[2] == pay_to_topic):
                try:
                    transferred = int(str(log.get("data") or "0x0"), 16) == amount
                except ValueError:
                    transferred = False
        if not authorization_used:
            return False, "authorization_event_missing"
        if not transferred:
            return False, "transfer_event_mismatch"
        return True, "verified"
    except (httpx.HTTPError, public_http.UnsafeURL, ValueError, TypeError, KeyError,
            OverflowError, RecursionError):
        return False, "rpc_error"


def _b64json(value: str | None) -> dict | None:
    if not value or len(value) > MAX_HEADER_BYTES:
        return None
    try:
        text = value.strip()
        text += "=" * (-len(text) % 4)
        obj = json.loads(base64.b64decode(text, altchars=b"-_", validate=True))
    except (ValueError, TypeError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _resource_url(challenge: dict) -> str | None:
    resource = challenge.get("resource")
    if isinstance(resource, dict):
        return resource.get("url")
    return resource if isinstance(resource, str) else None


def select_requirement(challenge: dict, endpoint_url: str) -> dict:
    """The single Base-USDC exact requirement we are willing to sign, or raise."""
    if challenge.get("x402Version") != 2:
        raise VerifyError("unsupported_challenge", "Only x402 v2 challenges are supported")
    resource = _resource_url(challenge)
    if resource and resource.rstrip("/") != endpoint_url.rstrip("/"):
        raise VerifyError("resource_mismatch", "The challenge names a different resource")
    accepts = challenge.get("accepts")
    if not isinstance(accepts, list):
        raise VerifyError("unsupported_challenge", "The challenge lists no payment options")
    for req in accepts:
        if not isinstance(req, dict):
            continue
        if (req.get("scheme") != "exact" or req.get("network") != BASE_NETWORK
                or _addr(req.get("asset")) != BASE_USDC):
            continue
        extra = req.get("extra") if isinstance(req.get("extra"), dict) else {}
        if extra.get("name") != USDC_DOMAIN["name"] or extra.get("version") != USDC_DOMAIN["version"]:
            continue
        method = extra.get("assetTransferMethod")
        if method not in (None, "eip3009"):
            continue
        pay_to = _addr(req.get("payTo"))
        amount = str(req.get("amount") or "")
        if not pay_to or not amount.isascii() or not amount.isdecimal():
            continue
        if not 0 < int(amount) <= MAX_AMOUNT_ATOMIC:
            raise VerifyError("amount_too_high", "Price exceeds the $1.00 verification limit")
        timeout = req.get("maxTimeoutSeconds")
        if not isinstance(timeout, int) or timeout <= 0:
            continue
        return {"scheme": "exact", "network": BASE_NETWORK, "asset": req["asset"],
                "amount": amount, "payTo": req["payTo"], "maxTimeoutSeconds": timeout,
                "extra": extra}
    raise VerifyError("unsupported_payment", "No Base USDC exact payment option is offered")


def _fetch(url: str, headers: dict | None = None,
           request: dict | None = None) -> tuple[httpx.Response, bytes, int]:
    request = _request((request or {}).get("method"), (request or {}).get("body"))
    request_headers = {"User-Agent": UA, "Accept": "application/json, */*", **(headers or {})}
    content = None
    if request["method"] == "POST":
        request_headers["Content-Type"] = "application/json"
        content = _canonical_body(request["body"])
    started = time.monotonic()
    with public_http.client(timeout=TIMEOUT, follow_redirects=False,
                            headers=request_headers) as client:
        with client.stream(request["method"], url, content=content) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() - started > MAX_REQUEST_SECONDS:
                    raise VerifyError("response_timeout", "Response exceeded the total time limit", 502)
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise VerifyError("response_too_large", "Response exceeds the size limit", 502)
            elapsed = int((time.monotonic() - started) * 1000)
            return response, bytes(body), elapsed


def fetch_challenge(url: str, request: dict | None = None) -> tuple[dict, str]:
    try:
        public_http.validate_url(url)
        response, _, _ = _fetch(url, request=request)
    except public_http.UnsafeURL as exc:
        raise VerifyError("unsafe_url", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise VerifyError("unreachable", "The endpoint could not be reached", 502) from exc
    if response.status_code != 402:
        raise VerifyError("no_challenge", "The endpoint did not answer 402 Payment Required")
    raw = response.headers.get("PAYMENT-REQUIRED") or ""
    challenge = _b64json(raw)
    if challenge is None:
        raise VerifyError("no_challenge", "No valid x402 v2 PAYMENT-REQUIRED header")
    return challenge, _sha(raw)


def typed_data(req: dict, payer: str, nonce: str, valid_after: int, valid_before: int) -> dict:
    return {
        "types": TYPES,
        "primaryType": "TransferWithAuthorization",
        "domain": {**USDC_DOMAIN, "chainId": BASE_CHAIN_ID, "verifyingContract": req["asset"]},
        "message": {"from": payer, "to": req["payTo"], "value": req["amount"],
                    "validAfter": str(valid_after), "validBefore": str(valid_before),
                    "nonce": nonce},
    }


def recover_signer(data: dict, signature: str) -> str | None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    msg = dict(data["message"])
    for key in ("value", "validAfter", "validBefore"):
        msg[key] = int(msg[key])
    try:
        signable = encode_typed_data(full_message={**data, "message": msg})
        return Account.recover_message(signable, signature=signature).lower()
    except Exception:
        return None


def prepare(user_id: int, service: dict, payer: str) -> dict:
    payer_addr = _addr(payer)
    if not payer_addr:
        raise VerifyError("bad_payer", "Enter a valid 0x wallet address")
    url = str(service.get("url") or "")
    if not url.startswith("https://"):
        raise VerifyError("unsafe_url", "Only https endpoints can be verified")
    request = request_template(service)
    challenge, challenge_hash = fetch_challenge(url, request)
    req = select_requirement(challenge, url)
    now = int(time.time())
    valid_after = now - 60
    valid_before = now + min(VALID_SECONDS, req["maxTimeoutSeconds"] + 60)
    nonce = "0x" + secrets.token_hex(32)
    vid = uuid.uuid4().hex
    with db.writer(immediate=True) as conn:
        expire_stale(conn, now)
        open_count = conn.execute(
            "SELECT COUNT(*) FROM paid_verifications WHERE user_id=? AND status IN ('prepared','dispatching')",
            (user_id,)).fetchone()[0]
        if open_count >= 3:
            raise VerifyError("too_many_open", "Finish or wait out your open verifications first", 429)
        conn.execute(
            "INSERT INTO paid_verifications(id,service_id,user_id,status,endpoint_url,endpoint_hash,"
            "challenge_hash,requirements,payer,pay_to,amount_atomic,nonce,valid_after,valid_before,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, int(service["id"]), int(user_id), "prepared", url, _sha(url), challenge_hash,
             json.dumps({"version": 1, "payment": req, "request": request},
                        separators=(",", ":")), payer_addr, _addr(req["payTo"]),
             req["amount"], nonce, valid_after, valid_before, now))
    return {"id": vid, "amount_atomic": req["amount"], "amount_usd": int(req["amount"]) / 1e6,
            "pay_to": req["payTo"], "network": BASE_NETWORK, "valid_before": valid_before,
            "request_method": request["method"],
            "request_body_sha256": request["body_sha256"],
            "typed_data": typed_data(req, payer_addr, nonce, valid_after, valid_before)}


def _load(conn, vid: str, user_id: int):
    row = conn.execute("SELECT * FROM paid_verifications WHERE id=? AND user_id=?",
                       (vid, user_id)).fetchone()
    if row is None:
        raise VerifyError("not_found", "Verification not found", 404)
    return dict(row)


def _finish(vid: str, **fields) -> None:
    fields["completed_at"] = int(time.time())
    cols = ",".join(f"{k}=?" for k in fields)
    with db.writer(immediate=True) as conn:
        conn.execute(f"UPDATE paid_verifications SET {cols} WHERE id=? AND status='dispatching'",
                     (*fields.values(), vid))


def score(http_status: int, settled: bool, tx_hash: str | None, content_type: str,
          body: bytes, latency_ms: int, price_matches: bool) -> tuple[int, dict]:
    delivered = 200 <= http_status < 300
    is_json = "json" in content_type.lower()
    json_ok = False
    if is_json:
        try:
            json.loads(body or b"null")
            json_ok = True
        except (ValueError, RecursionError):
            json_ok = False
    checks = {
        "delivered": delivered,
        "settled": bool(settled and tx_hash),
        "price_matches": price_matches,
        "non_empty": bool(body),
        "well_formed": json_ok if is_json else bool(body),
        "latency_ms": latency_ms,
    }
    pts = 0
    pts += 35 if delivered else 0
    pts += 20 if checks["settled"] else 0
    pts += 10 if price_matches else 0
    pts += 10 if checks["non_empty"] else 0
    pts += 10 if checks["well_formed"] else 0
    pts += 15 if latency_ms <= 3000 else 8 if latency_ms <= 10000 else 0
    return (pts if delivered else min(pts, 30)), checks


def execute(vid: str, user_id: int, signature: str) -> dict:
    signature = str(signature or "").strip()
    if not (signature.startswith("0x") and 132 <= len(signature) <= 134):
        raise VerifyError("bad_signature", "Signature is malformed")
    now = int(time.time())
    with db.writer(immediate=True) as conn:
        row = _load(conn, vid, user_id)
        if row["status"] != "prepared":
            raise VerifyError("not_prepared", "This verification was already used", 409)
        if row["valid_before"] - 15 < now:
            conn.execute("UPDATE paid_verifications SET status='expired', completed_at=? WHERE id=?", (now, vid))
            raise VerifyError("expired", "Authorization window expired; start again", 409)
        req, request = _stored_requirements(row["requirements"])
        data = typed_data(req, row["payer"], row["nonce"], row["valid_after"], row["valid_before"])
        if recover_signer(data, signature) != row["payer"]:
            raise VerifyError("bad_signature", "Signature does not match the payer wallet")
        claimed = conn.execute(
            "UPDATE paid_verifications SET status='dispatching', dispatched_at=? WHERE id=? AND status='prepared'",
            (now, vid)).rowcount
        if claimed != 1:
            raise VerifyError("not_prepared", "This verification was already used", 409)

    # Re-check both the public request template and live price before paying.
    try:
        with db.connect(read_only=True) as conn:
            service = conn.execute("SELECT * FROM services WHERE id=?", (row["service_id"],)).fetchone()
        live_service = dict(service) if service is not None else None
        live_request = request_template(live_service) if live_service is not None else None
    except VerifyError as exc:
        _finish(vid, status="failed_before_dispatch", failure_code=exc.code)
        raise
    live_url = str((live_service or {}).get("url") or "").rstrip("/")
    if live_url != row["endpoint_url"].rstrip("/") or live_request != request:
        _finish(vid, status="failed_before_dispatch", failure_code="request_changed")
        raise VerifyError("request_changed", "The listing changed its request template; nothing was paid", 409)
    try:
        challenge, challenge_hash = fetch_challenge(row["endpoint_url"], request)
        live = select_requirement(challenge, row["endpoint_url"])
    except VerifyError as exc:
        _finish(vid, status="failed_before_dispatch", failure_code=exc.code)
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        _finish(vid, status="failed_before_dispatch", failure_code="invalid_challenge")
        raise VerifyError("invalid_challenge", "The live payment challenge is invalid", 409) from exc
    if (live["amount"] != req["amount"] or _addr(live["payTo"]) != _addr(req["payTo"])):
        _finish(vid, status="failed_before_dispatch", failure_code="quote_changed")
        raise VerifyError("quote_changed", "The endpoint changed its price or recipient; nothing was paid", 409)

    payload = {
        "x402Version": 2,
        "accepted": req,
        "resource": {"url": row["endpoint_url"]},
        "payload": {"signature": signature, "authorization": {
            "from": row["payer"], "to": req["payTo"], "value": req["amount"],
            "validAfter": str(row["valid_after"]), "validBefore": str(row["valid_before"]),
            "nonce": row["nonce"]}},
    }
    header = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    try:
        response, body, latency = _fetch(row["endpoint_url"], {"PAYMENT-SIGNATURE": header}, request)
    except VerifyError as exc:
        _finish(vid, status="indeterminate", failure_code=exc.code)
        return result(vid, user_id)
    except (httpx.HTTPError, public_http.UnsafeURL):
        # The request may have settled before the failure; never retry.
        _finish(vid, status="indeterminate", failure_code="transport_error")
        return result(vid, user_id)

    try:
        receipt = _b64json(response.headers.get("PAYMENT-RESPONSE")
                           or response.headers.get("X-PAYMENT-RESPONSE")) or {}
        tx_hash = _tx_hash(receipt.get("transaction") or receipt.get("txHash"))
        settled = False
        settlement_check = "invalid_receipt"
        if receipt.get("success") is True and tx_hash is not None:
            settled, settlement_check = verify_onchain_settlement(
                req, row["payer"], row["nonce"], tx_hash)
        ctype = (response.headers.get("content-type") or "")[:100]
        points, checks = score(response.status_code, settled, tx_hash, ctype, body, latency, True)
        checks["settlement_check"] = settlement_check
        checks["settlement_checked_at"] = int(time.time())
        if response.status_code == 402:
            status = "payment_rejected"
        elif 200 <= response.status_code < 300:
            status = "succeeded"
        else:
            status = "delivery_failed"
        _finish(vid, status=status, http_status=response.status_code, latency_ms=latency,
                response_type=ctype, response_bytes=len(body), response_sha256=_sha(body),
                settled=int(settled), tx_hash=tx_hash, score=points,
                checks=json.dumps(checks, separators=(",", ":")))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        _finish(vid, status="indeterminate", failure_code="invalid_response")
    return result(vid, user_id)


def expire_stale(conn, now: int | None = None) -> int:
    now = now or int(time.time())
    cur = conn.execute("UPDATE paid_verifications SET status='expired', completed_at=? "
                       "WHERE status='prepared' AND valid_before<?", (now, now))
    stuck = conn.execute("UPDATE paid_verifications SET status='indeterminate', "
                         "failure_code='interrupted', completed_at=? "
                         "WHERE status='dispatching' AND dispatched_at<?", (now, now - 300))
    deleted = conn.execute(
        "DELETE FROM paid_verifications WHERE status IN "
        "('succeeded','delivery_failed','payment_rejected','failed_before_dispatch',"
        "'indeterminate','expired') AND completed_at<?",
        (now - RETENTION_DAYS * 86400,))
    return cur.rowcount + stuck.rowcount + deleted.rowcount


_PUBLIC = ("id", "status", "http_status", "latency_ms", "response_type", "response_bytes",
           "response_sha256", "settled", "tx_hash", "score", "failure_code",
           "amount_atomic", "pay_to", "created_at", "completed_at")


def _public(row: dict) -> dict:
    out = {k: row.get(k) for k in _PUBLIC}
    try:
        out["checks"] = json.loads(row["checks"]) if row.get("checks") else None
    except (TypeError, ValueError, RecursionError):
        out["checks"] = None
    out["amount_usd"] = int(row["amount_atomic"]) / 1e6
    out["settled"] = bool(row.get("settled"))
    return out


def _refresh_settlement(vid: str, user_id: int) -> None:
    with db.connect(read_only=True) as conn:
        row = _load(conn, vid, user_id)
    if row.get("settled") or not _tx_hash(row.get("tx_hash")):
        return
    try:
        checks = json.loads(row.get("checks") or "{}")
    except (TypeError, ValueError, RecursionError):
        return
    if checks.get("settlement_check") not in {"receipt_pending", "rpc_error"}:
        return
    now = int(time.time())
    if now - int(checks.get("settlement_checked_at") or 0) < SETTLEMENT_RETRY_SECONDS:
        return
    try:
        req, _ = _stored_requirements(row["requirements"])
        verified, reason = verify_onchain_settlement(
            req, row["payer"], row["nonce"], row["tx_hash"])
    except VerifyError:
        return
    checks["settlement_check"] = reason
    checks["settlement_checked_at"] = now
    score_value = int(row.get("score") or 0)
    if verified:
        checks["settled"] = True
        score_value = min(100, score_value + 20)
    with db.writer(immediate=True) as conn:
        conn.execute(
            "UPDATE paid_verifications SET settled=?,score=?,checks=? "
            "WHERE id=? AND user_id=? AND settled=0",
            (int(verified), score_value, json.dumps(checks, separators=(",", ":")),
             vid, user_id))


def result(vid: str, user_id: int) -> dict:
    _refresh_settlement(vid, user_id)
    with db.connect(read_only=True) as conn:
        return _public(_load(conn, vid, user_id))


def public_summary(conn, service_id: int) -> dict | None:
    since = int(time.time()) - EVIDENCE_DAYS * 86400
    service_row = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    if service_row is None:
        return None
    service = db.row_to_dict(service_row)
    try:
        current_request = request_template(service)
    except VerifyError:
        return None
    rows = conn.execute(
        "SELECT * FROM paid_verifications WHERE service_id=? AND endpoint_hash=? "
        "AND completed_at>=? "
        "AND status IN ('succeeded','delivery_failed','payment_rejected','indeterminate') "
        "ORDER BY completed_at DESC", (service_id, _sha(str(service.get("url") or "")), since))
    matching = []
    for raw in rows:
        row = dict(raw)
        try:
            req, request = _stored_requirements(row["requirements"])
        except VerifyError:
            continue
        try:
            checks = json.loads(row.get("checks") or "{}")
        except (TypeError, ValueError, RecursionError):
            continue
        if (request != current_request or not _payment_matches(service, req)
                or (row.get("settled") and checks.get("settlement_check") != "verified")):
            continue
        matching.append(row)
        if len(matching) >= 20:
            break
    rows = matching
    if not rows:
        return None
    ok = sum(r["status"] == "succeeded" for r in rows)
    return {"runs": len(rows), "succeeded": ok,
            "latest": _public(rows[0]),
            "best_score": max((r["score"] or 0) for r in rows),
            "window_days": EVIDENCE_DAYS}
