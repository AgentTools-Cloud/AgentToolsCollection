from __future__ import annotations

import json
import os
import re
from contextlib import closing
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBearer
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import auth, db, ownership
from .article_render import MAX_IMAGE_BYTES, private_html
from .article_payments import MAX_PAYMENT_BYTES, NowPaymentsClient, NowPaymentsConfig, payment_fingerprint
from .articles import ArticleError, ArticleStore, normalize_snapshot


PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}
PUBLIC_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
MAX_JSON_BYTES = 1024 * 1024
ARTICLE_API_CREDENTIAL = HTTPBearer(scheme_name="ArticleApiKey", auto_error=False,
                                   description="Site API key. A verified tool claim is required. Submissions require editorial review.")


class NewArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    snapshot: dict


class SaveArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    snapshot: dict


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


class PreviewArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: dict


class ReviewArticle(VersionAction):
    model_config = ConfigDict(extra="forbid")
    action: str
    feedback: str = Field(min_length=1, max_length=2000)


class ConfirmOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_hash: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    accept_terms: bool = Field(strict=True)


class CheckoutOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pay_currency: str = Field(pattern=r"^[a-z0-9]{2,32}$", strict=True)


class RefreshOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,63}$", strict=True)


class EmptyOrderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


def api_payload(model: type[BaseModel]) -> dict:
    return {"requestBody": {"required": True, "content": {
        "application/json": {"schema": model.model_json_schema()}}}}


def editor_ids() -> frozenset[int]:
    values = [value.strip() for value in os.getenv("ARTICLE_EDITOR_IDS", "").split(",")]
    if not values or any(not value.isascii() or not value.isdigit() or int(value) < 1 for value in values):
        return frozenset()
    return frozenset(int(value) for value in values)


def is_article_editor(user) -> bool:
    if user is None:
        return False
    try:
        return user["status"] == "active" and int(user["id"]) in editor_ids()
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def article_key_user(request: Request):
    user_id = auth._uid_from_bearer(request)
    if user_id is None:
        raise HTTPException(401, "A valid site API key is required", headers=PRIVATE_HEADERS)
    with closing(db.connect(read_only=True)) as connection:
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or user["status"] != "active":
        raise HTTPException(401, "The author account is unavailable", headers=PRIVATE_HEADERS)
    return user


def article_user(request: Request):
    if request.headers.get("authorization") is not None:
        return article_key_user(request)
    try:
        session = auth.unsign(request.cookies.get(auth.SESSION_COOKIE))
    except RuntimeError:
        raise HTTPException(503, "Sign-in is not configured", headers=PRIVATE_HEADERS) from None
    session_id = session.get("uid") if session else None
    if not session or "aud" in session or type(session_id) is not int or session_id < 1:
        raise HTTPException(401, "Please sign in first", headers=PRIVATE_HEADERS)
    user = auth.current_user(request)
    if user is None or user["status"] != "active" or int(user["id"]) != session_id:
        raise HTTPException(401, "Your session has expired", headers=PRIVATE_HEADERS)
    auth._require_same_origin_write(request)
    return user


def require_tool_claim(user_id: int) -> int:
    with closing(db.connect(read_only=True)) as connection:
        user = connection.execute("SELECT id,status FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None or user["status"] != "active":
            raise HTTPException(401, "The author account is unavailable", headers=PRIVATE_HEADERS)
        if not any(db.listings_for_host(connection, host)
                   for host in ownership.hosts_for_user(connection, user_id)):
            raise HTTPException(403, "A verified tool claim is required to submit articles", headers=PRIVATE_HEADERS)
    return user_id


def require_author(request: Request) -> int:
    if request.url.path.startswith(("/api/v1/articles", "/api/v1/article-payments")):
        return require_tool_claim(int(article_key_user(request)["id"]))
    return require_tool_claim(int(article_user(request)["id"]))


def require_editor(request: Request) -> int:
    user = article_user(request)
    if not is_article_editor(user):
        raise HTTPException(403, "Article editor access is required", headers=PRIVATE_HEADERS)
    return int(user["id"])


async def read_body(request: Request, maximum: int) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, "Content exceeds the size limit", headers=PRIVATE_HEADERS)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_payload(request: Request, model):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON content is required", headers=PRIVATE_HEADERS)
    try:
        return model.model_validate_json(await read_body(request, MAX_JSON_BYTES))
    except ValidationError:
        raise HTTPException(422, "Invalid input fields", headers=PRIVATE_HEADERS) from None


