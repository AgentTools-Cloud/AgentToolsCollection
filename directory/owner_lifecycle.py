"""Verified-owner withdrawals, restorations, merges and migration requests."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
import uuid

import httpx

from . import db, owner_declarations, ownership, public_http


class LifecycleBlocked(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS owner_retired_sources (
        retirement_id INTEGER NOT NULL REFERENCES retired_listings(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        identity_hash TEXT NOT NULL,
        PRIMARY KEY(retirement_id,kind,source,identity_hash)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS owner_retired_source_lookup "
                 "ON owner_retired_sources(kind,source,identity_hash)")
    conn.execute("""CREATE TABLE IF NOT EXISTS owner_lifecycle_requests (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        action TEXT NOT NULL CHECK(action IN ('retire','restore','migration','merge')),
        kind TEXT NOT NULL CHECK(kind IN ('x402','mcp','a2a')),
        slug TEXT NOT NULL,
        listing_id INTEGER NOT NULL,
        target_slug TEXT,
        target_id INTEGER,
        version TEXT NOT NULL,
        target_version TEXT,
        status TEXT NOT NULL CHECK(status IN ('retired','restored','pending','closed','merged')),
        retirement_id INTEGER REFERENCES retired_listings(id) ON DELETE CASCADE,
        result_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS owner_listing_merges (
        retirement_id INTEGER PRIMARY KEY REFERENCES retired_listings(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('x402','mcp','a2a')),
        source_slug TEXT NOT NULL,
        target_slug TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS owner_merge_target_lookup "
                 "ON owner_listing_merges(kind,target_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS owner_pending_migration "
                 "ON owner_lifecycle_requests(user_id,kind,listing_id,target_id) "
                 "WHERE action='migration' AND status='pending'")
    for kind, table in db._KIND_TABLE.items():
        conn.execute("CREATE TRIGGER IF NOT EXISTS owner_migration_delete_%s "
                     "AFTER DELETE ON %s BEGIN DELETE FROM owner_lifecycle_requests "
                     "WHERE action='migration' AND kind='%s' AND "
                     "(listing_id=old.id OR target_id=old.id); END" % (kind, table, kind))
        conn.execute("CREATE TRIGGER IF NOT EXISTS owner_merge_target_delete_%s "
                 "AFTER DELETE ON %s BEGIN DELETE FROM owner_listing_merges "
                 "WHERE kind='%s' AND target_id=old.id; END" % (kind, table, kind))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    default=str).encode()).hexdigest()


def _host(row, kind):
    column = db._PRIMARY_ENDPOINT[kind]
    value = row.get(column) or (row.get("card_url") if kind == "a2a" else "")
    return ownership.normalize_host(urlsplit(value).hostname or "")


def _load(conn, kind, slug, user_id):
    if kind not in db._KIND_TABLE:
        raise LifecycleBlocked("not_found")
    if db.find_privacy_suppression_any(conn, kind, {"slug": slug}) is not None:
        raise LifecycleBlocked("not_found")
    active = conn.execute("SELECT * FROM %s WHERE slug=?" % db._KIND_TABLE[kind],
                          (slug,)).fetchone()
    retired = None if active else db.get_retired_listing(conn, kind, slug)
    row = dict(active) if active else dict(((retired or {}).get("snapshot") or {}).get("row") or {})
    if not row or db.find_privacy_suppression(conn, kind, row) is not None:
        raise LifecycleBlocked("not_found")
    owner = ownership._active_whole_host_claim(conn, user_id, _host(row, kind))
    if retired and retired["retired_by"] != "owner:%s" % user_id:
        raise LifecycleBlocked("staff_review_required")
    if active:
        db.assert_listing_not_retired(conn, kind, row)
    return row, retired, owner


def state(conn, kind, slug, user_id):
    row, retired, _owner = _load(conn, kind, slug, user_id)
    version = fingerprint(retired if retired else row)
    requests = [json.loads(entry[0]) for entry in conn.execute(
        "SELECT result_json FROM owner_lifecycle_requests WHERE user_id=? AND "
        "kind=? AND (slug=? OR target_slug=?) ORDER BY created_at DESC LIMIT 20",
        (user_id, kind, slug, slug))]
    targets = []
    if not retired:
        for host in ownership.hosts_for_user(conn, user_id):
            for candidate in db.listings_for_host(conn, host):
                if candidate["kind"] != kind or candidate["slug"] == slug:
                    continue
                try:
                    target, target_retired, _owner = _load(conn, kind, candidate["slug"], user_id)
                except (LifecycleBlocked, ownership.OwnershipBlocked, ValueError):
                    continue
                if not target_retired:
                    targets.append({"slug": target["slug"], "name": target["name"],
                                    "version": fingerprint(target)})
                if len(targets) >= 100:
                    break
    return {"kind": kind, "slug": slug, "name": row["name"], "host": _host(row, kind),
            "version": version, "status": "retired" if retired else "active",
            "requests": requests, "merge_targets": targets,
            "operations_enabled": db.get_meta(conn, "owner_lifecycle_enabled") == "1"}


