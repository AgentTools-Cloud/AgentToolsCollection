from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


MAX_MARKDOWN_BYTES = 128 * 1024
RESERVED_SLUGS = frozenset({"contribute", "pricing", "feed", "media", "new", "admin"})
SNAPSHOT_FIELDS = frozenset({"title", "summary", "author", "markdown", "tags", "canonical_url", "cover_id",
                             "publication_type", "sponsor_name", "sponsored_plan"})
STABLECOIN_NETWORKS = MappingProxyType({"usdtbsc": "bsc", "usdcbsc": "bsc", "usdcbase": "base",
                                       "usdttrc20": "trx", "usdterc20": "eth", "usdc": "eth", "usdtton": "ton"})


def valid_stablecoin_networks(currencies: object) -> bool:
    return isinstance(currencies, Mapping) and bool(currencies) and all(
        currency in STABLECOIN_NETWORKS and STABLECOIN_NETWORKS[currency] == network
        for currency, network in currencies.items())


class ArticleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_slug(slug: str) -> str:
    if (not isinstance(slug, str) or len(slug) > 100
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
            or slug in RESERVED_SLUGS):
        raise ArticleError("invalid_slug")
    return slug


def normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) - SNAPSHOT_FIELDS:
        raise ArticleError("invalid_snapshot")
    normalized = {}
    for field, maximum in (("title", 180), ("summary", 360), ("author", 80)):
        value = snapshot.get(field)
        if (not isinstance(value, str) or not value.strip()
                or len(value.strip()) > maximum or any(ord(character) < 32 for character in value)):
            raise ArticleError("invalid_" + field)
        normalized[field] = value.strip()
    markdown = snapshot.get("markdown")
    if (not isinstance(markdown, str) or not markdown.strip() or "\x00" in markdown
            or len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES):
        raise ArticleError("invalid_markdown")
    normalized["markdown"] = markdown.replace("\r\n", "\n").strip() + "\n"
    tags = snapshot.get("tags", [])
    if not isinstance(tags, (list, tuple)) or len(tags) > 8:
        raise ArticleError("invalid_tags")
    normalized["tags"] = []
    for tag in tags:
        if (not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 40
                or any(ord(character) < 32 for character in tag)):
            raise ArticleError("invalid_tags")
        if tag.strip() not in normalized["tags"]:
            normalized["tags"].append(tag.strip())
    canonical = snapshot.get("canonical_url") or None
    if canonical is not None:
        if not isinstance(canonical, str) or len(canonical) > 2048:
            raise ArticleError("invalid_canonical")
        try:
            parsed = urlsplit(canonical)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and not parsed.username and not parsed.password
                     and parsed.port in (None, 443) and not parsed.fragment
                     and not any(character.isspace() for character in canonical)
                     and "\\" not in canonical)
        except ValueError:
            valid = False
        if not valid:
            raise ArticleError("invalid_canonical")
    normalized["canonical_url"] = canonical
    cover = snapshot.get("cover_id") or None
    if cover is not None and (not isinstance(cover, str) or not re.fullmatch(r"[0-9a-f]{32}", cover)):
        raise ArticleError("invalid_cover")
    normalized["cover_id"] = cover
    publication_type = snapshot.get("publication_type", "ordinary")
    if publication_type not in ("ordinary", "sponsored"):
        raise ArticleError("invalid_publication_type")
    if publication_type == "sponsored":
        sponsor_name = snapshot.get("sponsor_name")
        if (not isinstance(sponsor_name, str) or not sponsor_name.strip() or len(sponsor_name.strip()) > 120
                or any(ord(character) < 32 for character in sponsor_name)):
            raise ArticleError("invalid_sponsor_name")
        plan = snapshot.get("sponsored_plan", "standard")
        if plan not in ("standard", "premium"):
            raise ArticleError("invalid_article_plan")
        normalized.update(publication_type="sponsored", sponsor_name=sponsor_name.strip(), sponsored_plan=plan)
    elif snapshot.get("sponsor_name") or snapshot.get("sponsored_plan"):
        raise ArticleError("invalid_sponsor_metadata")
    from .article_render import render_markdown
    try:
        rendered = render_markdown(normalized["markdown"], sponsored=publication_type == "sponsored")
    except ValueError as exc:
        raise ArticleError(str(exc)) from None
    normalized.update(rendered)
    if cover and cover not in normalized["media_ids"]:
        normalized["media_ids"].append(cover)
        normalized["media_ids"].sort()
    return normalized


def snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def snapshot_hash(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ArticleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def init(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise ArticleError("unsafe_database_permissions")
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    latest_version INTEGER NOT NULL DEFAULT 0,
                    published_version INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    published_at INTEGER,
                    public_updated_at INTEGER,
                    withdrawn_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS article_versions (
                    article_id INTEGER NOT NULL REFERENCES articles(id),
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    editor_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (article_id, version)
                );
                CREATE TABLE IF NOT EXISTS article_audit (
                    id INTEGER PRIMARY KEY,
                    article_id INTEGER NOT NULL REFERENCES articles(id),
                    version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    editor_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS article_media (
                    id TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    mime TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    editor_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS article_version_media (
                    article_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    media_id TEXT NOT NULL REFERENCES article_media(id),
                    PRIMARY KEY (article_id,version,media_id),
                    FOREIGN KEY (article_id,version) REFERENCES article_versions(article_id,version)
                );
                CREATE TABLE IF NOT EXISTS article_submissions (
                    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','submitted','changes_requested','rejected','approved')),
                    feedback TEXT NOT NULL DEFAULT '',
                    author_id INTEGER NOT NULL,
                    submitted_at INTEGER,
                    reviewed_at INTEGER,
                    reviewer_id INTEGER,
                    FOREIGN KEY (article_id,version) REFERENCES article_versions(article_id,version)
                );
                CREATE TABLE IF NOT EXISTS article_orders (
                    id TEXT PRIMARY KEY,
                    article_id INTEGER NOT NULL REFERENCES articles(id),
                    version INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    reviewer_id INTEGER NOT NULL,
                    plan TEXT NOT NULL CHECK(plan IN ('standard','premium')),
                    price_usd_cents INTEGER NOT NULL CHECK(typeof(price_usd_cents)='integer' AND price_usd_cents>0),
                    terms_version TEXT NOT NULL,
                    terms_text TEXT NOT NULL DEFAULT '',
                    checkout_config_hash TEXT NOT NULL DEFAULT '',
                    snapshot_hash TEXT NOT NULL,
                    confirmation_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('awaiting_confirmation','awaiting_payment','payment_pending',
                        'creation_unknown','paid','published','cancelled','expired','manual_review','refunded')),
                    invoice_id TEXT UNIQUE,
                    invoice_url TEXT,
                    payment_id TEXT UNIQUE,
                    merchant_key_ref TEXT,
                    pay_currency TEXT,
                    pay_network TEXT,
                    outcome_currency TEXT,
                    provider_status TEXT,
                    failure_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    confirmed_at INTEGER,
                    paid_at INTEGER,
                    FOREIGN KEY (article_id,version) REFERENCES article_versions(article_id,version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS article_orders_active ON article_orders(article_id)
                    WHERE status NOT IN ('cancelled','expired','refunded');
                CREATE TABLE IF NOT EXISTS article_payment_events (
                    fingerprint TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES article_orders(id),
                    payment_id TEXT NOT NULL,
                    provider_status TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(article_orders)")}
            for name, declaration in (
                    ("invoice_url", "TEXT"), ("merchant_key_ref", "TEXT"), ("pay_currency", "TEXT"),
                    ("outcome_currency", "TEXT"), ("provider_status", "TEXT"), ("failure_code", "TEXT"),
                    ("terms_text", "TEXT NOT NULL DEFAULT ''"), ("checkout_config_hash", "TEXT NOT NULL DEFAULT ''"),
                    ("pay_network", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE article_orders ADD COLUMN {name} {declaration}")

    @contextmanager
    def _connection(self, write: bool = False):
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True, timeout=5, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _version(connection, article_id: int, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM article_versions WHERE article_id=? AND version=?",
            (article_id, version),
        ).fetchone()
        if row is None or snapshot_hash(row["snapshot_json"]) != row["snapshot_hash"]:
            raise ArticleError("snapshot_corrupt")
        return {"version": version, "snapshot": json.loads(row["snapshot_json"]),
                "hash": row["snapshot_hash"], "created_at": row["created_at"]}

    def _editing(self, connection, article_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if row is None:
            raise ArticleError("article_not_found")
        latest = self._version(connection, article_id, row["latest_version"])
        published = (self._version(connection, article_id, row["published_version"])
                     if row["published_version"] is not None else None)
        status = ("withdrawn" if row["withdrawn_at"] is not None else
                  "draft" if published is None else
                  "published" if row["latest_version"] == row["published_version"] else "published_with_draft")
        submission = connection.execute("SELECT * FROM article_submissions WHERE article_id=?",
                        (article_id,)).fetchone()
        review_status = (submission["status"] if submission is not None
                 and submission["version"] == row["latest_version"] else
                 "approved" if status == "published" else "draft")
        return {"id": article_id, "slug": row["slug"], "latest": latest,
            "published": published, "status": status, "updated_at": row["updated_at"],
            "review_status": review_status, "submission": dict(submission) if submission else None,
            "order": self._latest_order(connection, article_id)}

    @staticmethod
    def _latest_order(connection, article_id: int):
        row = connection.execute(
            "SELECT * FROM article_orders WHERE article_id=? ORDER BY rowid DESC LIMIT 1",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _order(connection, order_id: str):
        row = connection.execute("SELECT * FROM article_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise ArticleError("article_order_not_found")
        return dict(row)

    @staticmethod
    def _check_order_confirmation(order):
        fields = ("article_id", "version", "author_id", "plan", "price_usd_cents", "terms_version",
                  "terms_text", "checkout_config_hash", "snapshot_hash")
        expected = snapshot_hash(snapshot_json({name: order[name] for name in fields}))
        if (not isinstance(order["confirmation_hash"], str)
                or not secrets.compare_digest(expected, order["confirmation_hash"])):
            raise ArticleError("article_order_confirmation_conflict")

    @staticmethod
    def _check_frozen_order(state, order):
        ArticleStore._check_order_confirmation(order)
        if (state["latest"]["version"] != order["version"]
                or state["latest"]["hash"] != order["snapshot_hash"]):
            raise ArticleError("article_order_version_conflict")

    @staticmethod
    def _audit(connection, article_id: int, version: int, action: str, editor_id: int, now: int):
        connection.execute(
            "INSERT INTO article_audit(article_id,version,action,editor_id,created_at) VALUES(?,?,?,?,?)",
            (article_id, version, action, editor_id, now),
        )

    @staticmethod
    def _require_author(connection, article_id: int, author_id: int):
        if connection.execute(
                "SELECT 1 FROM article_versions WHERE article_id=? AND version=1 AND editor_id=?",
                (article_id, author_id)).fetchone() is None:
            raise ArticleError("article_not_found")

    @staticmethod
    def _require_media_author(connection, media_id: str, author_id: int):
        allowed = connection.execute(
            "SELECT 1 FROM article_media WHERE id=? AND editor_id=? UNION ALL "
            "SELECT 1 FROM article_version_media JOIN article_versions AS owner "
            "ON owner.article_id=article_version_media.article_id AND owner.version=1 "
            "WHERE article_version_media.media_id=? AND owner.editor_id=? LIMIT 1",
            (media_id, author_id, media_id, author_id),
        ).fetchone()
        if allowed is None:
            raise ArticleError("media_not_found")

    @staticmethod
    def _insert_version(connection, article_id: int, version: int, serialized: str, editor_id: int, now: int,
                        media_owner_id: int | None = None):
        media_ids = json.loads(serialized)["media_ids"]
        for media_id in media_ids:
            if connection.execute("SELECT 1 FROM article_media WHERE id=?", (media_id,)).fetchone() is None:
                raise ArticleError("media_not_found")
            if media_owner_id is not None:
                ArticleStore._require_media_author(connection, media_id, media_owner_id)
        connection.execute("INSERT INTO article_versions VALUES(?,?,?,?,?,?)",
                           (article_id, version, serialized, snapshot_hash(serialized), editor_id, now))
        connection.executemany("INSERT INTO article_version_media VALUES(?,?,?)",
                               [(article_id, version, media_id) for media_id in media_ids])

    def create(self, slug: str, snapshot: dict[str, Any], editor_id: int, *, author_only: bool = False) -> dict[str, Any]:
        validate_slug(slug)
        serialized = snapshot_json(normalize_snapshot(snapshot))
        now = int(time.time())
        with self._connection(write=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO articles(slug,latest_version,created_at,updated_at) VALUES(?,1,?,?)",
                    (slug, now, now),
                )
            except sqlite3.IntegrityError:
                raise ArticleError("slug_in_use") from None
            article_id = cursor.lastrowid
            self._insert_version(connection, article_id, 1, serialized, editor_id, now,
                                 media_owner_id=editor_id if author_only else None)
            if author_only:
                connection.execute(
                    "INSERT INTO article_submissions(article_id,version,status,author_id) VALUES(?,1,'draft',?)",
                    (article_id, editor_id),
                )
            self._audit(connection, article_id, 1, "create", editor_id, now)
            return self._editing(connection, article_id)

    def create_for_author(self, slug: str, snapshot: dict[str, Any], author_id: int):
        return self.create(slug, snapshot, author_id, author_only=True)

    def save(self, article_id: int, expected_version: int, snapshot: dict[str, Any], editor_id: int,
             *, author_only: bool = False):
        serialized = snapshot_json(normalize_snapshot(snapshot))
        now = int(time.time())
        with self._connection(write=True) as connection:
            if author_only:
                self._require_author(connection, article_id, editor_id)
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn":
                raise ArticleError("article_withdrawn")
            if state["latest"]["hash"] == snapshot_hash(serialized):
                return state
            order = state["order"]
            if order and (order["status"] not in ("cancelled", "expired", "refunded") or order["paid_at"] is not None):
                raise ArticleError("article_order_locked")
            if order and json.loads(serialized).get("publication_type") != "sponsored":
                raise ArticleError("article_order_type_locked")
            version = expected_version + 1
            self._insert_version(connection, article_id, version, serialized, editor_id, now,
                                 media_owner_id=editor_id if author_only else None)
            connection.execute(
                "UPDATE articles SET latest_version=?,updated_at=? WHERE id=?",
                (version, now, article_id),
            )
            self._audit(connection, article_id, version, "save", editor_id, now)
            return self._editing(connection, article_id)

    def save_for_author(self, article_id: int, expected_version: int, snapshot: dict[str, Any], author_id: int):
        return self.save(article_id, expected_version, snapshot, author_id, author_only=True)

    def submit(self, article_id: int, expected_version: int, author_id: int):
        now = int(time.time())
        with self._connection(write=True) as connection:
            self._require_author(connection, article_id, author_id)
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn":
                raise ArticleError("article_withdrawn")
            if state["status"] == "published" or state["review_status"] == "submitted":
                return state
            if state["order"] and (state["order"]["status"] not in ("cancelled", "expired", "refunded")
                                   or state["order"]["paid_at"] is not None):
                raise ArticleError("article_order_locked")
            connection.execute(
                "INSERT INTO article_submissions(article_id,version,status,author_id,submitted_at) "
                "VALUES(?,?,'submitted',?,?) ON CONFLICT(article_id) DO UPDATE SET "
                "version=excluded.version,status='submitted',author_id=excluded.author_id,"
                "submitted_at=excluded.submitted_at,feedback='',reviewed_at=NULL,reviewer_id=NULL",
                (article_id, expected_version, author_id, now),
            )
            connection.execute("UPDATE articles SET updated_at=? WHERE id=?", (now, article_id))
            self._audit(connection, article_id, expected_version, "submit", author_id, now)
            return self._editing(connection, article_id)

    def cancel_submission(self, article_id: int, expected_version: int, author_id: int):
        now = int(time.time())
        with self._connection(write=True) as connection:
            self._require_author(connection, article_id, author_id)
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["review_status"] != "submitted":
                raise ArticleError("review_not_pending")
            connection.execute("UPDATE article_submissions SET status='draft' WHERE article_id=?",
                               (article_id,))
            connection.execute("UPDATE articles SET updated_at=? WHERE id=?", (now, article_id))
            self._audit(connection, article_id, expected_version, "cancel_submission", author_id, now)
            return self._editing(connection, article_id)

    def review(self, article_id: int, expected_version: int, action: str, feedback: str, editor_id: int):
        if action not in ("changes_requested", "rejected"):
            raise ArticleError("invalid_review_action")
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 2000 or "\x00" in feedback:
            raise ArticleError("invalid_review_feedback")
        now = int(time.time())
        with self._connection(write=True) as connection:
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn" or state["review_status"] != "submitted":
                raise ArticleError("review_not_pending")
            connection.execute(
                "UPDATE article_submissions SET status=?,feedback=?,reviewed_at=?,reviewer_id=? WHERE article_id=?",
                (action, feedback.strip(), now, editor_id, article_id),
            )
            connection.execute("UPDATE articles SET updated_at=? WHERE id=?", (now, article_id))
            self._audit(connection, article_id, expected_version, action, editor_id, now)
            return self._editing(connection, article_id)

    def publish(self, article_id: int, expected_version: int, editor_id: int):
        now = int(time.time())
        with self._connection(write=True) as connection:
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn":
                raise ArticleError("article_withdrawn")
            if state["status"] == "published":
                return state
            order = state["order"]
            if state["latest"]["snapshot"].get("publication_type") == "sponsored" and order is None:
                raise ArticleError("article_payment_required")
            if order:
                self._check_frozen_order(state, order)
                if order["status"] != "paid" or order["paid_at"] is None:
                    raise ArticleError("article_payment_required")
                if state["review_status"] != "approved":
                    raise ArticleError("article_not_approved")
            elif state["submission"] is not None and state["review_status"] != "submitted":
                raise ArticleError("article_not_submitted")
            connection.execute(
                "UPDATE articles SET published_version=?,published_at=COALESCE(published_at,?),"
                "public_updated_at=?,updated_at=? WHERE id=?",
                (expected_version, now, now, now, article_id),
            )
            connection.execute(
                "UPDATE article_submissions SET status='approved',reviewed_at=?,reviewer_id=? WHERE article_id=?",
                (now, editor_id, article_id),
            )
            self._audit(connection, article_id, expected_version, "publish", editor_id, now)
            if order:
                connection.execute("UPDATE article_orders SET status='published',failure_code=NULL,updated_at=? WHERE id=?",
                                   (now, order["id"]))
            return self._editing(connection, article_id)

    def freeze_order(self, article_id: int, expected_version: int, plan: str, price_usd_cents: int,
                     terms_version: str, editor_id: int, *, terms_text: str = "", checkout_config_hash: str = ""):
        if plan not in ("standard", "premium"):
            raise ArticleError("invalid_article_plan")
        if type(price_usd_cents) is not int or not 1 <= price_usd_cents <= 100_000_000:
            raise ArticleError("invalid_article_price")
        if (not isinstance(terms_version, str) or not terms_version.strip() or len(terms_version) > 120
                or any(ord(character) < 32 for character in terms_version)):
            raise ArticleError("invalid_article_terms")
            if (not isinstance(terms_text, str) or not terms_text.strip() or len(terms_text) > 8000 or "\x00" in terms_text
                or not isinstance(checkout_config_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", checkout_config_hash)):
                raise ArticleError("invalid_article_terms")
        now = int(time.time())
        with self._connection(write=True) as connection:
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn":
                raise ArticleError("article_withdrawn")
            snapshot = state["latest"]["snapshot"]
            if snapshot.get("publication_type") != "sponsored" or snapshot.get("sponsored_plan") != plan:
                raise ArticleError("invalid_sponsor_metadata")
            from .article_render import outbound_link_count
            if outbound_link_count(snapshot["html"], os.getenv("PUBLIC_BASE_URL", "https://agent-tools.cloud")) > (
                    1 if plan == "standard" else 3):
                raise ArticleError("sponsored_link_limit")
            existing = state["order"]
            if existing and (existing["status"] not in ("cancelled", "expired", "refunded") or existing["paid_at"] is not None):
                self._check_frozen_order(state, existing)
                if (existing["plan"], existing["price_usd_cents"], existing["terms_version"], existing["terms_text"],
                    existing["checkout_config_hash"]) != (
                    plan, price_usd_cents, terms_version.strip(), terms_text, checkout_config_hash):
                    raise ArticleError("article_order_locked")
                return state
            if state["status"] == "published" or state["review_status"] != "submitted":
                raise ArticleError("review_not_pending")
            if connection.execute(
                    "SELECT 1 FROM article_orders WHERE article_id=? AND (paid_at IS NOT NULL OR "
                    "(provider_status IS NOT NULL AND failure_code IS NOT NULL)) LIMIT 1", (article_id,)).fetchone():
                raise ArticleError("article_payment_review_required")
            author = connection.execute(
                "SELECT editor_id FROM article_versions WHERE article_id=? AND version=1", (article_id,),
            ).fetchone()[0]
            details = {"article_id": article_id, "version": expected_version, "author_id": author,
                       "plan": plan, "price_usd_cents": price_usd_cents, "terms_version": terms_version.strip(),
                       "terms_text": terms_text, "checkout_config_hash": checkout_config_hash,
                       "snapshot_hash": state["latest"]["hash"]}
            order_id = "blog-" + secrets.token_hex(12)
            connection.execute(
                "INSERT INTO article_orders(id,article_id,version,author_id,reviewer_id,plan,price_usd_cents,"
                "terms_version,terms_text,checkout_config_hash,snapshot_hash,confirmation_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'awaiting_confirmation',?,?)",
                (order_id, article_id, expected_version, author, editor_id, plan, price_usd_cents,
                 terms_version.strip(), terms_text, checkout_config_hash, state["latest"]["hash"],
                 snapshot_hash(snapshot_json(details)), now, now),
            )
            connection.execute(
                "UPDATE article_submissions SET status='approved',reviewed_at=?,reviewer_id=? WHERE article_id=?",
                (now, editor_id, article_id),
            )
            connection.execute("UPDATE articles SET updated_at=? WHERE id=?", (now, article_id))
            self._audit(connection, article_id, expected_version, "sponsor_approve", editor_id, now)
            return self._editing(connection, article_id)

    def get_order_for_author(self, order_id: str, author_id: int):
        with self._connection() as connection:
            order = self._order(connection, order_id)
            if order["author_id"] != author_id:
                raise ArticleError("article_order_not_found")
            self._require_author(connection, order["article_id"], author_id)
            return order

    def get_order(self, order_id: str):
        with self._connection() as connection:
            return self._order(connection, order_id)

    def get_order_snapshot(self, order_id: str):
        with self._connection() as connection:
            order = self._order(connection, order_id)
            self._check_order_confirmation(order)
            version = self._version(connection, order["article_id"], order["version"])
            if version["hash"] != order["snapshot_hash"]:
                raise ArticleError("snapshot_corrupt")
            slug = connection.execute("SELECT slug FROM articles WHERE id=?", (order["article_id"],)).fetchone()[0]
            return {"slug": slug, "snapshot": version["snapshot"]}

    def list_orders(self, author_id: int | None = None, *, page: int = 1, alerts_only: bool = False):
        if type(page) is not int or not 1 <= page <= 10000:
            raise ArticleError("invalid_search")
        conditions, parameters = [], []
        if author_id is not None:
            conditions.append("orders.author_id=?")
            parameters.append(author_id)
        if alerts_only:
            conditions.append("(orders.status IN ('creation_unknown','manual_review','paid') OR orders.failure_code IS NOT NULL)")
        selection = " FROM article_orders AS orders" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*)" + selection, parameters).fetchone()[0]
            rows = connection.execute("SELECT orders.*" + selection + " ORDER BY orders.created_at DESC,orders.rowid DESC "
                                      "LIMIT 20 OFFSET ?", [*parameters, (page - 1) * 20]).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": 20}

    def publication_waiting(self, order_id: str):
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["status"] == "paid" and order["failure_code"] != "publication_waiting":
                now = int(time.time())
                connection.execute("UPDATE article_orders SET failure_code='publication_waiting',updated_at=? WHERE id=?",
                                   (now, order_id))
                self._audit(connection, order["article_id"], order["version"], "publication_waiting", 0, now)
            return self._order(connection, order_id)

    def confirm_order(self, order_id: str, author_id: int, confirmation_hash: str):
        now = int(time.time())
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["author_id"] != author_id:
                raise ArticleError("article_order_not_found")
            self._require_author(connection, order["article_id"], author_id)
            if (not isinstance(confirmation_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmation_hash)
                    or not secrets.compare_digest(order["confirmation_hash"], confirmation_hash)):
                raise ArticleError("article_order_confirmation_conflict")
            state = self._editing(connection, order["article_id"])
            self._check_frozen_order(state, order)
            if order["status"] in ("awaiting_payment", "payment_pending", "creation_unknown", "paid", "published"):
                return state
            if order["status"] != "awaiting_confirmation" or state["status"] == "withdrawn":
                raise ArticleError("article_order_not_pending")
            connection.execute(
                "UPDATE article_orders SET status='awaiting_payment',confirmed_at=?,updated_at=? WHERE id=?",
                (now, now, order_id),
            )
            self._audit(connection, order["article_id"], order["version"], "sponsor_confirm", author_id, now)
            return self._editing(connection, order["article_id"])

    def cancel_order(self, order_id: str, author_id: int):
        now = int(time.time())
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["author_id"] != author_id:
                raise ArticleError("article_order_not_found")
            self._require_author(connection, order["article_id"], author_id)
            if order["status"] == "cancelled":
                return self._editing(connection, order["article_id"])
            if order["status"] not in ("awaiting_confirmation", "awaiting_payment") or order["invoice_id"]:
                raise ArticleError("article_order_not_cancellable")
            connection.execute("UPDATE article_orders SET status='cancelled',updated_at=? WHERE id=?",
                               (now, order_id))
            self._audit(connection, order["article_id"], order["version"], "sponsor_cancel", author_id, now)
            return self._editing(connection, order["article_id"])

    @staticmethod
    def _amount(value) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 128:
            raise ArticleError("invalid_payment_amount")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ArticleError("invalid_payment_amount") from None
        if (not amount.is_finite() or amount < 0 or amount > Decimal("1e30")
            or not -30 <= amount.as_tuple().exponent <= 30 or len(amount.as_tuple().digits) > 40):
            raise ArticleError("invalid_payment_amount")
        return amount

    @staticmethod
    def _payment_id(value) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ArticleError("invalid_payment_id")
        identifier = str(value)
        if not re.fullmatch(r"[1-9][0-9]{0,63}", identifier):
            raise ArticleError("invalid_payment_id")
        return identifier

    def begin_invoice(self, order_id: str, author_id: int, merchant_key_ref: str,
                      pay_currency: str, outcome_currency: str | None, *, checkout_config_hash: str = "", pay_network: str = "",
                      outcome_currencies: Mapping[str, str] | None = None):
        if not isinstance(merchant_key_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", merchant_key_ref):
            raise ArticleError("invalid_merchant_ref")
        if (not isinstance(pay_currency, str) or pay_currency not in STABLECOIN_NETWORKS
                or (pay_network and STABLECOIN_NETWORKS[pay_currency] != pay_network)
                or (outcome_currency is None and not valid_stablecoin_networks(outcome_currencies))
                or (outcome_currency is not None and (not isinstance(outcome_currency, str)
                                                      or outcome_currency not in STABLECOIN_NETWORKS))):
            raise ArticleError("invalid_payment_currency")
        now = int(time.time())
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["author_id"] != author_id:
                raise ArticleError("article_order_not_found")
            self._require_author(connection, order["article_id"], author_id)
            state = self._editing(connection, order["article_id"])
            self._check_frozen_order(state, order)
            if state["status"] == "withdrawn":
                raise ArticleError("article_withdrawn")
            if order["status"] in ("payment_pending", "paid", "published") and order["invoice_url"]:
                return order, False
            if order["checkout_config_hash"] != checkout_config_hash:
                raise ArticleError("payment_configuration_changed")
            if order["status"] != "awaiting_payment" or order["confirmed_at"] is None:
                raise ArticleError("article_payment_creation_pending")
            connection.execute(
                "UPDATE article_orders SET status='creation_unknown',merchant_key_ref=?,pay_currency=?,pay_network=?,"
                "outcome_currency=?,updated_at=?,failure_code=NULL WHERE id=?",
                (merchant_key_ref, pay_currency, pay_network or None, outcome_currency, now, order_id),
            )
            self._audit(connection, order["article_id"], order["version"], "invoice_begin", author_id, now)
            return self._order(connection, order_id), True

    def attach_invoice(self, order_id: str, merchant_key_ref: str, invoice: dict):
        now = int(time.time())
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["merchant_key_ref"] != merchant_key_ref:
                raise ArticleError("payment_merchant_mismatch")
            identifier = self._payment_id(invoice.get("id"))
            if (invoice.get("order_id") != order_id or invoice.get("price_currency") != "usd"
                    or self._amount(invoice.get("price_amount")) != Decimal(order["price_usd_cents"]) / 100
                    or invoice.get("pay_currency") != order["pay_currency"]):
                raise ArticleError("payment_invoice_mismatch")
            url = invoice.get("invoice_url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = (parsed is not None and parsed.scheme == "https" and parsed.hostname == "nowpayments.io"
                         and parsed.port in (None, 443) and parsed.path in ("/payment", "/payment/")
                         and not parsed.username and not parsed.password and not parsed.fragment
                         and "\\" not in url and not any(character.isspace() for character in url))
                valid = valid and len(url) <= 2048 and parse_qs(parsed.query, max_num_fields=12).get("iid") == [identifier]
            except ValueError:
                valid = False
            if not valid:
                raise ArticleError("invalid_invoice_url")
            if order["invoice_id"] is not None and order["invoice_id"] != identifier:
                raise ArticleError("payment_invoice_mismatch")
            if connection.execute("SELECT 1 FROM article_orders WHERE invoice_id=? AND id<>?",
                                  (identifier, order_id)).fetchone():
                raise ArticleError("payment_invoice_mismatch")
            if order["status"] not in ("creation_unknown", "payment_pending", "paid", "published"):
                raise ArticleError("article_order_not_pending")
            if order["invoice_id"] == identifier and order["invoice_url"] == url:
                return order
            connection.execute(
                "UPDATE article_orders SET invoice_id=?,invoice_url=?,status=CASE WHEN status='creation_unknown' "
                "THEN 'payment_pending' ELSE status END,updated_at=? WHERE id=?",
                (identifier, url, now, order_id),
            )
            self._audit(connection, order["article_id"], order["version"], "invoice_attached", 0, now)
            return self._order(connection, order_id)

    def invoice_rejected(self, order_id: str, merchant_key_ref: str):
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["merchant_key_ref"] != merchant_key_ref:
                raise ArticleError("payment_merchant_mismatch")
            if order["status"] != "creation_unknown" or order["invoice_id"] is not None:
                raise ArticleError("article_order_not_pending")
            now = int(time.time())
            connection.execute("UPDATE article_orders SET status='awaiting_payment',failure_code='invoice_rejected',"
                               "updated_at=? WHERE id=?", (now, order_id))
            self._audit(connection, order["article_id"], order["version"], "invoice_rejected", 0, now)

    def apply_verified_payment(self, order_id: str, merchant_key_ref: str, payment: dict, fingerprint: str, *,
                               checkout_config_hash: str | None = None,
                               outcome_currencies: Mapping[str, str] | None = None):
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ArticleError("invalid_payment_event")
        status = payment.get("payment_status")
        if status not in ("waiting", "confirming", "confirmed", "sending", "partially_paid", "finished",
                          "failed", "expired", "refunded"):
            raise ArticleError("invalid_payment_status")
        payment_id = self._payment_id(payment.get("payment_id"))
        invoice_id = self._payment_id(payment.get("invoice_id"))
        now = int(time.time())
        with self._connection(write=True) as connection:
            order = self._order(connection, order_id)
            if order["merchant_key_ref"] != merchant_key_ref:
                raise ArticleError("payment_merchant_mismatch")
            if payment.get("order_id") != order_id or (order["invoice_id"] and order["invoice_id"] != invoice_id):
                raise ArticleError("payment_invoice_mismatch")
            if connection.execute("SELECT 1 FROM article_orders WHERE id<>? AND (invoice_id=? OR payment_id=?)",
                                  (order_id, invoice_id, payment_id)).fetchone():
                raise ArticleError("payment_invoice_mismatch")
            event = connection.execute("SELECT order_id FROM article_payment_events WHERE fingerprint=?",
                                       (fingerprint,)).fetchone()
            if event:
                if event["order_id"] != order_id:
                    raise ArticleError("payment_event_conflict")
                return order
            connection.execute("INSERT INTO article_payment_events VALUES(?,?,?,?,?)",
                               (fingerprint, order_id, payment_id, status, now))
            failure = None
            try:
                self._check_frozen_order(self._editing(connection, order["article_id"]), order)
                if checkout_config_hash is not None and checkout_config_hash != order["checkout_config_hash"]:
                    failure = "payment_configuration_changed"
                if order["outcome_currency"] is None and (checkout_config_hash != order["checkout_config_hash"]
                                                         or not valid_stablecoin_networks(outcome_currencies)):
                    failure = "payment_outcome_mismatch"
                if (payment.get("price_currency") != "usd"
                        or self._amount(payment.get("price_amount")) != Decimal(order["price_usd_cents"]) / 100
                        or payment.get("pay_currency") != order["pay_currency"]
                        or payment.get("pay_currency") not in STABLECOIN_NETWORKS):
                    failure = "payment_amount_or_currency_mismatch"
                if order["pay_network"] and payment.get("network") not in (None, order["pay_network"]):
                    failure = "payment_network_mismatch"
                if payment.get("parent_payment_id") is not None or payment.get("payment_extra_ids"):
                    failure = "payment_redeposit"
                if payment.get("origin_type") not in (None, "", "NORMAL"):
                    failure = "payment_origin_requires_review"
                if status == "finished":
                    expected = self._amount(payment.get("pay_amount"))
                    if expected <= 0 or self._amount(payment.get("actually_paid")) < expected:
                        failure = "payment_underpaid"
                    outcome = payment.get("outcome_currency")
                    if (not isinstance(outcome, str) or outcome not in STABLECOIN_NETWORKS
                            or (order["outcome_currency"] is not None and outcome != order["outcome_currency"])
                            or (order["outcome_currency"] is None
                                and (not valid_stablecoin_networks(outcome_currencies) or outcome not in outcome_currencies))
                            or self._amount(payment.get("outcome_amount")) <= 0):
                        failure = "payment_outcome_mismatch"
            except ArticleError:
                failure = "payment_snapshot_or_amount_invalid"
            if order["status"] in ("cancelled", "expired", "refunded", "manual_review"):
                failure = failure or "payment_order_inactive"
            if order["status"] == "awaiting_confirmation" or order["confirmed_at"] is None:
                failure = "payment_order_unconfirmed"
            if order["payment_id"] and order["payment_id"] != payment_id:
                failure = "payment_duplicate_deposit"
            if order["paid_at"] is not None and status in ("expired", "partially_paid", "failed", "refunded"):
                failure = failure or "payment_terminal_after_paid"
            if order["status"] in ("paid", "published") and not failure and status in (
                    "waiting", "confirming", "confirmed", "sending", "finished"):
                return order
            progression = {"waiting": 0, "confirming": 1, "confirmed": 2, "sending": 3, "finished": 4}
            if (not failure and status in progression and order["provider_status"] in progression
                    and progression[status] < progression[order["provider_status"]]):
                return order
            next_status = ("manual_review" if failure or status in ("partially_paid", "failed", "refunded") else
                           "expired" if status == "expired" else "paid" if status == "finished" else "payment_pending")
            latest_order = self._latest_order(connection, order["article_id"])
            if failure and latest_order["id"] != order_id:
                next_status = order["status"]
                if latest_order["status"] in ("awaiting_confirmation", "awaiting_payment", "payment_pending", "creation_unknown"):
                    connection.execute("UPDATE article_orders SET status='manual_review',failure_code='prior_payment_requires_review',"
                                       "updated_at=? WHERE id=?", (now, latest_order["id"]))
                    self._audit(connection, order["article_id"], latest_order["version"], "prior_payment_hold", 0, now)
            connection.execute(
                "UPDATE article_orders SET status=?,invoice_id=COALESCE(invoice_id,?),provider_status=?,failure_code=?,"
                "payment_id=COALESCE(payment_id,?),"
                 "outcome_currency=CASE WHEN ?='paid' THEN COALESCE(outcome_currency,?) ELSE outcome_currency END,"
                "paid_at=CASE WHEN ?='paid' THEN COALESCE(paid_at,?) ELSE paid_at END,updated_at=? WHERE id=?",
                (next_status, invoice_id, status, failure or (status if next_status == "manual_review" else None),
                  payment_id, next_status, payment.get("outcome_currency"), next_status, now, now, order_id),
            )
            self._audit(connection, order["article_id"], order["version"],
                        "payment_manual_review" if failure else "payment_" + next_status, 0, now)
            return self._order(connection, order_id)

    def withdraw(self, article_id: int, expected_version: int, editor_id: int):
        now = int(time.time())
        with self._connection(write=True) as connection:
            state = self._editing(connection, article_id)
            if state["latest"]["version"] != expected_version:
                raise ArticleError("version_conflict")
            if state["status"] == "withdrawn":
                return state
            if state["published"] is None:
                raise ArticleError("article_not_published")
            connection.execute("UPDATE articles SET withdrawn_at=?,updated_at=? WHERE id=?",
                               (now, now, article_id))
            self._audit(connection, article_id, state["published"]["version"], "withdraw", editor_id, now)
            return self._editing(connection, article_id)

    def get_for_edit(self, article_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            return self._editing(connection, article_id)

    def get_author_id(self, article_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT editor_id FROM article_versions WHERE article_id=? AND version=1",
                                     (article_id,)).fetchone()
            if row is None:
                raise ArticleError("article_not_found")
            return int(row["editor_id"])

    def get_for_author(self, article_id: int, author_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            self._require_author(connection, article_id, author_id)
            return self._editing(connection, article_id)

    def list_for_author(self, author_id: int, page: int = 1, page_size: int = 20):
        if not 1 <= page <= 10000 or not 1 <= page_size <= 60:
            raise ArticleError("invalid_search")
        selection = (" FROM articles JOIN article_versions AS owner ON owner.article_id=articles.id "
                     "AND owner.version=1 WHERE owner.editor_id=?")
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*)" + selection, (author_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT articles.id" + selection + " ORDER BY articles.updated_at DESC,articles.id DESC LIMIT ? OFFSET ?",
                (author_id, page_size, (page - 1) * page_size),
            ).fetchall()
            return {"items": [self._editing(connection, row["id"]) for row in rows],
                    "total": total, "page": page, "page_size": page_size}

    def list_for_review(self, page: int = 1, page_size: int = 20):
        if not 1 <= page <= 10000 or not 1 <= page_size <= 60:
            raise ArticleError("invalid_search")
        selection = (" FROM articles JOIN article_submissions ON article_submissions.article_id=articles.id "
                     "AND article_submissions.version=articles.latest_version "
                     "WHERE article_submissions.status='submitted' AND articles.withdrawn_at IS NULL")
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*)" + selection).fetchone()[0]
            rows = connection.execute(
                "SELECT articles.id" + selection + " ORDER BY article_submissions.submitted_at,articles.id LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
            return {"items": [self._editing(connection, row["id"]) for row in rows],
                    "total": total, "page": page, "page_size": page_size}

    def list_for_edit(self, page: int = 1, page_size: int = 20):
        if not 1 <= page <= 10000 or not 1 <= page_size <= 60:
            raise ArticleError("invalid_search")
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            rows = connection.execute("SELECT id FROM articles ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                                      (page_size, (page - 1) * page_size)).fetchall()
            return {"items": [self._editing(connection, row["id"]) for row in rows],
                    "total": total, "page": page, "page_size": page_size}

    def preview_version(self, article_id: int, version: int) -> dict[str, Any]:
        with self._connection() as connection:
            state = self._editing(connection, article_id)
            return {"slug": state["slug"], **self._version(connection, article_id, version)}

    def add_media(self, content: bytes, editor_id: int) -> dict[str, Any]:
        from .article_render import prepare_image
        try:
            prepared = prepare_image(content)
        except ValueError as exc:
            raise ArticleError(str(exc)) from None
        media_id = secrets.token_hex(16)
        with self._connection(write=True) as connection:
            connection.execute("INSERT INTO article_media VALUES(?,?,?,?,?,?,?)",
                               (media_id, prepared["content"], prepared["mime"], prepared["width"],
                                prepared["height"], editor_id, int(time.time())))
        return {"id": media_id, "mime": prepared["mime"], "width": prepared["width"], "height": prepared["height"]}

    def get_media(self, media_id: str, public: bool = True, *, author_id: int | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", media_id):
            raise ArticleError("media_not_found")
        with self._connection() as connection:
            if public and connection.execute(
                    "SELECT 1 FROM article_version_media JOIN articles ON articles.id=article_version_media.article_id "
                    "AND articles.published_version=article_version_media.version "
                    "WHERE articles.withdrawn_at IS NULL AND article_version_media.media_id=? LIMIT 1",
                    (media_id,)).fetchone() is None:
                raise ArticleError("media_not_found")
            if author_id is not None:
                self._require_media_author(connection, media_id, author_id)
            row = connection.execute("SELECT content,mime,width,height FROM article_media WHERE id=?",
                                     (media_id,)).fetchone()
            if row is None:
                raise ArticleError("media_not_found")
            return {"content": bytes(row["content"]), "mime": row["mime"], "width": row["width"], "height": row["height"]}

    def public_state(self, slug: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM articles WHERE slug=?", (slug,)).fetchone()
            if row is None or row["published_version"] is None:
                return {"status": "missing"}
            if row["withdrawn_at"] is not None:
                return {"status": "withdrawn"}
            version = self._version(connection, row["id"], row["published_version"])
            return {"status": "published", "article": {
                **version["snapshot"], "slug": row["slug"], "published_at": row["published_at"],
                "updated_at": row["public_updated_at"], "content_hash": version["hash"],
            }}

    def list_published(self, query: str = "", tag: str = "", page: int = 1, page_size: int = 20):
        if (not isinstance(page, int) or not 1 <= page <= 10000
                or not isinstance(page_size, int) or not 1 <= page_size <= 60
                or len(query) > 200 or len(tag) > 40):
            raise ArticleError("invalid_search")
        where = ["articles.published_version IS NOT NULL", "articles.withdrawn_at IS NULL"]
        parameters = []
        if query.strip():
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(json_extract(article_versions.snapshot_json,'$.title') LIKE ? ESCAPE '\\' "
                         "OR json_extract(article_versions.snapshot_json,'$.summary') LIKE ? ESCAPE '\\' "
                         "OR json_extract(article_versions.snapshot_json,'$.markdown') LIKE ? ESCAPE '\\')")
            parameters.extend(["%" + escaped + "%"] * 3)
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(article_versions.snapshot_json,'$.tags') "
                         "AS article_tag WHERE article_tag.value=?)")
            parameters.append(tag)
        selection = (" FROM articles JOIN article_versions ON article_versions.article_id=articles.id "
                     "AND article_versions.version=articles.published_version WHERE " + " AND ".join(where))
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*)" + selection, parameters).fetchone()[0]
            rows = connection.execute(
                "SELECT articles.*,article_versions.snapshot_json,article_versions.snapshot_hash" + selection
                + " ORDER BY articles.published_at DESC,articles.id DESC LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for row in rows:
                if snapshot_hash(row["snapshot_json"]) != row["snapshot_hash"]:
                    raise ArticleError("snapshot_corrupt")
                items.append({**json.loads(row["snapshot_json"]), "slug": row["slug"],
                              "published_at": row["published_at"], "updated_at": row["public_updated_at"],
                              "content_hash": row["snapshot_hash"]})
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination).resolve()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ArticleError("backup_exists") from None
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as source:
                with closing(sqlite3.connect(target, timeout=5)) as output:
                    source.backup(output)
                    if (output.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                            or output.execute("PRAGMA foreign_key_check").fetchone() is not None):
                        raise ArticleError("backup_failed")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def sitemap_entries(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT slug,public_updated_at FROM articles WHERE published_version IS NOT NULL "
                "AND withdrawn_at IS NULL ORDER BY id",
            ).fetchall()
            return [{"slug": validate_slug(row["slug"]), "updated_at": row["public_updated_at"]} for row in rows]

    def published_tags(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT article_tag.value FROM articles JOIN article_versions "
                "ON article_versions.article_id=articles.id AND article_versions.version=articles.published_version "
                "JOIN json_each(article_versions.snapshot_json,'$.tags') AS article_tag "
                "WHERE articles.withdrawn_at IS NULL ORDER BY article_tag.value",
            ).fetchall()
            return [row[0] for row in rows]