def article_error(error: ArticleError, private: bool = True) -> HTTPException:
    code = error.code
    status = (404 if code in {"article_not_found", "media_not_found", "article_order_not_found"} else
              401 if code == "invalid_payment_signature" else
              409 if code in {"version_conflict", "slug_in_use", "article_withdrawn", "article_not_submitted", "review_not_pending",
                              "article_order_locked", "article_order_type_locked", "article_order_version_conflict",
                              "article_order_confirmation_conflict", "article_order_not_pending", "article_payment_required",
                              "article_payment_creation_pending", "article_order_not_cancellable", "payment_configuration_changed",
                              "article_payment_review_required"} else
              503 if code in {"snapshot_corrupt", "sponsored_payments_unavailable", "payment_provider_unavailable"} else 422)
    return HTTPException(status, code, headers=PRIVATE_HEADERS if private else PUBLIC_HEADERS)


def published_article(store: ArticleStore, slug: str) -> dict:
    try:
        state = store.public_state(slug)
    except ArticleError as error:
        raise article_error(error, private=False) from None
    if state["status"] != "published":
        raise HTTPException(410 if state["status"] == "withdrawn" else 404,
                            "Article withdrawn" if state["status"] == "withdrawn" else "Article not found",
                            headers=PUBLIC_HEADERS)
    return state["article"]


def public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://agent-tools.cloud").rstrip("/")


def article_url(article: dict) -> str:
    return public_base_url() + "/articles/" + quote(article["slug"], safe="")