def replacement(conn, kind, retired):
    target = conn.execute("SELECT listing.* FROM owner_listing_merges merges "
                          "JOIN %s listing ON listing.id=merges.target_id "
                          "WHERE merges.retirement_id=? AND merges.kind=?" % db._KIND_TABLE[kind],
                          (retired["id"], kind)).fetchone()
    if target is None:
        return None
    target = dict(target)
    if db.find_privacy_suppression(conn, kind, target) is not None:
        return None
    db.assert_listing_not_retired(conn, kind, target)
    prefix = {"x402": "/services/", "mcp": "/mcp/servers/", "a2a": "/a2a/agents/"}[kind]
    return {"slug": target["slug"], "name": target["name"], "url": prefix + target["slug"]}


def managed_listings(conn, user_id):
    rows = []
    hosts = set(ownership.hosts_for_user(conn, user_id))
    if not hosts:
        return rows
    for host in hosts:
        for row in db.listings_for_host(conn, host):
            kind = row["kind"]
            try:
                entry = state(conn, kind, row["slug"], user_id)
            except (LifecycleBlocked, ownership.OwnershipBlocked, ValueError):
                continue
            rows.append({key: entry[key] for key in ("kind", "slug", "name", "status")})
    for retired in conn.execute("SELECT kind,slug FROM retired_listings WHERE status='active' AND "
                                "retired_by=? ORDER BY retired_at DESC LIMIT 100", ("owner:%s" % user_id,)):
        try:
            entry = state(conn, retired["kind"], retired["slug"], user_id)
        except (LifecycleBlocked, ownership.OwnershipBlocked, ValueError):
            continue
        rows.append({key: entry[key] for key in ("kind", "slug", "name", "status")})
    return ([entry for entry in rows if entry["status"] == "retired"][:100]
            + [entry for entry in rows if entry["status"] == "active"][:100])


def _request_id(value):
    try:
        return uuid.UUID(value).hex
    except (ValueError, AttributeError, TypeError):
        raise LifecycleBlocked("invalid_idempotency_key") from None


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise LifecycleBlocked("version_required")


def _previous(conn, request_id, user_id, action, kind, slug, version, target_slug=None):
    previous = conn.execute("SELECT * FROM owner_lifecycle_requests WHERE id=?",
                            (request_id,)).fetchone()
    if previous is None:
        return None
    if (previous["user_id"], previous["action"], previous["kind"], previous["slug"],
            previous["version"], previous["target_slug"]) != (
            user_id, action, kind, slug, version, target_slug):
        raise LifecycleBlocked("idempotency_conflict")
    return json.loads(previous["result_json"])


def _proofs(conn, kind, row, user_id, peer=None):
    hosts = set()
    for current in (row, peer) if peer else (row,):
        hosts.add(_host(current, kind))
        hosts.update(ownership.normalize_host(urlsplit(value).hostname or "")
                     for value in db._retirement_urls(kind, current))
    return tuple(ownership.fresh_ownership(conn, user_id, host) for host in sorted(hosts))


def _assert_proofs(conn, proofs, user_id):
    for proof in proofs:
        ownership.assert_fresh_ownership(conn, proof, user_id, proof.host)


def _operation_ready(conn, user_id, credential_id):
    if db.get_meta(conn, "owner_lifecycle_enabled") != "1":
        raise LifecycleBlocked("lifecycle_unavailable")
    if credential_id is not None:
        key = conn.execute("SELECT user_id,revoked_at FROM api_keys WHERE id=?", (credential_id,)).fetchone()
        if key is None or key["user_id"] != user_id or key["revoked_at"] is not None:
            raise ownership.OwnershipBlocked("credential_changed")


