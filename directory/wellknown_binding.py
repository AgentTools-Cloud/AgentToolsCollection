"""Endpoint-scoped supplemental attribution, without deleting/rekeying old listings.

An upstream entity can advertise several endpoints. Legacy attribution remains
intact; this table records which endpoint was actually observed, independently
of the historical one-source-per-listing slot. It never transfers ownership.
"""
import time

from . import db
from .ingestion import digest, validate_endpoint


class SourceBindingConflict(RuntimeError):
    pass


class StaleSourceBinding(RuntimeError):
    """Proven old discovery relationship; not a failed primary update."""


def _reconcile_existing_target(conn, kind, endpoint, binding, target, owners):
    """Repair only an unambiguous pointer after an unowned registry refresh.

    Never create a replacement, transfer a source identity, mutate listings, or
    reinterpret query/path identity. No independent exact match means hold.
    """
    registry = {'mcp': 'mcp-registry', 'a2a': 'a2aregistry'}[kind]
    if (target is None or target['source'] != registry
            or target['owner_verified'] or target['owner_edited']
            or any(owner['owner_verified'] or owner['owner_edited'] for owner in owners)):
        raise SourceBindingConflict('wellknown_binding_target_changed')
    db.assert_listing_not_retired(conn, kind, dict(target))
    table = {'mcp': 'mcp_servers', 'a2a': 'a2a_agents'}[kind]
    candidates = [row for row in conn.execute(
        f'SELECT * FROM {table} WHERE endpoint_url LIKE ? COLLATE NOCASE ORDER BY id',
        (db._endpoint_authority(endpoint) + '%',),
    ) if db._exact_endpoint(row['endpoint_url']) == endpoint]
    if len(candidates) != 1:
        raise SourceBindingConflict('wellknown_binding_target_changed')
    replacement = candidates[0]
    allowed_ids = {target['id'], replacement['id']}
    if (replacement['owner_verified'] or replacement['owner_edited']
            or any(owner['id'] not in allowed_ids for owner in owners)):
        raise SourceBindingConflict('wellknown_binding_target_changed')
    db.assert_listing_not_retired(conn, kind, dict(replacement))
    updated = conn.execute(
        "UPDATE wellknown_endpoint_sources SET listing_id=?,state='active',stale_target_hash=NULL,"
        'stale_at=NULL,state_changed_at=?,stale_transition_id=NULL '
        'WHERE kind=? AND identity_hash=? AND endpoint_hash=? AND listing_id=?',
        (replacement['id'], int(time.time()), kind, binding['identity_hash'], binding['endpoint_hash'], target['id']),
    )
    if updated.rowcount != 1:
        raise SourceBindingConflict('wellknown_binding_target_changed')
    conn.execute('UPDATE source_endpoint_warnings SET resolved_at=? '
                 'WHERE kind=? AND listing_id=? AND identity_hash=? AND old_endpoint_hash=? '
                 'AND resolved_at IS NULL',
                 (int(time.time()), kind, target['id'], binding['identity_hash'], binding['endpoint_hash']))
    return replacement


def record_reviewed_alias(conn, kind, source_id, endpoint_url, listing_id):
    """Operator decision: this exact upstream endpoint is covered by listing_id.

    Scoped to one (identity, exact endpoint); any other endpoint still holds.
    Never edits the listing, its endpoint, owner fields or provenance.
    """
    table = {'mcp': 'mcp_servers', 'a2a': 'a2a_agents'}[kind]
    if conn.execute(f'SELECT 1 FROM {table} WHERE id=?', (listing_id,)).fetchone() is None:
        raise ValueError('unknown listing')
    endpoint = db._exact_endpoint(endpoint_url)
    conn.execute(
        'INSERT INTO wellknown_reviewed_aliases '
        '(kind,identity_hash,endpoint_hash,listing_id,reviewed_at) VALUES(?,?,?,?,?) '
        'ON CONFLICT(kind,identity_hash,endpoint_hash) DO UPDATE SET '
        'listing_id=excluded.listing_id,reviewed_at=excluded.reviewed_at',
        (kind, digest(source_id), digest(endpoint), int(listing_id), int(time.time())),
    )


