"""Owned x402 interface declarations, separate from observed payment/health."""

from decimal import Decimal, InvalidOperation
from contextlib import closing
from copy import deepcopy
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit
import uuid

import httpx

from . import public_http
from .url_identity import exact_endpoint

FIELDS = ('method', 'price_usdc', 'resources', 'well_known_url', 'openapi_url')
METHODS = ('GET', 'POST')
MAX_RESOURCES = 50
MAX_BYTES = 65536
BLOCKERS = {
    'document_unavailable': 'A selected description document did not return JSON directly from its declared URL.',
    'document_too_large': 'The description document or payment response exceeds the verification limit.',
    'resource_not_advertised': 'A declared resource or method is missing from the same-origin descriptor and OpenAPI.',
    'resource_price_unverified': 'A sibling resource price is not confirmed by structured same-origin metadata.',
    'post_probe_not_advertised': 'POST verification requires an explicitly advertised empty JSON probe or an empty-object OpenAPI request schema.',
    'payment_challenge_missing': 'The exact primary resource did not return a valid, resource-bound HTTP 402 challenge.',
    'price_mismatch': 'The primary price declaration does not match the observed USDC payment requirement.',
    'network_unavailable': 'Verification could not complete within the bounded public HTTP budget.',
    'unsafe_target': 'A resource or document failed the same-origin/public-address safety checks.',
    'declaration_invalid': 'The saved declaration is invalid for the current primary endpoint.',
}


class DeclarationError(ValueError):
    pass


class VerificationBlocked(ValueError):
    def __init__(self, code):
        super().__init__(BLOCKERS[code])
        self.code = code


def method(value):
    if not isinstance(value, str) or value.strip().upper() not in METHODS:
        raise DeclarationError('method must be GET or POST')
    return value.strip().upper()


def price(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DeclarationError('price_usdc must be a positive decimal or null')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise DeclarationError('price_usdc must be a positive decimal') from None
    if (not amount.is_finite() or not 0 < amount <= 1000000
            or amount * 1000000 != (amount * 1000000).to_integral_value()):
        raise DeclarationError('price_usdc must be positive, at most 1000000, and have at most six decimals')
    return float(amount)


def url(value, primary_url):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise DeclarationError('resource/document URL must be a bounded absolute URL')
    if any(char.isspace() for char in value) or any(char in value for char in '{}\\'):
        raise DeclarationError('resource/document URL cannot contain whitespace, templates or backslashes')
    parsed = urlsplit(value)
    primary = urlsplit(primary_url)
    try:
        host = public_http.normalize_hostname(parsed.hostname or '')
        original_host = public_http.normalize_hostname(primary.hostname or '')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        original_port = primary.port or (443 if primary.scheme == 'https' else 80)
    except (ValueError, public_http.UnsafeURL):
        raise DeclarationError('resource/document URL has an invalid or non-public hostname') from None
    if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme, host, port) != (primary.scheme, original_host, original_port)):
        raise DeclarationError('resources and document URLs must share the owned primary origin, without credentials or fragments')
    if any(re.search(r'(?i)^(?:api[_-]?key|access[_-]?token|token|password|secret|authorization|cookie)$', key)
           for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise DeclarationError('resource/document URL cannot contain credential parameters')
    return value


def resources(value, primary_url):
    if isinstance(value, str):
        if len(value.encode()) > MAX_BYTES:
            raise DeclarationError('resources exceeds the declaration size limit')
        try:
            value = json.loads(value or '[]')
        except (ValueError, RecursionError):
            raise DeclarationError('resources must be a JSON array') from None
    if not isinstance(value, list) or len(value) > MAX_RESOURCES:
        raise DeclarationError('resources must be an array of at most 50 entries')
    out, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {'url', 'method', 'description', 'price_usdc'}:
            raise DeclarationError('each resource accepts only url, method, description and price_usdc')
        resource = {'url': url(entry.get('url'), primary_url), 'method': method(entry.get('method'))}
        identity = (exact_endpoint(resource['url']), resource['method'])
        if identity in seen:
            raise DeclarationError('duplicate resource URL and method')
        seen.add(identity)
        if 'description' in entry:
            text = entry['description']
            if not isinstance(text, str) or len(text) > 600 or any(ord(char) < 32 for char in text):
                raise DeclarationError('resource description must be at most 600 printable characters')
            if re.search(r'\b(?:sk-|rcsk_|atc_live_|wk_live_|gh[pousr]_)[A-Za-z0-9_-]{12,}', text):
                raise DeclarationError('resource description cannot contain credentials')
            resource['description'] = text
        if 'price_usdc' in entry:
            resource['price_usdc'] = price(entry['price_usdc'])
        out.append(resource)
    if len(json.dumps(out).encode()) > MAX_BYTES:
        raise DeclarationError('resources exceeds the declaration size limit')
    return out


def stored(row):
    value = row.get('owner_declaration') if isinstance(row, dict) else row['owner_declaration']
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('fields'), dict):
        return None
    return value


