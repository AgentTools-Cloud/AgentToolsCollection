"""Owner declaration validation and tiny-database persistence regressions."""

from contextlib import closing
import json
import os
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import auth, db, jobs, owner_declarations as declarations, routes
from directory import reverify_x402
from test_ingestion_pipeline import TemporaryDatabaseTestCase


class OwnerDeclarationTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            self.uid = conn.execute("INSERT INTO users(provider,provider_uid,login,status,created_at) "
                                    "VALUES('github','owner-fixture','owner-fixture','active',1)").lastrowid
            self.claim = conn.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,status,created_at,verified_at) "
                                      "VALUES(?,'owned.example','descriptor','fixture','verified',1,1)", (self.uid,)).lastrowid
            _, self.lid = db.upsert_service(conn, dict(slug='owned-fixture', name='Owned fixture',
                url='https://owned.example/v1/ping', source='fixture', source_id='owned-fixture',
                health='ok', health_checked=1, price_min=0.001, price_max=0.001, currency='USDC'))
            conn.execute("UPDATE services SET owner_verified=1,x402_ok=1,http_status=402,quality_score=90,tx_30d=7 WHERE id=?", (self.lid,))

    def edit(self, changes):
        with db.writer(immediate=True) as conn:
            return db.apply_listing_edits(conn, 'x402', self.lid, self.uid, self.claim, changes)

    def row(self):
        with closing(db.connect(read_only=True)) as conn:
            return db.row_to_dict(conn.execute('SELECT * FROM services WHERE id=?', (self.lid,)).fetchone())

    def test_method_resources_price_and_docs_saved_as_pending_not_measured_values(self):
        changes = dict(method='POST', price_usdc=0.002,
                       resources=[dict(url='https://owned.example/v1/ping', method='POST', price_usdc=0.002)],
                       well_known_url='https://owned.example/.well-known/x402', openapi_url='https://owned.example/openapi.json')
        applied, rejected = self.edit(changes)
        self.assertFalse(rejected)
        self.assertEqual(set(applied), set(changes))
        row = self.row()
        self.assertEqual(row['owner_declaration']['status'], 'pending')
        self.assertEqual(row['owner_declaration']['fields'], changes)
        self.assertEqual((row['health'], row['x402_ok'], row['quality_score'], row['tx_30d'], row['price_min']),
                         ('ok', 1, 90, 7, 0.001))
        self.assertNotIn('method', json.loads(row['owner_edited'] or '[]'))

    def test_crawler_cannot_erase_owner_declaration(self):
        self.edit({'method': 'POST'})
        before = self.row()['owner_declaration']
        with db.writer() as conn:
            db.upsert_service(conn, dict(slug='owned-fixture', name='Crawler', url='https://owned.example/v1/ping',
                                        source='fixture', source_id='owned-fixture'))
        self.assertEqual(self.row()['owner_declaration'], before)

    def test_normal_text_edit_still_works_and_is_pinned(self):
        applied, rejected = self.edit({'description': 'Owner supplied description'})
        self.assertEqual(applied, ['description'])
        self.assertFalse(rejected)
        self.assertIn('description', json.loads(self.row()['owner_edited']))

    def test_invalid_resources_do_not_partially_apply_normal_fields(self):
        applied, rejected = self.edit({'description': 'must not apply', 'resources': [{'url': 'https://foreign.example/a', 'method': 'GET'}]})
        self.assertFalse(applied)
        self.assertTrue(rejected)
        self.assertNotEqual(self.row()['description'], 'must not apply')

    def test_credentials_templates_and_private_hostnames_are_rejected(self):
        for target in ('https://user:secret@owned.example/ping', 'https://owned.example/ping?api_key=secret',
                       'https://127.0.0.1/ping', 'https://host.internal/ping', 'https://owned.example/{tenant}'):
            with self.subTest(target=target), self.assertRaises(declarations.DeclarationError):
                declarations.url(target, 'https://owned.example/ping')

    def test_resource_method_identity_and_bounds(self):
        primary = 'https://owned.example/v1/ping'
        pairs = [dict(url=primary, method='GET'), dict(url=primary, method='POST')]
        self.assertEqual(len(declarations.resources(pairs, primary)), 2)
        for values in (pairs + [pairs[0]], [dict(url=primary, method='DELETE')], pairs * 26,
                       [dict(url=primary, method='GET', health='ok')]):
            with self.subTest(values=values), self.assertRaises(declarations.DeclarationError):
                declarations.resources(values, primary)

    def test_price_must_be_real_bounded_usdc_decimal(self):
        for value in (True, 'NaN', 'Infinity', -1, 0, '0.0000001', '1e-1000000', 1000001, {}):
            with self.subTest(value=value), self.assertRaises(declarations.DeclarationError):
                declarations.price(value)
        self.assertEqual(declarations.price('0.000001'), 0.000001)

    def test_same_declaration_does_not_create_a_new_revision(self):
        self.edit({'method': 'POST'})
        revision = self.row()['owner_declaration']['revision']
        applied, rejected = self.edit({'method': 'POST'})
        self.assertFalse(applied)
        self.assertFalse(rejected)
        self.assertEqual(self.row()['owner_declaration']['revision'], revision)

    def test_disabled_account_or_revoked_claim_cannot_edit(self):
        with db.writer() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.uid,))
        with self.assertRaises(PermissionError):
            self.edit({'method': 'POST'})
        with db.writer() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (self.uid,))
            conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
        with self.assertRaises(PermissionError):
            self.edit({'method': 'POST'})

    def test_changed_primary_clears_old_declaration(self):
        self.edit({'method': 'POST'})
        with db.writer() as conn:
            conn.execute("UPDATE services SET openapi_url='https://owned.example/old-api.json',"
                         "quality=?,confidence=0.99,currency='USDT',conformance=? WHERE id=?",
                         (json.dumps({'tx_30d': 900}), json.dumps({'ok': True}), self.lid))
        self.edit({'url': 'https://owned.example/v2/ping'})
        row = self.row()
        self.assertIsNone(row['owner_declaration'])
        self.assertIsNone(row['health'])
        for field in ('openapi_url', 'quality', 'confidence', 'currency', 'conformance'):
            self.assertIsNone(row[field], field)

    def test_primary_resource_and_price_consistency(self):
        applied, rejected = self.edit(dict(method='POST', price_usdc=0.003,
            resources=[dict(url='https://owned.example/v1/ping', method='GET', price_usdc=0.001)]))
        self.assertFalse(applied)
        self.assertTrue(rejected)

    def observe(self, *, price='2000', has_probe=True, extra_response=None, gzip_descriptor=False, openapi=None,
                extra_resources=()):
        def handler(request):
            if request.url.path == '/.well-known/x402':
                entry = {'url': 'https://owned.example/v1/ping', 'method': 'POST'}
                if has_probe:
                    entry['probe_body'] = {}
                body = json.dumps({'resources': [entry, *extra_resources]}).encode()
                if gzip_descriptor:
                    import gzip
                    return httpx.Response(200, content=gzip.compress(body), headers={
                        'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
                return httpx.Response(200, content=body, headers={'Content-Type': 'application/json'})
            if request.url.path == '/openapi.json':
                if openapi is not None:
                    return httpx.Response(200, json=openapi)
                return httpx.Response(404)
            if request.url.path == '/v1/ping':
                self.assertEqual(request.method, 'POST')
                self.assertEqual(json.loads(request.content), {})
                if extra_response is not None:
                    return extra_response
                return httpx.Response(402, json={'x402Version': 2,
                    'resource': {'url': 'https://owned.example/v1/ping'},
                    'accepts': [{'scheme': 'exact', 'network': 'eip155:8453', 'amount': price,
                                 'asset': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
                                 'payTo': '0x1111111111111111111111111111111111111111'}]})
            raise AssertionError('unexpected verification URL')
        with patch.object(declarations.public_http, 'client', side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)):
            return declarations.observe(self.row())

    def test_compressed_descriptor_is_decoded_once(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        result = self.observe(gzip_descriptor=True)
        self.assertEqual(result['status'], 'verified', result)

    def test_resource_count_reports_all_advertised_resources_beyond_sample_cap(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        extra = [{'url': 'https://owned.example/v1/r%d' % i, 'method': 'GET'} for i in range(120)]
        result = self.observe(extra_resources=extra)
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(len(result['resource_samples']), declarations.MAX_RESOURCES)
        self.assertEqual(result['resource_count'], 121)

    def test_openapi_between_512kb_and_2mb_is_accepted(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        result = self.observe(openapi={'openapi': '3.0.0', 'paths': {}, 'x-pad': 'x' * (800 * 1024)})
        self.assertEqual(result['status'], 'verified', result)

    def test_primary_post_requires_explicit_empty_probe(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        result = self.observe(has_probe=False)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blocker'], 'post_probe_not_advertised')

    def test_primary_price_mismatch_stays_blocked(self):
        self.edit({'method': 'POST', 'price_usdc': 0.003})
        result = self.observe()
        self.assertEqual(result['blocker'], 'price_mismatch')
        self.assertEqual(self.row()['price_min'], 0.001)

    def test_fixed_primary_price_does_not_accept_an_upto_payment_limit(self):
        primary = 'https://owned.example/v1/ping'
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        result = self.observe(extra_response=httpx.Response(402, json={
            'x402Version': 2, 'resource': {'url': primary}, 'accepts': [{
                'scheme': 'upto', 'network': 'eip155:8453', 'amount': '2000',
                'asset': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
                'payTo': '0x1111111111111111111111111111111111111111'}]}))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blocker'], 'price_mismatch')

    def test_privacy_purge_removes_saved_declaration_and_public_edit_history(self):
        self.edit({'method': 'POST', 'resources': [
            {'url': 'https://owned.example/v1/ping', 'method': 'POST'}]})
        with db.writer(immediate=True) as conn:
            db.privacy_purge_listing(conn, 'x402', 'owned-fixture', actor='owner-fixture')
            self.assertIsNone(conn.execute('SELECT owner_declaration FROM services WHERE id=?', (self.lid,)).fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM listing_edits WHERE listing_id=?', (self.lid,)).fetchone()[0], 0)
            self.assertIsNotNone(db.find_privacy_suppression(conn, 'x402', {
                'owner_declaration': json.dumps({'url': 'https://owned.example/v1/ping'})}))

    def test_verified_primary_applies_observed_terms_not_owner_health_or_score(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        original = self.row()['owner_declaration']
        result = self.observe()
        self.assertEqual(result['status'], 'verified')
        with db.writer(immediate=True) as conn:
            updated = declarations.apply_validation(conn, self.lid, self.uid, self.claim, original, result)
        row = self.row()
        self.assertEqual(updated['status'], 'verified')
        self.assertEqual((row['price_min'], row['x402_ok'], row['http_status']), (0.002, 1, 402))
        self.assertIsNone(row['quality_score'])
        self.assertIsNone(row['tx_30d'])
        self.assertEqual(row['call_info']['method'], 'POST')
        self.assertIn('sibling resources', row['owner_declaration']['verification_scope'])

    def verified_row(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        before = self.row()['owner_declaration']
        result = self.observe()
        with db.writer(immediate=True) as conn:
            declarations.apply_validation(conn, self.lid, self.uid, self.claim, before, result)
        return self.row()

    def test_periodic_native_reverify_does_not_replace_verified_interface(self):
        row = self.verified_row()
        with db.writer() as conn:
            conn.execute('UPDATE services SET updated_at=1 WHERE id=?', (self.lid,))
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(reverify_x402._gather(conn, ('services',), None, False), [])
        self.assertTrue(declarations.has_verified_interface(row))

    def test_new_pending_revision_still_keeps_previous_verified_interface(self):
        self.verified_row()
        self.edit({'price_usdc': 0.003})
        row = self.row()
        self.assertEqual(row['owner_declaration']['status'], 'pending')
        self.assertTrue(declarations.has_verified_interface(row))
        self.assertEqual(row['price_min'], 0.002)

    def health(self, probe):
        with patch.object(jobs.crawlers, 'probe_health', side_effect=probe), \
                patch.object(jobs, 'cmd_ownership_recheck'), patch.object(jobs, 'cmd_rescore'), \
                patch.object(jobs, '_maintain_down_since'), patch.object(jobs, 'cmd_health_a2a'), \
                patch.object(jobs, 'cmd_health_mcp'):
            jobs.cmd_health()

    def test_health_uses_verified_primary_post_without_pending_method_override(self):
        self.verified_row()
        self.edit({'price_usdc': 0.003})
        def probe(url, well_known, **kwargs):
            self.assertEqual(kwargs['method_hint'], 'POST')
            self.assertTrue(kwargs['empty_post_probe'])
            return {'status': 'ok', 'latency_ms': 10, 'http_status': 402, 'x402': True}
        self.health(probe)
        self.assertEqual(self.row()['price_min'], 0.002)
        self.assertEqual(self.row()['owner_declaration']['status'], 'pending')

    def test_old_health_cannot_apply_after_primary_url_changed(self):
        def probe(*args, **kwargs):
            self.edit({'url': 'https://owned.example/v2/ping'})
            return {'status': 'down', 'latency_ms': 10, 'http_status': 404, 'x402': False}
        self.health(probe)
        self.assertEqual(self.row()['url'], 'https://owned.example/v2/ping')
        self.assertIsNone(self.row()['health'])

    def test_old_health_cannot_override_newly_verified_method_or_price(self):
        self.verified_row()
        def probe(*args, **kwargs):
            self.edit({'price_usdc': 0.003})
            before = self.row()['owner_declaration']
            result = self.observe(price='3000')
            with db.writer(immediate=True) as conn:
                declarations.apply_validation(conn, self.lid, self.uid, self.claim, before, result)
            return {'status': 'down', 'latency_ms': 10, 'http_status': 404, 'x402': False}
        self.health(probe)
        self.assertEqual((self.row()['health'], self.row()['http_status'], self.row()['price_min']), ('ok', 402, 0.003))

    def test_old_onchain_cannot_override_newly_verified_payment_terms(self):
        self.verified_row()
        def activity(*args, **kwargs):
            self.edit({'price_usdc': 0.003})
            before = self.row()['owner_declaration']
            result = self.observe(price='3000')
            with db.writer(immediate=True) as conn:
                declarations.apply_validation(conn, self.lid, self.uid, self.claim, before, result)
            return {'tx': 101, 'payers': 37, 'ok': True, 'capped': False}
        with patch.object(jobs.crawlers, 'BULK_LOG_CHAINS', ()), \
                patch.object(jobs.crawlers, 'fetch_payto_activity', side_effect=activity), \
                patch.object(jobs.time, 'sleep'), patch.object(jobs, 'cmd_rescore'):
            jobs.cmd_onchain()
        row = self.row()
        self.assertEqual(row['price_min'], 0.003)
        self.assertIsNone(row['tx_30d'])
        self.assertIsNone(row['payto_checked'])

    def test_current_onchain_result_still_applies_with_a_pending_declaration(self):
        self.verified_row()
        self.edit({'price_usdc': 0.003})
        with patch.object(jobs.crawlers, 'BULK_LOG_CHAINS', ()), \
                patch.object(jobs.crawlers, 'fetch_payto_activity', return_value={
                    'tx': 101, 'payers': 37, 'ok': True, 'capped': False}), \
                patch.object(jobs.time, 'sleep'), patch.object(jobs, 'cmd_rescore'):
            jobs.cmd_onchain()
        row = self.row()
        self.assertEqual((row['price_min'], row['tx_30d'], row['payto_payers_30d']),
                         (0.002, 101, 37))
        self.assertEqual(row['owner_declaration']['status'], 'pending')

    def test_rescore_does_not_restore_old_demand_after_terms_changed(self):
        self.verified_row()
        with db.writer() as conn:
            conn.execute('UPDATE services SET tx_30d=101,payto_payers_30d=37 WHERE id=?', (self.lid,))
        self.edit({'price_usdc': 0.003})
        before = self.row()['owner_declaration']
        result = self.observe(price='3000')
        with db.writer(immediate=True) as conn:
            declarations.apply_validation(conn, self.lid, self.uid, self.claim, before, result)
            db.rescore_services(conn)
        row = self.row()
        demand = next(component for component in db.score_breakdown(row)['components']
                      if component['name'] == 'Demand')
        self.assertEqual(demand['points'], 0)
        self.assertIsNone(row['tx_30d'])

    def test_verified_post_health_sends_only_the_empty_body_and_requires_bound_payment(self):
        endpoint = 'https://owned.example/v1/ping'
        def response(request):
            self.assertEqual(request.method, 'POST')
            self.assertEqual(json.loads(request.content), {})
            return httpx.Response(402, json={'x402Version': 2, 'resource': {'url': endpoint},
                'accepts': [{'scheme': 'exact', 'network': 'eip155:8453', 'amount': '2000',
                             'asset': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
                             'payTo': '0x1111111111111111111111111111111111111111'}]})
        with patch.object(jobs.crawlers.public_http, 'client', side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(response), follow_redirects=False)):
            result = jobs.crawlers.probe_health(endpoint, method_hint='POST', empty_post_probe=True)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['x402'])

    def test_inflight_reverify_cannot_overwrite_newly_verified_interface(self):
        row = self.verified_row()
        task = {'table': 'services', 'id': self.lid, 'slug': row['slug'],
                'endpoint': row['url'], 'explicit_price': False}
        stale_payment = dict(row['payment'], amount_raw='1000', max_amount_usdc=0.001)
        stale_metadata = {'descriptor_observed': True, 'resource_count': 1,
                          'resource_samples': [{'url': row['url'], 'method': 'GET'}],
                          'price_min': 0.001, 'price_max': 0.001}
        completed = dict(task, verdict={'status': 'verified', 'verified_url': row['url'],
                                       'payment': stale_payment}, resources=stale_metadata)
        with patch.object(reverify_x402, '_gather', return_value=[task]), \
                patch.object(reverify_x402, '_probe', return_value=completed):
            reverify_x402.reverify(targets=('services',), workers=1)
        after = self.row()
        for field in ('payment', 'call_info', 'resource_samples', 'resource_count', 'price_min', 'price_max'):
            self.assertEqual(after[field], row[field])

    def test_verification_result_cannot_apply_after_owner_changed_declaration(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        original = self.row()['owner_declaration']
        result = self.observe()
        self.edit({'price_usdc': 0.003})
        with self.assertRaises(declarations.DeclarationError), db.writer(immediate=True) as conn:
            declarations.apply_validation(conn, self.lid, self.uid, self.claim, original, result)
        self.assertEqual(self.row()['price_min'], 0.001)

    def test_verification_result_cannot_apply_after_claim_revoked(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        original = self.row()['owner_declaration']
        result = self.observe()
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
        with self.assertRaises(PermissionError), db.writer(immediate=True) as conn:
            declarations.apply_validation(conn, self.lid, self.uid, self.claim, original, result)
        self.assertEqual(self.row()['price_min'], 0.001)

    def test_blocked_verification_retains_previously_observed_fields(self):
        self.edit({'method': 'POST', 'price_usdc': 0.003})
        original = self.row()['owner_declaration']
        result = self.observe()
        with db.writer(immediate=True) as conn:
            updated = declarations.apply_validation(conn, self.lid, self.uid, self.claim, original, result)
        self.assertEqual(updated['status'], 'blocked')
        self.assertEqual(self.row()['price_min'], 0.001)
        self.assertEqual(self.row()['owner_declaration']['blocker'], 'price_mismatch')

    def test_openapi_empty_schema_allows_only_the_empty_object(self):
        doc = {'components': {'schemas': {'Empty': {'type': 'object', 'properties': {}}}}}
        operation = {'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Empty'}}}}}
        self.assertTrue(declarations._empty_probe(operation, doc))
        doc['components']['schemas']['Empty']['required'] = ['url']
        self.assertFalse(declarations._empty_probe(operation, doc))

    def test_openapi_server_path_does_not_advertise_an_undeclared_origin_path(self):
        primary = 'https://owned.example/v1/ping'
        document = {'servers': [{'url': 'https://owned.example/v2'}],
                    'paths': {'/v1/ping': {'post': {'x-probe-body': {}}}}}
        _, operations = declarations._advertised({}, document, primary)
        self.assertNotIn(primary, operations)
        self.assertIn('https://owned.example/v2/v1/ping', operations)

    def test_verified_resources_keep_same_url_get_post_aliases(self):
        primary = 'https://owned.example/v1/ping'
        self.edit({'method': 'POST', 'resources': [
            {'url': primary, 'method': 'POST'}, {'url': primary, 'method': 'GET'}]})
        def response(request):
            if request.url.path == '/.well-known/x402':
                return httpx.Response(200, json={'resources': [
                    {'url': primary, 'method': 'POST', 'probe_body': {}}]})
            if request.url.path == '/openapi.json':
                return httpx.Response(200, json={'paths': {'/v1/ping': {'get': {}, 'post': {}}}})
            return httpx.Response(402, json={'x402Version': 2, 'resource': {'url': primary},
                'accepts': [{'scheme': 'exact', 'network': 'eip155:8453', 'amount': '2000',
                             'asset': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
                             'payTo': '0x1111111111111111111111111111111111111111'}]})
        with patch.object(declarations.public_http, 'client', side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(response), follow_redirects=False)):
            result = declarations.observe(self.row())
        self.assertEqual(result['status'], 'verified')
        self.assertEqual({sample['method'] for sample in result['resource_samples']}, {'GET', 'POST'})

    def test_empty_openapi_schema_rejects_nonempty_const_and_reference_overrides(self):
        document = {'components': {'schemas': {'Empty': {'type': 'object', 'properties': {}}}}}
        for schema in ({'type': 'object', 'properties': {}, 'const': {'url': 'required'}},
                       {'$ref': '#/components/schemas/Empty', 'required': ['url']}):
            with self.subTest(schema=schema):
                operation = {'requestBody': {'content': {'application/json': {'schema': schema}}}}
                self.assertFalse(declarations._empty_probe(operation, document))

    def test_same_origin_redirect_is_not_a_resource_verification(self):
        self.edit({'method': 'POST', 'price_usdc': 0.002})
        result = self.observe(extra_response=httpx.Response(302, headers={'Location': '/other'}))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blocker'], 'payment_challenge_missing')

    def http_client(self):
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        return TestClient(app)

    def test_api_reads_and_saves_structured_declaration(self):
        with patch.object(auth, 'current_user', return_value={'id': self.uid, 'login': 'owner-fixture'}), self.http_client() as client:
            response = client.patch('/api/v1/listings/x402/owned-fixture', json={
                'method': 'POST', 'price_usdc': 0.002,
                'resources': [{'url': 'https://owned.example/v1/ping', 'method': 'POST'}]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['declaration']['status'], 'pending')
            response = client.get('/api/v1/listings/x402/owned-fixture')
            self.assertEqual(response.json()['fields']['method'], 'POST')
            self.assertIsInstance(response.json()['fields']['resources'], list)
            self.assertIn('no-store', response.headers['cache-control'])

    def test_api_rejects_measured_fields_and_malformed_resources(self):
        with patch.object(auth, 'current_user', return_value={'id': self.uid, 'login': 'owner-fixture'}), self.http_client() as client:
            for payload in ({'x402_ok': 1}, {'health': 'ok'}, {'resources': {'url': 'https://owned.example/v1/ping'}},
                            {'method': 'DELETE'}, {'price_usdc': True}):
                with self.subTest(payload=payload):
                    response = client.patch('/api/v1/listings/x402/owned-fixture', json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertIsNone(self.row()['owner_declaration'])

    def test_web_displays_and_saves_pending_declaration(self):
        with patch.object(auth, 'current_user', return_value={'id': self.uid, 'login': 'owner-fixture'}), self.http_client() as client:
            response = client.get('/listings/x402/owned-fixture/edit')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('name="method"', response.text)
            self.assertIn('name="resources"', response.text)
            response = client.post('/listings/x402/owned-fixture/edit', data={'method': 'POST', 'price_usdc': '0.002'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('Declaration Status: Pending', response.text)
            self.assertIn('Verify saved declaration', response.text)
        self.assertEqual(self.row()['price_min'], 0.001)

    def test_web_blank_interface_fields_do_not_create_a_declaration(self):
        with patch.object(auth, 'current_user', return_value={'id': self.uid, 'login': 'owner-fixture'}), self.http_client() as client:
            response = client.post('/listings/x402/owned-fixture/edit', data={
                'description': 'Text only', 'method': '', 'price_usdc': '', 'resources': '',
                'well_known_url': '', 'openapi_url': ''})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.row()['owner_declaration'])

    def test_full_application_owner_key_keeps_edit_page_private(self):
        from server import app
        with db.writer() as conn:
            key = db.create_api_key(conn, self.uid, name='owner-fixture')
        initialize = db.init_db
        with patch.object(db, 'init_db', side_effect=lambda: initialize(self.path)), TestClient(app) as client:
            response = client.get('/listings/x402/owned-fixture/edit',
                                  headers={'Authorization': 'Bearer ' + key})
            self.assertEqual(response.status_code, 200)
            self.assertIn('private', response.headers['cache-control'])
            self.assertIn('no-store', response.headers['cache-control'])

    def test_signed_owner_session_saves_but_disabled_account_is_refused(self):
        with patch.dict(os.environ, {'SESSION_SECRET': 'owner-test-signing-only'}), self.http_client() as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({'uid': self.uid}, 60))
            response = client.patch('/api/v1/listings/x402/owned-fixture', json={'method': 'POST'},
                                    headers={'Origin': 'https://agent-tools.cloud'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['declaration']['status'], 'pending')
            with db.writer() as conn:
                conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.uid,))
            response = client.patch('/api/v1/listings/x402/owned-fixture', json={'method': 'GET'},
                                    headers={'Origin': 'https://agent-tools.cloud'})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.row()['owner_declaration']['fields']['method'], 'POST')

    def test_owner_key_save_verify_and_public_card_end_to_end(self):
        primary = 'https://owned.example/v1/ping'
        with db.writer() as conn:
            key = db.create_api_key(conn, self.uid, name='owner-fixture')
        def provider(request):
            if request.url.path == '/.well-known/x402':
                return httpx.Response(200, json={'resources': [{
                    'url': primary, 'method': 'POST', 'probe_body': {}}]})
            if request.url.path == '/openapi.json':
                return httpx.Response(404)
            self.assertEqual(request.method, 'POST')
            self.assertEqual(json.loads(request.content), {})
            return httpx.Response(402, json={'x402Version': 2, 'resource': {'url': primary},
                'accepts': [{'scheme': 'exact', 'network': 'eip155:8453', 'amount': '2000',
                             'asset': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
                             'payTo': '0x1111111111111111111111111111111111111111'}]})
        with patch.object(declarations.public_http, 'client', side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=False)), self.http_client() as client:
            headers = {'Authorization': 'Bearer ' + key}
            saved = client.patch('/api/v1/listings/x402/owned-fixture',
                                 json={'method': 'POST', 'price_usdc': 0.002}, headers=headers)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()['declaration']['status'], 'pending')
            self.assertEqual(client.get('/api/v1/services/owned-fixture').json()['price_min'], 0.001)
            verified = client.post('/api/v1/listings/x402/owned-fixture/verify', headers=headers)
            self.assertEqual(verified.status_code, 200)
            self.assertEqual(verified.json()['status'], 'verified')
            public = client.get('/api/v1/services/owned-fixture').json()
            self.assertEqual(public['price_min'], 0.002)
            self.assertEqual(public['call']['template']['http_x402']['method'], 'POST')
            self.assertIn("--data '{}'", public['call']['template']['http_x402']['curl_probe'])
            self.assertNotIn('owner_declaration', public)

    def test_cross_origin_cookie_write_is_refused_before_verification(self):
        self.edit({'method': 'POST'})
        with patch.dict(os.environ, {'SESSION_SECRET': 'owner-test-signing-only'}), \
                patch.object(declarations, 'verify_listing') as verify, self.http_client() as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({'uid': self.uid}, 60))
            response = client.post('/listings/x402/owned-fixture/edit', data={'method': 'GET'},
                                   headers={'Origin': 'https://sibling.agent-tools.cloud'})
            self.assertEqual(response.status_code, 403)
            response = client.post('/api/v1/listings/x402/owned-fixture/verify',
                                   headers={'Origin': 'https://foreign.example'})
            self.assertEqual(response.status_code, 403)
            verify.assert_not_called()
        self.assertEqual(self.row()['owner_declaration']['fields']['method'], 'POST')

    def test_api_verification_is_separate_bounded_and_does_not_leak_raw_errors(self):
        self.edit({'method': 'POST'})
        with patch.object(auth, 'current_user', return_value={'id': self.uid, 'login': 'owner-fixture'}), \
                patch.object(auth.limits, 'check_ip_limits', return_value=None) as limited, \
                patch.object(declarations, 'verify_listing', return_value={'status': 'blocked', 'blocker': 'A fixed safe message'}) as verify, \
                self.http_client() as client:
            response = client.post('/api/v1/listings/x402/owned-fixture/verify')
            self.assertEqual(response.status_code, 422, response.text)
            verify.assert_called_once_with(self.lid, self.uid, self.claim)
            self.assertEqual(limited.call_count, 2)
            limited.return_value = {'retry_after': 60, 'window': 'minute', 'limit': 2}
            response = client.post('/api/v1/listings/x402/owned-fixture/verify')
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '60')
            self.assertEqual(verify.call_count, 1)

    def test_anonymous_or_wrong_owner_cannot_write_or_verify(self):
        self.edit({'method': 'POST'})
        with self.http_client() as client, patch.object(declarations, 'verify_listing') as verify:
            response = client.post('/api/v1/listings/x402/owned-fixture/verify')
            self.assertEqual(response.status_code, 401, response.text)
            with patch.object(auth, 'current_user', return_value={'id': self.uid + 1, 'login': 'other'}):
                response = client.patch('/api/v1/listings/x402/owned-fixture', json={'method': 'POST'})
                self.assertEqual(response.status_code, 403, response.text)
                response = client.post('/api/v1/listings/x402/owned-fixture/verify')
                self.assertEqual(response.status_code, 403, response.text)
            verify.assert_not_called()

    def test_public_html_uses_same_exact_primary_template_as_json(self):
        with db.writer() as conn:
            conn.execute('UPDATE services SET resource_samples=? WHERE id=?', (json.dumps([
                {'url': 'https://owned.example/sibling', 'method': 'GET'},
                {'url': 'https://owned.example/v1/ping', 'method': 'POST', 'probe_body': {}},
            ]), self.lid))
        with self.http_client() as client:
            response = client.get('/services/owned-fixture')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('curl -sS -i -X POST https://owned.example/v1/ping', response.text)
            self.assertNotIn('curl -X GET https://owned.example/sibling', response.text)

    def test_public_search_and_export_do_not_expose_pending_fields_or_internal_revision(self):
        active = self.verified_row()
        self.edit({'price_usdc': 0.003})
        with self.http_client() as client:
            for endpoint, key in (('/api/v1/search?q=Owned', 'services'),
                                  ('/api/v1/export?kind=x402', 'items')):
                response = client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                item = next(value for value in response.json()[key] if value['id'] == self.lid)
                self.assertNotIn('owner_declaration', item)
                self.assertNotIn(active['call_info']['owner_declaration_revision'], json.dumps(item))
                self.assertEqual(item['price_min'], 0.002)