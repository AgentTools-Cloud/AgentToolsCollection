"""Offline primary-observation, stale alias, rollback and privacy regressions."""

from copy import deepcopy
import json
from unittest.mock import patch

import httpx
from directory import a2a, db, ingestion, source_lifecycle as lifecycle
from test_ingestion_pipeline import TemporaryDatabaseTestCase, TABLES, _row, _upsert


class SourceLifecycleTests(TemporaryDatabaseTestCase):
    def test_accept_current_readback_never_modifies_primary_metadata(self):
        row, _, lid, _ = self.fixture(label='readback')
        before = self.listings('mcp')
        with db.writer(immediate=True) as conn:
            self.assertEqual(lifecycle.accept_current_observation(conn, 'mcp', lid,
                              self.receipt('mcp', row)), 0)
        self.assertEqual(self.listings('mcp'), before)
        self.assertEqual(len(self.query('SELECT * FROM listing_endpoint_observations')), 1)

    def test_lifecycle_migration_twice_preserves_legacy_binding_and_audit(self):
        row, alias, lid, _ = self.fixture(label='legacy-schema')
        original = self.bindings()[0]
        with db.writer() as conn:
            conn.executescript('''
                DROP TABLE wellknown_endpoint_sources;
                CREATE TABLE wellknown_endpoint_sources (
                    kind TEXT,identity_hash TEXT,endpoint_hash TEXT,source_id TEXT,
                    listing_id INTEGER,first_seen INTEGER,last_seen INTEGER,
                    PRIMARY KEY(kind,identity_hash,endpoint_hash)
                );
            ''')
            conn.execute('INSERT INTO wellknown_endpoint_sources VALUES(?,?,?,?,?,?,?)',
                         tuple(original[key] for key in ('kind','identity_hash','endpoint_hash',
                               'source_id','listing_id','first_seen','last_seen')))
            ingestion.record_issue(conn, 'mcp', 'wellknown', alias, 'hold', 'wellknown_binding_target_changed')
        audit = self.issues()
        db.init_db(self.path)
        db.init_db(self.path)
        binding = self.bindings()[0]
        for field in ('kind','identity_hash','endpoint_hash','source_id','listing_id','first_seen','last_seen'):
            self.assertEqual(binding[field], original[field])
        self.assertEqual(binding['state'], 'active')
        self.assertEqual(self.issues(), audit)
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.warnings(), [])

    def test_rebind_reactivates_alias_and_preserves_historical_pending_warning(self):
        row, alias, old_id, _ = self.fixture(label='rebound')
        self.refresh('mcp', row, 'https://rebound-new.example/mcp')
        replacement = _row('mcp', 'rebound-independent', row['endpoint_url'], source='mcp-registry')
        new_id = self.seed('mcp', replacement)
        self.assert_result(self.ingest_wellknown('mcp', [alias]), updated=1)
        binding = self.bindings()[0]
        self.assertEqual(binding['listing_id'], new_id)
        self.assertEqual(binding['state'], 'active')
        for field in ('stale_at', 'stale_target_hash', 'stale_transition_id'):
            self.assertIsNone(binding[field])
        self.assertIsNotNone(self.warnings()[0]['resolved_at'])
        with db.writer() as conn:
            rendered = lifecycle.visible_warning_rows(conn)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]['listing_id'], old_id)
        self.refresh('mcp', replacement, 'https://rebound-final.example/mcp')
        self.assertEqual(self.bindings()[0]['state'], 'stale')
        self.assertEqual(self.ingest_wellknown('mcp', [alias])['stale'], 1)

    def test_purge_approval_plan_changes_when_supplemental_alias_is_added(self):
        row, alias, lid, _ = self.fixture(label='purge-plan')
        with db.writer() as conn:
            plan_before = db._privacy_impact_plan(conn, TABLES, {'mcp': {lid}, 'a2a': set(), 'x402': set()},
                                                 {'purge-plan.example'}, {row['slug']}, [], [])
        extra = dict(alias, source_id='another-private-alias')
        self.bind('mcp', extra)
        with db.writer() as conn:
            plan_after = db._privacy_impact_plan(conn, TABLES, {'mcp': {lid}, 'a2a': set(), 'x402': set()},
                                                {'purge-plan.example'}, {row['slug']}, [], [])
            self.assertNotEqual(plan_before, plan_after)
            self.assertNotIn('wellknown_endpoint_sources', plan_after['rows'])
            db.privacy_purge_listing(conn, 'mcp', row['slug'], 'fixture')
        self.assert_result(self.ingest_wellknown('mcp', [dict(extra, endpoint_url='https://elsewhere.example/mcp')]), rejected=1)

    def receipt(self, kind, row, timestamp=None):
        from time import time
        evidence = 'current_mcp_registry' if kind == 'mcp' else 'live_agent_card'
        return lifecycle.EndpointObservation(kind, row['source'], lifecycle.digest(row['source_id']),
                                             lifecycle.digest(db._exact_endpoint(row['endpoint_url'])),
                                             'a' * 64, 'b' * 64, int(time()) if timestamp is None else timestamp,
                                             evidence)

    def fixture(self, kind='mcp', label='fixture'):
        source = 'mcp-registry' if kind == 'mcp' else 'agenstry'
        row = _row(kind, label, source=source)
        lid = self.seed(kind, row)
        alias = dict(row, source='wellknown', source_id='alias-' + label)
        self.bind(kind, alias)
        prior = self.listings(kind)[-1]
        return row, alias, lid, prior

    def refresh(self, kind, row, url, **extra):
        incoming = dict(row, endpoint_url=url, **extra)
        incoming['_endpoint_observation'] = self.receipt(kind, incoming)
        with db.writer(immediate=True) as conn:
            result = _upsert(kind)(conn, incoming)
        return incoming, result

    def warnings(self):
        return self.query('SELECT * FROM source_endpoint_warnings ORDER BY warning_key')

    def transitions(self):
        return self.query('SELECT * FROM listing_endpoint_transitions ORDER BY id')

    def test_current_primary_refresh_stales_old_binding_with_hash_only_evidence(self):
        for kind in ('mcp', 'a2a'):
            with self.subTest(kind=kind):
                row, alias, lid, _ = self.fixture(kind, 'accepted-' + kind)
                old = self.bindings(kind)[0]
                updated, result = self.refresh(kind, row, 'https://current-' + kind + '.example/' + kind)
                self.assertEqual(result, (False, lid))
                binding = self.bindings(kind)[0]
                self.assertEqual(binding['state'], 'stale')
                for field in ('endpoint_hash', 'identity_hash', 'source_id', 'listing_id', 'first_seen'):
                    self.assertEqual(binding[field], old[field])
                self.assertEqual(binding['stale_target_hash'], lifecycle.digest(db._exact_endpoint(updated['endpoint_url'])))
                self.assertTrue(self.warnings())
                serialized = json.dumps(self.transitions() + self.warnings())
                self.assertNotIn(row['endpoint_url'], serialized)
                self.assertNotIn(updated['endpoint_url'], serialized)
                self.assertNotIn(row['source_id'], serialized)
                self.assertEqual(self.listings(kind)[0]['endpoint_url'], updated['endpoint_url'])

    def test_stale_replay_skips_without_updating_primary_or_claiming_success(self):
        row, alias, lid, _ = self.fixture()
        self.refresh('mcp', row, 'https://new.example/mcp')
        before, sources = self.listings('mcp'), self.sources()
        result = self.ingest_wellknown('mcp', [alias])
        self.assertEqual(result, dict(added=0, updated=0, rejected=0, errors=[], stale=1, warnings=0))
        self.assertEqual(self.listings('mcp'), before)
        self.assertEqual(self.sources(), sources)
        self.assertEqual(self.warnings()[0]['occurrences'], 2)

    def test_unknown_sql_change_and_json_receipt_remain_real_holds(self):
        row, alias, lid, _ = self.fixture(label='unknown')
        incoming = dict(row, endpoint_url='https://changed.example/mcp',
                        _endpoint_observation={'kind': 'mcp', 'source': 'mcp-registry'})
        self.ingest('mcp', [incoming], source='mcp-registry')
        self.assertEqual(self.bindings()[0]['state'], 'active')
        self.assert_result(self.ingest_wellknown('mcp', [alias]),
                           errors=['source binding held: wellknown_binding_target_changed'])
        self.assertEqual(self.warnings(), [])

    def test_owner_edits_and_secondary_identity_are_not_current_primary_receipts(self):
        row, alias, lid, _ = self.fixture(label='owned')
        with db.writer() as conn:
            conn.execute('UPDATE mcp_servers SET owner_edited=? WHERE id=?', ('{"name":"Pinned"}', lid))
        self.refresh('mcp', row, 'https://owned-new.example/mcp')
        self.assertEqual(self.bindings()[0]['state'], 'active')
        self.assertEqual(self.warnings(), [])
        self.assert_result(self.ingest_wellknown('mcp', [alias]),
                           errors=['source binding held: wellknown_binding_target_changed'])

    def test_wrong_identity_receipt_does_not_authorize_stale(self):
        row, alias, lid, _ = self.fixture(label='wrong-identity')
        incoming = dict(row, endpoint_url='https://other.example/mcp')
        observation = self.receipt('mcp', incoming)
        from dataclasses import replace
        incoming['_endpoint_observation'] = replace(observation, identity_hash='c' * 64)
        self.ingest('mcp', [incoming], source='mcp-registry')
        self.assertEqual(self.bindings()[0]['state'], 'active')
        self.assertEqual(self.warnings(), [])

    def test_accepted_a_b_c_chain_and_return_reactivate_exact_binding(self):
        row, alias, lid, _ = self.fixture(label='chain')
        b, _ = self.refresh('mcp', row, 'https://b.example/mcp')
        c, _ = self.refresh('mcp', b, 'https://c.example/mcp')
        with db.writer() as conn:
            binding = conn.execute('SELECT * FROM wellknown_endpoint_sources').fetchone()
            self.assertTrue(lifecycle.stale_proof(conn, 'mcp', binding))
        self.assertEqual(len(self.warnings()), 2)
        self.assertEqual(self.ingest_wellknown('mcp', [alias])['stale'], 1)
        self.refresh('mcp', c, row['endpoint_url'])
        self.assertEqual(self.bindings()[0]['state'], 'active')
        self.assertTrue(all(warning['resolved_at'] is not None for warning in self.warnings()))
        self.assert_result(self.ingest_wellknown('mcp', [alias]), updated=1)

    def test_only_old_hash_and_linked_chain_bindings_are_staled(self):
        row, alias, lid, _ = self.fixture(label='specific')
        with db.writer() as conn:
            conn.execute('INSERT INTO wellknown_endpoint_sources '
                         '(kind,identity_hash,endpoint_hash,source_id,listing_id,first_seen,last_seen) '
                         'VALUES(?,?,?,?,?,?,?)', ('mcp', 'd' * 64, 'e' * 64, 'unrelated-alias', lid, 1, 1))
        self.refresh('mcp', row, 'https://specific-new.example/mcp')
        states = {item['source_id']: item['state'] for item in self.bindings()}
        self.assertEqual(states['unrelated-alias'], 'active')
        self.assertEqual(states[alias['source_id']], 'stale')

    def test_savepoint_rollback_removes_partial_stale_sql_then_warning_audit_survives(self):
        row, alias, lid, _ = self.fixture(label='rollback')
        self.refresh('mcp', row, 'https://rollback-new.example/mcp')
        def injected(conn, row):
            conn.execute("INSERT INTO meta(key,value) VALUES('must-rollback','partial')")
            from directory.wellknown_binding import StaleSourceBinding
            raise StaleSourceBinding('wellknown_stale_endpoint')
        result = self.ingest('mcp', [alias], source='wellknown', upsert=injected)
        self.assertEqual(result['stale'], 1)
        self.assertEqual(self.query("SELECT * FROM meta WHERE key='must-rollback'"), [])
        self.assertEqual(self.warnings()[0]['occurrences'], 2)

    def test_historical_known_preimage_bootstrap_does_not_change_primary(self):
        row, alias, lid, prior = self.fixture(label='historical')
        with db.writer() as conn:
            conn.execute('UPDATE mcp_servers SET endpoint_url=? WHERE id=?', ('https://history-current.example/mcp', lid))
        current = dict(row, endpoint_url='https://history-current.example/mcp')
        self.refresh('mcp', current, current['endpoint_url'])
        before, sources = self.listings('mcp'), self.sources()
        binding = self.bindings()[0]
        with db.writer(immediate=True) as conn:
            self.assertEqual(lifecycle.bootstrap_prior_binding(conn, 'mcp', prior,
                              binding['identity_hash'], binding['endpoint_hash']), 1)
            self.assertEqual(lifecycle.bootstrap_prior_binding(conn, 'mcp', prior,
                              binding['identity_hash'], binding['endpoint_hash']), 0)
        self.assertEqual(self.listings('mcp'), before)
        self.assertEqual(self.sources(), sources)
        self.assertEqual(self.ingest_wellknown('mcp', [alias])['stale'], 1)

    def test_scoped_target_change_hold_resolves_but_null_and_other_reasons_remain(self):
        row, alias, lid, prior = self.fixture(label='scoped')
        with db.writer() as conn:
            conn.execute('UPDATE mcp_servers SET endpoint_url=? WHERE id=?', ('https://scoped-new.example/mcp', lid))
        self.ingest_wellknown('mcp', [alias])
        with db.writer() as conn:
            ingestion.record_issue(conn, 'mcp', 'wellknown', dict(alias, description='legacy-null'),
                                   'hold', 'wellknown_binding_target_changed')
            conn.execute('UPDATE ingestion_issues SET endpoint_hash=NULL WHERE content_hash=?',
                         (ingestion.content_digest(dict(alias, description='legacy-null')),))
            ingestion.record_issue(conn, 'mcp', 'wellknown', alias, 'hold', 'wellknown_owner_endpoint_changed')
        incoming = dict(row, endpoint_url='https://scoped-new.example/mcp')
        self.refresh('mcp', incoming, incoming['endpoint_url'])
        binding = self.bindings()[0]
        with db.writer() as conn:
            lifecycle.bootstrap_prior_binding(conn, 'mcp', prior, binding['identity_hash'], binding['endpoint_hash'])
        issues = self.issues()
        self.assertEqual(sum(issue['resolved_at'] is None for issue in issues), 2)
        self.assertTrue(any(issue['endpoint_hash'] is None and issue['resolved_at'] is None for issue in issues))

    def test_privacy_purge_removes_stale_alias_receipts_warnings_and_audits(self):
        row, alias, lid, _ = self.fixture(label='privacy')
        self.refresh('mcp', row, 'https://privacy-current.example/mcp')
        with db.writer() as conn:
            ingestion.record_issue(conn, 'mcp', 'wellknown', alias, 'hold', 'wellknown_binding_target_changed')
            db.privacy_purge_listing(conn, 'mcp', row['slug'], 'fixture privacy')
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.bindings(), [])
        self.assertEqual(self.issues(), [])
        self.assert_result(self.ingest_wellknown('mcp', [dict(alias, endpoint_url='https://otherhost.example/mcp')]), rejected=1)

    def test_suppressed_alias_never_generates_a_warning(self):
        row, alias, lid, _ = self.fixture(label='suppressed')
        with db.writer() as conn:
            db._add_privacy_suppression(conn, 'mcp', 'source', 'wellknown\0' + alias['source_id'],
                                       'fixture', 'test')
        self.refresh('mcp', row, 'https://suppressed-new.example/mcp')
        self.assertEqual(self.warnings(), [])

    def test_real_fetch_receipts_only_from_valid_same_origin_cards(self):
        card = {'name': 'Agent', 'url': 'https://agent.example/a2a'}
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=card))) as client:
            observed, location = a2a.fetch_agent_card('https://agent.example', client=client)
        row = a2a.card_to_row(observed, location, source='agenstry', source_id='agent.example')
        self.assertIsInstance(row.get('_endpoint_observation'), lifecycle.EndpointObservation)
        ordinary = a2a.card_to_row(card, location, source='agenstry', source_id='agent.example')
        self.assertNotIn('_endpoint_observation', ordinary)

    def test_registry_requires_explicit_current_active_evidence(self):
        for official in ({'status': 'active'}, {'isLatest': False, 'status': 'active'},
                         {'isLatest': True, 'status': 'deleted'}):
            self.assertIsNone(lifecycle.registry_observation('example', 'https://registry.example/mcp',
                              {}, official, 'https://registry.modelcontextprotocol.io/'))
        result = lifecycle.registry_observation('example', 'https://registry.example/mcp', {},
                 {'isLatest': True, 'status': 'active'}, 'https://registry.modelcontextprotocol.io/')
        self.assertIsInstance(result, lifecycle.EndpointObservation)