def _assert_unshared_identity(conn, kind, row, active=True):
    for value in db._retirement_urls(kind, row):
        for other_kind, (table, columns) in db._RETIREMENT_TABLES.items():
            other = db._find_exact_endpoint_row(conn, table, columns, value,
                                               exclude_id=row["id"] if active and other_kind == kind else None)
            if other is not None:
                raise LifecycleBlocked("shared_endpoint")
    identities = _source_identities(conn, kind, row) if active else {
        (str(row["source"]), str(row["source_id"]))} if row.get("source") and row.get("source_id") else set()
    for source, identity in identities:
        if any(int(other["id"]) != int(row["id"]) for other in
               db._source_identity_rows(conn, kind, source, identity)):
            raise LifecycleBlocked("shared_endpoint")


def _record(conn, request_id, user_id, action, kind, row, version, result,
            retirement_id=None, target=None, target_version=None):
    conn.execute("INSERT INTO owner_lifecycle_requests(id,user_id,action,kind,slug,listing_id,"
                 "target_slug,target_id,version,target_version,status,retirement_id,result_json,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (request_id, user_id, action, kind, row["slug"], row["id"],
                  target["slug"] if target else None, target["id"] if target else None,
                  version, target_version, result["status"], retirement_id,
                  json.dumps(result), int(time.time())))
    from . import notices
    notices.record_lifecycle(conn, action, request_id)
    if action in ("retire", "restore", "merge"):
        db.set_meta(conn, "owner_catalog_revision", request_id)


def _source_identities(conn, kind, row):
    sources = [row] + [dict(entry) for entry in conn.execute(
        "SELECT source,source_id FROM listing_sources WHERE kind=? AND listing_id=?",
        (kind, row["id"]))]
    sources.extend({"source": "wellknown", "source_id": entry[0]} for entry in conn.execute(
        "SELECT source_id FROM wellknown_endpoint_sources WHERE kind=? AND listing_id=?",
        (kind, row["id"])))
    return {(str(entry["source"]), str(entry["source_id"])) for entry in sources
            if entry.get("source") and entry.get("source_id")}


def _no_merge_dependants(conn, kind, listing_id):
    if conn.execute("SELECT 1 FROM owner_listing_merges merges JOIN retired_listings retired "
                    "ON retired.id=merges.retirement_id WHERE merges.kind=? AND merges.target_id=? "
                    "AND retired.status='active' LIMIT 1", (kind, listing_id)).fetchone():
        raise LifecycleBlocked("merge_target_in_use")


def _withdraw(conn, kind, current, user_id, reason):
    _assert_unshared_identity(conn, kind, current)
    _no_merge_dependants(conn, kind, current["id"])
    sources = _source_identities(conn, kind, current)
    history = {table: [dict(entry) for entry in conn.execute(
        "SELECT * FROM %s WHERE kind=? AND listing_id=?" % table, (kind, current["id"]))]
        for table in ("wellknown_endpoint_sources", "listing_endpoint_observations",
                      "listing_endpoint_transitions", "source_endpoint_warnings")}
    retired = db.retire_listing(conn, kind, current["slug"], reason, "owner:%s" % user_id)
    snapshot = json.loads(retired["snapshot"])
    snapshot["source_history"] = history
    conn.execute("UPDATE retired_listings SET snapshot=? WHERE id=?",
                 (json.dumps(snapshot), retired["id"]))
    conn.execute("DELETE FROM wellknown_endpoint_sources WHERE kind=? AND listing_id=?",
                 (kind, current["id"]))
    for source, identity in sources:
        conn.execute("INSERT OR IGNORE INTO owner_retired_sources(retirement_id,kind,source,identity_hash) "
                     "VALUES(?,?,?,?)", (retired["id"], kind, source,
                     hashlib.sha256(identity.encode()).hexdigest()))
    return retired


def retire(kind, slug, user_id, version, request_id, credential_id=None):
    _version(version)
    request_id = _request_id(request_id)
    with closing(db.connect(read_only=True)) as conn:
        _operation_ready(conn, user_id, credential_id)
        row, retired, _owner = _load(conn, kind, slug, user_id)
        previous = _previous(conn, request_id, user_id, "retire", kind, slug, version)
        if not previous and (retired or fingerprint(row) != version):
            raise LifecycleBlocked("listing_changed")
        proofs = _proofs(conn, kind, row, user_id)
    def write():
        with db.writer(immediate=True) as conn:
            _operation_ready(conn, user_id, credential_id)
            _assert_proofs(conn, proofs, user_id)
            previous = _previous(conn, request_id, user_id, "retire", kind, slug, version)
            if previous:
                return previous
            current, retired, _owner = _load(conn, kind, slug, user_id)
            if retired or fingerprint(current) != version:
                raise LifecycleBlocked("listing_changed")
            retired = _withdraw(conn, kind, current, user_id, "Withdrawn by verified operator")
            result = {"kind": kind, "slug": slug, "status": "retired", "request_id": request_id}
            _record(conn, request_id, user_id, "retire", kind, current, version, result,
                    retirement_id=retired["id"])
            return result
    return db.with_retry(write)


