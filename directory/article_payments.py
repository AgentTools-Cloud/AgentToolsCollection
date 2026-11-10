from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import httpx

from .articles import ArticleError, ArticleStore, STABLECOIN_NETWORKS, valid_stablecoin_networks


API_BASE = "https://api.nowpayments.io/v1"
CONFIG_PATH = Path("/etc/agent-tools/nowpayments-articles.json")
MAX_PAYMENT_BYTES = 64 * 1024
MAX_CURRENCY_METADATA_BYTES = 1024 * 1024


def valid_ipn_secret(secret: object) -> bool:
    return (isinstance(secret, str) and 16 <= len(secret) <= 512
            and not secret.startswith(("https://", "http://"))
            and all(33 <= ord(character) <= 126 and character != "*" for character in secret))


def strict_payment_json(raw: bytes, *, decimal: bool = True, maximum: int = MAX_PAYMENT_BYTES) -> dict[str, Any]:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid_number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("invalid_number")
        return number

    if not raw or len(raw) > maximum:
        raise ArticleError("invalid_payment_json")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_float=Decimal if decimal else finite_float, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ArticleError("invalid_payment_json") from None
    if not isinstance(result, dict):
        raise ArticleError("invalid_payment_json")
    return result


@dataclass(frozen=True)
class NowPaymentsConfig:
    api_key: str = field(default="", repr=False)
    ipn_secret: str = field(default="", repr=False)
    enabled: bool = False
    approved: bool = False
    signature_verified: bool = False
    signature_format: str = ""
    prices: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    currencies: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    outcome_currency: str = ""
    outcome_network: str = ""
    settlement_mode: str = "specified"
    settlement_currencies: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    terms_version: str = ""
    terms_text: str = ""
    fixed_rate: bool | None = None
    fee_paid_by_user: bool | None = None

    @property
    def ready(self) -> bool:
        currencies_ready = valid_stablecoin_networks(self.currencies)
        settlement_ready = ((self.settlement_mode == "specified" and self.outcome_currency
                             and self.outcome_currency in STABLECOIN_NETWORKS
                             and STABLECOIN_NETWORKS[self.outcome_currency] == self.outcome_network)
                            or (self.settlement_mode == "merchant-default" and self.settlement_currencies
                                and not self.outcome_currency and not self.outcome_network
                                and valid_stablecoin_networks(self.settlement_currencies)))
        return bool(self.enabled and self.approved and self.signature_verified
                    and self.signature_format == "python-sorted-v1" and self.api_key and valid_ipn_secret(self.ipn_secret)
                    and self.api_key != self.ipn_secret
                    and set(self.prices) == {"standard", "premium"} and currencies_ready
                    and settlement_ready and self.terms_version and self.terms_text
                    and isinstance(self.fixed_rate, bool) and isinstance(self.fee_paid_by_user, bool))

    @property
    def merchant_ref(self) -> str:
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()

    @property
    def checkout_hash(self) -> str:
        configuration = {"currencies": dict(self.currencies), "outcome_currency": self.outcome_currency,
                         "outcome_network": self.outcome_network, "fixed_rate": self.fixed_rate,
                         "fee_paid_by_user": self.fee_paid_by_user, "merchant_ref": self.merchant_ref}
        if self.settlement_mode == "merchant-default":
            configuration.update(settlement_mode=self.settlement_mode,
                                 settlement_currencies=dict(self.settlement_currencies))
        return payment_fingerprint(configuration)

    @property
    def publication_terms(self) -> str:
        currencies = ", ".join(currency + " (" + network + ")" for currency, network in self.currencies.items())
        settlement = ("Settlement: the existing NOWPayments merchant payout settings."
                  if self.settlement_mode == "merchant-default" else
                  "Settlement currency: " + self.outcome_currency + " (" + self.outcome_network + ")")
        return (self.terms_text.strip() + "\n\nPayment currencies: " + currencies
            + "\n" + settlement
                + "\nPayment fees paid by: " + ("the author." if self.fee_paid_by_user else "Agent Tools.")
                + "\nExchange rate: " + ("fixed for the provider's quoted period." if self.fixed_rate else "floating."))

    @classmethod
    def from_file(cls, path: Path | None = None) -> NowPaymentsConfig:
        path = Path(path) if path is not None else Path(os.getenv("NOWPAYMENTS_ARTICLES_CONFIG", str(CONFIG_PATH)))
        if not path.is_absolute():
            raise ArticleError("unsafe_payment_configuration")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return cls()
        try:
            with os.fdopen(descriptor, "rb") as handle:
                information = os.fstat(handle.fileno())
                if information.st_mode & 0o077 or information.st_uid not in (0, os.geteuid()):
                    raise ArticleError("unsafe_payment_configuration")
                data = strict_payment_json(handle.read(MAX_PAYMENT_BYTES + 1))
        except OSError:
            raise ArticleError("invalid_payment_configuration") from None
        base = data.get("rest_api_base", API_BASE)
        if not isinstance(base, str) or base.rstrip("/") != API_BASE:
            raise ArticleError("invalid_payment_configuration")
        for name in ("api_key", "ipn_secret"):
            value = data.get(name, "")
            if not isinstance(value, str) or len(value) > 512 or any(character.isspace() for character in value):
                raise ArticleError("invalid_payment_configuration")
            if name == "api_key" and any(not 33 <= ord(character) <= 126 for character in value):
                raise ArticleError("invalid_payment_configuration")
            if name == "ipn_secret" and value and not valid_ipn_secret(value):
                raise ArticleError("invalid_payment_configuration")
        prices = data.get("prices_usd_cents", {})
        currencies = data.get("pay_currencies", {})
        settlement_mode = data.get("settlement_mode", "specified")
        settlement_currencies = data.get("settlement_currencies", {})
        if (not isinstance(prices, dict) or set(prices) - {"standard", "premium"}
                or any(type(value) is not int or not 1 <= value <= 100_000_000 for value in prices.values())
                or not isinstance(currencies, dict) or len(currencies) > 12
                or settlement_mode not in ("specified", "merchant-default")
                or not isinstance(settlement_currencies, dict) or len(settlement_currencies) > 16):
            raise ArticleError("invalid_payment_configuration")
        for mapping in (currencies, settlement_currencies):
            if mapping and not valid_stablecoin_networks(mapping):
                raise ArticleError("invalid_payment_configuration")
        for name, maximum in (("outcome_currency", 32), ("outcome_network", 40),
                              ("terms_version", 120), ("terms_text", 6500)):
            value = data.get(name, "")
            if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
                raise ArticleError("invalid_payment_configuration")
        for name in ("outcome_currency", "outcome_network"):
            if data.get(name) and not re.fullmatch(r"[a-z0-9_-]{2,40}", data[name]):
                raise ArticleError("invalid_payment_configuration")
        return cls(api_key=data.get("api_key", ""), ipn_secret=data.get("ipn_secret", ""),
                   enabled=data.get("sponsored_articles_enabled") is True,
                   approved=data.get("business_approved") is True and data.get("settlement_verified") is True,
                   signature_verified=data.get("ipn_signature_verified") is True,
                   signature_format=data.get("ipn_signature_format", ""), prices=MappingProxyType(prices),
                   currencies=MappingProxyType(currencies), outcome_currency=data.get("outcome_currency", ""),
                   outcome_network=data.get("outcome_network", ""), terms_version=data.get("terms_version", ""),
                   settlement_mode=settlement_mode, settlement_currencies=MappingProxyType(settlement_currencies),
                   terms_text=data.get("terms_text", ""), fixed_rate=data.get("is_fixed_rate"),
                   fee_paid_by_user=data.get("is_fee_paid_by_user"))

    def verify_ipn(self, raw: bytes, signature: str | None) -> dict[str, Any]:
        if not self.ready:
            raise ArticleError("sponsored_payments_unavailable")
        return verify_payment_signature(raw, signature, self.ipn_secret)