def editable_fields(row):
    from . import db
    base = dict(row)
    fields = {name: base[name] for name in db._EDITABLE_FIELDS['x402'] if name not in FIELDS}
    current = stored(base)
    fields.update(current['fields'] if current else {})
    return {**fields, **{name: fields.get(name) for name in FIELDS}}


def prepare(row, changes):
    primary_url = row['url']
    previous = stored(row)
    fields = dict(previous['fields']) if previous and exact_endpoint(previous.get('primary_url')) == exact_endpoint(primary_url) else {}
    for key, value in changes.items():
        if key not in FIELDS:
            raise DeclarationError('unsupported declaration field')
        if key == 'method':
            if value is None or value == '':
                fields.pop(key, None)
            else:
                fields[key] = method(value)
        elif key == 'price_usdc':
            fields[key] = price(value)
        elif key == 'resources':
            fields[key] = resources(value, primary_url)
        else:
            fields[key] = None if value is None or value == '' else url(value, primary_url)
    primary_method = fields.get('method')
    primary_resources = [item for item in fields.get('resources') or []
                         if exact_endpoint(item['url']) == exact_endpoint(primary_url)]
    if fields.get('resources') and not primary_resources:
        raise DeclarationError('resources must include the current primary URL')
    if primary_method and primary_resources:
        matching = [item for item in primary_resources if item['method'] == primary_method]
        if not matching:
            raise DeclarationError('primary method is not present in resources')
        declared_price = matching[0].get('price_usdc')
        if fields.get('price_usdc') is not None and declared_price is not None and fields['price_usdc'] != declared_price:
            raise DeclarationError('primary price differs from the matching resource price')
    if previous and previous['fields'] == fields and previous.get('primary_url') == primary_url:
        return previous, []
    changed = [name for name in FIELDS if name in changes and
               (not previous or previous['fields'].get(name) != fields.get(name))]
    return {'version': 1, 'revision': uuid.uuid4().hex, 'primary_url': primary_url,
            'fields': fields, 'status': 'pending', 'saved_at': int(time.time())}, changed


def public_state(row):
    declaration = stored(dict(row))
    if not declaration:
        return None
    return {key: deepcopy(declaration[key]) for key in
            ('version', 'primary_url', 'fields', 'status', 'saved_at', 'checked_at', 'blocker', 'verification_scope')
            if key in declaration}


def has_verified_interface(row):
    row = dict(row)
    declaration = stored(row)
    info = row.get('call_info')
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except (ValueError, RecursionError):
            return False
    return bool(row.get('owner_verified') and declaration and isinstance(info, dict)
                and re.fullmatch(r'[a-f0-9]{32}', str(info.get('owner_declaration_revision') or ''))
                and exact_endpoint(str(info.get('primary_url') or '')) == exact_endpoint(str(row.get('url') or ''))
                and exact_endpoint(str(declaration.get('primary_url') or '')) == exact_endpoint(str(row.get('url') or '')))


def _response(client, target, primary_url, deadline, request_method='GET'):
    url(target, primary_url)
    if time.monotonic() >= deadline:
        raise VerificationBlocked('network_unavailable')
    options = {'json': {}} if request_method == 'POST' else {}
    with client.stream(request_method, target, **options) as response:
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 512 * 1024:
                raise VerificationBlocked('document_too_large')
            if time.monotonic() >= deadline:
                raise VerificationBlocked('network_unavailable')
        if exact_endpoint(str(response.url)) != exact_endpoint(target):
            raise VerificationBlocked('unsafe_target')
        return httpx.Response(response.status_code, headers=response.headers, content=bytes(raw), request=response.request)