def merge(kind, slug, user_id, version, request_id, target_slug, target_version,
          credential_id=None):
    _version(version)
    _version(target_version)
    request_id = _request_id(request_id)
    if (not isinstance(target_slug, str) or not re.fullmatch(r"[a-z0-9_-]{1,180}", target_slug)
            or target_slug == slug):
        raise LifecycleBlocked("invalid_target")
    with closing(db.connect(read_only=True)) as conn:
        _operation_ready(conn, user_id, credential_id)
        source, retired, _owner = _load(conn, kind, slug, user_id)
        target, target_retired, _target_owner = _load(conn, kind, target_slug, user_id)
        previous = _previous(conn, request_id, user_id, "merge", kind, slug, version, target_slug)
        if previous:
            entry = conn.execute("SELECT target_version FROM owner_lifecycle_requests WHERE id=?",
                                 (request_id,)).fetchone()
            if entry["target_version"] != target_version:
                raise LifecycleBlocked("idempotency_conflict")
        elif retired or target_retired or fingerprint(source) != version or fingerprint(target) != target_version:
            raise LifecycleBlocked("listing_changed")
        if target_retired:
            raise LifecycleBlocked("listing_changed")
        proofs = _proofs(conn, kind, source, user_id, peer=target)
    observation = None
    if not previous:
        try:
            observation = observe_restore(kind, target)
        except (httpx.HTTPError, public_http.UnsafeURL, ValueError, TypeError, KeyError):
            raise LifecycleBlocked("service_unverified") from None
    def write():
        with db.writer(immediate=True) as conn:
            _operation_ready(conn, user_id, credential_id)
            _assert_proofs(conn, proofs, user_id)
            previous = _previous(conn, request_id, user_id, "merge", kind, slug, version, target_slug)
            if previous:
                return previous
            source, retired, _owner = _load(conn, kind, slug, user_id)
            current_target, target_retired, _owner = _load(conn, kind, target_slug, user_id)
            if (retired or target_retired or fingerprint(source) != version
                    or fingerprint(current_target) != target_version
                    or not 0 <= time.time() - observation["checked_at"] <= 60):
                raise LifecycleBlocked("listing_changed")
            retired = _withdraw(conn, kind, source, user_id, "Replaced by verified operator")
            conn.execute("INSERT INTO owner_listing_merges(retirement_id,kind,source_slug,target_slug,"
                         "target_id,user_id,created_at) VALUES(?,?,?,?,?,?,?)",
                         (retired["id"], kind, slug, target_slug, current_target["id"], user_id, int(time.time())))
            result = {"kind": kind, "slug": slug, "status": "merged", "target_slug": target_slug,
                      "request_id": request_id}
            _record(conn, request_id, user_id, "merge", kind, source, version, result,
                    retirement_id=retired["id"], target=current_target, target_version=target_version)
            return result
    return db.with_retry(write)


