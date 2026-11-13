"""One-time claim invitations from public contacts, never from account emails."""
from __future__ import annotations

from contextlib import closing
from email.headerregistry import Address
from html.parser import HTMLParser
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

import tldextract

from . import db, mailer, ownership, public_http

SOURCE_LABELS = {
    "402index": "402 Index",
    "a2a": "a public A2A endpoint",
    "aegis": "Aegis",
    "mcp-registry": "the official MCP Registry",
    "mcp": "a public MCP endpoint",
    "mcp-catalog": "MCP Catalog",
    "mcp-keepalive": "MCP Keepalive",
    "official-remote": "a public MCP endpoint",
    "apify": "Apify",
    "chiark": "Chiark",
    "prowl": "Prowl",
    "pulsemcp": "PulseMCP",
    "smithery": "Smithery",
    "wellknown": "Wellknown",
    "capkey": "Capkey",
    "agenstry": "Agenstry",
    "agenstry-paid": "Agenstry",
    "agentgates": "AgentGates",
    "cdp-bazaar": "the x402 Bazaar",
    "flows-litprotocol": "Lit Protocol",
    "github-topic": "a public GitHub topic",
    "mppdev": "mpp.dev",
    "nohumans": "NoHumans",
    "pay-skills-pr": "Pay Skills",
    "paygent-discover": "Paygent",
    "the402": "The402",
    "verantis": "Verantis",
    "a2aregistry": "the A2A Registry",
    "a2a-registry": "the A2A Registry",
    "localmark": "LocalMark",
    "awesome-a2a": "Awesome A2A",
    "awesome-x402": "Awesome x402",
    "x402scan": "x402scan",
    "x402index": "x402 Index",
    "x402": "a public x402 endpoint",
    "x402-fuchss": "a public x402 directory",
    "x402-list": "x402 List",
    "zero": "Zero",
    "seed": "a public agent-card endpoint",
}
TABLES = {"x402": "services", "mcp": "mcp_servers", "a2a": "a2a_agents"}
VIEW_PATHS = {"x402": "/services/", "mcp": "/mcp/servers/", "a2a": "/a2a/agents/"}
UNSUBSCRIBE_PATH = "/email/claim-invitations/unsubscribe"
_SUFFIXES = tldextract.TLDExtract(
    cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True,
)
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NON_CONTACT_MAILBOXES = {
    "abuse", "privacy", "legal", "security", "noreply", "no-reply",
    "donotreply", "do-not-reply", "postmaster", "takedown", "dmca", "dpo",
    "example", "your-email",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_invite_candidates (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('x402','mcp','a2a')),
    listing_id INTEGER NOT NULL,
    host TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    checked_at INTEGER,
    reason TEXT,
    UNIQUE(kind,listing_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_invite_candidates_state
    ON claim_invite_candidates(state,created_at);
CREATE TABLE IF NOT EXISTS claim_invite_outbox (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    listing_id INTEGER NOT NULL,
    host TEXT,
    publisher TEXT,
    source TEXT NOT NULL,
    email TEXT,
    contact_url TEXT,
    contact_hash TEXT,
    contact_checked_at INTEGER,
    review_ref TEXT,
    message_id TEXT,
    email_key TEXT NOT NULL UNIQUE,
    host_key TEXT NOT NULL UNIQUE,
    publisher_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft',
    created_at INTEGER NOT NULL,
    approved_at INTEGER,
    approved_hash TEXT,
    attempted_at INTEGER,
    completed_at INTEGER,
    unsubscribe_nonce TEXT,
    unsubscribe_hash TEXT UNIQUE,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_claim_invite_outbox_state
    ON claim_invite_outbox(state,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_invite_review_ref
    ON claim_invite_outbox(review_ref) WHERE review_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS claim_invite_items (
    invitation_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    listing_id INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    PRIMARY KEY(invitation_id,candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_invite_items_host ON claim_invite_items(host_key);
CREATE TABLE IF NOT EXISTS claim_invite_suppressions (
    email_key TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
"""


def init_schema(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claim_invite_outbox'").fetchone()
    if existing:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(claim_invite_outbox)")}
        for name, column_type in (("unsubscribe_nonce", "TEXT"), ("contact_hash", "TEXT"),
                                  ("contact_checked_at", "INTEGER"), ("review_ref", "TEXT"), ("message_id", "TEXT")):
            if name not in columns:
                connection.execute(f"ALTER TABLE claim_invite_outbox ADD COLUMN {name} {column_type}")
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(claim_invite_outbox)")}
    if "unsubscribe_nonce" not in columns:
        connection.execute("ALTER TABLE claim_invite_outbox ADD COLUMN unsubscribe_nonce TEXT")
    connection.execute(
        "INSERT OR IGNORE INTO claim_invite_items(invitation_id,candidate_id,kind,listing_id,host_key) "
        "SELECT id,candidate_id,kind,listing_id,host_key FROM claim_invite_outbox"
    )
    for kind, table in TABLES.items():
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_claim_invite_delete_v2 "
            f"AFTER DELETE ON {table} BEGIN "
            "UPDATE claim_invite_outbox SET email=NULL,host=NULL,publisher=NULL,contact_url=NULL,"
            "state=CASE WHEN attempted_at IS NULL THEN 'canceled' ELSE state END "
            f"WHERE id IN (SELECT invitation_id FROM claim_invite_items WHERE kind='{kind}' AND listing_id=old.id); "
            f"DELETE FROM claim_invite_candidates WHERE kind='{kind}' AND listing_id=old.id; END"
        )


def discovery_enabled() -> bool:
    return os.getenv("AGENT_TOOLS_CLAIM_INVITES_DISCOVER", "0") == "1"


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("claim_invite_transaction_required")


def _registered_domain(host: str) -> str:
    normalized = ownership.normalize_host(host)
    parsed = _SUFFIXES(normalized)
    if not parsed.suffix or not parsed.domain:
        raise ValueError("claim_invite_unknown_public_suffix")
    return parsed.top_domain_under_public_suffix


def normalize_contact(email: str, host: str) -> str:
    value = str(email).strip()
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("claim_invite_invalid_email")
    try:
        address = Address(addr_spec=value)
        domain = ownership.normalize_host(address.domain)
    except (ValueError, ownership.UnsafeTarget) as error:
        raise ValueError("claim_invite_invalid_email") from error
    if not address.username or _registered_domain(domain) != _registered_domain(host):
        raise ValueError("claim_invite_contact_not_on_publisher_domain")
    return address.username.casefold() + "@" + domain


class _ContactDocument(HTMLParser):
    ignored_tags = {"script", "style", "template", "noscript", "pre", "code", "textarea"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.text: list[str] = []
        self.addresses: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.ignored_tags:
            self.ignored_depth += 1
        if self.ignored_depth:
            return
        if tag == "a":
            attributes = dict(attrs)
            href = attributes.get("href") or ""
            if href.casefold().startswith("mailto:"):
                self.addresses.append(unquote(href[7:].split("?", 1)[0]))
            elif href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.ignored_tags and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.text.append(data)


def extract_public_contacts(document: str, *, host: str, contact_url: str,
                            content_type: str = "text/html") -> list[str]:
    parsed = urlsplit(contact_url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or _registered_domain(parsed.hostname or "") != _registered_domain(host)):
        raise ValueError("claim_invite_contact_provenance_required")
    addresses: list[str] = []
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type == "text/html":
        page = _ContactDocument()
        page.feed(document)
        addresses.extend(page.addresses)
        addresses.extend(_EMAIL_PATTERN.findall(" ".join(page.text)))
    elif media_type in {"application/json", "application/ld+json"}:
        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, entry in value.items():
                    if str(key).casefold() in {"email", "contactemail", "contact_email", "supportemail"}:
                        if isinstance(entry, str):
                            addresses.append(entry.removeprefix("mailto:"))
                    elif isinstance(entry, (dict, list)):
                        collect(entry)
            elif isinstance(value, list):
                for entry in value:
                    collect(entry)

        collect(json.loads(document))
    elif media_type == "text/plain":
        addresses.extend(_EMAIL_PATTERN.findall(document))
    else:
        return []
    accepted = set()
    for address in addresses:
        try:
            normalized = normalize_contact(address.rstrip("."), host)
        except (ValueError, ownership.UnsafeTarget):
            continue
        if normalized.split("@", 1)[0].split("+", 1)[0] not in _NON_CONTACT_MAILBOXES:
            accepted.add(normalized)
    return sorted(accepted)


def fetch_public_contact_page(url: str, *, host: str, http_client=None) -> dict:
    own_client = http_client is None
    http_client = http_client or public_http.client(
        timeout=4.0, follow_redirects=False, trust_env=False,
        headers={"User-Agent": "AgentTools-ContactReview/1.0 (+https://agent-tools.cloud)",
                 "Accept": "text/html,application/json,text/plain;q=0.8"},
    )
    started = time.monotonic()
    try:
        for attempt in range(4):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                    or _registered_domain(parsed.hostname or "") != _registered_domain(host)):
                raise ValueError("claim_invite_contact_provenance_required")
            with http_client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if attempt == 3 or not response.headers.get("location"):
                        raise ValueError("claim_invite_redirect_limit")
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise ValueError("claim_invite_http_" + str(response.status_code))
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().casefold() not in {
                        "text/html", "text/plain", "application/json", "application/ld+json"}:
                    raise ValueError("claim_invite_unsupported_contact_page")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 262144:
                        raise ValueError("claim_invite_contact_page_too_large")
                    if time.monotonic() - started > 8:
                        raise ValueError("claim_invite_contact_page_timeout")
                document = bytes(body).decode("utf-8", errors="replace")
                emails = extract_public_contacts(document, host=host, contact_url=url, content_type=content_type)
                links = []
                if content_type.split(";", 1)[0].strip().casefold() == "text/html":
                    page = _ContactDocument()
                    page.feed(document)
                    for href in page.links:
                        linked = urljoin(url, href)
                        linked_path = urlsplit(linked).path.casefold()
                        if re.search(r"(?:contact|about|support|impressum)", linked_path):
                            links.append(linked)
                return {"url": url, "emails": emails, "links": list(dict.fromkeys(links)),
                        "sha256": hashlib.sha256(body).hexdigest(), "fetched_at": int(time.time())}
        raise ValueError("claim_invite_redirect_limit")
    finally:
        if own_client:
            http_client.close()


def evaluate_listing(connection: sqlite3.Connection, kind: str,
                     listing: dict) -> tuple[dict | None, str]:
    if kind not in TABLES:
        return None, "unknown_kind"
    row = dict(listing)
    endpoint = row.get("url") if kind == "x402" else row.get("endpoint_url")
    try:
        parsed = urlsplit(endpoint or "")
        host = ownership.normalize_host(parsed.hostname or "")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None, "invalid_https_endpoint"
    except (ValueError, ownership.UnsafeTarget):
        return None, "invalid_https_endpoint"
    if host == "agent-tools.cloud" or host.endswith(".agent-tools.cloud"):
        return None, "own_service"
    if row.get("owner_verified"):
        return None, "claimed"
    if db.is_shared_host(connection, host):
        return None, "shared_host"
    reviewed = connection.execute("SELECT verdict FROM shared_hosts WHERE host=?", (host,)).fetchone()
    if reviewed is None and db.is_gateway_origin(connection, "https://" + host):
        return None, "unreviewed_gateway"
    if connection.execute(
        "SELECT 1 FROM domain_ownership WHERE host=? AND status='verified' LIMIT 1", (host,),
    ).fetchone():
        return None, "claimed"
    if kind in {"mcp", "a2a"} and db._safety_mark(
            row.get("name"), row.get("description"),
            row.get("tools_text") if kind == "mcp" else row.get("skill_names")):
        return None, "safety_flag"
    try:
        db.assert_listing_not_retired(connection, kind, row)
    except db.RetiredListingError:
        return None, "retired_or_privacy_suppressed"
    row["invite_host"] = host
    return row, "eligible"


def _listing(connection: sqlite3.Connection, kind: str, listing_id: int) -> dict | None:
    table = TABLES.get(kind)
    if table is None:
        return None
    found = connection.execute(f"SELECT * FROM {table} WHERE id=?", (listing_id,)).fetchone()
    if found is None:
        return None
    return evaluate_listing(connection, kind, dict(found))[0]


def enqueue_listing(connection: sqlite3.Connection, kind: str, source: str,
                    listing_id: int, original: dict | None = None, *, manual: bool = False) -> bool:
    if (not manual and not discovery_enabled()) or source not in SOURCE_LABELS:
        return False
    _require_transaction(connection)
    row = _listing(connection, kind, listing_id)
    if row is None:
        return False
    host = row["invite_host"]
    publisher = _registered_domain(host)
    cursor = connection.execute(
        "INSERT INTO claim_invite_candidates(kind,listing_id,host,publisher,source,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(kind,listing_id) DO NOTHING",
        (kind, listing_id, host, publisher, source, int(time.time())),
    )
    return cursor.rowcount == 1


def current_candidate(connection: sqlite3.Connection, candidate_id: int,
                      *, healthy: bool = True) -> tuple[dict, dict] | None:
    found = connection.execute("SELECT * FROM claim_invite_candidates WHERE id=?", (candidate_id,)).fetchone()
    if found is None:
        return None
    candidate = dict(found)
    row = _listing(connection, candidate["kind"], candidate["listing_id"])
    if row is None or row["invite_host"] != candidate["host"]:
        return None
    if healthy and row.get("health") != "ok":
        return None
    return candidate, row


def _secret(connection: sqlite3.Connection) -> bytes:
    db._verify_privacy_suppression_key(connection)
    current = db._privacy_key_id()
    previous = db.get_meta(connection, "claim_invites:key_id")
    if previous and not hmac.compare_digest(str(previous), current):
        raise RuntimeError("claim_invites_key_changed")
    if previous is None:
        _require_transaction(connection)
        db.set_meta(connection, "claim_invites:key_id", current)
    return db._privacy_secret()


def _key(secret: bytes, purpose: str, value: str) -> str:
    return hmac.new(secret, ("claim-invites:v1:" + purpose + "\0" + value).encode(), hashlib.sha256).hexdigest()


def reserve_contact(connection: sqlite3.Connection, candidate_id: int,
                    email: str, contact_url: str, *, evidence: dict | None = None) -> int | None:
    _require_transaction(connection)
    current = current_candidate(connection, candidate_id)
    if current is None or current[0]["state"] != "pending":
        return None
    candidate, row = current
    parsed = urlsplit(contact_url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or _registered_domain(parsed.hostname or "") != _registered_domain(candidate["host"])):
        raise ValueError("claim_invite_contact_provenance_required")
    address = normalize_contact(email, candidate["host"])
    if address.split("@", 1)[0].split("+", 1)[0] in _NON_CONTACT_MAILBOXES:
        raise ValueError("claim_invite_contact_purpose_not_allowed")
    now = int(time.time())
    if (not isinstance(evidence, dict) or evidence.get("url") != contact_url
            or address not in evidence.get("emails", [])
            or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256") or ""))
            or not isinstance(evidence.get("fetched_at"), int)
            or evidence["fetched_at"] < now - 86400 or evidence["fetched_at"] > now + 30):
        raise ValueError("claim_invite_recent_public_contact_proof_required")
    secret = _secret(connection)
    email_key = _key(secret, "email", address)
    if connection.execute("SELECT 1 FROM claim_invite_suppressions WHERE email_key=?", (email_key,)).fetchone():
        connection.execute("UPDATE claim_invite_candidates SET state='suppressed',reason='opted_out' WHERE id=?", (candidate_id,))
        return None
    publisher_key = _key(secret, "publisher", candidate["publisher"])
    host_key = _key(secret, "host", candidate["host"])
    previous = connection.execute(
        "SELECT * FROM claim_invite_outbox WHERE publisher_key=? OR email_key=? OR host_key=? "
        "OR id IN (SELECT invitation_id FROM claim_invite_items WHERE host_key=?) LIMIT 1",
        (publisher_key, email_key, host_key, host_key),
    ).fetchone()
    if previous:
        if (previous["publisher_key"] == publisher_key and previous["state"] == "draft"
                and previous["attempted_at"] is None and previous["email"]):
            connection.execute(
                "INSERT OR IGNORE INTO claim_invite_items(invitation_id,candidate_id,kind,listing_id,host_key) VALUES(?,?,?,?,?)",
                (previous["id"], candidate_id, candidate["kind"], candidate["listing_id"], host_key),
            )
            connection.execute("UPDATE claim_invite_candidates SET state='queued',checked_at=? WHERE id=?", (now, candidate_id))
        else:
            connection.execute("UPDATE claim_invite_candidates SET state='deduplicated',checked_at=?,reason='already_invited' WHERE id=?", (now, candidate_id))
        return None
    cursor = connection.execute(
        "INSERT INTO claim_invite_outbox(candidate_id,kind,listing_id,host,publisher,source,email,contact_url,"
        "contact_hash,contact_checked_at,email_key,host_key,publisher_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT DO NOTHING",
        (candidate_id, candidate["kind"], candidate["listing_id"], candidate["host"],
         candidate["publisher"], candidate["source"], address, contact_url, evidence["sha256"], evidence["fetched_at"], email_key,
         host_key, publisher_key, now),
    )
    created = cursor.rowcount == 1
    if created:
        connection.execute(
            "INSERT INTO claim_invite_items(invitation_id,candidate_id,kind,listing_id,host_key) VALUES(?,?,?,?,?)",
            (cursor.lastrowid, candidate_id, candidate["kind"], candidate["listing_id"], host_key),
        )
    connection.execute(
        "UPDATE claim_invite_candidates SET state=?,checked_at=?,reason=? WHERE id=?",
        ("queued" if created else "deduplicated", int(time.time()), None if created else "already_invited", candidate_id),
    )
    return int(cursor.lastrowid) if created else None


def template_hash() -> str:
    subject, text, markup = mailer.claim_invitation_message(
        "LISTING_NAME", "PUBLIC_SOURCE", mailer.SITE + "/services/LISTING_SLUG",
        mailer.SITE + UNSUBSCRIBE_PATH + "#UNSUBSCRIBE_TOKEN",
    )
    return hashlib.sha256("\0".join((subject, text, markup)).encode()).hexdigest()


def preview(connection: sqlite3.Connection, invitation_id: int) -> dict | None:
    found = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
    if found is None:
        return None
    invitation = dict(found)
    if not invitation["email"]:
        return None
    listings = []
    for member in connection.execute("SELECT candidate_id FROM claim_invite_items WHERE invitation_id=? ORDER BY kind,listing_id", (invitation_id,)):
        current = current_candidate(connection, member["candidate_id"])
        if current is None or current[0]["publisher"] != invitation["publisher"]:
            return None
        candidate, listing = current
        entry = {"kind": candidate["kind"], "listing_id": listing["id"], "host": candidate["host"],
                 "name": listing["name"], "source_name": SOURCE_LABELS[candidate["source"]],
                 "listing_url": mailer.SITE + VIEW_PATHS[candidate["kind"]] + quote(str(listing["slug"]), safe="")}
        if listing.get("readiness_grade") and listing.get("readiness_score") is not None:
            entry["readiness"] = {"grade": listing["readiness_grade"],
                                  "score": round(float(listing["readiness_score"]))}
        listings.append(entry)
    if not listings:
        return None
    token = unsubscribe_token(connection, invitation_id) or "UNSUBSCRIBE_TOKEN"
    subject, text, markup = mailer.claim_invitation_group_message(listings, mailer.SITE + UNSUBSCRIBE_PATH + "#" + token)
    local, domain = invitation["email"].split("@", 1)
    payload = (invitation["email"], mailer.CLAIM_INVITE_FROM, mailer.REPLY_TO, invitation["message_id"] or "",
               subject, text, markup, json.dumps(listings, sort_keys=True, separators=(",", ":")))
    return {"id": invitation_id, "host": invitation["host"], "recipient_hint": local[:2] + "***@" + domain,
            "recipient": invitation["email"], "sender": mailer.CLAIM_INVITE_FROM, "listings": listings,
            "contact_url": invitation["contact_url"], "contact_checked_at": invitation["contact_checked_at"],
            "ready_for_approval": bool(invitation["unsubscribe_nonce"] and invitation["message_id"]
                                       and invitation["contact_checked_at"] and invitation["contact_checked_at"] >= int(time.time()) - 86400),
            "subject": subject, "text": text, "html": markup, "template_hash": template_hash(),
            "content_hash": hashlib.sha256("\0".join(payload).encode()).hexdigest()}


def unsubscribe_token(connection: sqlite3.Connection, invitation_id: int) -> str | None:
    invitation = connection.execute(
        "SELECT unsubscribe_nonce,unsubscribe_hash FROM claim_invite_outbox WHERE id=?", (invitation_id,),
    ).fetchone()
    if invitation is None or not invitation["unsubscribe_nonce"]:
        return None
    token = _key(_secret(connection), "unsubscribe", str(invitation_id) + "\0" + invitation["unsubscribe_nonce"])
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), invitation["unsubscribe_hash"] or ""):
        raise RuntimeError("claim_invite_unsubscribe_token_mismatch")
    return token


def new_unsubscribe_token(connection: sqlite3.Connection, invitation_id: int) -> str:
    _require_transaction(connection)
    nonce = secrets.token_urlsafe(32)
    token = _key(_secret(connection), "unsubscribe", str(invitation_id) + "\0" + nonce)
    cursor = connection.execute(
        "UPDATE claim_invite_outbox SET unsubscribe_nonce=?,unsubscribe_hash=? "
        "WHERE id=? AND unsubscribe_hash IS NULL AND attempted_at IS NULL AND state='draft'",
        (nonce, hashlib.sha256(token.encode()).hexdigest(), invitation_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("claim_invite_unsubscribe_token_already_issued")
    return token


def unsubscribe(connection: sqlite3.Connection, token: str) -> bool:
    _require_transaction(connection)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", str(token)):
        return False
    found = connection.execute(
        "SELECT email_key FROM claim_invite_outbox WHERE unsubscribe_hash=?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()
    if found is None:
        return False
    connection.execute(
        "INSERT INTO claim_invite_suppressions(email_key,created_at) VALUES(?,?) ON CONFLICT DO NOTHING",
        (found["email_key"], int(time.time())),
    )
    connection.execute(
        "UPDATE claim_invite_outbox SET email=NULL,contact_url=NULL,state=CASE WHEN attempted_at IS NULL "
        "THEN 'suppressed' ELSE state END WHERE email_key=?", (found["email_key"],),
    )
    return True


def prepare_preview(connection: sqlite3.Connection, invitation_id: int) -> dict | None:
    _require_transaction(connection)
    invitation = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
    if invitation is None or invitation["state"] not in {"draft", "approved"} or invitation["attempted_at"] is not None:
        return None
    if invitation["unsubscribe_hash"] is None:
        new_unsubscribe_token(connection, invitation_id)
    if not invitation["message_id"]:
        connection.execute("UPDATE claim_invite_outbox SET message_id=? WHERE id=?",
                           (mailer.make_msgid(domain="mail.agent-tools.cloud"), invitation_id))
    rendered = preview(connection, invitation_id)
    if rendered and invitation["state"] == "approved" and invitation["approved_hash"] != rendered["content_hash"]:
        connection.execute("UPDATE claim_invite_outbox SET state='draft',approved_at=NULL,approved_hash=NULL,reason='content_changed' WHERE id=?",
                           (invitation_id,))
    return rendered


def approve(connection: sqlite3.Connection, invitation_id: int, expected_hash: str) -> bool:
    _require_transaction(connection)
    invitation = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
    rendered = preview(connection, invitation_id)
    if (invitation is None or invitation["state"] not in {"draft", "approved"} or invitation["attempted_at"] is not None
            or rendered is None or not rendered["ready_for_approval"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash))
            or not hmac.compare_digest(rendered["content_hash"], expected_hash)):
        return False
    if invitation["state"] == "approved" and invitation["approved_hash"] == expected_hash:
        return True
    connection.execute("UPDATE claim_invite_outbox SET state='approved',approved_hash=?,approved_at=?,reason=NULL WHERE id=?",
                       (expected_hash, int(time.time()), invitation_id))
    return True


def reject(connection: sqlite3.Connection, invitation_id: int) -> bool:
    _require_transaction(connection)
    cursor = connection.execute(
        "UPDATE claim_invite_outbox SET state='rejected',email=NULL,contact_url=NULL,approved_hash=NULL,approved_at=NULL "
        "WHERE id=? AND attempted_at IS NULL AND state IN ('draft','approved')", (invitation_id,),
    )
    return cursor.rowcount == 1


def _approval_matches(invitation, rendered: dict | None, expected_hash: str, *, in_flight: bool = False) -> bool:
    return bool(invitation is not None and rendered and rendered["ready_for_approval"]
                and invitation["state"] == ("sending" if in_flight else "approved")
                and invitation["approved_at"] and invitation["approved_at"] >= int(time.time()) - 86400
                and re.fullmatch(r"[0-9a-f]{64}", str(expected_hash))
                and hmac.compare_digest(invitation["approved_hash"] or "", expected_hash)
                and hmac.compare_digest(rendered["content_hash"], expected_hash))


def send_once(invitation_id: int, expected_hash: str) -> dict:
    with closing(db.connect(read_only=True)) as connection:
        connection.execute("BEGIN")
        invitation = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
        if invitation is not None and invitation["attempted_at"] is not None:
            return {"id": invitation_id, "status": "already_attempted", "attempt_status": invitation["state"]}
        rendered = preview(connection, invitation_id)
        if not _approval_matches(invitation, rendered, expected_hash):
            return {"id": invitation_id, "status": "blocked", "reason": "manual_approval_required"}
        invitation = dict(invitation)
    if not mailer.claim_invitation_configured():
        return {"id": invitation_id, "status": "blocked", "reason": "sender_configuration_unavailable"}
    try:
        evidence = fetch_public_contact_page(invitation["contact_url"], host=invitation["host"])
    except Exception as error:
        return {"id": invitation_id, "status": "blocked", "reason": "contact_verification_failed", "error_type": type(error).__name__}
    if invitation["email"] not in evidence["emails"]:
        return {"id": invitation_id, "status": "blocked", "reason": "contact_no_longer_public"}
    with db.writer(immediate=True) as connection:
        current = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
        if current is not None and current["attempted_at"] is not None:
            return {"id": invitation_id, "status": "already_attempted", "attempt_status": current["state"]}
        rendered = preview(connection, invitation_id)
        if not _approval_matches(current, rendered, expected_hash):
            return {"id": invitation_id, "status": "blocked", "reason": "approval_or_listing_changed"}
        if connection.execute("SELECT 1 FROM claim_invite_suppressions WHERE email_key=?", (current["email_key"],)).fetchone():
            return {"id": invitation_id, "status": "blocked", "reason": "opted_out"}
        publisher = current["publisher"]
        if connection.execute("SELECT 1 FROM domain_ownership WHERE status='verified' AND (host=? OR host LIKE ?) LIMIT 1",
                              (publisher, "%." + publisher)).fetchone():
            return {"id": invitation_id, "status": "blocked", "reason": "publisher_already_verified"}
        prior_keys = ["approval-mail:to:" + current["email"]]
        prior_keys.extend("approval-mail:host:" + listing["host"] for listing in rendered["listings"])
        if any(connection.execute("SELECT 1 FROM rate_limits WHERE key=? LIMIT 1", (key,)).fetchone() for key in prior_keys):
            return {"id": invitation_id, "status": "blocked", "reason": "previous_submission_notification"}
        connection.execute("UPDATE claim_invite_outbox SET state='sending',attempted_at=?,contact_hash=?,contact_checked_at=? "
                           "WHERE id=? AND state='approved' AND attempted_at IS NULL",
                           (int(time.time()), evidence["sha256"], evidence["fetched_at"], invitation_id))
        recipient = current["email"]
        message_id = current["message_id"]

    def before_dispatch() -> bool:
        with closing(db.connect(read_only=True)) as connection:
            connection.execute("BEGIN")
            current = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (invitation_id,)).fetchone()
            if not _approval_matches(current, preview(connection, invitation_id), expected_hash, in_flight=True):
                return False
            return not connection.execute(
                "SELECT 1 FROM domain_ownership WHERE status='verified' AND (host=? OR host LIKE ?) LIMIT 1",
                (current["publisher"], "%." + current["publisher"]),
            ).fetchone()

    try:
        result = mailer.send_claim_invitation(recipient, rendered["subject"], rendered["text"], rendered["html"],
                                             message_id=message_id, before_dispatch=before_dispatch)
    except Exception as error:
        result = {"status": "indeterminate", "reason": type(error).__name__, "message_id": message_id}
    if result.get("status") not in {"accepted", "failed", "indeterminate", "skipped"}:
        result = {"status": "indeterminate", "reason": "unexpected_transport_result", "message_id": message_id}
    try:
        with db.writer(immediate=True) as connection:
            connection.execute("UPDATE claim_invite_outbox SET state=?,completed_at=?,reason=?,email=NULL,contact_url=NULL "
                               "WHERE id=? AND state='sending' AND attempted_at IS NOT NULL",
                               (result["status"], int(time.time()), result.get("reason"), invitation_id))
            connection.execute("UPDATE claim_invite_candidates SET state='attempted' WHERE id IN "
                               "(SELECT candidate_id FROM claim_invite_items WHERE invitation_id=?)", (invitation_id,))
    except sqlite3.Error:
        result["record_saved"] = False
    return {"id": invitation_id, **result}


def add_cli(parser) -> None:
    commands = parser.add_parser("claim-invites", help="人工审核的一次性认领邀请")
    actions = commands.add_subparsers(dest="invite_action", required=True)
    queue = actions.add_parser("list", help="列出候选和待审邮件，不发送")
    queue.add_argument("--limit", type=int, default=50)
    for action in ("preview", "approve", "reject", "send"):
        command = actions.add_parser(action)
        command.add_argument("id", type=int)
        if action == "preview":
            command.add_argument("--output", required=True, help="私有0600预览文件；不会输出完整正文到日志")
        if action in {"approve", "send"}:
            command.add_argument("--expect-hash", required=True)
    stage = actions.add_parser("stage", help="从已审核证据建立草稿并重新核验公开邮箱，不发送")
    stage.add_argument("--input", required=True)
    stage.add_argument("--expect-sha256", required=True)


def _private_preview(path: str, rendered: dict) -> None:
    target = Path(path)
    if not target.parent.is_dir() or target.parent.stat().st_mode & 0o077:
        raise ValueError("claim_invite_private_output_directory_required")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(rendered, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def stage_review(path: str, expected_hash: str) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if os.fstat(handle.fileno()).st_mode & 0o077:
            raise ValueError("claim_invite_private_input_required")
        raw = handle.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), str(expected_hash)):
        raise ValueError("claim_invite_review_input_hash_mismatch")
    report = json.loads(raw)
    outcomes = []
    for group in report["invitations"]:
        reference = group["review_id"]
        if group["state"] != "pending_review":
            outcomes.append({"ref": reference, "status": "deferred"})
            continue
        with closing(db.connect(read_only=True)) as connection:
            previous = connection.execute("SELECT id FROM claim_invite_outbox WHERE review_ref=?", (reference,)).fetchone()
            if previous:
                outcomes.append({"ref": reference, "status": "already_staged", "id": previous[0]})
                continue
            rows = []
            for listing in group["listings"]:
                row = _listing(connection, listing["kind"], int(listing["listing_id"]))
                if row is not None and row["invite_host"] == listing["host"] and row.get("health") == "ok":
                    rows.append((listing["kind"], row))
        if not rows or len({_registered_domain(row["invite_host"]) for _kind, row in rows}) != 1:
            outcomes.append({"ref": reference, "status": "ineligible"})
            continue
        recipient = normalize_contact(group["recipient"], rows[0][1]["invite_host"])
        contact = next(entry for entry in group["contacts"] if entry["email"] == recipient)
        try:
            evidence = fetch_public_contact_page(contact["contact_url"], host=rows[0][1]["invite_host"])
            if recipient not in evidence["emails"]:
                outcomes.append({"ref": reference, "status": "contact_not_public"})
                continue
        except Exception as error:
            outcomes.append({"ref": reference, "status": "contact_probe_failed", "error_type": type(error).__name__})
            continue
        with db.writer(immediate=True) as connection:
            invitation_id = None
            for kind, row in rows:
                enqueue_listing(connection, kind, row["source"], row["id"], manual=True)
                candidate = connection.execute("SELECT id FROM claim_invite_candidates WHERE kind=? AND listing_id=?", (kind, row["id"])).fetchone()
                if candidate is None:
                    continue
                created = reserve_contact(connection, candidate[0], recipient, evidence["url"], evidence=evidence)
                linked = connection.execute("SELECT invitation_id FROM claim_invite_items WHERE candidate_id=?", (candidate[0],)).fetchone()
                invitation_id = created or (linked[0] if linked else invitation_id)
            if invitation_id is not None:
                connection.execute("UPDATE claim_invite_outbox SET review_ref=? WHERE id=? AND review_ref IS NULL AND state='draft'", (reference, invitation_id))
        outcomes.append({"ref": reference, "status": "draft" if invitation_id else "deduplicated", "id": invitation_id})
    return {"results": outcomes, "sent": 0}


def run_cli(args) -> int:
    action = args.invite_action
    if action == "list":
        with closing(db.connect(read_only=True)) as connection:
            result = {"candidate_states": [dict(row) for row in connection.execute("SELECT state,count(*) AS count FROM claim_invite_candidates GROUP BY state")],
                      "invitations": [dict(row) for row in connection.execute("SELECT id,review_ref,host,state,created_at,approved_at,attempted_at,completed_at,reason FROM claim_invite_outbox ORDER BY id DESC LIMIT ?", (min(max(args.limit, 1), 100),))]}
    elif action == "stage":
        result = stage_review(args.input, args.expect_sha256)
    elif action == "preview":
        with closing(db.connect(read_only=True)) as connection:
            row = connection.execute("SELECT * FROM claim_invite_outbox WHERE id=?", (args.id,)).fetchone()
            if row is None or not row["email"] or row["attempted_at"] is not None:
                print(json.dumps({"id": args.id, "status": "unavailable"}))
                return 1
            row = dict(row)
        evidence = fetch_public_contact_page(row["contact_url"], host=row["host"])
        if row["email"] not in evidence["emails"]:
            print(json.dumps({"id": args.id, "status": "contact_not_public"}))
            return 1
        with db.writer(immediate=True) as connection:
            connection.execute("UPDATE claim_invite_outbox SET contact_hash=?,contact_checked_at=? WHERE id=? AND email=? AND attempted_at IS NULL", (evidence["sha256"], evidence["fetched_at"], args.id, row["email"]))
            rendered = prepare_preview(connection, args.id)
        if rendered is None:
            print(json.dumps({"id": args.id, "status": "unavailable"}))
            return 1
        _private_preview(args.output, rendered)
        result = {"id": args.id, "recipient_hint": rendered["recipient_hint"], "content_hash": rendered["content_hash"], "status": "preview_only"}
    elif action == "approve":
        with db.writer(immediate=True) as connection:
            approved = approve(connection, args.id, args.expect_hash)
        result = {"id": args.id, "status": "approved" if approved else "blocked", "sent": 0}
    elif action == "reject":
        with db.writer(immediate=True) as connection:
            rejected = reject(connection, args.id)
        result = {"id": args.id, "status": "rejected" if rejected else "blocked", "sent": 0}
    elif action == "send":
        result = send_once(args.id, args.expect_hash)
    else:
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("status") in {"blocked", "failed", "indeterminate", "skipped"} else 0