def _document(client, target, primary_url, deadline, required):
    response = _response(client, target, primary_url, deadline)
    if response.status_code != 200:
        if required:
            raise VerificationBlocked('document_unavailable')
        return None
    try:
        document = response.json()
    except (ValueError, RecursionError):
        raise VerificationBlocked('document_unavailable') from None
    if not isinstance(document, dict):
        raise VerificationBlocked('document_unavailable')
    return document


def _schema(value, document):
    seen = set()
    for _ in range(5):
        if not isinstance(value, dict):
            return {}
        ref = value.get('$ref')
        if ref is None:
            return value
        if (not isinstance(ref, str) or not ref.startswith('#/') or ref in seen
                or set(value) - {'$ref', 'title', 'description', 'summary', '$comment'}):
            return {}
        seen.add(ref)
        value = document
        for part in ref[2:].split('/'):
            value = value.get(part.replace('~1', '/').replace('~0', '~')) if isinstance(value, dict) else None
    return {}


def _empty_probe(operation, document):
    if operation.get('x-probe-body') == {}:
        return True
    body = _schema(operation.get('requestBody') or {}, document)
    value = _schema((body.get('content') or {}).get('application/json', {}).get('schema') or {}, document)
    return bool(value.get('type') == 'object' and value.get('properties') == {}
                and not value.get('required') and not value.get('minProperties')
                and set(value) <= {'type', 'properties', 'required', 'minProperties',
                                  'maxProperties', 'additionalProperties', 'title',
                                  'description', 'default', 'examples', '$comment'})


def _advertised(descriptor, openapi, primary_url):
    from . import crawlers
    origin = primary_url.split('/', 3)[:3]
    origin = '/'.join(origin)
    resources_by_key = {}
    for entry in crawlers._advertised_entries(descriptor or {}):
        entry = {'url': entry} if isinstance(entry, str) else entry
        if not isinstance(entry, dict):
            continue
        target = crawlers._entry_url(entry, origin)
        try:
            url(target, primary_url)
        except DeclarationError:
            continue
        resources_by_key[exact_endpoint(target)] = entry
    operations = {}

    def server_bases(values):
        if values is None:
            return [origin]
        bases = []
        if not isinstance(values, list):
            return bases
        for server in values[:MAX_RESOURCES]:
            if isinstance(server, dict) and isinstance(server.get('url'), str):
                try:
                    target = server['url']
                    if target.startswith('/') and not target.startswith('//'):
                        target = origin + target
                    base = url(target, primary_url)
                    if urlsplit(base).query:
                        continue
                except DeclarationError:
                    continue
                bases.append(base.rstrip('/'))
        return bases

    for path, methods in ((openapi or {}).get('paths') or {}).items():
        if not isinstance(path, str) or not path.startswith('/') or not isinstance(methods, dict):
            continue
        for request_method in METHODS:
            operation = methods.get(request_method.lower())
            if not isinstance(operation, dict):
                continue
            servers = operation.get('servers', methods.get('servers', (openapi or {}).get('servers')))
            for base in server_bases(servers):
                target = base + path
                try:
                    url(target, primary_url)
                except DeclarationError:
                    continue
                operations.setdefault(exact_endpoint(target), {})[request_method.lower()] = operation
    return resources_by_key, operations


