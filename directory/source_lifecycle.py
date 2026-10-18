"""Hash-only receipts for accepted endpoint updates and stale source aliases.

Private receipt objects are created by trusted fetchers, not upstream JSON.
Stale relationships retain their original identity, endpoint hash and target.
"""

from dataclasses import dataclass
import hashlib
import json
import time


def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def payload_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class EndpointObservation:
    kind: str
    source: str
    identity_hash: str
    endpoint_hash: str
    reference_hash: str
    content_hash: str
    observed_at: int
    evidence_kind: str


class ObservedAgentCard(dict):
    def __init__(self, card, requested_url, final_url, endpoint):
        super().__init__(card)
        self.reference_hash = payload_digest([requested_url, final_url])
        self.content_hash = payload_digest(card)
        self.endpoint_hash = digest(endpoint)
        self.observed_at = int(time.time())


def card_observation(card, source, source_id):
    if not isinstance(card, ObservedAgentCard) or source not in ('agenstry', 'a2aregistry'):
        return None
    return EndpointObservation('a2a', source, digest(source_id), card.endpoint_hash,
                               card.reference_hash, card.content_hash,
                               card.observed_at, 'live_agent_card')


def registry_observation(source_id, endpoint, server, official, reference):
    if official.get('isLatest') is not True or official.get('status') != 'active':
        return None
    from . import db
    from .ingestion import validate_endpoint
    validate_endpoint(endpoint)
    return EndpointObservation('mcp', 'mcp-registry', digest(source_id),
                               digest(db._exact_endpoint(endpoint)), digest(reference),
                               payload_digest([server, official]), int(time.time()),
                               'current_mcp_registry')


def table(kind):
    return {'mcp': 'mcp_servers', 'a2a': 'a2a_agents'}[kind]


def accepted_receipt(kind, existing, row):
    from . import db
    observation = row.get('_endpoint_observation')
    evidence_kind = {('mcp', 'mcp-registry'): 'current_mcp_registry',
                     ('a2a', 'agenstry'): 'live_agent_card',
                     ('a2a', 'a2aregistry'): 'live_agent_card'}.get((kind, row.get('source')))
    if (type(observation) is not EndpointObservation
            or observation.kind != kind
            or not evidence_kind or observation.evidence_kind != evidence_kind
            or existing['source'] != row.get('source')
            or row.get('source') != observation.source
            or existing['source_id'] != row.get('source_id')
            or digest(existing['source_id']) != observation.identity_hash
            or existing['owner_verified'] or existing['owner_edited']
            or row.get('_snapshot_fallback')
            or observation.observed_at > time.time() + 60
            or time.time() - observation.observed_at > 3600
            or digest(db._exact_endpoint(row.get('endpoint_url') or '')) != observation.endpoint_hash):
        return None
    return observation


def change_type(old_url, new_url):
    from urllib.parse import urlsplit
    old, new = urlsplit(old_url), urlsplit(new_url)
    changes = []
    if (old.scheme, old.netloc) != (new.scheme, new.netloc):
        changes.append('authority')
    if old.path != new.path:
        changes.append('path')
    if old.query != new.query:
        changes.append('query')
    return '+'.join(changes) or 'normalized_endpoint'


def add_transition(conn, kind, listing_id, observation, old_hash, new_hash, change,
                   previous_id=None, basis_hash=None):
    cursor = conn.execute(
        'INSERT INTO listing_endpoint_transitions '
        '(kind,listing_id,source,identity_hash,old_endpoint_hash,new_endpoint_hash,'
        'reference_hash,content_hash,evidence_kind,observed_at,accepted_at,'
        'previous_transition_id,change_type,basis_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (kind, listing_id, observation.source, observation.identity_hash, old_hash, new_hash,
         observation.reference_hash, observation.content_hash, observation.evidence_kind,
         observation.observed_at, int(time.time()), previous_id, change, basis_hash))
    return cursor.lastrowid


