"""JWS signatures for agent-tools.cloud's own A2A Agent Card (A2A 1.0 §8.4).

The private key is an ES256 (P-256) PEM read from AGENT_TOOLS_A2A_SIGNING_KEY
(default /etc/agent-tools/a2a-card-signing-es256.pem). It is never logged or
served; only the public JWK is published at /.well-known/jwks.json.

Verifiers disagree on what exactly gets canonicalised, so the card carries two
detached signatures made with the same key over:

1. the a2a-sdk canonical form (card parsed into the 1.0 AgentCard proto, then
   RFC 8785) - what `a2a.utils.signing.create_signature_verifier` checks;
2. RFC 8785 of the raw served JSON minus `signatures` with empty values
   removed - what raw-JSON verifiers (and our own readiness check) use.

A2A verifiers accept a card when at least one signature verifies. If the key
is missing or unusable the card is served unsigned rather than not at all.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from typing import Any

log = logging.getLogger("directory.card_signing")

KEY_PATH_ENV = "AGENT_TOOLS_A2A_SIGNING_KEY"
DEFAULT_KEY_PATH = "/etc/agent-tools/a2a-card-signing-es256.pem"
ALG = "ES256"
JWKS_PATH = "/.well-known/jwks.json"

_lock = threading.Lock()
_state: dict[str, Any] = {"loaded": False, "key": None, "jwk": None, "kid": None}
_sig_cache: dict[tuple[str, bytes, bytes | None], list[dict]] = {}
_SIG_CACHE_MAX = 64


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load() -> None:
    """Load the private key once; derive public JWK and a thumbprint kid."""
    if _state["loaded"]:
        return
    with _lock:
        if _state["loaded"]:
            return
        path = os.environ.get(KEY_PATH_ENV, DEFAULT_KEY_PATH)
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            with open(path, "rb") as fh:
                key = serialization.load_pem_private_key(fh.read(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError("signing key must be an EC P-256 private key")
            nums = key.public_key().public_numbers()
            x, y = _b64u(nums.x.to_bytes(32, "big")), _b64u(nums.y.to_bytes(32, "big"))
            # RFC 7638 JWK thumbprint -> stable kid for this key.
            thumb = _b64u(hashlib.sha256(json.dumps(
                {"crv": "P-256", "kty": "EC", "x": x, "y": y},
                separators=(",", ":"), sort_keys=True).encode()).digest())
            kid = f"agent-tools-a2a-es256-{thumb[:16]}"
            _state.update(key=key, kid=kid, jwk={
                "kty": "EC", "crv": "P-256", "x": x, "y": y,
                "use": "sig", "alg": ALG, "kid": kid})
        except FileNotFoundError:
            log.warning("A2A card signing key not found; serving unsigned card")
        except Exception as exc:  # noqa: BLE001 - never break the card route
            log.error("A2A card signing key unusable (%s); serving unsigned card",
                      type(exc).__name__)
        _state["loaded"] = True


def reset_for_tests() -> None:
    with _lock:
        _state.update(loaded=False, key=None, jwk=None, kid=None)
        _sig_cache.clear()


def public_jwks() -> dict:
    _load()
    return {"keys": [dict(_state["jwk"])] if _state["jwk"] else []}


def _payloads(card: dict) -> list[bytes]:
    from . import a2a_readiness

    out: list[bytes] = []
    sdk = a2a_readiness._sdk_canonical(card)
    if sdk:
        out.append(sdk)
    raw = a2a_readiness.canonical_card(card)
    if raw not in out:
        out.append(raw)
    return out


def sign_card(card: dict, base_url: str) -> dict:
    """Return a copy of ``card`` with detached JWS ``signatures`` attached."""
    _load()
    key, kid = _state["key"], _state["kid"]
    unsigned = {k: v for k, v in card.items() if k != "signatures"}
    if key is None:
        return unsigned
    import jwt  # PyJWT

    jku = base_url.rstrip("/") + JWKS_PATH
    payloads = _payloads(unsigned)
    cache_key = (jku, payloads[0], payloads[1] if len(payloads) > 1 else None)
    sigs = _sig_cache.get(cache_key)
    if sigs is None:
        header = {"alg": ALG, "typ": "JOSE", "kid": kid, "jku": jku}
        sigs = []
        for payload in payloads:
            compact = jwt.api_jws.encode(payload=payload, key=key,
                                         algorithm=ALG, headers=header)
            protected, _, signature = compact.split(".")
            sigs.append({"protected": protected, "signature": signature})
        with _lock:
            if len(_sig_cache) >= _SIG_CACHE_MAX:
                _sig_cache.clear()
            _sig_cache[cache_key] = sigs
    return {**unsigned, "signatures": [dict(s) for s in sigs]}