def observe(row):
    from . import cards, crawlers
    declaration = stored(row)
    if not declaration or exact_endpoint(declaration.get('primary_url')) != exact_endpoint(row['url']):
        raise DeclarationError('No declaration for the current primary endpoint')
    try:
        validated, _ = prepare(row, declaration['fields'])
        fields = validated['fields']
        primary_url = row['url']
        origin = primary_url.split('/', 3)[:3]
        origin = '/'.join(origin)
        descriptor_url = fields.get('well_known_url') or row.get('well_known_url') or origin + '/.well-known/x402'
        openapi_url = fields.get('openapi_url') or row.get('openapi_url') or origin + '/openapi.json'
        primary_method = fields.get('method') or cards.build_call_template(row).get('http_x402', {}).get('method') or 'GET'
        method(primary_method)
        deadline = time.monotonic() + 30
        with public_http.client(timeout=httpx.Timeout(connect=3, read=8, write=5, pool=3),
                                follow_redirects=False, headers={'Accept': 'application/json', 'User-Agent': crawlers.UA}) as client:
            descriptor = _document(client, descriptor_url, primary_url, deadline, bool(fields.get('well_known_url')))
            openapi = _document(client, openapi_url, primary_url, deadline, bool(fields.get('openapi_url')))
            advertised, operations = _advertised(descriptor, openapi, primary_url)
            primary_key = exact_endpoint(primary_url)
            entry = advertised.get(primary_key) or {}
            operation = (operations.get(primary_key) or {}).get(primary_method.lower()) or {}
            if primary_method == 'POST' and not (
                    str(entry.get('method') or '').upper() == 'POST' and entry.get('probe_body') == {}
                    or operation and _empty_probe(operation, openapi or {})):
                raise VerificationBlocked('post_probe_not_advertised')
            samples = fields.get('resources')
            if samples:
                samples = deepcopy(samples)
                for sample in samples:
                    key = exact_endpoint(sample['url'])
                    metadata = advertised.get(key) or {}
                    known_method = str(metadata.get('method') or 'GET').upper()
                    if not (key in advertised and sample['method'] == known_method
                            or sample['method'].lower() in (operations.get(key) or {})):
                        raise VerificationBlocked('resource_not_advertised')
                    if key != primary_key and sample.get('price_usdc') is not None:
                        known_price = crawlers._extract_payment(metadata)
                        if (not known_price or known_price.get('currency') != 'USDC'
                                or sample['price_usdc'] != known_price.get('max_amount_usdc')):
                            raise VerificationBlocked('resource_price_unverified')
            else:
                samples = []
                for key, metadata in list(advertised.items())[:MAX_RESOURCES]:
                    target = crawlers._entry_url(metadata, origin)
                    methods = operations.get(key) or {}
                    resource_method = str(metadata.get('method') or ('POST' if 'post' in methods else 'GET')).upper()
                    if resource_method not in METHODS:
                        continue
                    sample = {'url': target, 'method': resource_method}
                    if isinstance(metadata.get('description'), str):
                        sample['description'] = metadata['description'][:600]
                    samples.append(sample)
            started = time.monotonic()
            response = _response(client, primary_url, primary_url, deadline, primary_method)
            latency = int((time.monotonic() - started) * 1000)
            payment = crawlers._challenge_payment(response, primary_url) if response.status_code == 402 else None
            if not crawlers._valid_payment(payment):
                raise VerificationBlocked('payment_challenge_missing')
            expected_prices = [fields.get('price_usdc')] + [sample.get('price_usdc') for sample in samples
                if exact_endpoint(sample['url']) == primary_key and sample['method'] == primary_method]
            if any(value is not None and (payment.get('currency') != 'USDC'
                                         or str(payment.get('scheme') or '').lower() not in ('exact', 'exact-onchain', 'eip3009')
                                         or value != payment.get('max_amount_usdc')) for value in expected_prices):
                raise VerificationBlocked('price_mismatch')
            payment.pop('facilitator', None)
            primary = {'url': primary_url, 'method': primary_method, 'accepts': payment.get('accepts') or []}
            if primary_method == 'POST':
                primary['probe_body'] = {}
            samples = [primary] + [sample for sample in samples if not (
                exact_endpoint(sample['url']) == primary_key and sample['method'] == primary_method)]
            return {'status': 'verified', 'checked_at': int(time.time()), 'method': primary_method,
                    'payment': payment, 'latency_ms': latency, 'resource_samples': samples[:MAX_RESOURCES],
                    'resource_count': min(len(samples), MAX_RESOURCES),
                    'well_known_url': descriptor_url if descriptor is not None else None,
                    'openapi_url': openapi_url if openapi is not None else None}
    except VerificationBlocked as exc:
        code = exc.code
    except (DeclarationError, public_http.UnsafeURL):
        code = 'unsafe_target'
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, RecursionError):
        code = 'network_unavailable'
    return {'status': 'blocked', 'blocker': code, 'checked_at': int(time.time())}