def verify_payment_signature(raw: bytes, signature: str | None, secret: str) -> dict[str, Any]:
    if not valid_ipn_secret(secret):
        raise ArticleError("invalid_payment_configuration")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
        raise ArticleError("invalid_payment_signature")
    notification = strict_payment_json(raw, decimal=False)
    try:
        canonical = json.dumps(notification, sort_keys=True, ensure_ascii=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ArticleError("invalid_payment_json") from None
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise ArticleError("invalid_payment_signature")
    return notification


def payment_fingerprint(payment: dict[str, Any]) -> str:
    canonical = json.dumps(payment, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NowPaymentsClient:
    def __init__(self, config: NowPaymentsConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def _request(self, method: str, path: str, *, payload: dict | None = None,
                       parameters: dict | None = None) -> dict[str, Any]:
        if not self.config.ready:
            raise ArticleError("sponsored_payments_unavailable")
        maximum = MAX_CURRENCY_METADATA_BYTES if method == "GET" and path == "full-currencies" else MAX_PAYMENT_BYTES
        try:
            async with httpx.AsyncClient(base_url=API_BASE + "/", transport=self.transport, trust_env=False,
                                        follow_redirects=False, timeout=httpx.Timeout(15, connect=5)) as client:
                async with client.stream(method, path, headers={"x-api-key": self.config.api_key},
                                         json=payload, params=parameters) as response:
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > maximum:
                            raise ArticleError("invalid_payment_json")
                    if response.status_code not in (200, 201):
                        if method == "POST" and response.status_code in (400, 401, 403, 422):
                            body = strict_payment_json(bytes(chunks))
                            if body.get("status") is False and not body.get("id"):
                                raise ArticleError("payment_provider_rejected")
                        raise ArticleError("payment_provider_unavailable")
                    return strict_payment_json(bytes(chunks), maximum=maximum)
        except httpx.HTTPError:
            raise ArticleError("payment_provider_unavailable") from None

    async def preflight(self, cents: int, currency: str) -> None:
        if (currency not in self.config.currencies or currency not in STABLECOIN_NETWORKS
                or STABLECOIN_NETWORKS.get(currency) != self.config.currencies[currency]):
            raise ArticleError("invalid_payment_currency")
        merchant = await self._request("GET", "merchant/coins")
        metadata = await self._request("GET", "full-currencies")
        coins = merchant.get("selectedCurrencies", merchant.get("currencies"))
        entries = metadata.get("currencies")
        if not isinstance(coins, list) or not isinstance(entries, list):
            raise ArticleError("payment_currency_unavailable")
        enabled = {coin.lower() for coin in coins if isinstance(coin, str)}
        if currency not in enabled:
            raise ArticleError("payment_currency_unavailable")
        networks = {}
        entries_by_code = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("code"), str):
                continue
            code = entry["code"].lower()
            if code in networks and networks[code] != entry.get("network"):
                raise ArticleError("payment_network_mismatch")
            networks[code] = entry.get("network")
            entries_by_code[code] = entry
        settlements = (self.config.settlement_currencies if self.config.settlement_mode == "merchant-default" else
                       {self.config.outcome_currency: self.config.outcome_network})
        if (networks.get(currency) != self.config.currencies[currency]
                or any(networks.get(code) != network for code, network in settlements.items())):
            raise ArticleError("payment_network_mismatch")
        if (entries_by_code[currency].get("enable") is not True
                or entries_by_code[currency].get("available_for_payment") is not True):
            raise ArticleError("payment_currency_unavailable")
        if self.config.settlement_mode == "merchant-default" and any(
                entries_by_code[code].get("enable") is not True
                or entries_by_code[code].get("is_stable") is not True
                or entries_by_code[code].get("available_for_to_conversion") is not True
                or entries_by_code[code].get("available_for_payout") is not True for code in settlements):
            raise ArticleError("payment_currency_unavailable")
        parameters = {
            "currency_from": currency,
            "is_fixed_rate": str(self.config.fixed_rate).lower(),
            "is_fee_paid_by_user": str(self.config.fee_paid_by_user).lower()}
        if self.config.settlement_mode == "specified":
            parameters["currency_to"] = self.config.outcome_currency
        minimum = await self._request("GET", "min-amount", parameters=parameters)
        if (minimum.get("currency_from") is not None and
                (not isinstance(minimum["currency_from"], str) or minimum["currency_from"].lower() != currency)):
            raise ArticleError("payment_currency_unavailable")
        estimate = await self._request("GET", "estimate", parameters={
            "amount": str(Decimal(cents) / 100), "currency_from": "usd", "currency_to": currency})
        if (not isinstance(estimate.get("currency_from"), str) or estimate["currency_from"].lower() != "usd"
            or not isinstance(estimate.get("currency_to"), str) or estimate["currency_to"].lower() != currency
                or ArticleStore._amount(estimate.get("estimated_amount")) < ArticleStore._amount(minimum.get("min_amount"))):
            raise ArticleError("payment_below_minimum")

    async def create_invoice(self, order: dict[str, Any], site_url: str) -> dict[str, Any]:
        if order.get("merchant_key_ref") != self.config.merchant_ref:
            raise ArticleError("payment_merchant_mismatch")
        if not re.fullmatch(r"https://[a-z0-9.-]+(?::443)?", site_url):
            raise ArticleError("invalid_payment_site")
        currency = order.get("pay_currency")
        if (not isinstance(currency, str) or currency not in self.config.currencies
            or currency not in STABLECOIN_NETWORKS
            or STABLECOIN_NETWORKS[currency] != self.config.currencies[currency]):
            raise ArticleError("invalid_payment_currency")
        return await self._request("POST", "invoice", payload={
            "price_amount": float(Decimal(order["price_usd_cents"]) / 100), "price_currency": "usd",
            "pay_currency": order["pay_currency"], "order_id": order["id"],
            "order_description": "Sponsored Blog publication - " + order["plan"],
            "ipn_callback_url": site_url + "/api/v1/article-payments/nowpayments/ipn",
            "success_url": site_url + "/account/article-orders/" + order["id"],
            "cancel_url": site_url + "/account/article-orders/" + order["id"],
            "is_fixed_rate": self.config.fixed_rate, "is_fee_paid_by_user": self.config.fee_paid_by_user})

    async def get_payment(self, payment_id: str | int, merchant_ref: str) -> dict[str, Any]:
        if merchant_ref != self.config.merchant_ref:
            raise ArticleError("payment_merchant_mismatch")
        identifier = ArticleStore._payment_id(payment_id)
        payment = await self._request("GET", "payment/" + identifier)
        if ArticleStore._payment_id(payment.get("payment_id")) != identifier:
            raise ArticleError("payment_id_mismatch")
        return payment