def record_warning(conn, kind, binding, current_hash, transition_id, change):
    now = int(time.time())
    key = payload_digest([kind, binding['identity_hash'], binding['endpoint_hash'], current_hash])
    fresh = conn.execute('SELECT 1 FROM source_endpoint_warnings WHERE warning_key=?', (key,)).fetchone() is None
    conn.execute(
        'INSERT INTO source_endpoint_warnings '
        '(warning_key,kind,listing_id,identity_hash,old_endpoint_hash,current_endpoint_hash,'
        'transition_id,change_type,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(warning_key) DO UPDATE SET last_seen=excluded.last_seen,'
        'occurrences=occurrences+1,resolved_at=NULL',
        (key, kind, binding['listing_id'], binding['identity_hash'], binding['endpoint_hash'],
         current_hash, transition_id, change, now, now))
    conn.execute(
        "UPDATE ingestion_issues SET resolved_at=? WHERE kind=? AND source='wellknown' "
        "AND identity_hash=? AND endpoint_hash=? AND disposition='hold' "
        "AND reason='wellknown_binding_target_changed' AND resolved_at IS NULL",
        (now, kind, binding['identity_hash'], binding['endpoint_hash']))
    return int(fresh)


def stale_proof(conn, kind, binding, expected_current=None):
    from . import db
    if binding['state'] != 'stale':
        return False
    target = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?', (binding['listing_id'],)).fetchone()
    if target is None or target['owner_verified'] or target['owner_edited']:
        return False
    current = expected_current or digest(db._exact_endpoint(target['endpoint_url'] or ''))
    if binding['stale_target_hash'] != current:
        return False
    accepted = conn.execute('SELECT * FROM listing_endpoint_observations WHERE kind=? AND listing_id=?',
                            (kind, binding['listing_id'])).fetchone()
    if (accepted is None or accepted['source'] != target['source']
            or accepted['identity_hash'] != digest(target['source_id'])
            or accepted['endpoint_hash'] != current):
        return False
    event_id, wanted, seen = binding['stale_transition_id'], current, set()
    while event_id and event_id not in seen:
        seen.add(event_id)
        event = conn.execute('SELECT * FROM listing_endpoint_transitions WHERE id=?', (event_id,)).fetchone()
        if (event is None or event['kind'] != kind or event['listing_id'] != binding['listing_id']
                or event['identity_hash'] != accepted['identity_hash']
                or event['source'] != accepted['source'] or event['new_endpoint_hash'] != wanted):
            return False
        if event['old_endpoint_hash'] == binding['endpoint_hash']:
            return True
        wanted, event_id = event['old_endpoint_hash'], event['previous_transition_id']
    return False