def request_migration(kind, target_slug, user_id, source_slug, version, request_id, credential_id=None):
    _version(version)
    request_id = _request_id(request_id)
    if not isinstance(source_slug, str) or not re.fullmatch(r"[a-z0-9_-]{1,180}", source_slug):
        raise LifecycleBlocked("invalid_source")
    if source_slug == target_slug:
        raise LifecycleBlocked("invalid_source")
    with closing(db.connect(read_only=True)) as conn:
        _operation_ready(conn, user_id, credential_id)
        target, retired, _owner = _load(conn, kind, target_slug, user_id)
        previous = conn.execute("SELECT * FROM owner_lifecycle_requests WHERE id=?", (request_id,)).fetchone()
        if previous:
            if (previous["user_id"], previous["action"], previous["kind"], previous["slug"],
                    previous["target_slug"], previous["target_version"]) != (
                    user_id, "migration", kind, source_slug, target_slug, version):
                raise LifecycleBlocked("idempotency_conflict")
            proof = ownership.fresh_ownership(conn, user_id, _host(target, kind))
            ownership.assert_fresh_ownership(conn, proof, user_id, proof.host)
            return json.loads(previous["result_json"])
        if retired or fingerprint(target) != version:
            raise LifecycleBlocked("listing_changed")
        if db.find_privacy_suppression_any(conn, kind, {"slug": source_slug}) is not None:
            raise LifecycleBlocked("not_found")
        source = conn.execute("SELECT * FROM %s WHERE slug=?" % db._KIND_TABLE[kind],
                              (source_slug,)).fetchone()
        if source is None:
            raise LifecycleBlocked("not_found")
        source = dict(source)
        db.assert_listing_not_retired(conn, kind, source)
        source_version = fingerprint(source)
        proof = ownership.fresh_ownership(conn, user_id, _host(target, kind))
    def write():
        with db.writer(immediate=True) as conn:
            _operation_ready(conn, user_id, credential_id)
            ownership.assert_fresh_ownership(conn, proof, user_id, proof.host)
            previous = _previous(conn, request_id, user_id, "migration", kind, source_slug,
                                 source_version, target_slug)
            if previous:
                return previous
            current, retired, _owner = _load(conn, kind, target_slug, user_id)
            fresh_source = conn.execute("SELECT * FROM %s WHERE id=?" % db._KIND_TABLE[kind],
                                        (source["id"],)).fetchone()
            if (retired or fingerprint(current) != version or fresh_source is None
                    or fingerprint(dict(fresh_source)) != source_version):
                raise LifecycleBlocked("listing_changed")
            db.assert_listing_not_retired(conn, kind, dict(fresh_source))
            pending = conn.execute("SELECT result_json FROM owner_lifecycle_requests "
                                   "WHERE user_id=? AND kind=? AND listing_id=? AND target_id=? "
                                   "AND action='migration' AND status='pending'",
                                   (user_id, kind, source["id"], current["id"])).fetchone()
            if pending:
                return json.loads(pending[0])
            result = {"kind": kind, "slug": source_slug, "target_slug": target_slug,
                      "status": "pending", "request_id": request_id}
            _record(conn, request_id, user_id, "migration", kind, source, source_version,
                    result, target=current, target_version=version)
            return result
    return db.with_retry(write)


def _protocol_response(client, target, deadline, payload=None):
    if time.monotonic() >= deadline:
        raise LifecycleBlocked("service_unverified")
    method = "POST" if payload is not None else "GET"
    options = {"json": payload} if payload is not None else {}
    with client.stream(method, target, **options) as response:
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 512 * 1024 or time.monotonic() >= deadline:
                raise LifecycleBlocked("service_unverified")
        return httpx.Response(response.status_code, content=bytes(content),
                              headers=response.headers, request=response.request)


