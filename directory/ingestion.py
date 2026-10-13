"""Classified, transactional ingestion. No raw payloads or credentials in audit rows."""
from copy import deepcopy
import hashlib
import json
import sqlite3
import time
from urllib.parse import urlsplit

import idna

from . import db


def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def identity(row):
    return digest(row.get('source_id') or row.get('slug') or row.get('endpoint_url') or row.get('url'))


def content_digest(row):
    return digest(json.dumps(row, sort_keys=True, default=str, ensure_ascii=False))


def record_issue(conn, kind, source, row, disposition, reason):
    # Suppression/retirement gate MUST precede this call. Keep only opaque hashes;
    # do not introduce a new copy of potentially private upstream content.
    now = int(time.time())
    raw_endpoint = row.get('url') if kind == 'x402' else row.get('endpoint_url')
    endpoint = db._exact_endpoint(raw_endpoint) if isinstance(raw_endpoint, str) else ''
    endpoint_hash = digest(endpoint) if endpoint else None
    conn.execute(
        'INSERT INTO ingestion_issues '
        '(kind,source,identity_hash,content_hash,endpoint_hash,disposition,reason,first_seen,last_seen) '
        'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,source,identity_hash,content_hash,reason) '
        'DO UPDATE SET last_seen=excluded.last_seen,occurrences=occurrences+1,resolved_at=NULL,'
        'endpoint_hash=COALESCE(excluded.endpoint_hash,ingestion_issues.endpoint_hash)',
        (kind, source, identity(row), content_digest(row), endpoint_hash, disposition, reason, now, now),
    )


def resolve_issue(conn, kind, source, row):
    if source == 'wellknown' and kind in ('mcp', 'a2a'):
        # One entity can advertise several endpoints. Its successful endpoint
        # must never resolve a sibling's hold. Legacy audits have no endpoint
        # hash: resolve only an exact, freshly re-observed content version.
        endpoint_hash = digest(db._exact_endpoint(row.get('endpoint_url') or ''))
        conn.execute('UPDATE ingestion_issues SET resolved_at=?,endpoint_hash=? '
                     'WHERE kind=? AND source=? AND identity_hash=? AND resolved_at IS NULL '
                     'AND (endpoint_hash=? OR (endpoint_hash IS NULL AND content_hash=?))',
                     (int(time.time()), endpoint_hash, kind, source, identity(row),
                      endpoint_hash, content_digest(row)))
        return
    conn.execute('UPDATE ingestion_issues SET resolved_at=? '
                 'WHERE kind=? AND source=? AND identity_hash=? AND resolved_at IS NULL',
                 (int(time.time()), kind, source, identity(row)))


def validate_endpoint(value):
    if not isinstance(value, str) or not value.strip():
        raise db.MetadataValidationError('missing_endpoint')
    if any(ch in value for ch in '{}'):
        raise db.MetadataValidationError('needs_configuration:url_template')
    if any(ch.isspace() for ch in value):
        raise db.MetadataValidationError('endpoint_contains_whitespace')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise db.MetadataValidationError('requires_absolute_http_endpoint')
        if parsed.username or parsed.password or parsed.fragment:
            raise db.MetadataValidationError('endpoint_credentials_or_fragment')
        if any(len(label.encode('utf-8')) > 63 for label in parsed.hostname.split('.')):
            raise db.MetadataValidationError('hostname_label_too_long')
        idna.encode(parsed.hostname.rstrip('.'), uts46=True)
        parsed.port
    except db.MetadataValidationError:
        raise
    except (ValueError, UnicodeError, idna.IDNAError):
        raise db.MetadataValidationError('invalid_http_authority') from None
    if not db._exact_endpoint(value):
        raise db.MetadataValidationError('invalid_endpoint')


def ingest_batch(conn, kind, source, rows, upsert):
    from .wellknown_binding import SourceBindingConflict
    result = dict(added=0, updated=0, rejected=0, errors=[])
    if not conn.in_transaction:
        conn.execute('BEGIN')
    for original in rows:
        row = deepcopy(original)  # upsert mutates; retries must start from pristine input
        conn.execute('SAVEPOINT ingest_row')
        try:
            db.assert_listing_not_retired(conn, kind, row)
            # A producer must explicitly distinguish install-only MCP metadata
            # from a callable endpoint. Empty strings and malformed non-null
            # URLs never receive the catalog exception.
            catalog = (kind == 'mcp' and row.get('kind') == 'catalog'
                       and row.get('endpoint_url') is None)
            if not catalog:
                validate_endpoint(row.get('url') if kind == 'x402' else row.get('endpoint_url'))
            created, _ = upsert(conn, row)
            resolve_issue(conn, kind, source, original)
            result['added' if created else 'updated'] += 1
        except db.RetiredListingError:
            conn.execute('ROLLBACK TO ingest_row')
            result['rejected'] += 1
        except db.MetadataValidationError as exc:
            conn.execute('ROLLBACK TO ingest_row')
            reason = str(exc)
            record_issue(conn, kind, source, original, 'rejected', reason)
            result['rejected'] += 1
        except SourceBindingConflict as exc:
            conn.execute('ROLLBACK TO ingest_row')
            record_issue(conn, kind, source, original, 'hold', str(exc))
            result['errors'].append('source binding held: ' + str(exc))
        except Exception:
            conn.execute('ROLLBACK TO ingest_row')
            raise  # lock retries / disk errors / programmer errors must not become bad metadata
        finally:
            conn.execute('RELEASE ingest_row')
    return result