def _reviewed_alias_target(conn, kind, table, ih, eh):
    alias = conn.execute(
        'SELECT listing_id FROM wellknown_reviewed_aliases '
        'WHERE kind=? AND identity_hash=? AND endpoint_hash=?', (kind, ih, eh),
    ).fetchone()
    if alias is None:
        return None
    target = conn.execute(f'SELECT * FROM {table} WHERE id=?', (alias['listing_id'],)).fetchone()
    if target is None:
        return None  # reviewed target vanished: fall back to normal review
    db.assert_listing_not_retired(conn, kind, dict(target))
    conn.execute('UPDATE wellknown_reviewed_aliases SET last_seen=? '
                 'WHERE kind=? AND identity_hash=? AND endpoint_hash=?',
                 (int(time.time()), kind, ih, eh))
    return target


def upsert_listing(conn, kind, row):
    db.assert_listing_not_retired(conn, kind, row)
    validate_endpoint(row.get('endpoint_url'))
    table = {'mcp': 'mcp_servers', 'a2a': 'a2a_agents'}[kind]
    endpoint = db._exact_endpoint(row['endpoint_url'])
    sid = row.get('source_id')
    if not sid:
        raise db.MetadataValidationError('missing_upstream_identity')
    ih, eh = digest(sid), digest(endpoint)
    reviewed = _reviewed_alias_target(conn, kind, table, ih, eh)
    if reviewed is not None:
        return False, int(reviewed['id'])
    binding = conn.execute(
        'SELECT * FROM wellknown_endpoint_sources '
        'WHERE kind=? AND identity_hash=? AND endpoint_hash=?', (kind, ih, eh),
    ).fetchone()
    owners = db._source_identity_rows(conn, kind, 'wellknown', sid)
    target = None
    if binding:
        target = conn.execute(f'SELECT * FROM {table} WHERE id=?', (binding['listing_id'],)).fetchone()
        if target is None or db._exact_endpoint(target['endpoint_url']) != endpoint:
            from .source_lifecycle import stale_proof
            if stale_proof(conn, kind, binding):
                candidates = [candidate for candidate in conn.execute(
                    f'SELECT endpoint_url FROM {table} WHERE endpoint_url LIKE ? COLLATE NOCASE',
                    (db._endpoint_authority(endpoint) + '%',),
                ) if db._exact_endpoint(candidate['endpoint_url']) == endpoint]
                if not candidates:
                    raise StaleSourceBinding('wellknown_stale_endpoint')
                # A genuinely independent old endpoint still exists: use the
                # original exact-target reconciliation/ambiguity guards.
            target = _reconcile_existing_target(conn, kind, endpoint, binding, target, owners)
    if target is None:
        matching = [o for o in owners if db._exact_endpoint(o['endpoint_url']) == endpoint]
        if matching:
            # Keep the existing upstream-owned listing when present. This does
            # not merge duplicate rows or modify any endpoint/owner/slug.
            target = min(matching, key=lambda o: (o['source'] != 'wellknown', o['id']))
        else:
            if any(o['owner_verified'] or o['owner_edited'] for o in owners):
                raise SourceBindingConflict('wellknown_owner_endpoint_changed')
            target = db._find_exact_endpoint_row(conn, table, ('endpoint_url',), endpoint)
    created = False
    if target is None:
        # Do not let generic source/card matching silently mutate another
        # endpoint. Existing multi-endpoint entities require an explicit target.
        if owners:
            raise SourceBindingConflict('wellknown_new_endpoint_requires_review')
        if kind == 'a2a' and row.get('card_url'):
            existing_card = db._find_exact_endpoint_row(conn, table, ('card_url',), row['card_url'])
            if existing_card is not None:
                raise SourceBindingConflict('wellknown_synthetic_card_collision')
        created, lid = (db.upsert_mcp_server if kind == 'mcp' else db.upsert_a2a_agent)(conn, row)
        target = conn.execute(f'SELECT * FROM {table} WHERE id=?', (lid,)).fetchone()
    if target is None or db._exact_endpoint(target['endpoint_url']) != endpoint:
        raise SourceBindingConflict('wellknown_endpoint_mismatch')
    db.assert_listing_not_retired(conn, kind, dict(target))
    now = int(time.time())
    conn.execute(
        'INSERT INTO wellknown_endpoint_sources '
        '(kind,identity_hash,endpoint_hash,source_id,listing_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) '
        'ON CONFLICT(kind,identity_hash,endpoint_hash) DO UPDATE SET last_seen=excluded.last_seen',
        (kind, ih, eh, sid, target['id'], now, now),
    )
    # Preserve the old public provenance slot; update only if that slot already
    # names this entity. The supplemental binding represents all other aliases.
    conn.execute('UPDATE listing_sources SET last_seen=? WHERE kind=? AND listing_id=? '
                 'AND source="wellknown" AND source_id=?', (now, kind, target['id'], sid))
    return created, int(target['id'])