"""Bounded owner-declared MCP pricing; never infer prices from prose."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


AUTH_CHOICES = (
    ("", "Unspecified"), ("none", "No credentials for public MCP discovery"),
    ("api_key", "API key"), ("bearer_api_key", "Bearer API key"),
    ("oauth", "OAuth"), ("oauth2", "OAuth 2"),
    ("authorization_header", "Authorization header"),
    ("required_headers", "Required headers"), ("required", "Credentials required"),
    ("smithery_api_key", "Smithery API key"), ("apify_token", "Apify token"),
)


def normalize_auth(value, previous=None):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError("Auth must be an authentication scheme, not credentials")
    value = value.strip()
    if value.lower() in {choice[0] for choice in AUTH_CHOICES}:
        return value.lower() or None
    if value == previous:
        return value
    raise ValueError("Auth must be a supported authentication scheme, not credentials")


def auth_choices(previous):
    choices = [{"value": value, "label": label} for value, label in AUTH_CHOICES]
    if previous and previous not in {choice["value"] for choice in choices}:
        choices.append({"value": previous, "label": "Existing scheme: " + previous})
    return choices


def normalize(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 32768:
            raise ValueError("Pricing declaration exceeds 32 KiB")
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("Pricing must be a JSON object") from None
    if not isinstance(value, dict) or set(value) - {"offers", "buyer_approval_required"}:
        raise ValueError("Pricing accepts offers and buyer_approval_required only")
    approval = value.get("buyer_approval_required")
    if approval is not None and not isinstance(approval, bool):
        raise ValueError("buyer_approval_required must be a boolean or null")
    offers = value.get("offers")
    if not isinstance(offers, list) or not 1 <= len(offers) <= 20:
        raise ValueError("Pricing requires between 1 and 20 explicit offers")
    normalized = []
    for offer in offers:
        if (not isinstance(offer, dict)
                or set(offer) - {"@type", "name", "description", "price", "priceCurrency"}
                or offer.get("@type", "Offer") != "Offer"):
            raise ValueError("Each pricing offer must use supported Offer fields")
        name = offer.get("name")
        description = offer.get("description")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Each offer requires an operation name of at most 200 characters")
        if description is not None and (not isinstance(description, str) or len(description) > 2000):
            raise ValueError("Offer description must be text of at most 2000 characters")
        price = offer.get("price")
        currency = offer.get("priceCurrency")
        if (isinstance(price, bool) or not isinstance(price, (str, int, float))
                or not re.fullmatch(r"\d{1,12}(?:\.\d{1,8})?", str(price))):
            raise ValueError("Each offer requires an explicit non-negative decimal price")
        try:
            amount = Decimal(str(price))
        except InvalidOperation:
            raise ValueError("Invalid pricing amount") from None
        if not amount.is_finite() or amount > Decimal("1000000"):
            raise ValueError("Pricing amount is outside the supported range")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Each offer requires an explicit three-letter currency")
        entry = {"@type": "Offer", "name": name.strip(), "price": format(amount, "f"),
                 "priceCurrency": currency}
        if description and description.strip():
            entry["description"] = description.strip()
        normalized.append(entry)
    result = {"offers": normalized, "buyer_approval_required": approval}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 32768:
        raise ValueError("Pricing declaration exceeds 32 KiB")
    return result


def serialize(value, endpoint):
    pricing = normalize(value)
    if pricing is None:
        return None
    if not endpoint:
        raise ValueError("A public MCP endpoint is required before declaring pricing")
    pricing["_endpoint_hash"] = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    return json.dumps(pricing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read(value, endpoint):
    if not value or not endpoint:
        return None
    try:
        stored = json.loads(value) if isinstance(value, str) else dict(value)
        if stored.pop("_endpoint_hash", None) != hashlib.sha256(endpoint.encode("utf-8")).hexdigest():
            return None
        return normalize(stored)
    except (TypeError, ValueError, AttributeError):
        return None


def access_summary(pricing):
    paid = any(Decimal(offer["price"]) > 0 for offer in pricing["offers"])
    free = any(Decimal(offer["price"]) == 0 for offer in pricing["offers"])
    return {"paid": paid, "billing": "mixed-free-and-paid" if paid and free else "per-operation" if paid else "free",
            "buyer_approval_required": pricing["buyer_approval_required"]}