def apply_validation(conn, listing_id, user_id, ownership_id, expected_declaration, result):
    from . import db
    current, _ = db.owner_edit_state(conn, 'x402', listing_id, user_id, ownership_id)
    declaration = stored(current)
    if declaration != expected_declaration or exact_endpoint(current['url']) != exact_endpoint(declaration.get('primary_url')):
        raise DeclarationError('Declaration changed during verification; please retry the current version')
    if not 0 <= time.time() - result['checked_at'] <= 60:
        raise DeclarationError('Verification result is no longer current')
    updated = deepcopy(declaration)
    updated.update(status=result['status'], checked_at=result['checked_at'])
    if result['status'] == 'blocked':
        updated['blocker'] = result['blocker']
    else:
        updated.pop('blocker', None)
        updated['verification_scope'] = 'Primary payment challenge verified; sibling resources are first-party metadata, not paid business tests.'
        payment = result['payment']
        old = db.row_to_dict(current)
        from . import cards
        old_method = cards.build_call_template(old).get('http_x402', {}).get('method')
        old_payment = old.get('payment') if isinstance(old.get('payment'), dict) else {}
        changed_terms = old_method != result['method'] or any(
            old_payment.get(key) != payment.get(key) for key in ('amount_raw', 'network', 'asset', 'pay_to'))
        if changed_terms:
            reset = ('quality', 'quality_score', 'tx_30d', 'payto_tx_30d', 'payto_payers_30d', 'payto_checked', 'conformance')
            cols = {row[1] for row in conn.execute('PRAGMA table_info(services)')}
            reset = [name for name in reset if name in cols]
            if reset:
                conn.execute('UPDATE services SET ' + ','.join(name + '=NULL' for name in reset) + ' WHERE id=?', (listing_id,))
            conn.execute('DELETE FROM service_paytos WHERE service_id=?', (listing_id,))
        call_info = {'primary_url': current['url'], 'method': result['method'],
                     'owner_declaration_revision': declaration['revision'],
                     'verification_scope': updated['verification_scope']}
        values = {'health': 'ok', 'health_checked': result['checked_at'], 'http_status': 402, 'x402_ok': 1,
                  'latency_ms': result['latency_ms'], 'payment': payment, 'currency': payment.get('currency'),
                  'price_min': payment.get('max_amount_usdc'), 'price_max': payment.get('max_amount_usdc'),
                  'chains': payment.get('networks') or [payment['network']], 'call_info': call_info,
                  'well_known_url': result['well_known_url'], 'openapi_url': result['openapi_url'],
                  'resource_samples': result['resource_samples'], 'resource_count': result['resource_count']}
        for sample in result['resource_samples']:
            db.assert_listing_not_retired(conn, 'x402', {'url': sample['url']})
        if db.find_privacy_suppression(conn, 'x402', {'owner_declaration': json.dumps(updated)}) is not None:
            raise DeclarationError('Declaration references operator-removed data')
        conn.execute('UPDATE services SET ' + ','.join(name + '=?' for name in values) + ' WHERE id=?',
                     [db._to_json(value) if isinstance(value, (dict, list)) else value for value in values.values()] + [listing_id])
        conn.execute('INSERT INTO health_history(service_id,checked_at,status,latency_ms,http_status,x402) VALUES(?,?,?,?,?,?)',
                     (listing_id, result['checked_at'], 'ok', result['latency_ms'], 402, 1))
    conn.execute('UPDATE services SET owner_declaration=? WHERE id=?', (json.dumps(updated, ensure_ascii=False), listing_id))
    return {'status': updated['status'], 'blocker': BLOCKERS.get(updated.get('blocker')),
            'declaration': public_state({'owner_declaration': updated})}


def verify_listing(listing_id, user_id, ownership_id):
    from . import db
    with closing(db.connect(read_only=True)) as conn:
        current, _ = db.owner_edit_state(conn, 'x402', listing_id, user_id, ownership_id)
        row = db.row_to_dict(current)
        declaration = stored(row)
        if not declaration:
            raise DeclarationError('Save a declaration before verification')
    result = observe(row)
    def write():
        with db.writer(immediate=True) as conn:
            return apply_validation(conn, listing_id, user_id, ownership_id, declaration, result)
    return db.with_retry(write)