def observe_restore(kind, row):
    from . import cards, crawlers
    if kind == "x402":
        row = db.row_to_dict(row)
        observed_row = deepcopy(row)
        declaration = owner_declarations.stored(row)
        if not declaration:
            primary_method = cards.build_call_template(row).get("http_x402", {}).get("method") or "GET"
            fields = {"method": primary_method}
            for name in ("well_known_url", "openapi_url"):
                if row.get(name):
                    fields[name] = row[name]
            declaration, _changed = owner_declarations.prepare(row, fields)
            observed_row["owner_declaration"] = declaration
        result = owner_declarations.observe(observed_row)
        if result["status"] != "verified":
            raise LifecycleBlocked("service_unverified")
        return {"checked_at": result["checked_at"], "declaration": declaration,
                "validation": result}
    deadline = time.monotonic() + 20
    started = time.monotonic()
    with public_http.client(timeout=httpx.Timeout(connect=3, read=5, write=5, pool=3),
                            follow_redirects=False, headers={"Accept": "application/json, text/event-stream",
                            "User-Agent": crawlers.UA}) as client:
        if kind == "mcp":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": crawlers.MCP_LATEST_REVISION, "capabilities": {},
                "clientInfo": {"name": "agent-tools.cloud", "version": "0.1"}}}
            response = _protocol_response(client, row["endpoint_url"], deadline, payload)
            envelope = crawlers._mcp_parse_response(response)
            result = envelope.get("result") if isinstance(envelope, dict) else None
            if (response.status_code != 200 or not isinstance(result, dict)
                    or envelope.get("jsonrpc") != "2.0" or envelope.get("error")
                    or envelope.get("id") != 1 or not isinstance(result.get("protocolVersion"), str)
                    or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result.get("serverInfo"), dict)
                    or not isinstance(result["serverInfo"].get("name"), str)):
                raise LifecycleBlocked("service_unverified")
            values = {"conformance": "partial", "protocol_version": result["protocolVersion"][:80]}
        elif kind == "a2a":
            target = row.get("card_url")
            if not target:
                raise LifecycleBlocked("service_unverified")
            response = _protocol_response(client, target, deadline)
            card = response.json()
            endpoints = [card.get("url")] if isinstance(card, dict) else []
            if isinstance(card, dict):
                for name in ("supportedInterfaces", "additionalInterfaces"):
                    endpoints.extend(item.get("url") for item in card.get(name) or [] if isinstance(item, dict))
            if (response.status_code != 200 or not isinstance(card, dict)
                    or not isinstance(card.get("name"), str) or not card["name"].strip()
                    or not isinstance(card.get("skills"), list) or not card["skills"]
                    or not any(db._exact_endpoint(value or "") == db._exact_endpoint(row.get("endpoint_url") or "")
                               for value in endpoints if isinstance(value, str))):
                raise LifecycleBlocked("service_unverified")
            values = {"conformance": "pass"}
        else:
            raise LifecycleBlocked("not_found")
    checked_at = int(time.time())
    return {"checked_at": checked_at, "values": {**values, "health": "ok",
            "health_checked": checked_at, "latency_ms": int((time.monotonic() - started) * 1000)}}