def mark_transition_bindings(conn, kind, listing_id, old_hash, new_hash, event_id, change):
    now, fresh = int(time.time()), 0
    for binding in conn.execute('SELECT * FROM wellknown_endpoint_sources WHERE kind=? AND listing_id=?',
                                (kind, listing_id)).fetchall():
        from . import db
        try:
            db.assert_listing_not_retired(conn, kind, {'source': 'wellknown', 'source_id': binding['source_id']})
        except db.RetiredListingError:
            # Never introduce a new audit/warning for a suppressed alias.
            continue
        owners = db._source_identity_rows(conn, kind, 'wellknown', binding['source_id'])
        if (any(owner['owner_verified'] or owner['owner_edited'] for owner in owners)
                or any(owner['id'] != listing_id
                       and digest(db._exact_endpoint(owner['endpoint_url'] or '')) == binding['endpoint_hash']
                       for owner in owners)):
            continue  # Real endpoint ownership ambiguity remains a hold.
        if binding['endpoint_hash'] == new_hash:
            if binding['state'] == 'stale' and stale_proof(conn, kind, binding, old_hash):
                conn.execute("UPDATE wellknown_endpoint_sources SET state='active',stale_target_hash=NULL,"
                             'stale_at=NULL,state_changed_at=?,stale_transition_id=NULL '
                             'WHERE kind=? AND identity_hash=? AND endpoint_hash=?',
                             (now, kind, binding['identity_hash'], binding['endpoint_hash']))
                conn.execute('UPDATE source_endpoint_warnings SET resolved_at=? WHERE kind=? '
                             'AND listing_id=? AND identity_hash=? AND old_endpoint_hash=? '
                             'AND resolved_at IS NULL',
                             (now, kind, listing_id, binding['identity_hash'], binding['endpoint_hash']))
            continue
        relevant = binding['state'] == 'active' and binding['endpoint_hash'] == old_hash
        chained = binding['state'] == 'stale' and stale_proof(conn, kind, binding, old_hash)
        if not (relevant or chained):
            continue
        scoped_event_id = event_id
        if chained:
            # Historical aliases may have different accepted preimage chains.
            # Preserve each chain rather than sharing one listing-wide parent.
            event = conn.execute('SELECT * FROM listing_endpoint_transitions WHERE id=?',
                                 (event_id,)).fetchone()
            if event['previous_transition_id'] != binding['stale_transition_id']:
                observation = EndpointObservation(kind, event['source'], event['identity_hash'],
                                                  new_hash, event['reference_hash'], event['content_hash'],
                                                  event['observed_at'], event['evidence_kind'])
                scoped_event_id = add_transition(conn, kind, listing_id, observation, old_hash,
                                                 new_hash, change, binding['stale_transition_id'])
        conn.execute("UPDATE wellknown_endpoint_sources SET state='stale',stale_target_hash=?,"
                     'stale_at=COALESCE(stale_at,?),state_changed_at=?,stale_transition_id=? '
                     'WHERE kind=? AND identity_hash=? AND endpoint_hash=? AND listing_id=?',
                     (new_hash, now, now, scoped_event_id, kind, binding['identity_hash'],
                      binding['endpoint_hash'], listing_id))
        conn.execute('UPDATE source_endpoint_warnings SET resolved_at=? WHERE kind=? '
                     'AND listing_id=? AND identity_hash=? AND old_endpoint_hash=? '
                     'AND current_endpoint_hash<>? AND resolved_at IS NULL',
                     (now, kind, listing_id, binding['identity_hash'], binding['endpoint_hash'], new_hash))
        fresh += record_warning(conn, kind, binding, new_hash, scoped_event_id, change)
    return fresh


def record_primary_observation(conn, kind, existing, row):
    """Run only at the actual same-primary-identity UPDATE inside its transaction."""
    from . import db
    observation = accepted_receipt(kind, existing, row)
    if observation is None:
        return 0
    db.assert_listing_not_retired(conn, kind, dict(existing))
    old_url, new_url = db._exact_endpoint(existing['endpoint_url'] or ''), db._exact_endpoint(row['endpoint_url'])
    old_hash, new_hash = digest(old_url), digest(new_url)
    prior = conn.execute('SELECT * FROM listing_endpoint_observations WHERE kind=? AND listing_id=?',
                         (kind, existing['id'])).fetchone()
    event_id = (prior['last_transition_id'] if prior and prior['endpoint_hash'] == old_hash
                and prior['identity_hash'] == observation.identity_hash else None)
    fresh = 0
    if old_url and old_hash != new_hash:
        change = change_type(old_url, new_url)
        event_id = add_transition(conn, kind, existing['id'], observation, old_hash, new_hash, change, event_id)
        fresh = mark_transition_bindings(conn, kind, existing['id'], old_hash, new_hash, event_id, change)
    conn.execute(
        'INSERT INTO listing_endpoint_observations '
        '(kind,listing_id,source,identity_hash,endpoint_hash,reference_hash,content_hash,'
        'evidence_kind,observed_at,accepted_at,last_transition_id) VALUES(?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(kind,listing_id) DO UPDATE SET source=excluded.source,'
        'identity_hash=excluded.identity_hash,endpoint_hash=excluded.endpoint_hash,'
        'reference_hash=excluded.reference_hash,content_hash=excluded.content_hash,'
        'evidence_kind=excluded.evidence_kind,observed_at=excluded.observed_at,'
        'accepted_at=excluded.accepted_at,last_transition_id=excluded.last_transition_id',
        (kind, existing['id'], observation.source, observation.identity_hash, new_hash,
         observation.reference_hash, observation.content_hash, observation.evidence_kind,
         observation.observed_at, int(time.time()), event_id))
    return fresh