def article_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def create_article_router(store: ArticleStore, templates: Jinja2Templates | None = None,
                          *, payments: NowPaymentsClient | None = None) -> APIRouter:
    router = APIRouter()
    if payments is None:
        try:
            config = NowPaymentsConfig.from_file()
        except (ArticleError, OSError):
            config = NowPaymentsConfig()
        payments = NowPaymentsClient(config)
    config = payments.config
    if templates is None:
        templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    templates.env.globals.setdefault("build_version", "articles-20261108.1")
    templates.env.globals.setdefault("current_user", auth.current_user)
    templates.env.globals["articles_enabled"] = True
    templates.env.globals["sponsored_enabled"] = config.ready
    templates.env.globals["sponsored_prices"] = dict(config.prices) if config.ready else {}
    templates.env.globals["sponsored_currencies"] = dict(config.currencies) if config.ready else {}
    templates.env.globals["article_order_labels"] = {
        "awaiting_confirmation": "Awaiting your confirmation", "awaiting_payment": "Awaiting payment",
        "payment_pending": "Payment pending", "creation_unknown": "Invoice creation requires review",
        "paid": "Paid, publication pending", "published": "Published", "cancelled": "Cancelled",
        "expired": "Expired", "manual_review": "Payment requires review", "refunded": "Refunded"}
    templates.env.globals["article_status_labels"] = {
        "draft": "Draft", "published": "Published", "published_with_draft": "Published with a new draft", "withdrawn": "Withdrawn",
        "submitted": "Pending review", "changes_requested": "Changes requested", "rejected": "Rejected", "approved": "Approved"}
    templates.env.filters["article_date"] = article_date
    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    def unavailable():
        raise HTTPException(404, "This endpoint is unavailable", headers=PRIVATE_HEADERS)

    if not config.ready:
        for prefix in ("/articles/pricing", "/account/article-orders", "/admin/article-orders",
                       "/api/v1/article-payments"):
            router.add_api_route(prefix, unavailable, methods=methods, include_in_schema=False)
            router.add_api_route(prefix + "/{unavailable_path:path}", unavailable,
                                 methods=methods, include_in_schema=False)
    router.add_api_route("/articles/{unavailable_path:path}", unavailable,
                         methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    router.add_api_route("/admin/article-notifications", unavailable, methods=methods, include_in_schema=False)
    router.add_api_route("/admin/article-notifications/{unavailable_path:path}", unavailable,
                         methods=methods, include_in_schema=False)

    def require_payments():
        if not config.ready:
            unavailable()

    def allow_sponsored(snapshot):
        if snapshot.get("publication_type") == "sponsored":
            require_payments()

    def order_context(order, *, author_mode=True, api_mode=False):
        frozen = store.get_order_snapshot(order["id"])
        snapshot = frozen["snapshot"]
        visible = {name: value for name, value in order.items()
                   if name not in ("merchant_key_ref", "checkout_config_hash", "pay_network", "reviewer_id")}
        return {"order": visible, "slug": frozen["slug"], "snapshot": snapshot,
                "html": private_html(snapshot["html"], author_mode=author_mode, api_mode=api_mode, sponsored=True),
            "author_mode": author_mode, "checkout_available": order["checkout_config_hash"] == config.checkout_hash}

    def publish_paid(order):
        if order["status"] != "paid":
            return order
        try:
            with closing(db.connect(read_only=True)) as connection:
                reviewer = connection.execute("SELECT * FROM users WHERE id=?", (order["reviewer_id"],)).fetchone()
            if not is_article_editor(reviewer):
                return store.publication_waiting(order["id"])
            require_tool_claim(order["author_id"])
            require_tool_claim(order["reviewer_id"])
            store.publish(order["article_id"], order["version"], order["reviewer_id"])
        except (ArticleError, HTTPException):
            return store.publication_waiting(order["id"])
        return store.get_order(order["id"])

    async def refresh_payment(order, payment_id=None):
        identifier = payment_id or order["payment_id"]
        if not identifier:
            return order
        payment = await payments.get_payment(identifier, order["merchant_key_ref"])
        updated = await run_in_threadpool(store.apply_verified_payment, order["id"], config.merchant_ref,
                                         payment, payment_fingerprint(payment), checkout_config_hash=config.checkout_hash,
                                         outcome_currencies=config.settlement_currencies)
        return await run_in_threadpool(publish_paid, updated)

    @router.api_route("/articles/pricing", methods=["GET", "HEAD"], include_in_schema=False)
    def pricing(request: Request):
        require_payments()
        return templates.TemplateResponse(request, "article_orders.html", {"pricing": True, "terms_text": config.publication_terms},
                                          headers=PUBLIC_HEADERS)

    @router.get("/api/v1/article-payments/orders", tags=["article-payments"], summary="List your publication orders",
                dependencies=[Depends(ARTICLE_API_CREDENTIAL)])
    @router.get("/account/article-orders/api", include_in_schema=False)
    def author_orders_api(request: Request, page: int = 1):
        require_payments()
        result = store.list_orders(require_author(request), page=page)
        result["items"] = [{name: value for name, value in order.items()
                            if name not in ("merchant_key_ref", "checkout_config_hash", "pay_network", "reviewer_id")}
                           for order in result["items"]]
        return JSONResponse(result, headers=PRIVATE_HEADERS)

    @router.get("/account/article-orders", include_in_schema=False)
    def author_orders(request: Request, page: int = 1):
        require_payments()
        return author_view(request, "article_orders.html", lambda author_id: {
            "pricing": False, "result": store.list_orders(author_id, page=page)})

    @router.get("/admin/article-orders", include_in_schema=False)
    @router.get("/admin/article-orders/alerts", include_in_schema=False)
    def editor_orders(request: Request, page: int = 1):
        require_payments()
        require_editor(request)
        return templates.TemplateResponse(request, "article_orders.html", {
            "pricing": False, "author_mode": False, "alerts_only": request.url.path.endswith("/alerts"),
            "result": store.list_orders(page=page, alerts_only=request.url.path.endswith("/alerts"))},
            headers=PRIVATE_HEADERS)

    @router.get("/account/article-orders/{order_id}", include_in_schema=False)
    def author_order(request: Request, order_id: str):
        require_payments()
        return author_view(request, "article_order.html", lambda author_id:
                           order_context(store.get_order_for_author(order_id, author_id)))

    @router.get("/admin/article-orders/{order_id}", include_in_schema=False)
    def editor_order(request: Request, order_id: str):
        require_payments()
        require_editor(request)
        try:
            context = order_context(store.get_order(order_id), author_mode=False)
        except ArticleError as error:
            raise article_error(error) from None
        return templates.TemplateResponse(request, "article_order.html", context, headers=PRIVATE_HEADERS)

    @router.get("/api/v1/article-payments/orders/{order_id}", tags=["article-payments"],
                summary="Read your frozen publication order", dependencies=[Depends(ARTICLE_API_CREDENTIAL)])
    @router.get("/account/article-orders/api/{order_id}", include_in_schema=False)
    def author_order_api(request: Request, order_id: str):
        require_payments()
        try:
            order = store.get_order_for_author(order_id, require_author(request))
            return JSONResponse(order_context(order, api_mode=request.url.path.startswith("/api/v1/")), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/{article_id}/approve-sponsored", include_in_schema=False)
    async def approve_sponsored(request: Request, article_id: int):
        require_payments()
        reviewer_id = require_tool_claim(require_editor(request))
        payload = await read_payload(request, VersionAction)
        try:
            state = await run_in_threadpool(store.get_for_edit, article_id)
            require_tool_claim(await run_in_threadpool(store.get_author_id, article_id))
            plan = state["latest"]["snapshot"].get("sponsored_plan")
            if plan not in config.prices:
                raise ArticleError("invalid_sponsor_metadata")
            state = await run_in_threadpool(store.freeze_order, article_id, payload.expected_version, plan,
                                           config.prices[plan], config.terms_version, reviewer_id,
                                           terms_text=config.publication_terms, checkout_config_hash=config.checkout_hash)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/article-payments/orders/{order_id}/confirm", tags=["article-payments"],
                 summary="Accept your frozen article, price and terms", dependencies=[Depends(ARTICLE_API_CREDENTIAL)],
                 openapi_extra=api_payload(ConfirmOrder))
    @router.post("/account/article-orders/api/{order_id}/confirm", include_in_schema=False)
    async def confirm_order(request: Request, order_id: str):
        require_payments()
        author_id = require_author(request)
        payload = await read_payload(request, ConfirmOrder)
        if payload.accept_terms is not True:
            raise HTTPException(422, "Please accept the frozen publication terms", headers=PRIVATE_HEADERS)
        try:
            await run_in_threadpool(store.confirm_order, order_id, author_id, payload.confirmation_hash)
            order = await run_in_threadpool(store.get_order_for_author, order_id, author_id)
            return JSONResponse(order_context(order, api_mode=request.url.path.startswith("/api/v1/")), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/article-payments/orders/{order_id}/cancel", tags=["article-payments"],
                 summary="Cancel an order before invoice creation", dependencies=[Depends(ARTICLE_API_CREDENTIAL)],
                 openapi_extra=api_payload(EmptyOrderAction))
    @router.post("/account/article-orders/api/{order_id}/cancel", include_in_schema=False)
    async def cancel_order(request: Request, order_id: str):
        require_payments()
        author_id = require_author(request)
        await read_payload(request, EmptyOrderAction)
        try:
            await run_in_threadpool(store.cancel_order, order_id, author_id)
            order = await run_in_threadpool(store.get_order_for_author, order_id, author_id)
            return JSONResponse(order_context(order, api_mode=request.url.path.startswith("/api/v1/")), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/article-payments/orders/{order_id}/checkout", tags=["article-payments"],
                 summary="Create or reuse the invoice for your confirmed order", dependencies=[Depends(ARTICLE_API_CREDENTIAL)],
                 openapi_extra=api_payload(CheckoutOrder))
    @router.post("/account/article-orders/api/{order_id}/checkout", include_in_schema=False)
    async def checkout_order(request: Request, order_id: str):
        require_payments()
        author_id = require_author(request)
        payload = await read_payload(request, CheckoutOrder)
        try:
            order = await run_in_threadpool(store.get_order_for_author, order_id, author_id)
            if order["status"] == "awaiting_payment":
                if order["checkout_config_hash"] != config.checkout_hash:
                    raise ArticleError("payment_configuration_changed")
                await payments.preflight(order["price_usd_cents"], payload.pay_currency)
            order, create = await run_in_threadpool(store.begin_invoice, order_id, author_id, config.merchant_ref,
                                                   payload.pay_currency, config.outcome_currency or None,
                                                   checkout_config_hash=config.checkout_hash,
                                                   pay_network=config.currencies.get(payload.pay_currency, ""),
                                                   outcome_currencies=config.settlement_currencies)
            if create:
                try:
                    invoice = await payments.create_invoice(order, public_base_url())
                except ArticleError as error:
                    if error.code == "payment_provider_rejected":
                        await run_in_threadpool(store.invoice_rejected, order_id, config.merchant_ref)
                    raise
                order = await run_in_threadpool(store.attach_invoice, order_id, config.merchant_ref, invoice)
            return JSONResponse(order_context(order, api_mode=request.url.path.startswith("/api/v1/")), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/article-payments/orders/{order_id}/refresh", tags=["article-payments"],
                 summary="Verify a payment and retry publication without creating another invoice",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(RefreshOrder))
    @router.post("/account/article-orders/api/{order_id}/refresh", include_in_schema=False)
    @router.post("/admin/article-orders/api/{order_id}/refresh", include_in_schema=False)
    async def refresh_order(request: Request, order_id: str):
        require_payments()
        author_mode = not request.url.path.startswith("/admin/")
        actor_id = require_author(request) if author_mode else require_tool_claim(require_editor(request))
        payload = await read_payload(request, RefreshOrder)
        try:
            if author_mode:
                order = await run_in_threadpool(store.get_order_for_author, order_id, actor_id)
            else:
                order = await run_in_threadpool(store.get_order, order_id)
            order = await refresh_payment(order, payload.payment_id)
            return JSONResponse(order_context(order, author_mode=author_mode,
                                              api_mode=request.url.path.startswith("/api/v1/")), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/article-payments/nowpayments/ipn", include_in_schema=False)
    async def payment_ipn(request: Request):
        require_payments()
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "JSON content is required", headers=PRIVATE_HEADERS)
        try:
            notification = config.verify_ipn(await read_body(request, MAX_PAYMENT_BYTES), request.headers.get("x-nowpayments-sig"))
            order_id = notification.get("order_id")
            if not isinstance(order_id, str) or not re.fullmatch(r"blog-[0-9a-f]{24}", order_id):
                raise ArticleError("article_order_not_found")
            identifier = ArticleStore._payment_id(notification.get("payment_id"))
            order = await run_in_threadpool(store.get_order, order_id)
            await refresh_payment(order, identifier)
        except ArticleError as error:
            raise article_error(error) from None
        return JSONResponse({"received": True}, headers=PRIVATE_HEADERS)

    def author_view(request: Request, template_name: str, load):
        try:
            user = article_user(request)
            require_tool_claim(int(user["id"]))
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/auth/github/login?" + urlencode({"next": request.url.path}),
                                        status_code=302, headers=PRIVATE_HEADERS)
            raise
        try:
            context = load(int(user["id"]))
        except ArticleError as error:
            raise article_error(error) from None
        try:
            default_author = user["login"] or ""
        except (KeyError, IndexError):
            default_author = ""
        return templates.TemplateResponse(request, template_name,
                                          {**context, "author_mode": True, "default_author": default_author},
                                          headers=PRIVATE_HEADERS)

    @router.api_route("/articles/contribute", methods=["GET", "HEAD"], include_in_schema=False)
    def contribute():
        return RedirectResponse("/account/articles/new", status_code=303, headers=PRIVATE_HEADERS)

    @router.get("/account/articles", include_in_schema=False)
    def author_management(request: Request, page: int = 1):
        return author_view(request, "article_admin.html",
                           lambda author_id: {"result": store.list_for_author(author_id, page=page)})

    @router.get("/account/articles/new", include_in_schema=False)
    def author_new_editor(request: Request):
        return author_view(request, "article_editor.html", lambda author_id: {"state": None})

    @router.get("/account/articles/{article_id:int}", include_in_schema=False)
    def author_editor(request: Request, article_id: int):
        return author_view(request, "article_editor.html",
                           lambda author_id: {"state": store.get_for_author(article_id, author_id)})

    @router.post("/api/v1/articles", tags=["articles"], summary="Save an article draft for your account",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(NewArticle))
    @router.post("/account/articles/api/new", include_in_schema=False)
    async def author_create(request: Request):
        author_id = require_author(request)
        payload = await read_payload(request, NewArticle)
        allow_sponsored(payload.snapshot)
        try:
            state = await run_in_threadpool(store.create_for_author, payload.slug, payload.snapshot, author_id)
            return JSONResponse(state, status_code=201, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.get("/api/v1/articles", tags=["articles"], summary="List your articles",
                dependencies=[Depends(ARTICLE_API_CREDENTIAL)])
    @router.get("/account/articles/api", include_in_schema=False)
    def author_list(request: Request, page: int = 1):
        author_id = require_author(request)
        try:
            return JSONResponse(store.list_for_author(author_id, page=page), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.get("/api/v1/articles/{article_id:int}", tags=["articles"], summary="Read your article and review feedback",
                dependencies=[Depends(ARTICLE_API_CREDENTIAL)])
    @router.get("/account/articles/api/{article_id}", include_in_schema=False)
    def author_detail(request: Request, article_id: int):
        author_id = require_author(request)
        try:
            return JSONResponse(store.get_for_author(article_id, author_id), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/articles/{article_id:int}/save", tags=["articles"], summary="Save a revision of your article",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(SaveArticle))
    @router.post("/account/articles/api/{article_id}/save", include_in_schema=False)
    async def author_save(request: Request, article_id: int):
        author_id = require_author(request)
        payload = await read_payload(request, SaveArticle)
        allow_sponsored(payload.snapshot)
        try:
            state = await run_in_threadpool(store.save_for_author, article_id, payload.expected_version,
                                           payload.snapshot, author_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/articles/{article_id:int}/submit", tags=["articles"], summary="Submit the current version for review",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(VersionAction))
    @router.post("/account/articles/api/{article_id}/submit", include_in_schema=False)
    async def author_submit(request: Request, article_id: int):
        author_id = require_author(request)
        payload = await read_payload(request, VersionAction)
        try:
            state = await run_in_threadpool(store.submit, article_id, payload.expected_version, author_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/articles/{article_id:int}/cancel-submission", tags=["articles"], summary="Cancel your pending submission",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(VersionAction))
    @router.post("/account/articles/api/{article_id}/cancel-submission", include_in_schema=False)
    async def author_cancel(request: Request, article_id: int):
        author_id = require_author(request)
        payload = await read_payload(request, VersionAction)
        try:
            state = await run_in_threadpool(store.cancel_submission, article_id, payload.expected_version, author_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/api/v1/articles/preview", tags=["articles"], summary="Preview your article and attachments",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra=api_payload(PreviewArticle))
    @router.post("/account/articles/preview", include_in_schema=False)
    async def author_preview(request: Request):
        author_id = require_author(request)
        payload = await read_payload(request, PreviewArticle)
        try:
            snapshot = await run_in_threadpool(normalize_snapshot, payload.snapshot)
            for media_id in snapshot["media_ids"]:
                await run_in_threadpool(store.get_media, media_id, public=False, author_id=author_id)
        except ArticleError as error:
            raise article_error(error) from None
        return JSONResponse({"title": snapshot["title"], "summary": snapshot["summary"], "author": snapshot["author"],
                             "html": private_html(snapshot["html"], author_mode=True,
                                                  api_mode=request.url.path.startswith("/api/v1/articles"),
                                                  sponsored=snapshot.get("publication_type") == "sponsored"), "toc": snapshot["toc"],
                             "cover_id": snapshot["cover_id"]}, headers=PRIVATE_HEADERS)

    @router.post("/api/v1/articles/media", tags=["articles"], summary="Upload an image for your article",
                 dependencies=[Depends(ARTICLE_API_CREDENTIAL)], openapi_extra={"requestBody": {
                     "required": True, "content": {"application/octet-stream": {
                         "schema": {"type": "string", "format": "binary"}}}}})
    @router.post("/account/articles/media", include_in_schema=False)
    async def author_upload(request: Request):
        author_id = require_author(request)
        content = await read_body(request, MAX_IMAGE_BYTES)
        try:
            media = await run_in_threadpool(store.add_media, content, author_id)
            return JSONResponse(media, status_code=201, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.get("/api/v1/articles/media/{media_id}", tags=["articles"], summary="Read your private article image",
                dependencies=[Depends(ARTICLE_API_CREDENTIAL)])
    @router.get("/account/articles/media/{media_id}", include_in_schema=False)
    def author_media(request: Request, media_id: str):
        author_id = require_author(request)
        try:
            media = store.get_media(media_id, public=False, author_id=author_id)
        except ArticleError as error:
            raise article_error(error) from None
        return Response(media["content"], media_type=media["mime"], headers={
            **PRIVATE_HEADERS, "X-Content-Type-Options": "nosniff"})

    @router.get("/admin/articles/api", include_in_schema=False)
    def editing_list(request: Request, page: int = 1):
        require_editor(request)
        try:
            return JSONResponse(store.list_for_edit(page=page), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/new", include_in_schema=False)
    async def create(request: Request):
        editor_id = require_tool_claim(require_editor(request))
        payload = await read_payload(request, NewArticle)
        allow_sponsored(payload.snapshot)
        try:
            state = await run_in_threadpool(store.create, payload.slug, payload.snapshot, editor_id)
            return JSONResponse(state,
                                status_code=201, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.get("/admin/articles/api/{article_id}", include_in_schema=False)
    def editing_detail(request: Request, article_id: int):
        require_editor(request)
        try:
            return JSONResponse(store.get_for_edit(article_id), headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/{article_id}/save", include_in_schema=False)
    async def save(request: Request, article_id: int):
        editor_id = require_tool_claim(require_editor(request))
        payload = await read_payload(request, SaveArticle)
        allow_sponsored(payload.snapshot)
        try:
            state = await run_in_threadpool(store.save, article_id, payload.expected_version, payload.snapshot, editor_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/{article_id}/publish", include_in_schema=False)
    async def publish(request: Request, article_id: int):
        editor_id = require_tool_claim(require_editor(request))
        payload = await read_payload(request, VersionAction)
        try:
            author_id = await run_in_threadpool(store.get_author_id, article_id)
            require_tool_claim(author_id)
            state = await run_in_threadpool(store.publish, article_id, payload.expected_version, editor_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/{article_id}/review", include_in_schema=False)
    async def review(request: Request, article_id: int):
        editor_id = require_editor(request)
        payload = await read_payload(request, ReviewArticle)
        try:
            state = await run_in_threadpool(store.review, article_id, payload.expected_version,
                                           payload.action, payload.feedback, editor_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/api/{article_id}/withdraw", include_in_schema=False)
    async def withdraw(request: Request, article_id: int):
        editor_id = require_editor(request)
        payload = await read_payload(request, VersionAction)
        try:
            state = await run_in_threadpool(store.withdraw, article_id, payload.expected_version, editor_id)
            return JSONResponse(state, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.post("/admin/articles/media", include_in_schema=False)
    async def upload(request: Request):
        editor_id = require_tool_claim(require_editor(request))
        content = await read_body(request, MAX_IMAGE_BYTES)
        try:
            media = await run_in_threadpool(store.add_media, content, editor_id)
            return JSONResponse(media, status_code=201, headers=PRIVATE_HEADERS)
        except ArticleError as error:
            raise article_error(error) from None

    @router.get("/admin/articles/media/{media_id}", include_in_schema=False)
    def private_media(request: Request, media_id: str):
        require_editor(request)
        try:
            media = store.get_media(media_id, public=False)
        except ArticleError as error:
            raise article_error(error) from None
        return Response(media["content"], media_type=media["mime"], headers={
            **PRIVATE_HEADERS, "X-Content-Type-Options": "nosniff"})

    @router.api_route("/articles/media/{media_id}", methods=["GET", "HEAD"], include_in_schema=False)
    def public_media(media_id: str):
        try:
            media = store.get_media(media_id)
        except ArticleError as error:
            raise article_error(error, private=False) from None
        return Response(media["content"], media_type=media["mime"], headers=PUBLIC_HEADERS)

    @router.api_route("/articles/feed.xml", methods=["GET", "HEAD"], include_in_schema=False)
    def feed():
        root = ElementTree.Element("rss", {"version": "2.0"})
        channel = ElementTree.SubElement(root, "channel")
        for name, value in (("title", "Agent Tools Blog"), ("link", public_base_url() + "/articles"),
                    ("description", "Engineering practice for agents, MCP, and x402")):
            ElementTree.SubElement(channel, name).text = value
        try:
            articles = store.list_published(page_size=60)["items"]
        except ArticleError as error:
            raise article_error(error, private=False) from None
        for article in articles:
            item = ElementTree.SubElement(channel, "item")
            summary = (("Sponsored by " + article["sponsor_name"] + ". ") if article.get("publication_type") == "sponsored" else "") + article["summary"]
            for name, value in (("title", article["title"]), ("link", article_url(article)),
                                ("guid", article_url(article)), ("description", summary),
                                ("pubDate", format_datetime(datetime.fromtimestamp(article["published_at"], timezone.utc)))):
                ElementTree.SubElement(item, name).text = value
        return Response(ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
                        media_type="application/rss+xml", headers=PUBLIC_HEADERS)

    @router.api_route("/articles/{slug}.md", methods=["GET", "HEAD"], include_in_schema=False)
    def markdown(slug: str):
        article = published_article(store, slug)
        metadata = {field: article[field] for field in ("title", "summary", "author", "tags", "canonical_url")}
        metadata["url"] = article_url(article)
        metadata["content_hash"] = article["content_hash"]
        if article.get("publication_type") == "sponsored":
            metadata.update(publication_type="sponsored", sponsor_name=article["sponsor_name"], sponsored_plan=article["sponsored_plan"])
        header = "---\n" + "\n".join(name + ": " + json.dumps(value, ensure_ascii=False)
                                   for name, value in metadata.items()) + "\n---\n\n"
        notice = "Sponsored publication.\n\n" if article.get("publication_type") == "sponsored" else ""
        return Response(header + notice + article["markdown"], media_type="text/plain", headers=PUBLIC_HEADERS)

    @router.get("/article-assets/{asset_name}", include_in_schema=False)
    def assets(asset_name: str):
        types = {"articles.css": "text/css", "editor.js": "text/javascript", "orders.js": "text/javascript", "sprite.svg": "image/svg+xml"}
        if asset_name not in types:
            raise HTTPException(404, "Asset not found", headers=PUBLIC_HEADERS)
        return FileResponse(Path(__file__).resolve().parent / "article_assets" / asset_name,
                            media_type=types[asset_name], headers=PUBLIC_HEADERS)

    @router.api_route("/articles", methods=["GET", "HEAD"], include_in_schema=False)
    def listing(request: Request, query: str = Query("", alias="q", max_length=200),
                tag: str = Query("", max_length=40), page: int = Query(1, ge=1, le=10000)):
        try:
            result = store.list_published(query=query, tag=tag, page=page, page_size=12)
            tags = store.published_tags()
        except ArticleError as error:
            raise article_error(error, private=False) from None
        pages = max(1, (result["total"] + 11) // 12)
        if page > pages:
            raise HTTPException(404, "Page not found", headers=PUBLIC_HEADERS)
        parameters = {"q": query, "tag": tag}
        return templates.TemplateResponse(request, "articles.html", {
            "result": result, "tags": tags, "query": query, "selected_tag": tag, "pages": pages,
            "previous_url": "/articles?" + urlencode({**parameters, "page": page - 1}) if page > 1 else None,
            "next_url": "/articles?" + urlencode({**parameters, "page": page + 1}) if page < pages else None,
            "page_canonical": public_base_url() + "/articles"}, headers=PUBLIC_HEADERS)

    @router.api_route("/articles/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
    def detail(request: Request, slug: str):
        article = published_article(store, slug)
        canonical = article["canonical_url"] or article_url(article)
        published_at = datetime.fromtimestamp(article["published_at"], timezone.utc).isoformat()
        updated_at = datetime.fromtimestamp(article["updated_at"], timezone.utc).isoformat()
        structured = {"@context": "https://schema.org", "@type": "Article", "headline": article["title"],
                      "description": article["summary"], "datePublished": published_at, "dateModified": updated_at,
                      "mainEntityOfPage": canonical,
                      "author": {"@type": "Organization" if article["author"] == "Agent Tools" else "Person",
                                 "name": article["author"]},
                      "publisher": {"@type": "Organization", "name": "Agent Tools", "url": public_base_url()}}
        if article["cover_id"]:
            structured["image"] = public_base_url() + "/articles/media/" + article["cover_id"]
        if article.get("publication_type") == "sponsored":
            structured.update(sponsor={"@type": "Organization", "name": article["sponsor_name"]},
                              genre="Sponsored article", isAccessibleForFree=True)
        return templates.TemplateResponse(request, "article.html", {
            "article": article, "page_canonical": canonical, "structured_article": structured,
            "published_iso": published_at, "updated_iso": updated_at}, headers=PUBLIC_HEADERS)

    @router.get("/admin/articles", include_in_schema=False)
    def management(request: Request, page: int = 1, queue: str = Query("", pattern="^(|submitted)$")):
        require_editor(request)
        try:
            result = store.list_for_review(page=page) if queue else store.list_for_edit(page=page)
        except ArticleError as error:
            raise article_error(error) from None
        return templates.TemplateResponse(request, "article_admin.html", {"result": result, "queue": queue},
                          headers=PRIVATE_HEADERS)

    @router.get("/admin/articles/new", include_in_schema=False)
    def new_editor(request: Request):
        require_tool_claim(require_editor(request))
        return templates.TemplateResponse(request, "article_editor.html", {"state": None}, headers=PRIVATE_HEADERS)

    @router.get("/admin/articles/{article_id}", include_in_schema=False)
    def editor(request: Request, article_id: int):
        require_editor(request)
        try:
            state = store.get_for_edit(article_id)
        except ArticleError as error:
            raise article_error(error) from None
        return templates.TemplateResponse(request, "article_editor.html", {"state": state}, headers=PRIVATE_HEADERS)

    @router.post("/admin/articles/preview", include_in_schema=False)
    async def preview(request: Request):
        require_tool_claim(require_editor(request))
        payload = await read_payload(request, PreviewArticle)
        try:
            snapshot = await run_in_threadpool(normalize_snapshot, payload.snapshot)
            for media_id in snapshot["media_ids"]:
                await run_in_threadpool(store.get_media, media_id, public=False)
        except ArticleError as error:
            raise article_error(error) from None
        return JSONResponse({"title": snapshot["title"], "summary": snapshot["summary"], "author": snapshot["author"],
                             "html": private_html(snapshot["html"], sponsored=snapshot.get("publication_type") == "sponsored"), "toc": snapshot["toc"],
                             "cover_id": snapshot["cover_id"]}, headers=PRIVATE_HEADERS)

    if not config.ready:
        for route in router.routes:
            if route.path.startswith("/api/v1/article-payments"):
                route.include_in_schema = False
    return router