def restore(kind, slug, user_id, version, request_id, credential_id=None):
    _version(version)
    request_id = _request_id(request_id)
    with closing(db.connect(read_only=True)) as conn:
        _operation_ready(conn, user_id, credential_id)
        row, retired, owner = _load(conn, kind, slug, user_id)
        previous = _previous(conn, request_id, user_id, "restore", kind, slug, version)
        if previous:
            proof = ownership.fresh_ownership(conn, user_id, _host(row, kind))
            ownership.assert_fresh_ownership(conn, proof, user_id, proof.host)
            return previous
        if retired is None or fingerprint(retired) != version:
            raise LifecycleBlocked("listing_changed")
        if db.find_privacy_suppression(conn, kind, retired["snapshot"]) is not None:
            raise LifecycleBlocked("not_found")
        expected_retirement = fingerprint(retired)
        ownership_id = int(owner["id"])
        proofs = _proofs(conn, kind, row, user_id)
    try:
        observation = observe_restore(kind, row)
    except (httpx.HTTPError, public_http.UnsafeURL, ValueError, TypeError, KeyError):
        raise LifecycleBlocked("service_unverified") from None
    def write():
        with db.writer(immediate=True) as conn:
            _operation_ready(conn, user_id, credential_id)
            _assert_proofs(conn, proofs, user_id)
            previous = _previous(conn, request_id, user_id, "restore", kind, slug, version)
            if previous:
                return previous
            current, retired, owner = _load(conn, kind, slug, user_id)
            if (retired is None or fingerprint(retired) != expected_retirement
                    or int(owner["id"]) != ownership_id
                    or not 0 <= time.time() - observation["checked_at"] <= 60):
                raise LifecycleBlocked("listing_changed")
            if db.find_privacy_suppression(conn, kind, retired["snapshot"]) is not None:
                raise LifecycleBlocked("not_found")
            _assert_unshared_identity(conn, kind, current, active=False)
            snapshot = retired["snapshot"]
            identities = [current] + snapshot.get("sources", [])
            for source in identities:
                if db._source_identity_rows(conn, kind, source.get("source"), source.get("source_id")):
                    raise LifecycleBlocked("shared_endpoint")
            for source in conn.execute("SELECT source,identity_hash FROM owner_retired_sources WHERE retirement_id=?",
                                        (retired["id"],)):
                for candidate in conn.execute("SELECT source_id FROM listing_sources WHERE kind=? AND source=?",
                                               (kind, source["source"])):
                    if hashlib.sha256(str(candidate["source_id"]).encode()).hexdigest() == source["identity_hash"]:
                        raise LifecycleBlocked("shared_endpoint")
            new_id = conn.execute("SELECT MAX(value)+1 FROM ("
                                  "SELECT COALESCE(MAX(id),0) AS value FROM %s UNION ALL "
                                  "SELECT COALESCE(MAX(listing_id),0) FROM owner_lifecycle_requests WHERE kind=? UNION ALL "
                                  "SELECT COALESCE(MAX(CAST(json_extract(snapshot,'$.row.id') AS INTEGER)),0) "
                                  "FROM retired_listings WHERE kind=?)"
                                  % db._KIND_TABLE[kind], (kind, kind)).fetchone()[0]
            stored_snapshot = conn.execute("SELECT snapshot FROM retired_listings WHERE id=?",
                                            (retired["id"],)).fetchone()[0]
            snapshot["row"]["id"] = int(new_id)
            conn.execute("UPDATE retired_listings SET snapshot=? WHERE id=?",
                         (json.dumps(snapshot), retired["id"]))
            restored = db.restore_retired_listing(conn, kind, slug, "owner:%s" % user_id,
                                                  "Service reverified by operator")
            conn.execute("DELETE FROM owner_listing_merges WHERE retirement_id=?", (retired["id"],))
            conn.execute("UPDATE retired_listings SET snapshot=? WHERE id=?",
                         (stored_snapshot, retired["id"]))
            db.assert_listing_not_retired(conn, kind, restored)
            listing_id = int(restored["id"])
            columns = {entry[1] for entry in conn.execute("PRAGMA table_info(%s)" % db._KIND_TABLE[kind])}
            resets = set(db._MEASURED_RESET[kind]) & columns
            if kind == "x402":
                resets.add("owner_declaration")
                conn.execute("DELETE FROM service_paytos WHERE service_id=?", (listing_id,))
            if resets:
                conn.execute("UPDATE %s SET %s WHERE id=?" % (db._KIND_TABLE[kind],
                             ",".join(name + "=NULL" for name in sorted(resets))), (listing_id,))
            conn.execute("UPDATE %s SET updated_at=? WHERE id=?" % db._KIND_TABLE[kind],
                         (int(time.time()), listing_id))
            if kind == "x402":
                declaration = deepcopy(observation["declaration"])
                declaration["status"] = "pending"
                declaration.pop("checked_at", None)
                declaration.pop("blocker", None)
                conn.execute("UPDATE services SET owner_declaration=?,owner_verified=1 WHERE id=?",
                             (json.dumps(declaration), listing_id))
                owner_declarations.apply_validation(conn, listing_id, user_id, ownership_id,
                                                     declaration, observation["validation"])
            else:
                values = {name: value for name, value in observation["values"].items() if name in columns}
                values["owner_verified"] = 1
                conn.execute("UPDATE %s SET %s WHERE id=?" % (db._KIND_TABLE[kind],
                             ",".join(name + "=?" for name in values)), [*values.values(), listing_id])
                if kind == "mcp":
                    conn.execute("INSERT INTO mcp_health_history(server_id,checked_at,status,latency_ms) "
                                 "VALUES(?,?,'ok',?)", (listing_id, observation["checked_at"], values["latency_ms"]))
            result = {"kind": kind, "slug": slug, "status": "restored", "request_id": request_id}
            _record(conn, request_id, user_id, "restore", kind, restored, version, result,
                    retirement_id=retired["id"])
            return result
    return db.with_retry(write)


def review_requests(conn):
    return [{"request_id": entry["id"], "kind": entry["kind"], "source_slug": entry["slug"],
             "target_slug": entry["target_slug"], "created_at": entry["created_at"],
             "review_version": fingerprint(dict(entry))} for entry in conn.execute(
        "SELECT * FROM owner_lifecycle_requests WHERE action='migration' AND status='pending' "
        "ORDER BY created_at LIMIT 100")]


def close_review(conn, request_id, review_version, resolution):
    if resolution not in ("handled", "declined"):
        raise LifecycleBlocked("invalid_resolution")
    entry = conn.execute("SELECT * FROM owner_lifecycle_requests WHERE id=? AND "
                         "action='migration' AND status='pending'", (_request_id(request_id),)).fetchone()
    if entry is None or fingerprint(dict(entry)) != review_version:
        raise LifecycleBlocked("listing_changed")
    result = json.loads(entry["result_json"])
    result.update(status="closed", resolution=resolution, reviewed_at=int(time.time()),
                  reviewed_by="staff-cli")
    conn.execute("UPDATE owner_lifecycle_requests SET status='closed',result_json=? WHERE id=?",
                 (json.dumps(result), entry["id"]))
    return result