def accept_current_observation(conn, kind, listing_id, observation):
    """Operator readback: fresh canonical evidence must equal the unchanged row."""
    from . import db
    target = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?', (listing_id,)).fetchone()
    if target is None:
        raise ValueError('Current observation target disappeared')
    row = dict(target, _endpoint_observation=observation)
    if accepted_receipt(kind, target, row) is None:
        raise ValueError('Fresh observation does not identify the current primary endpoint')
    db.assert_listing_not_retired(conn, kind, dict(target))
    return record_primary_observation(conn, kind, target, row)


def record_stale_observation(conn, kind, row):
    from . import db
    from .ingestion import validate_endpoint
    from .wellknown_binding import SourceBindingConflict
    db.assert_listing_not_retired(conn, kind, row)
    validate_endpoint(row.get('endpoint_url'))
    binding = conn.execute('SELECT * FROM wellknown_endpoint_sources WHERE kind=? '
                            'AND identity_hash=? AND endpoint_hash=?',
                            (kind, digest(row.get('source_id')), digest(db._exact_endpoint(row['endpoint_url'])))).fetchone()
    if binding is None or not stale_proof(conn, kind, binding):
        raise SourceBindingConflict('wellknown_binding_target_changed')
    target = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?', (binding['listing_id'],)).fetchone()
    db.assert_listing_not_retired(conn, kind, dict(target))
    if any(owner['owner_verified'] or owner['owner_edited']
           for owner in db._source_identity_rows(conn, kind, 'wellknown', row.get('source_id'))):
        raise SourceBindingConflict('wellknown_owner_endpoint_changed')
    if any(owner['id'] != target['id']
           and digest(db._exact_endpoint(owner['endpoint_url'] or '')) == binding['endpoint_hash']
           for owner in db._source_identity_rows(conn, kind, 'wellknown', row.get('source_id'))):
        raise SourceBindingConflict('wellknown_binding_target_changed')
    conn.execute('UPDATE wellknown_endpoint_sources SET last_seen=? WHERE kind=? '
                 'AND identity_hash=? AND endpoint_hash=?',
                 (int(time.time()), kind, binding['identity_hash'], binding['endpoint_hash']))
    event = conn.execute('SELECT change_type FROM listing_endpoint_transitions WHERE id=?',
                         (binding['stale_transition_id'],)).fetchone()
    return record_warning(conn, kind, binding, binding['stale_target_hash'], binding['stale_transition_id'], event[0])


def bootstrap_prior_binding(conn, kind, prior, identity_hash, endpoint_hash):
    """Operator reconciliation: known snapshot preimage plus fresh accepted receipt."""
    from . import db
    from .ingestion import validate_endpoint
    target = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?', (prior['id'],)).fetchone()
    accepted = conn.execute('SELECT * FROM listing_endpoint_observations WHERE kind=? AND listing_id=?',
                            (kind, prior['id'])).fetchone()
    validate_endpoint(prior['endpoint_url'])
    if (target is None or accepted is None or prior['source'] != target['source']
            or prior['source_id'] != target['source_id'] or prior['created_at'] != target['created_at']
            or prior.get('owner_verified') or prior.get('owner_edited')
            or target['owner_verified'] or target['owner_edited']
            or accepted['identity_hash'] != digest(prior['source_id'])
            or accepted['endpoint_hash'] != digest(db._exact_endpoint(target['endpoint_url']))
            or accepted['source'] not in ('agenstry', 'a2aregistry', 'mcp-registry')
            or accepted['observed_at'] > time.time() + 60
            or time.time() - accepted['observed_at'] > 3600
            or digest(db._exact_endpoint(prior['endpoint_url'])) != endpoint_hash):
        raise ValueError('Historical binding lacks matching fresh primary-source evidence')
    binding = conn.execute('SELECT * FROM wellknown_endpoint_sources WHERE kind=? '
                            'AND listing_id=? AND identity_hash=? AND endpoint_hash=?',
                            (kind, prior['id'], identity_hash, endpoint_hash)).fetchone()
    if binding is None or accepted['endpoint_hash'] == endpoint_hash:
        return 0
    db.assert_listing_not_retired(conn, kind, dict(prior))
    db.assert_listing_not_retired(conn, kind, dict(target))
    db.assert_listing_not_retired(conn, kind, {'source': 'wellknown', 'source_id': binding['source_id']})
    if any(owner['owner_verified'] or owner['owner_edited']
           for owner in db._source_identity_rows(conn, kind, 'wellknown', binding['source_id'])):
        raise ValueError('Owned historical source identity remains held')
    if any(owner['id'] != target['id']
           and digest(db._exact_endpoint(owner['endpoint_url'] or '')) == endpoint_hash
           for owner in db._source_identity_rows(conn, kind, 'wellknown', binding['source_id'])):
        raise ValueError('Historical old endpoint still has a different identity owner')
    if binding['state'] == 'stale' and stale_proof(conn, kind, binding):
        return 0
    observation = EndpointObservation(kind, accepted['source'], accepted['identity_hash'],
                                      accepted['endpoint_hash'], accepted['reference_hash'],
                                      accepted['content_hash'], accepted['observed_at'], accepted['evidence_kind'])
    change = change_type(db._exact_endpoint(prior['endpoint_url']), db._exact_endpoint(target['endpoint_url']))
    basis = payload_digest([kind, prior['id'], prior['created_at'], digest(prior['source_id']), endpoint_hash])
    event_id = add_transition(conn, kind, prior['id'], observation, endpoint_hash,
                              accepted['endpoint_hash'], change, basis_hash=basis)
    now = int(time.time())
    conn.execute("UPDATE wellknown_endpoint_sources SET state='stale',stale_target_hash=?,"
                 'stale_at=COALESCE(stale_at,?),state_changed_at=?,stale_transition_id=? '
                 'WHERE kind=? AND listing_id=? AND identity_hash=? AND endpoint_hash=?',
                 (accepted['endpoint_hash'], now, now, event_id, kind, prior['id'], identity_hash, endpoint_hash))
    conn.execute('UPDATE listing_endpoint_observations SET last_transition_id=? WHERE kind=? AND listing_id=?',
                 (event_id, kind, prior['id']))
    return record_warning(conn, kind, binding, accepted['endpoint_hash'], event_id, change)


def visible_warning_rows(conn):
    from . import db
    result = []
    for warning in conn.execute('SELECT * FROM source_endpoint_warnings ORDER BY first_seen,warning_key'):
        kind = warning['kind']
        target = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?', (warning['listing_id'],)).fetchone()
        binding = conn.execute('SELECT source_id,listing_id FROM wellknown_endpoint_sources WHERE kind=? '
                               'AND identity_hash=? AND endpoint_hash=?',
                               (kind, warning['identity_hash'], warning['old_endpoint_hash'])).fetchone()
        if target is None or binding is None:
            continue
        try:
            db.assert_listing_not_retired(conn, kind, dict(target))
            if binding['listing_id'] != warning['listing_id']:
                rebound = conn.execute(f'SELECT * FROM {table(kind)} WHERE id=?',
                                       (binding['listing_id'],)).fetchone()
                if rebound is None:
                    continue
                db.assert_listing_not_retired(conn, kind, dict(rebound))
            db.assert_listing_not_retired(conn, kind, {'source': 'wellknown', 'source_id': binding['source_id']})
        except db.RetiredListingError:
            continue
        item = dict(warning)
        item['current_context'] = digest(db._exact_endpoint(target['endpoint_url'] or '')) == warning['current_endpoint_hash']
        # Names may contain untrusted URLs/credentials: expose only the listing
        # ID and stable internal detail route, never upstream query metadata.
        item['detail_path'] = ('/mcp/servers/' if kind == 'mcp' else '/a2a/agents/') + target['slug']
        result.append(item)
    return result