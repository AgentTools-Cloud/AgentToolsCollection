"""Owner lifecycle authorization and tiny-database regressions."""

from contextlib import closing
from dataclasses import replace
import asyncio
import io
import json
import sqlite3
import threading
import time
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from directory import auth, routes
from directory import db, jobs, owner_lifecycle as lifecycle, ownership
from test_ingestion_pipeline import TemporaryDatabaseTestCase


class OwnershipFixture(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.token = "fixture-published-token"
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','lifecycle-fixture','lifecycle-fixture','active',1)"
            ).lastrowid
            self.claim = conn.execute(
                "INSERT INTO domain_ownership(user_id,host,method,token_hash,token,"
                "status,created_at,verified_at,last_checked,fail_count) "
                "VALUES(?,'owned.example','descriptor',?,?,'verified',1,1,1,0)",
                (self.uid, ownership.token_hash(self.token), self.token)
            ).lastrowid

    def receipt(self, user_id=None):
        with closing(db.connect(read_only=True)) as conn:
            return ownership.fresh_ownership(conn, self.uid if user_id is None else user_id,
                                             "owned.example")

    def claim_row(self):
        with closing(db.connect(read_only=True)) as conn:
            return dict(conn.execute("SELECT * FROM domain_ownership WHERE id=?",
                                     (self.claim,)).fetchone())


class ScopedOwnershipTests(OwnershipFixture):
    def add_scope(self, scope, host="platform.example", user_id=None):
        with db.writer() as conn:
            return conn.execute(
                "INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,"
                "token,status,created_at,verified_at,last_checked) "
                "VALUES(?,?,?,'url_descriptor',?,?,'verified',1,1,1)",
                (self.uid if user_id is None else user_id, host, scope,
                 ownership.token_hash(self.token), self.token),
            ).lastrowid

    def test_path_claim_is_not_whole_host_ownership(self):
        self.add_scope("/team/one/mcp")
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNone(ownership.owner_of_host(conn, "platform.example"))
            self.assertEqual(ownership.hosts_for_user(conn, self.uid), ["owned.example"])

    def test_path_claim_only_matches_the_exact_endpoint(self):
        claim_id = self.add_scope("/team/one/mcp")
        with closing(db.connect(read_only=True)) as conn:
            owner = ownership.owner_for_url(conn, "https://PLATFORM.EXAMPLE:443/team/one/mcp")
            self.assertEqual(owner["id"], claim_id)
            for path in ("/team/two/mcp", "/team/one/mcp/child", "/team/one/mcp-other", "/team/one/mcp/"):
                with self.subTest(path=path):
                    self.assertIsNone(ownership.owner_for_url(conn, "https://platform.example" + path))

    def test_query_is_part_of_the_exact_endpoint(self):
        self.add_scope("/team/one/mcp?region=eu")
        with closing(db.connect(read_only=True)) as conn:
            self.assertTrue(ownership.may_edit(conn, self.uid, "https://platform.example/team/one/mcp?region=eu"))
            self.assertFalse(ownership.may_edit(conn, self.uid, "https://platform.example/team/one/mcp?region=us"))
            self.assertFalse(ownership.may_edit(conn, self.uid, "https://platform.example/team/one/mcp"))

    def test_default_mcp_method_preserves_trailing_slash_target(self):
        target = 'https://platform.example/team/one/mcp/?region=eu'
        self.assertEqual(ownership.default_url_method(target), 'mcp_resource')
        self.assertEqual(ownership.normalize_claim_url(target), target)
        self.assertEqual(ownership.claim_url_scope(target), ('platform.example', '/team/one/mcp/?region=eu'))

    def test_trailing_slash_claim_matches_ingested_endpoint_and_metadata_edit(self):
        target = 'https://platform.example/team/one/mcp/'
        claim_id = self.add_scope('/team/one/mcp/')
        listing_id = self.add_mcp('scoped-slash', target)
        with db.writer(immediate=True) as conn:
            stored = conn.execute('SELECT endpoint_url FROM mcp_servers WHERE id=?', (listing_id,)).fetchone()
            self.assertEqual(stored['endpoint_url'], target)
            self.assertTrue(ownership.may_edit(conn, self.uid, target))
            self.assertFalse(ownership.may_edit(conn, self.uid, target.rstrip('/')))
            applied, rejected = db.apply_listing_edits(
                conn, 'mcp', listing_id, self.uid, claim_id, {'description': 'Slash owner correction'})
            self.assertEqual(applied, ['description'])
            self.assertFalse(rejected)

    def test_verified_slash_destination_can_be_used_without_borrowing_non_slash_scope(self):
        source_claim = self.add_scope('/start/mcp')
        destination_claim = self.add_scope('/end/mcp/')
        listing_id = self.add_mcp('slash-destination', 'https://platform.example/start/mcp')
        with db.writer(immediate=True) as conn:
            applied, rejected = db.apply_listing_edits(
                conn, 'mcp', listing_id, self.uid, source_claim,
                {'endpoint_url': 'https://platform.example/end/mcp/'})
            self.assertEqual(applied, ['endpoint_url'])
            self.assertFalse(rejected)
            applied, rejected = db.apply_listing_edits(
                conn, 'mcp', listing_id, self.uid, destination_claim,
                {'endpoint_url': 'https://platform.example/end/mcp'})
            self.assertEqual(applied, [])
            self.assertTrue(rejected)

    def test_slash_variants_remain_distinct_listing_identities(self):
        first = self.add_mcp('without-slash', 'https://platform.example/one/mcp')
        second = self.add_mcp('with-slash', 'https://platform.example/one/mcp/')
        self.assertNotEqual(first, second)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM mcp_servers').fetchone()[0], 2)

    def test_legacy_root_endpoint_and_pricing_edit_is_not_a_migration(self):
        listing_id = self.add_mcp('legacy-root-price', 'https://owned.example/')
        pricing = {'offers': [{'name': 'review-operation', 'price': '1', 'priceCurrency': 'USD'}]}
        with db.writer(immediate=True) as conn:
            conn.execute("UPDATE mcp_servers SET endpoint_url='https://owned.example',"
                         "health='ok',tool_count=7,quality_score=90 WHERE id=?", (listing_id,))
            applied, rejected = db.apply_listing_edits(
                conn, 'mcp', listing_id, self.uid, self.claim,
                {'endpoint_url': 'https://owned.example', 'pricing': pricing})
            self.assertEqual(applied, ['pricing'])
            self.assertFalse(rejected)
            row = conn.execute('SELECT * FROM mcp_servers WHERE id=?', (listing_id,)).fetchone()
            self.assertEqual(row['endpoint_url'], 'https://owned.example')
            self.assertEqual((row['health'], row['tool_count'], row['quality_score']), ('ok', 7, 90))

    def test_scoped_owner_takes_precedence_without_changing_whole_host_claim(self):
        with db.writer() as conn:
            other_uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','scoped-fixture','scoped-fixture','active',1)"
            ).lastrowid
        scoped_claim = self.add_scope("/one/mcp", host="owned.example", user_id=other_uid)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(ownership.owner_for_url(conn, "https://owned.example/one/mcp")["id"], scoped_claim)
            self.assertEqual(ownership.owner_for_url(conn, "https://owned.example/two/mcp")["id"], self.claim)
            self.assertEqual(ownership.owner_of_host(conn, "owned.example")["id"], self.claim)

    def test_scoped_urls_reject_unsafe_or_ambiguous_targets(self):
        for target in (
                "http://platform.example/one", "https://127.0.0.1/one",
                "https://platform.example:8443/one", "https://user:pass@platform.example/one",
                "https://platform.example/one#fragment", "https://platform.example/one/../two",
                "https://platform.example/one/%2e%2e/two", "https://platform.example/one%2ftwo",
                "https://platform.example/one%255ctwo", "https://platform.example/one?api_key=private",
                "https://platform.example/one?access_token=private", "https://platform.example/a b",
                "https://platform.example/one%0atwo", "https://platform.example/one\x00two",
                "https://platform.example/one%invalid"):
            with self.subTest(target=target):
                with self.assertRaises(ownership.UnsafeTarget):
                    ownership.normalize_claim_url(target)

    def issue_scoped(self, target="https://platform.example/team/one/mcp"):
        with db.writer() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
                "VALUES('platform.example',25,25,'shared',1)")
            return ownership.issue_url(conn, self.uid, target, "url_descriptor")

    def test_scoped_claim_verifies_shared_endpoint_without_granting_the_host(self):
        claim_id, token = self.issue_scoped()
        response = ownership._FetchResponse(200, json.dumps({
            ownership.TOKEN_FIELD: token}).encode(), {})
        with patch.object(ownership, "_read_https", return_value=response) as reader:
            with db.writer() as conn:
                self.assertTrue(ownership.verify_claim(conn, claim_id, token)[0])
                self.assertTrue(ownership.may_edit(conn, self.uid, "https://platform.example/team/one/mcp"))
                self.assertFalse(ownership.may_edit(conn, self.uid, "https://platform.example/team/two/mcp"))
                self.assertIsNone(ownership.owner_of_host(conn, "platform.example"))
            reader.assert_called_once_with("platform.example", "/team/one/mcp", "application/json")
            self.assertNotIn(token, str(reader.call_args))

    def test_url_claim_does_not_delete_other_pending_scopes_or_domain_claims(self):
        target = "https://owned.example/team/one/mcp"
        with db.writer() as conn:
            first, _token = ownership.issue_url(conn, self.uid, target, "url_descriptor")
            second, _token = ownership.issue_url(conn, self.uid, "https://owned.example/team/two/mcp", "url_descriptor")
            whole, _token = ownership.issue(conn, self.uid, "owned.example", "descriptor")
            replacement, _token = ownership.issue_url(conn, self.uid, target, "url_descriptor")
            self.assertIsNone(conn.execute("SELECT id FROM domain_ownership WHERE id=?", (first,)).fetchone())
            rows = conn.execute("SELECT id FROM domain_ownership WHERE status='pending'").fetchall()
            self.assertEqual({row["id"] for row in rows}, {second, whole, replacement})

    def test_url_claim_rejects_wrong_token_and_redirected_proof(self):
        claim_id, token = self.issue_scoped()
        for response in (
                ownership._FetchResponse(200, b'{"agentToolsVerify":"someone-else"}', {}),
                ownership._FetchResponse(302, json.dumps({ownership.TOKEN_FIELD: token}).encode(),
                                         {"location": "https://another.example/proof"})):
            with self.subTest(status=response.status_code):
                with patch.object(ownership, "_read_https", return_value=response) as reader:
                    with db.writer() as conn:
                        self.assertFalse(ownership.verify_claim(conn, claim_id, token)[0])
                        self.assertIsNone(ownership.owner_for_url(conn, "https://platform.example/team/one/mcp"))
                    reader.assert_called_once()

    def test_expired_url_claim_is_rejected_before_network_access(self):
        claim_id, token = self.issue_scoped()
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET created_at=1 WHERE id=?", (claim_id,))
        with patch.object(ownership, "_read_https") as reader:
            with db.writer() as conn:
                ok, detail = ownership.verify_claim(conn, claim_id, token)
                self.assertFalse(ok)
                self.assertIn("expired", detail)
            reader.assert_not_called()

    def test_shared_endpoint_can_be_rechecked_without_enabling_domain_claims(self):
        claim_id, token = self.issue_scoped()
        response = ownership._FetchResponse(200, json.dumps({ownership.TOKEN_FIELD: token}).encode(), {})
        with patch.object(ownership, "_read_https", return_value=response):
            with db.writer() as conn:
                self.assertTrue(ownership.verify_claim(conn, claim_id, token)[0])
            with patch.object(ownership, "probe", return_value=(True, "domain fixture")):
                with db.writer() as conn:
                    self.assertEqual(ownership.recheck(conn), (2, 0))
                    self.assertIsNotNone(ownership.owner_for_url(conn, "https://platform.example/team/one/mcp"))
                    with self.assertRaises(ownership.ClaimError):
                        ownership.issue(conn, self.uid, "platform.example", "descriptor")

    def test_recheck_discards_failure_after_new_success_on_another_connection(self):
        claim_id = self.add_scope('/review/mcp')
        with db.writer(immediate=True) as conn:
            conn.execute('UPDATE domain_ownership SET fail_count=2 WHERE id=?', (claim_id,))
        verified = []

        def domain_probe(method, host, token):
            with patch.object(ownership, 'probe_url', return_value=(True, 'fresh fixture proof')):
                with db.writer(immediate=True) as writer_conn:
                    verified.append(ownership.verify_claim(writer_conn, claim_id, self.token)[0])
            return True, 'domain fixture proof'

        with patch.object(ownership, 'probe', side_effect=domain_probe), patch.object(ownership, 'probe_url', return_value=(False, 'old fixture failure')):
            self.assertEqual(jobs.cmd_ownership_recheck(), 0)
        self.assertEqual(verified, [True])
        with closing(db.connect(read_only=True)) as conn:
            current = conn.execute('SELECT * FROM domain_ownership WHERE id=?', (claim_id,)).fetchone()
            self.assertEqual(current['status'], 'verified')
            self.assertEqual(current['fail_count'], 0)
            self.assertTrue(ownership.may_edit(conn, self.uid, 'https://platform.example/review/mcp'))

    def test_recheck_discards_same_second_success_with_zero_failure_count(self):
        claim_id = self.add_scope('/team/one/mcp')
        moment = int(time.time())
        with db.writer(immediate=True) as conn:
            conn.execute('UPDATE domain_ownership SET verified_at=?,last_checked=?,fail_count=0 WHERE id=?',
                         (moment, moment, claim_id))
        def interrupted_probe(method, target, token):
            with patch.object(ownership, 'probe_url', return_value=(True, 'fresh fixture proof')):
                with db.writer(immediate=True) as conn:
                    self.assertTrue(ownership.verify_claim(conn, claim_id, token)[0])
            return False, 'old fixture failure'
        with patch.object(ownership.time, 'time', return_value=moment), \
                patch.object(ownership, 'probe', return_value=(True, 'domain fixture')), \
                patch.object(ownership, 'probe_url', side_effect=interrupted_probe):
            jobs.cmd_ownership_recheck()
        with closing(db.connect(read_only=True)) as conn:
            row = conn.execute('SELECT * FROM domain_ownership WHERE id=?', (claim_id,)).fetchone()
            self.assertEqual((row['status'], row['fail_count']), ('verified', 0))
            self.assertGreaterEqual(row['verification_revision'], 2)

    def test_verification_revision_upgrade_preserves_existing_claim_fields(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE domain_ownership '
                         '(id INTEGER PRIMARY KEY,user_id INTEGER,host TEXT,scope_path TEXT,'
                         'method TEXT,token_hash TEXT,token TEXT,status TEXT,created_at INTEGER,'
                         'verified_at INTEGER,last_checked INTEGER,fail_count INTEGER)')
            conn.execute("INSERT INTO domain_ownership VALUES "
                         "(1,1,'owned.example',NULL,'descriptor','fixture','published-fixture','verified',1,2,2,0)")
            before = dict(conn.execute('SELECT * FROM domain_ownership').fetchone())
            db.ensure_ownership_verification_revision(conn)
            db.ensure_ownership_verification_revision(conn)
            after = dict(conn.execute('SELECT * FROM domain_ownership').fetchone())
            self.assertEqual(after.pop('verification_revision'), 0)
            self.assertEqual(after, before)
            for revision in (1, 2):
                conn.execute('UPDATE domain_ownership SET verified_at=verified_at,'
                             'last_checked=last_checked,fail_count=fail_count WHERE id=1')
                after = dict(conn.execute('SELECT * FROM domain_ownership').fetchone())
                self.assertEqual(after.pop('verification_revision'), revision)
                self.assertEqual(after, before)

    def test_recheck_retains_consecutive_failure_threshold(self):
        claim_id = self.add_scope('/review/mcp')
        with patch.object(ownership, 'probe', return_value=(True, 'domain fixture proof')), patch.object(ownership, 'probe_url', return_value=(False, 'fixture failure')):
            for expected_count in range(1, ownership.MAX_FAILS + 1):
                self.assertEqual(jobs.cmd_ownership_recheck(), 0)
                with closing(db.connect(read_only=True)) as conn:
                    row = conn.execute('SELECT status,fail_count FROM domain_ownership WHERE id=?', (claim_id,)).fetchone()
                    self.assertEqual(row['fail_count'], expected_count)
                    self.assertEqual(row['status'], 'revoked' if expected_count == ownership.MAX_FAILS else 'verified')

    def test_recheck_refuses_to_probe_inside_a_write_transaction(self):
        with db.writer(immediate=True) as conn, patch.object(ownership, 'probe') as probe:
            with self.assertRaises(ValueError):
                ownership.recheck(conn)
            probe.assert_not_called()

    def mcp_reader(self, token, calls, *, resource_uri=None, redirect=False):
        def read(host, path, accept, **options):
            self.assertEqual((host, path), ("platform.example", "/team/one/mcp"))
            payload = json.loads(options.get("body") or b"{}")
            self.assertNotIn(token, json.dumps(payload))
            if redirect:
                return ownership._FetchResponse(307, b"", {"location": "https://other.example/mcp"})
            if options["method"] in ("GET", "DELETE"):
                return ownership._FetchResponse(405 if options["method"] == "GET" else 204, b"", {})
            method = payload["method"]
            calls.append(method)
            if method == "notifications/initialized":
                return ownership._FetchResponse(202, b"", {})
            if method == "initialize":
                result = {"protocolVersion": payload["params"]["protocolVersion"],
                          "capabilities": {"resources": {}},
                          "serverInfo": {"name": "fixture", "version": "1"}}
            else:
                self.assertEqual(method, "resources/read")
                self.assertEqual(payload["params"]["uri"], ownership.MCP_VERIFY_RESOURCE)
                result = {"contents": [{"uri": resource_uri or ownership.MCP_VERIFY_RESOURCE,
                                         "mimeType": "application/json",
                                         "text": json.dumps({ownership.TOKEN_FIELD: token})}]}
            return ownership._FetchResponse(200, json.dumps({
                "jsonrpc": "2.0", "id": payload["id"], "result": result}).encode(),
                {"content-type": "application/json", "mcp-session-id": "fixture-session"})
        return read

    def test_mcp_claim_uses_the_sdk_and_only_reads_its_fixed_resource(self):
        with db.writer() as conn:
            claim_id, token = ownership.issue_url(
                conn, self.uid, "https://platform.example/team/one/mcp", "mcp_resource")
        calls = []
        with patch.object(ownership, "_read_https", side_effect=self.mcp_reader(token, calls)):
            with db.writer() as conn:
                ok, detail = ownership.verify_claim(conn, claim_id, token)
                self.assertTrue(ok, detail)
                self.assertIsNone(ownership.owner_of_host(conn, "platform.example"))
        self.assertEqual(calls, ["initialize", "notifications/initialized", "resources/read"])

    def test_mcp_claim_rejects_other_resource_uris_and_redirects(self):
        token = "fixture-resource-token"
        for options in ({"resource_uri": "agent-tools://another-service"}, {"redirect": True}):
            with self.subTest(options=options):
                with patch.object(ownership, "_read_https", side_effect=self.mcp_reader(token, [], **options)):
                    self.assertFalse(ownership.probe_url(
                        "mcp_resource", "https://platform.example/team/one/mcp", token)[0])

    def mcp_sse_connection(self, token, calls, *, wrong_id=False, oversized=False):
        reader = self.mcp_reader(token, calls)
        connections = []

        class Response:
            def __init__(self, fetched):
                self.status = fetched.status_code
                self.headers = fetched.headers
                self.stream = io.BytesIO(fetched.body)

            def getheaders(self):
                return list(self.headers.items())

            def read(self, limit):
                return self.stream.read(limit)

            def readline(self, limit):
                return self.stream.readline(limit)

        class Connection:
            sock = None

            def __init__(self, host, address):
                self.host = host
                self.closed = False
                connections.append(self)

            def request(self, method, path, body=None, headers=None):
                fetched = reader(self.host, path, 'application/json', method=method, body=body)
                message = json.loads(body) if body else {}
                if message.get('method') == 'resources/read':
                    result = json.loads(fetched.body)
                    if wrong_id:
                        result['id'] = 'not-the-request-id'
                    notification = {'jsonrpc': '2.0', 'method': 'notifications/message',
                                    'params': {'level': 'info', 'data': 'x' * 65537 if oversized else 'fixture notification'}}
                    prefix = ': keepalive\n\nevent: message\ndata: ' + json.dumps(notification) + '\n\n'
                    frame = 'event: message\n' + '\n'.join('data: ' + line for line in json.dumps(result, indent=2).splitlines()) + '\n\n'
                    fetched = ownership._FetchResponse(200, (prefix + frame).encode(), {'content-type': 'text/event-stream'})
                self.response = Response(fetched)

            def getresponse(self):
                return self.response

            def close(self):
                self.closed = True

        return Connection, connections

    def test_mcp_sse_notification_and_multiline_result_verify_and_close(self):
        calls = []
        connection, opened = self.mcp_sse_connection(self.token, calls)
        with patch.object(ownership, '_public_addresses', return_value=['93.184.216.34']), patch.object(ownership, '_PinnedHTTPSConnection', connection):
            ok, detail = ownership.probe_url('mcp_resource', 'https://platform.example/team/one/mcp', self.token)
        self.assertTrue(ok, detail)
        self.assertEqual(calls, ['initialize', 'notifications/initialized', 'resources/read'])
        self.assertTrue(opened and all(item.closed for item in opened))

    def test_mcp_sse_wrong_response_id_is_rejected(self):
        connection, opened = self.mcp_sse_connection(self.token, [], wrong_id=True)
        with patch.object(ownership, '_public_addresses', return_value=['93.184.216.34']), patch.object(ownership, '_PinnedHTTPSConnection', connection), patch.object(ownership, '_READ_TIMEOUT', 0.2):
            self.assertFalse(ownership.probe_url('mcp_resource', 'https://platform.example/team/one/mcp', self.token)[0])
        self.assertTrue(opened and all(item.closed for item in opened))

    def test_mcp_sse_notification_bytes_count_toward_response_limit(self):
        connection, opened = self.mcp_sse_connection(self.token, [], oversized=True)
        with patch.object(ownership, '_public_addresses', return_value=['93.184.216.34']), patch.object(ownership, '_PinnedHTTPSConnection', connection):
            self.assertFalse(ownership.probe_url('mcp_resource', 'https://platform.example/team/one/mcp', self.token)[0])
        self.assertTrue(opened and all(item.closed for item in opened))

    GATEWAY_URL = "https://server.smithery.ai/acme/tool/mcp"
    GATEWAY_KEY = "fixture-gateway-key-0001"

    def gateway_reader(self, token, calls, paths, *, require_key=True):
        def read(host, path, accept, **options):
            self.assertEqual(host, "server.smithery.ai")
            paths.append(path)
            payload = json.loads(options.get("body") or b"{}")
            self.assertNotIn(self.GATEWAY_KEY, json.dumps(payload))
            self.assertNotIn(self.GATEWAY_KEY, json.dumps(options.get("request_headers") or {}))
            if require_key and not path.endswith("/mcp?api_key=" + self.GATEWAY_KEY):
                return ownership._FetchResponse(401, b'{"error":"unauthorized"}', {"content-type": "application/json"})
            if options["method"] in ("GET", "DELETE"):
                return ownership._FetchResponse(405 if options["method"] == "GET" else 204, b"", {})
            method = payload["method"]
            calls.append(method)
            if method == "notifications/initialized":
                return ownership._FetchResponse(202, b"", {})
            if method == "initialize":
                result = {"protocolVersion": payload["params"]["protocolVersion"], "capabilities": {},
                          "serverInfo": {"name": "gateway-fixture", "version": "1"}}
            else:
                self.assertEqual(payload["params"]["uri"], ownership.MCP_VERIFY_RESOURCE)
                result = {"contents": [{"uri": ownership.MCP_VERIFY_RESOURCE, "mimeType": "application/json",
                                         "text": json.dumps({ownership.TOKEN_FIELD: token})}]}
            return ownership._FetchResponse(200, json.dumps({
                "jsonrpc": "2.0", "id": payload["id"], "result": result}).encode(),
                {"content-type": "application/json", "mcp-session-id": "gateway-session"})
        return read

    def no_operator_key(self):
        self.enterContext(patch.object(ownership, "operator_gateway_key", return_value=None))

    def operator_key(self):
        self.enterContext(patch.object(ownership, "operator_gateway_key", return_value=self.GATEWAY_KEY))

    def test_operator_gateway_key_verifies_without_claimant_key(self):
        self.operator_key()
        with db.writer() as conn:
            claim_id, token = ownership.issue_url(conn, self.uid, self.GATEWAY_URL, "mcp_resource")
        calls, paths = [], []
        with patch.object(ownership, "_read_https", side_effect=self.gateway_reader(token, calls, paths)):
            with db.writer() as conn:
                ok, detail = ownership.verify_claim(conn, claim_id, token)
        self.assertTrue(ok, detail)
        self.assertEqual(calls, ["initialize", "notifications/initialized", "resources/read"])
        self.assertTrue(paths and all(path == "/acme/tool/mcp?api_key=" + self.GATEWAY_KEY for path in paths))
        with closing(sqlite3.connect(self.path)) as raw:
            self.assertNotIn(self.GATEWAY_KEY, "\n".join(raw.iterdump()))

    def test_operator_gateway_key_is_not_sent_to_other_hosts(self):
        with patch.object(ownership, "OPERATOR_GATEWAY_KEY_FILES", {}):
            self.assertIsNone(ownership.operator_gateway_key(self.GATEWAY_URL))
        self.assertIsNone(ownership.operator_gateway_key("https://platform.example/team/one/mcp"))
        paths = []
        self.operator_key()
        with patch.object(ownership, "_read_https", side_effect=self.mcp_reader(self.token, [])) as reader:
            ownership.probe_url("mcp_resource", "https://platform.example/team/one/mcp", self.token)
        self.assertTrue(all("api_key" not in call.args[1] for call in reader.call_args_list))
        self.assertFalse(paths)

    def test_operator_gateway_key_file_is_validated(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "key")
            with patch.object(ownership, "OPERATOR_GATEWAY_KEY_FILES", {"server.smithery.ai": path}):
                self.assertIsNone(ownership.operator_gateway_key(self.GATEWAY_URL))
                with open(path, "w") as stream:
                    stream.write(self.GATEWAY_KEY + "\n")
                self.assertEqual(ownership.operator_gateway_key(self.GATEWAY_URL), self.GATEWAY_KEY)
                with open(path, "w") as stream:
                    stream.write("bad key&x=1")
                self.assertIsNone(ownership.operator_gateway_key(self.GATEWAY_URL))

    def test_gateway_recheck_with_operator_key_counts_failures(self):
        self.operator_key()
        with db.writer() as conn:
            claim_id = conn.execute(
                "INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,"
                "token,status,created_at,verified_at,last_checked) "
                "VALUES(?,'server.smithery.ai','/acme/tool/mcp','mcp_resource',?,?,'verified',1,1,1)",
                (self.uid, ownership.token_hash(self.token), self.token)).lastrowid
        with patch.object(ownership, "probe", return_value=(True, "domain fixture")), \
                patch.object(ownership, "probe_url", return_value=(False, "token removed")):
            for _ in range(ownership.MAX_FAILS):
                self.assertEqual(jobs.cmd_ownership_recheck(), 0)
        with closing(db.connect(read_only=True)) as conn:
            row = conn.execute("SELECT status FROM domain_ownership WHERE id=?", (claim_id,)).fetchone()
            self.assertEqual(row["status"], "revoked")

    def test_gateway_key_is_used_once_in_the_probe_query_and_never_stored(self):
        self.no_operator_key()
        with db.writer() as conn:
            claim_id, token = ownership.issue_url(conn, self.uid, self.GATEWAY_URL, "mcp_resource")
        calls, paths = [], []
        with patch.object(ownership, "_read_https", side_effect=self.gateway_reader(token, calls, paths)):
            with closing(db.connect(read_only=True)) as conn:
                prepared = ownership.prepare_claim_verification(
                    conn, claim_id, token, gateway_api_key=self.GATEWAY_KEY)
            self.assertTrue(prepared.ok, prepared.detail)
            self.assertNotIn(self.GATEWAY_KEY, prepared.detail)
            with db.writer() as conn:
                ok, detail, _notification = ownership.apply_claim_verification(
                    conn, claim_id, token, verification=prepared)
                self.assertTrue(ok, detail)
                self.assertIsNone(ownership.owner_of_host(conn, "server.smithery.ai"))
                self.assertTrue(ownership.may_edit(conn, self.uid, self.GATEWAY_URL))
                self.assertFalse(ownership.may_edit(conn, self.uid, "https://server.smithery.ai/acme/other/mcp"))
        self.assertEqual(calls, ["initialize", "notifications/initialized", "resources/read"])
        self.assertTrue(paths and all(path == "/acme/tool/mcp?api_key=" + self.GATEWAY_KEY for path in paths))
        with closing(sqlite3.connect(self.path)) as raw:
            dump = "\n".join(raw.iterdump())
        self.assertNotIn(self.GATEWAY_KEY, dump)

    def test_gateway_without_any_key_reports_refusal(self):
        self.no_operator_key()
        with db.writer() as conn:
            claim_id, token = ownership.issue_url(conn, self.uid, self.GATEWAY_URL, "mcp_resource")
        paths = []
        with patch.object(ownership, "_read_https", side_effect=self.gateway_reader(token, [], paths)):
            with db.writer() as conn:
                ok, detail = ownership.verify_claim(conn, claim_id, token)
        self.assertFalse(ok)
        self.assertIn("gateway refused", detail)
        self.assertTrue(paths and all("api_key" not in path for path in paths))

    def test_gateway_key_is_refused_for_other_hosts_methods_and_whole_host_claims(self):
        with patch.object(ownership, "_read_https") as reader:
            for method, url, key in (
                    ("mcp_resource", "https://platform.example/team/one/mcp", self.GATEWAY_KEY),
                    ("url_descriptor", self.GATEWAY_URL, self.GATEWAY_KEY),
                    ("mcp_resource", self.GATEWAY_URL, "short"),
                    ("mcp_resource", self.GATEWAY_URL, "bad key&other=1")):
                with self.subTest(method=method, url=url, key=key):
                    ok, detail = ownership.probe_url(method, url, "fixture-token", gateway_api_key=key)
                    self.assertFalse(ok)
                    self.assertNotIn(key, detail)
            with closing(db.connect(read_only=True)) as conn:
                prepared = ownership.prepare_claim_verification(
                    conn, self.claim, self.token, gateway_api_key=self.GATEWAY_KEY)
            self.assertIsNone(prepared.claim)
            reader.assert_not_called()

    def test_gateway_recheck_without_operator_key_is_inconclusive(self):
        self.no_operator_key()
        with db.writer() as conn:
            claim_id = conn.execute(
                "INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,"
                "token,status,created_at,verified_at,last_checked) "
                "VALUES(?,'server.smithery.ai','/acme/tool/mcp','mcp_resource',?,?,'verified',1,1,1)",
                (self.uid, ownership.token_hash(self.token), self.token)).lastrowid
        with patch.object(ownership, "probe", return_value=(True, "domain fixture")), \
                patch.object(ownership, "probe_url", return_value=(False, "gateway refused")) as prober:
            for _ in range(ownership.MAX_FAILS + 1):
                self.assertEqual(jobs.cmd_ownership_recheck(), 0)
        for call in prober.call_args_list:
            self.assertNotIn("gateway_api_key", call.kwargs)
        with closing(db.connect(read_only=True)) as conn:
            row = conn.execute("SELECT status,fail_count FROM domain_ownership WHERE id=?", (claim_id,)).fetchone()
            self.assertEqual((row["status"], row["fail_count"]), ("verified", 0))

    def test_gateway_key_api_and_browser_verification_paths(self):
        client = self.browser_client()
        self.no_operator_key()
        opened = client.post("/api/v1/listing-claims", json={"url": self.GATEWAY_URL, "method": "mcp_resource"}).json()
        with patch.object(ownership, "_read_https", side_effect=self.gateway_reader(opened["token"], [], [])):
            refused = client.post(opened["next"]["url"], json={"token": opened["token"]})
            self.assertEqual(refused.status_code, 422, refused.text)
            verified = client.post(opened["next"]["url"], json={
                "token": opened["token"], "gateway_api_key": self.GATEWAY_KEY})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertNotIn(self.GATEWAY_KEY, verified.text)
        self.operator_key()
        page = client.post("/account/service-claims", data={"url": "https://server.smithery.ai/acme/two/mcp"})
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn('name="gateway_api_key"', page.text)
        with closing(db.connect(read_only=True)) as conn:
            pending = conn.execute(
                "SELECT id FROM domain_ownership WHERE scope_path='/acme/two/mcp' AND status='pending'").fetchone()
        token = page.text.split('name="token" value="', 1)[1].split('"', 1)[0]
        paths = []
        with patch.object(ownership, "_read_https", side_effect=self.gateway_reader(token, [], paths)):
            response = client.post("/account/service-claims/%s/verify" % pending["id"],
                                   data={"token": token}, follow_redirects=False)
        self.assertEqual(response.status_code, 302, (paths, response.text[-300:]))
        self.assertIn("its own gateway key", client.get("/docs/claim").text)

    def add_mcp(self, slug, target):
        with db.writer() as conn:
            db.upsert_mcp_server(conn, {
                "name": slug, "slug": slug, "endpoint_url": target,
                "source": "fixture", "source_id": slug, "confidence": 0.5})
            return conn.execute("SELECT id FROM mcp_servers WHERE slug=?", (slug,)).fetchone()["id"]

    def test_verified_projection_and_claim_results_only_include_the_owned_url(self):
        claim_id = self.add_scope("/team/one/mcp")
        first = self.add_mcp("scoped-one", "https://platform.example/team/one/mcp")
        self.add_mcp("scoped-two", "https://platform.example/team/two/mcp")
        self.add_mcp("scoped-query", "https://platform.example/team/one/mcp?tenant=other")
        with db.writer() as conn:
            result = db.sync_owner_verified(conn)
            self.assertEqual(result["mcp_servers"], 1)
            rows = conn.execute("SELECT id,owner_verified FROM mcp_servers").fetchall()
            self.assertEqual({row["id"] for row in rows if row["owner_verified"]}, {first})
            claim = conn.execute("SELECT * FROM domain_ownership WHERE id=?", (claim_id,)).fetchone()
            self.assertEqual([row["slug"] for row in ownership.listings_for_claim(conn, claim)], ["scoped-one"])

    def test_scoped_owner_can_edit_metadata_but_not_an_unverified_sibling_endpoint(self):
        claim_id = self.add_scope("/team/one/mcp")
        listing_id = self.add_mcp("scoped-one", "https://platform.example/team/one/mcp")
        with db.writer() as conn:
            applied, rejected = db.apply_listing_edits(
                conn, "mcp", listing_id, self.uid, claim_id, {"description": "Owner correction"})
            self.assertEqual((applied, rejected), (["description"], []))
            applied, rejected = db.apply_listing_edits(
                conn, "mcp", listing_id, self.uid, claim_id,
                {"endpoint_url": "https://platform.example/team/two/mcp"})
            self.assertEqual(applied, [])
            self.assertTrue(rejected)
        destination_claim = self.add_scope("/team/two/mcp")
        with db.writer() as conn:
            applied, rejected = db.apply_listing_edits(
                conn, "mcp", listing_id, self.uid, claim_id,
                {"endpoint_url": "https://platform.example/team/two/mcp"})
            self.assertIn("endpoint_url", applied)
            self.assertEqual(rejected, [])
            self.assertEqual(db.owner_edit_state(
                conn, "mcp", listing_id, self.uid, destination_claim)[1]["id"], destination_claim)
            with self.assertRaises(PermissionError):
                db.owner_edit_state(conn, "mcp", listing_id, self.uid, claim_id)

    def test_browser_edit_authorization_uses_exact_url_ownership(self):
        claim_id = self.add_scope("/team/one/mcp")
        self.add_mcp("scoped-one", "https://platform.example/team/one/mcp")
        self.add_mcp("scoped-two", "https://platform.example/team/two/mcp")
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        with patch.object(auth, "current_user", return_value={"id": self.uid}):
            self.assertTrue(auth.may_edit_listing(request, "mcp", "https://platform.example/team/one/mcp"))
            self.assertFalse(auth.may_edit_listing(request, "mcp", "https://platform.example/team/two/mcp"))
            self.assertEqual(auth._load_editable(request, "mcp", "scoped-one")[3]["id"], claim_id)
            with self.assertRaises(Exception) as refused:
                auth._load_editable(request, "mcp", "scoped-two")
            self.assertEqual(refused.exception.status_code, 403)

    def test_full_url_is_the_only_identity_and_infers_the_proof_method(self):
        for target, method in (
                ("https://glama.ai/endpoints/profile-123/mcp", "mcp_resource"),
                ("https://github.com/team/service", "github_file"),
                ("https://www.npmjs.com/package/@team/service", "npm_package"),
                ("https://unlisted.example/custom?region=eu", "url_descriptor")):
            with self.subTest(target=target):
                self.assertEqual(ownership.resolve_service_url(target), target)
                self.assertEqual(ownership.default_url_method(target), method)
        for target in ("", "http://platform.example/service", "https://platform.example/../service"):
            with self.subTest(target=target):
                with self.assertRaises(ownership.ClaimError):
                    ownership.resolve_service_url(target)

    def test_url_proof_is_not_taken_from_user_generated_nested_data(self):
        token = "fixture-top-level-token"
        for document in (
                {"items": [{ownership.TOKEN_FIELD: token}]},
                {"metadata": {ownership.TOKEN_FIELD: token}},
                [{ownership.TOKEN_FIELD: token}]):
            with self.subTest(document=document):
                response = ownership._FetchResponse(200, json.dumps(document).encode(), {})
                with patch.object(ownership, "_read_https", return_value=response):
                    self.assertFalse(ownership.probe_url("url_descriptor", "https://platform.example/one", token)[0])

    def test_platform_proof_reads_only_the_expected_repository_or_package(self):
        token = "fixture-platform-token"
        proof = json.dumps({ownership.TOKEN_FIELD: token}).encode()
        with patch.object(ownership, "_read_https", return_value=ownership._FetchResponse(200, proof, {})) as reader:
            self.assertTrue(ownership.probe_url("github_file", "https://github.com/team/service", token)[0])
            reader.assert_called_once_with("api.github.com", "/repos/team/service/contents/.well-known/agent-tools.json",
                                           "application/vnd.github.raw+json")
        manifest = json.dumps({"name": "@team/service", ownership.TOKEN_FIELD: token}).encode()
        with patch.object(ownership, "_read_https", return_value=ownership._FetchResponse(200, manifest, {})) as reader:
            self.assertTrue(ownership.probe_url("npm_package", "https://www.npmjs.com/package/@team/service", token)[0])
            reader.assert_called_once_with("registry.npmjs.org", "/@team%2Fservice/latest", "application/json")
        with patch.object(ownership, "_read_https") as reader:
            self.assertFalse(ownership.probe_url("github_file", "https://github.com.evil.example/team/service", token)[0])
            reader.assert_not_called()
        with patch.object(ownership, "_read_https", return_value=ownership._FetchResponse(200, manifest, {})):
            self.assertFalse(ownership.probe_url("npm_package", "https://www.npmjs.com/package/@team/another", token)[0])

    def service_client(self):
        self.enterContext(patch.object(routes, "_require_api_user", return_value={"id": self.uid}))
        self.enterContext(patch.object(auth, "current_user", return_value={"id": self.uid, "status": "active"}))
        self.enterContext(patch.object(routes.limits, "check_ip_limits", return_value=None))
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(auth.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_service_claim_api_only_accepts_complete_urls(self):
        client = self.service_client()
        response = client.post("/api/v1/listing-claims", json={"url": "https://glama.ai/endpoints/profile-123/mcp"})
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual(result["url"], "https://glama.ai/endpoints/profile-123/mcp")
        self.assertEqual(result["scope"], "url")
        self.assertEqual(result["method"], "mcp_resource")
        self.assertEqual(result["document"], {ownership.TOKEN_FIELD: result["token"]})
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn("/api/v1/listing-claims/", result["next"]["url"])
        response = client.post("/api/v1/listing-claims", json={
            "url": "https://unlisted-platform.example/team/resource?region=eu", "method": "url_descriptor"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["url"], "https://unlisted-platform.example/team/resource?region=eu")
        self.assertEqual(client.post("/api/v1/listing-claims", json={"platform": "glama", "suffix": "profile-123"}).status_code, 422)

    def test_service_claim_api_only_returns_and_edits_the_verified_target(self):
        client = self.service_client()
        self.add_mcp("scoped-one", "https://platform.example/team/one/mcp")
        self.add_mcp("scoped-two", "https://platform.example/team/two/mcp")
        opened = client.post("/api/v1/listing-claims", json={
            "url": "https://platform.example/team/one/mcp", "method": "url_descriptor"}).json()
        response = ownership._FetchResponse(200, json.dumps(opened["document"]).encode(), {})
        with patch.object(ownership, "_read_https", return_value=response):
            verified = client.post(opened["next"]["url"], json={"token": opened["token"]})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual([row["slug"] for row in verified.json()["listings"]], ["scoped-one"])
        edited = client.patch("/api/v1/listings/mcp/scoped-one", json={"description": "Scoped correction"})
        self.assertEqual(edited.status_code, 200, edited.text)
        refused = client.patch("/api/v1/listings/mcp/scoped-two", json={"description": "Not owned"})
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_service_claim_api_rejects_cross_account_verification_and_unsafe_input(self):
        client = self.service_client()
        opened = client.post("/api/v1/listing-claims", json={
            "url": "https://platform.example/one", "method": "url_descriptor"}).json()
        with patch.object(routes, "_require_api_user", return_value={"id": self.uid + 100}):
            with patch.object(ownership, "_read_https") as reader:
                response = client.post(opened["next"]["url"], json={"token": opened["token"]})
                self.assertEqual(response.status_code, 403)
                reader.assert_not_called()
        for payload in (
                {"url": "http://platform.example/one"},
                {"url": "https://127.0.0.1/one"},
                {"url": "https://platform.example/one?apiKey=private"},
                {"url": "https://github.com/team/.."},
                {"url": "https://other.example/one", "method": "github_file"},
                {"platform": "glama", "url": "https://other.example/one"}):
            with self.subTest(payload=payload):
                self.assertEqual(client.post("/api/v1/listing-claims", json=payload).status_code, 422)

    def test_url_claim_rechecks_account_and_privacy_policy_before_persisting_proof(self):
        claim_id, token = self.issue_scoped()
        response = ownership._FetchResponse(200, json.dumps({ownership.TOKEN_FIELD: token}).encode(), {})
        with patch.object(ownership, "_read_https", return_value=response):
            with closing(db.connect(read_only=True)) as conn:
                prepared = ownership.prepare_claim_verification(conn, claim_id, token)
        with db.writer() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.uid,))
            self.assertFalse(ownership.apply_claim_verification(conn, claim_id, token, verification=prepared)[0])
            conn.execute("UPDATE users SET status='active' WHERE id=?", (self.uid,))
            db._add_privacy_suppression(conn, "*", "host", "platform.example", "fixture", "fixture")
            self.assertFalse(ownership.apply_claim_verification(conn, claim_id, token, verification=prepared)[0])
            self.assertIsNone(ownership.owner_for_url(conn, "https://platform.example/team/one/mcp"))

    def test_service_claim_openapi_documents_authentication_and_error_states(self):
        client = self.service_client()
        schema = client.get("/openapi.json").json()
        path = schema["paths"]["/api/v1/listing-claims"]["post"]
        self.assertEqual(path["security"], [{"AccountAPIKey": []}])
        self.assertTrue({"201", "401", "403", "422", "429", "503"}.issubset(path["responses"]))
        self.assertIn("ServiceClaimRequest", schema["components"]["schemas"])
        self.assertEqual(schema["components"]["schemas"]["ServiceClaimRequest"]["required"], ["url"])
        self.assertNotIn("platform", schema["components"]["schemas"]["ServiceClaimRequest"]["properties"])
        self.assertEqual(client.get("/api/v1/claim-methods").status_code, 200)
        self.assertEqual(client.get("/api/v1/claim-platforms").status_code, 404)

    def browser_client(self):
        client = self.service_client()
        with closing(db.connect(read_only=True)) as conn:
            profile = dict(conn.execute("SELECT * FROM users WHERE id=?", (self.uid,)).fetchone())
        self.enterContext(patch.object(auth, "current_user", return_value=profile))
        return client

    def test_browser_url_form_issues_a_token_and_shows_the_exact_target(self):
        client = self.browser_client()
        response = client.post("/account/service-claims", data={"url": "https://glama.ai/endpoints/profile-123/mcp"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("https://glama.ai/endpoints/profile-123/mcp", response.text)
        self.assertIn("/account/service-claims/", response.text)
        self.assertIn("agent-tools://ownership", response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn('id="claim-platform"', response.text)
        self.assertNotIn('id="claim-suffix"', response.text)
        self.assertNotIn("/account/claims.js", response.text)

    def test_browser_account_and_me_do_not_expand_url_permissions_to_the_host(self):
        client = self.browser_client()
        self.add_scope("/team/one/mcp")
        self.add_mcp("scoped-one", "https://platform.example/team/one/mcp")
        self.add_mcp("scoped-two", "https://platform.example/team/two/mcp")
        response = client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["hosts"], ["owned.example"])
        self.assertEqual(response.json()["urls"], ["https://platform.example/team/one/mcp"])
        self.assertTrue(client.get("/api/v1/me?kind=mcp&slug=scoped-one").json()["can_edit_listing"])
        self.assertFalse(client.get("/api/v1/me?kind=mcp&slug=scoped-two").json()["can_edit_listing"])
        response = client.get("/account")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("/listings/mcp/scoped-one/edit", response.text)
        self.assertNotIn("/listings/mcp/scoped-two/edit", response.text)
        response = client.get("/listings/mcp/scoped-one/edit")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("Manage listing", response.text)

    def test_cookie_authenticated_url_claim_writes_require_the_same_origin(self):
        client = self.browser_client()
        with patch.object(auth, "unsign", return_value={"uid": self.uid}):
            response = client.post("/account/service-claims", data={"url": "https://platform.example/one"},
                                   headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403, response.text)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM domain_ownership WHERE scope_path IS NOT NULL").fetchone()[0], 0)

    def test_cached_detail_permission_is_not_inferred_from_domain_ownership(self):
        client = self.browser_client()
        with db.writer() as conn:
            other_uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','other-owner','other-owner','active',1)").lastrowid
        self.add_scope("/other/mcp", host="owned.example", user_id=other_uid)
        self.add_mcp("overlapping-scope", "https://owned.example/other/mcp")
        response = client.get("/api/v1/me?kind=mcp&slug=overlapping-scope")
        self.assertIn("owned.example", response.json()["hosts"])
        self.assertFalse(response.json()["can_edit_listing"])
        document = client.get("/docs/claim")
        self.assertEqual(document.status_code, 200)
        self.assertIn("/api/v1/listing-claims", document.text)
        self.assertIn("agent-tools://ownership", document.text)


class ClaimMethodRecommendationTests(OwnershipFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(auth, "_require_user", return_value={
            "id": self.uid, "login": "lifecycle-fixture", "status": "active"}))
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(auth.router)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_recommendation_follows_listing_protocol(self):
        self.assertEqual(ownership.recommended_method({"x402", "mcp"}), ("descriptor", "x402"))
        self.assertEqual(ownership.recommended_method({"a2a", "mcp"}), ("descriptor", "a2a"))
        self.assertEqual(ownership.recommended_method({"mcp"}), ("wellknown_file", "mcp"))
        self.assertEqual(ownership.recommended_method(set()), ("wellknown_file", ""))

    def submit_claim(self, host, kinds, method=None):
        listings = [{"kind": kind} for kind in kinds]
        data = {"host": host} if method is None else {"host": host, "method": method}
        original = db.listings_for_host

        def fixture_listings(conn, requested):
            return listings if requested == host else original(conn, requested)

        with patch.object(db, "listings_for_host", side_effect=fixture_listings):
            response = self.client.post("/account/domains", data=data)
        self.assertEqual(response.status_code, 200, response.text[:300])
        with closing(db.connect(read_only=True)) as conn:
            row = conn.execute("SELECT method FROM domain_ownership WHERE user_id=? AND host=? "
                               "AND status='pending'", (self.uid, host)).fetchone()
        return row["method"], response.text

    def test_account_form_recommends_by_protocol(self):
        method, page = self.submit_claim("mcp-only.example", ["mcp"])
        self.assertEqual(method, "wellknown_file")
        self.assertIn("/.well-known/agent-tools-verify.txt", page)
        self.assertIn("do not create one just for verification", page)
        method, page = self.submit_claim("pay.example", ["x402", "mcp"])
        self.assertEqual(method, "descriptor")
        self.assertIn("/.well-known/x402:", page)
        method, page = self.submit_claim("agent.example", ["a2a"])
        self.assertEqual(method, "descriptor")
        self.assertIn("/.well-known/agent-card.json", page)
        method, page = self.submit_claim("explicit.example", ["mcp"], method="dns_txt")
        self.assertEqual(method, "dns_txt")
        self.assertIn('value="auto" selected', page)
        self.assertNotIn("(easiest)", page)


class PendingClaimRecheckTests(OwnershipFixture):
    def setUp(self):
        super().setUp()
        self.pending_token = ownership.new_token()
        now = int(time.time())
        with db.writer() as conn:
            self.pending = conn.execute(
                "INSERT INTO domain_ownership(user_id,host,method,token_hash,status,created_at) "
                "VALUES(?,'pending.example','wellknown_file',?,'pending',?)",
                (self.uid, ownership.token_hash(self.pending_token), now - 700)).lastrowid
        self.published = ""
        self.fetches = []

        def fetch(host, path, accept):
            self.fetches.append((host, path))
            return ownership._FetchResponse(200, self.published.encode(), {})

        self.enterContext(patch.object(ownership, "_get", side_effect=fetch))
        self.enterContext(patch.object(ownership, "normalize_host", side_effect=lambda host: host))

    def pending_row(self):
        with closing(db.connect(read_only=True)) as conn:
            return dict(conn.execute("SELECT * FROM domain_ownership WHERE id=?",
                                     (self.pending,)).fetchone())

    def run_recheck(self):
        with closing(db.connect(read_only=True)) as conn:
            return ownership.recheck_pending(conn)

    def test_hashed_token_matches_only_the_issued_value(self):
        hashed = ownership.HashedToken(ownership.token_hash(self.pending_token))
        self.assertFalse(hashed.in_text("atc_" + "A" * 32))
        self.assertFalse(hashed.matches(self.pending_token + "x"))
        self.assertTrue(hashed.in_text("noise\n%s\n" % self.pending_token))
        self.assertEqual(hashed.found, self.pending_token)

    def test_published_token_is_verified_without_the_owner_returning(self):
        self.published = self.pending_token + "\n"
        self.assertEqual(self.run_recheck(), (1, 1))
        row = self.pending_row()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["token"], self.pending_token)

    def test_missing_or_wrong_token_waits_for_the_next_point_only(self):
        self.published = "atc_" + "B" * 32
        self.assertEqual(self.run_recheck(), (1, 0))
        row = self.pending_row()
        self.assertEqual((row["status"], row["fail_count"]), ("pending", 0))
        self.assertIsNotNone(row["last_checked"])
        self.assertEqual(self.run_recheck(), (0, 0))
        self.assertEqual(len(self.fetches), 1)

    def test_schedule_is_bounded_by_the_window(self):
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET created_at=? WHERE id=?",
                         (int(time.time()) - ownership.HOST_PENDING_RECHECK_SECONDS - 60, self.pending))
        self.published = self.pending_token
        self.assertEqual(self.run_recheck(), (0, 0))
        self.assertEqual(self.fetches, [])
        self.assertLessEqual(len(ownership.PENDING_RECHECK_OFFSETS), 10)

    def test_new_token_replaces_the_pending_check(self):
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET token_hash=? WHERE id=?",
                         (ownership.token_hash(ownership.new_token()), self.pending))
        self.published = self.pending_token
        self.assertEqual(self.run_recheck(), (1, 0))
        self.assertEqual(self.pending_row()["status"], "pending")


class VisibleClaimTokenTests(OwnershipFixture):
    def context(self):
        request = Request({"type": "http", "query_string": b"", "headers": []})
        return auth._account_context(request, {"id": self.uid, "login": "lifecycle-fixture"})

    def test_issued_tokens_stay_visible_to_their_account(self):
        with db.writer() as conn:
            host_id, host_token = ownership.issue(conn, self.uid, "visible.example", "wellknown_file")
        with db.writer() as conn:
            conn.execute("INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,status,created_at) "
                         "VALUES(?,'visible.example','/mcp','mcp_resource',?,'pending',?)",
                         (self.uid, ownership.token_hash(ownership.new_token()), int(time.time())))
        pending = {row["id"]: row for row in self.context()["pending"]}
        self.assertEqual(pending[host_id]["token"], host_token)
        self.assertIn("agent-tools-verify.txt", pending[host_id]["instruction"])
        legacy = [row for row in pending.values() if row["scope_path"] == "/mcp"][0]
        self.assertIsNone(legacy["token"])
        self.assertIn("ownership", legacy["instruction"])
        verified = {row["id"]: row for row in self.context()["verified"]}
        self.assertEqual(verified[self.claim]["token"], self.token)

    def test_mismatched_stored_token_is_not_shown(self):
        with db.writer() as conn:
            conn.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,token,status,created_at) "
                         "VALUES(?,'tampered.example','dns_txt',?,'atc_wrong','pending',?)",
                         (self.uid, ownership.token_hash(ownership.new_token()), int(time.time())))
        row = [r for r in self.context()["pending"] if r["host"] == "tampered.example"][0]
        self.assertIsNone(row["token"])


class ClaimVerificationContentionTests(OwnershipFixture):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            conn.execute(
                "UPDATE domain_ownership SET status='pending',token=NULL,"
                "verified_at=NULL WHERE id=?", (self.claim,))
        self.enterContext(patch.object(
            routes, "_require_api_user", return_value={"id": self.uid}))
        self.probe = self.enterContext(patch.object(
            ownership, "probe", return_value=(True, "fixture")))
        self.enterContext(patch.object(auth, "_require_user", return_value={
            "id": self.uid, "login": "lifecycle-fixture", "status": "active"}))
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.include_router(auth.router)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def verify(self):
        return self.client.post(
            f"/api/v1/claims/{self.claim}/verify", json={"token": self.token})

    def test_claim_write_lock_returns_retryable_503_without_changes(self):
        before = self.claim_row()
        with closing(db.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            try:
                response = self.verify()
                elapsed = time.monotonic() - started
            finally:
                blocker.rollback()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), "2")
        self.assertLess(elapsed, 3.0)
        self.assertEqual(self.claim_row(), before)
        self.probe.assert_called_once()

    def test_claim_recovers_from_short_lock_without_repeating_probe(self):
        with closing(db.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            release = threading.Timer(0.15, blocker.rollback)
            release.start()
            try:
                response = self.verify()
            finally:
                release.join(timeout=2.0)
                blocker.rollback()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.claim_row()["status"], "verified")
        self.probe.assert_called_once()

    def verify_browser(self):
        return self.client.post(
            f"/account/domains/{self.claim}/verify", data={"token": self.token},
            follow_redirects=False)

    def test_browser_claim_lock_returns_retryable_503_without_changes(self):
        before = self.claim_row()
        with closing(db.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            try:
                response = self.verify_browser()
                elapsed = time.monotonic() - started
            finally:
                blocker.rollback()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), "2")
        self.assertLess(elapsed, 3.0)
        self.assertEqual(self.claim_row(), before)
        self.probe.assert_called_once()

    def test_browser_claim_short_lock_preserves_success_redirect(self):
        with closing(db.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            release = threading.Timer(0.15, blocker.rollback)
            release.start()
            try:
                response = self.verify_browser()
            finally:
                release.join(timeout=2.0)
                blocker.rollback()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/account")
        self.assertEqual(self.claim_row()["status"], "verified")
        self.probe.assert_called_once()

    def test_wrong_account_and_token_never_probe_or_change_claim(self):
        before = self.claim_row()
        with patch.object(routes, "_require_api_user", return_value={"id": self.uid + 1}):
            response = self.verify()
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            f"/api/v1/claims/{self.claim}/verify", json={"token": "wrong-fixture-token"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.claim_row(), before)
        self.probe.assert_not_called()

    def test_claim_changes_after_probe_are_not_accepted(self):
        before = self.claim_row()
        for column, value in (
                ("host", "changed.example"), ("method", "dns_txt"),
                ("token_hash", ownership.token_hash("changed-fixture-token")),
                ("status", "revoked"), ("scope_path", "/tenant")):
            with self.subTest(column=column):
                def change_claim(*_args):
                    with db.writer() as conn:
                        conn.execute(
                            f"UPDATE domain_ownership SET {column}=? WHERE id=?",
                            (value, self.claim))
                    return True, "fixture"

                self.probe.reset_mock()
                self.probe.side_effect = change_claim
                response = self.verify()
                self.assertEqual(response.status_code, 422)
                self.assertIsNone(self.claim_row()["token"])
                self.assertIsNone(self.claim_row()["verified_at"])
                self.probe.assert_called_once()
                with db.writer() as conn:
                    conn.execute(
                        f"UPDATE domain_ownership SET {column}=? WHERE id=?",
                        (before[column], self.claim))

    def test_probe_does_not_block_the_http_event_loop(self):
        entered = threading.Event()

        def slow_probe(*_args):
            entered.set()
            threading.Event().wait(0.3)
            return True, "fixture"

        self.probe.side_effect = slow_probe

        @self.app.get("/fixture-responsive")
        def responsive():
            return {"ok": True}

        async def exercise():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
                verification = asyncio.create_task(client.post(
                    f"/api/v1/claims/{self.claim}/verify", json={"token": self.token}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                    self.assertFalse(verification.done())
                    started = time.monotonic()
                    response = await client.get("/fixture-responsive")
                    self.assertEqual(response.status_code, 200)
                    self.assertLess(time.monotonic() - started, 0.15)
                finally:
                    result = await verification
                self.assertEqual(result.status_code, 200)

        asyncio.run(exercise())

    def test_transfer_notification_runs_once_after_commit_without_write_lock(self):
        with db.writer() as conn:
            previous_uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','previous-fixture','previous-fixture','active',1)"
            ).lastrowid
            conn.execute(
                "INSERT INTO domain_ownership(user_id,host,method,token_hash,token,"
                "status,created_at,verified_at,last_checked,fail_count) "
                "VALUES(?,'owned.example','descriptor',?,'previous-fixture-token',"
                "'verified',1,1,1,0)",
                (previous_uid, ownership.token_hash("previous-fixture-token")))
        observations = []

        def observe_notification(*_args):
            committed = self.claim_row()["status"] == "verified"
            with closing(db.connect(timeout=0.05)) as writer:
                try:
                    writer.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    observations.append(False)
                else:
                    observations.append(committed)
                    writer.rollback()

        with patch.object(ownership, "_notify_displaced", side_effect=observe_notification) as notify:
            response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observations, [True])
        notify.assert_called_once()
        self.probe.assert_called_once()


class FreshOwnershipTests(OwnershipFixture):
    def test_reads_same_published_token_without_rotation_or_write(self):
        before = self.claim_row()
        with patch.object(ownership, "probe", return_value=(True, "fixture")) as probe:
            receipt = self.receipt()
        probe.assert_called_once_with("descriptor", "owned.example", self.token)
        self.assertEqual(self.claim_row(), before)
        self.assertNotIn(self.token, repr(receipt))
        with db.writer(immediate=True) as conn:
            ownership.assert_fresh_ownership(conn, receipt, self.uid, "owned.example")

    def test_single_probe_failure_never_revokes_or_changes_failure_count(self):
        before = self.claim_row()
        with patch.object(ownership, "probe", return_value=(False, self.token)):
            with self.assertRaises(ownership.OwnershipBlocked) as caught:
                self.receipt()
        self.assertEqual(caught.exception.code, "control_unavailable")
        self.assertNotIn(self.token, str(caught.exception))
        self.assertEqual(self.claim_row(), before)

    def test_other_account_is_refused_before_network(self):
        with patch.object(ownership, "probe") as probe:
            with self.assertRaises(ownership.OwnershipBlocked):
                self.receipt(self.uid + 1)
        probe.assert_not_called()

    def test_scoped_shared_or_disabled_claim_is_refused(self):
        for change in ("UPDATE domain_ownership SET scope_path='/tenant'",
                       "UPDATE users SET status='disabled'"):
            with self.subTest(change=change), db.writer() as conn:
                conn.execute(change)
            with patch.object(ownership, "probe") as probe:
                with self.assertRaises(ownership.OwnershipBlocked):
                    self.receipt()
            probe.assert_not_called()
            with db.writer() as conn:
                conn.execute("UPDATE domain_ownership SET scope_path=NULL")
                conn.execute("UPDATE users SET status='active'")
        with patch.object(db, "is_shared_host", return_value=True), patch.object(ownership, "probe") as probe:
            with self.assertRaises(ownership.OwnershipBlocked):
                self.receipt()
        probe.assert_not_called()

    def test_revocation_after_probe_is_rechecked_inside_writer(self):
        with patch.object(ownership, "probe", return_value=(True, "fixture")):
            receipt = self.receipt()
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
        with db.writer(immediate=True) as conn:
            with self.assertRaises(ownership.OwnershipBlocked):
                ownership.assert_fresh_ownership(conn, receipt, self.uid, "owned.example")

    def test_token_change_after_probe_is_refused(self):
        with patch.object(ownership, "probe", return_value=(True, "fixture")):
            receipt = self.receipt()
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET token=?,token_hash=? WHERE id=?",
                         ("new-fixture-token", ownership.token_hash("new-fixture-token"), self.claim))
        with db.writer(immediate=True) as conn:
            with self.assertRaises(ownership.OwnershipBlocked):
                ownership.assert_fresh_ownership(conn, receipt, self.uid, "owned.example")

    def test_stale_future_wrong_host_and_wrong_account_receipts_are_refused(self):
        with patch.object(ownership, "probe", return_value=(True, "fixture")), \
                patch.object(ownership.time, "time", return_value=1000):
            receipt = self.receipt()
        for altered, user_id, host in (
                (replace(receipt, checked_at=900), self.uid, "owned.example"),
                (replace(receipt, checked_at=1001), self.uid, "owned.example"),
                (receipt, self.uid, "other.example"),
                (receipt, self.uid + 1, "owned.example")):
            with self.subTest(host=host, user_id=user_id), db.writer(immediate=True) as conn, \
                    patch.object(ownership.time, "time", return_value=1000):
                with self.assertRaises(ownership.OwnershipBlocked):
                    ownership.assert_fresh_ownership(conn, altered, user_id, host)


class OwnerLifecycleTests(OwnershipFixture):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            db.set_meta(conn, "owner_lifecycle_enabled", "1")
            _, self.lid = db.upsert_service(conn, {
                "slug": "owned-lifecycle", "name": "Owned lifecycle", "source": "fixture",
                "source_id": "owned-lifecycle", "url": "https://owned.example/ping",
                "health": "ok", "quality_score": 90, "tx_30d": 7})
            conn.execute("UPDATE services SET owner_verified=1 WHERE id=?", (self.lid,))
        self.enterContext(patch.object(ownership, "probe", return_value=(True, "fixture")))
        self.request_id = "1234567890abcdef1234567890abcdef"

    def state(self):
        with closing(db.connect(read_only=True)) as conn:
            return lifecycle.state(conn, "x402", "owned-lifecycle", self.uid)

    def test_withdrawal_keeps_snapshot_and_blocks_old_url_resurrection(self):
        result = lifecycle.retire("x402", "owned-lifecycle", self.uid,
                                  self.state()["version"], self.request_id)
        self.assertEqual(result["status"], "retired")
        self.assertEqual(self.state()["status"], "retired")
        with closing(db.connect(read_only=True)) as conn:
            retired = db.get_retired_listing(conn, "x402", "owned-lifecycle")
            self.assertEqual(retired["snapshot"]["row"]["tx_30d"], 7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 0)
        with self.assertRaises(db.RetiredListingError), db.writer() as conn:
            db.upsert_service(conn, {"slug": "resurrection", "name": "Resurrection",
                                    "url": "https://owned.example/ping", "source": "other"})
        with self.assertRaises(db.RetiredListingError), db.writer() as conn:
            db.upsert_service(conn, {"slug": "moved-resurrection", "name": "Resurrection",
                "url": "https://owned.example/moved", "source": "fixture", "source_id": "owned-lifecycle"})

    def test_replay_is_idempotent(self):
        version = self.state()["version"]
        result = lifecycle.retire("x402", "owned-lifecycle", self.uid, version, self.request_id)
        self.assertEqual(lifecycle.retire("x402", "owned-lifecycle", self.uid, version,
                                         self.request_id), result)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listing_retirement_events").fetchone()[0], 1)

    def test_stale_listing_version_refuses_without_retirement(self):
        version = self.state()["version"]
        with db.writer() as conn:
            conn.execute("UPDATE services SET description='changed' WHERE id=?", (self.lid,))
        with self.assertRaises(lifecycle.LifecycleBlocked):
            lifecycle.retire("x402", "owned-lifecycle", self.uid, version, self.request_id)
        self.assertEqual(self.state()["status"], "active")

    def test_unowned_protocol_alias_cannot_be_tombstoned(self):
        with db.writer() as conn:
            conn.execute("UPDATE services SET mcp_url='https://other.example/mcp' WHERE id=?", (self.lid,))
        with self.assertRaises(ownership.OwnershipBlocked):
            lifecycle.retire("x402", "owned-lifecycle", self.uid,
                             self.state()["version"], self.request_id)

    def test_shared_endpoint_requires_staff_review(self):
        with db.writer() as conn:
            db.upsert_mcp_server(conn, {"slug": "protocol-peer", "name": "Peer",
                                       "endpoint_url": "https://owned.example/ping", "source": "fixture"})
        with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
            lifecycle.retire("x402", "owned-lifecycle", self.uid,
                             self.state()["version"], self.request_id)
        self.assertEqual(caught.exception.code, "shared_endpoint")

    def test_staff_retirement_cannot_be_restored_through_owner_entry(self):
        with db.writer() as conn:
            db.retire_listing(conn, "x402", "owned-lifecycle", "staff decision", "staff")
        with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
            self.state()
        self.assertEqual(caught.exception.code, "staff_review_required")

    def test_migration_of_unverified_old_host_is_only_a_review_request(self):
        with db.writer() as conn:
            _, old_id = db.upsert_service(conn, {"slug": "old-lifecycle", "name": "Old",
                "url": "https://old.example/ping", "source": "old", "quality_score": 20, "tx_30d": 99})
        result = lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
                                            "old-lifecycle", self.state()["version"], self.request_id)
        self.assertEqual(result["status"], "pending")
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT tx_30d FROM services WHERE id=?", (old_id,)).fetchone()[0], 99)
            self.assertEqual(conn.execute("SELECT tx_30d FROM services WHERE id=?", (self.lid,)).fetchone()[0], 7)
        again = lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
                                            "old-lifecycle", self.state()["version"], self.request_id)
        self.assertEqual(again, result)

    def test_migration_requests_are_removed_with_their_deleted_source(self):
        with db.writer() as conn:
            _, old_id = db.upsert_service(conn, {"slug": "old-lifecycle", "name": "Old",
                                               "url": "https://old.example/ping", "source": "old"})
        lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
                                    "old-lifecycle", self.state()["version"], self.request_id)
        with closing(db.connect(read_only=True)) as conn:
            impact = db._privacy_impact_plan(conn, db._KIND_TABLE, {"x402": [old_id], "mcp": [], "a2a": []},
                                            {"old.example"}, {"old-lifecycle"}, [], [])
            self.assertTrue(impact["rows"]["owner_lifecycle_requests"])
        with db.writer() as conn:
            conn.execute("DELETE FROM services WHERE id=?", (old_id,))
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_lifecycle_requests").fetchone()[0], 0)

    def retire_fixture(self):
        lifecycle.retire("x402", "owned-lifecycle", self.uid, self.state()["version"], self.request_id)
        return self.state()["version"]

    def restoration_observation(self):
        with closing(db.connect(read_only=True)) as conn:
            row = db.get_retired_listing(conn, "x402", "owned-lifecycle")["snapshot"]["row"]
        declaration, _changed = lifecycle.owner_declarations.prepare(row, {"method": "GET"})
        checked = int(time.time())
        payment = {"network": "eip155:8453", "networks": ["eip155:8453"], "amount_raw": "1000",
                   "currency": "USDC", "max_amount_usdc": 0.001, "asset": "fixture", "pay_to": "fixture"}
        validation = {"status": "verified", "checked_at": checked, "method": "GET", "payment": payment,
                      "latency_ms": 12, "well_known_url": None, "openapi_url": None, "resource_count": 1,
                      "resource_samples": [{"url": row["url"], "method": "GET"}]}
        return {"checked_at": checked, "declaration": declaration, "validation": validation}

    def test_dual_scope_owner_restores_with_fresh_domain_authority(self):
        with db.writer() as conn:
            conn.execute("INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,token,"
                         "status,created_at,verified_at,last_checked) "
                         "VALUES(?,'owned.example','/ping','url_descriptor',?,?,'verified',1,1,1)",
                         (self.uid, ownership.token_hash(self.token), self.token))
        version = self.retire_fixture()
        with patch.object(lifecycle, 'observe_restore', return_value=self.restoration_observation()):
            result = lifecycle.restore('x402', 'owned-lifecycle', self.uid, version,
                                       '2234567890abcdef1234567890abcdef')
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.state()['status'], 'active')

    def test_url_only_owner_still_cannot_restore_without_domain_authority(self):
        version = self.retire_fixture()
        with db.writer() as conn:
            conn.execute("INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,"
                         "status,created_at,verified_at) "
                         "VALUES(?,'owned.example','/ping','url_descriptor','fixture','verified',1,1)", (self.uid,))
            conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
        with patch.object(lifecycle, 'observe_restore') as observer:
            with self.assertRaises(ownership.OwnershipBlocked):
                lifecycle.restore('x402', 'owned-lifecycle', self.uid, version,
                                  '2234567890abcdef1234567890abcdef')
            observer.assert_not_called()
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNotNone(db.get_retired_listing(conn, 'x402', 'owned-lifecycle'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM services').fetchone()[0], 0)

    def test_restore_does_not_override_another_accounts_url_claim(self):
        version = self.retire_fixture()
        with db.writer() as conn:
            other_uid = conn.execute("INSERT INTO users(provider,provider_uid,login,status,created_at) "
                                     "VALUES('github','restore-other','restore-other','active',1)").lastrowid
            conn.execute("INSERT INTO domain_ownership(user_id,host,scope_path,method,token_hash,"
                         "status,created_at,verified_at) "
                         "VALUES(?,'owned.example','/ping','url_descriptor','fixture','verified',1,1)", (other_uid,))
        with patch.object(lifecycle, 'observe_restore', return_value=self.restoration_observation()):
            with self.assertRaises(lifecycle.LifecycleBlocked):
                lifecycle.restore('x402', 'owned-lifecycle', self.uid, version,
                                  '2234567890abcdef1234567890abcdef')
        self.assertEqual(self.state()['status'], 'retired')

    def test_restore_keeps_historical_snapshot_but_not_current_reputation(self):
        version = self.retire_fixture()
        observation = self.restoration_observation()
        with patch.object(lifecycle, "observe_restore", return_value=observation):
            result = lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                       "2234567890abcdef1234567890abcdef")
        self.assertEqual(result["status"], "restored")
        with closing(db.connect(read_only=True)) as conn:
            current = dict(conn.execute("SELECT * FROM services WHERE slug='owned-lifecycle'").fetchone())
            self.assertNotEqual(current["id"], self.lid)
            self.assertIsNone(current["quality_score"])
            self.assertIsNone(current["tx_30d"])
            self.assertEqual(current["health"], "ok")
            self.assertEqual(current["price_min"], 0.001)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM service_paytos").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM health_history").fetchone()[0], 1)

    def test_failed_service_verification_keeps_listing_retired(self):
        version = self.retire_fixture()
        with patch.object(lifecycle, "observe_restore", side_effect=lifecycle.LifecycleBlocked("service_unverified")):
            with self.assertRaises(lifecycle.LifecycleBlocked):
                lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                  "2234567890abcdef1234567890abcdef")
        self.assertEqual(self.state()["status"], "retired")

    def test_restore_writer_failure_rolls_back_snapshot_and_active_row(self):
        version = self.retire_fixture()
        with patch.object(lifecycle, "observe_restore", return_value=self.restoration_observation()), \
                patch.object(lifecycle.owner_declarations, "apply_validation", side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                  "2234567890abcdef1234567890abcdef")
        self.assertEqual(self.state()["status"], "retired")
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 0)

    def test_restore_checks_revocation_after_service_observation(self):
        version = self.retire_fixture()
        observation = self.restoration_observation()
        def revoke(*args):
            with db.writer() as conn:
                conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
            return observation
        with patch.object(lifecycle, "observe_restore", side_effect=revoke):
            with self.assertRaises(ownership.OwnershipBlocked):
                lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                  "2234567890abcdef1234567890abcdef")
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNotNone(db.get_retired_listing(conn, "x402", "owned-lifecycle"))

    def client(self):
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        return TestClient(app)

    def test_api_requires_auth_and_explicit_confirmation(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/v1/listings/x402/owned-lifecycle/lifecycle").status_code, 401)
            with patch.object(auth, "current_user", return_value={"id": self.uid}):
                response = client.post("/api/v1/listings/x402/owned-lifecycle/retire", json={
                    "version": self.state()["version"], "confirm": False},
                    headers={"Idempotency-Key": self.request_id})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state()["status"], "active")

    def test_api_retirement_and_owner_state_are_private(self):
        with self.client() as client, patch.object(auth, "current_user", return_value={"id": self.uid}):
            response = client.get("/api/v1/listings/x402/owned-lifecycle/lifecycle")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            response = client.post("/api/v1/listings/x402/owned-lifecycle/retire", json={
                "version": response.json()["version"], "confirm": True},
                headers={"Idempotency-Key": self.request_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            with closing(db.connect(read_only=True)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM internal_notices WHERE category='owner_lifecycle'").fetchone()[0], 1)

    def test_cookie_api_requires_exact_origin_and_csrf(self):
        with patch.dict("os.environ", {"SESSION_SECRET": "offline-session-fixture", "PUBLIC_BASE_URL": "https://agent-tools.cloud"}), \
                self.client() as client, patch.object(auth, "current_user", return_value={"id": self.uid}):
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": self.uid}, 600))
            state = client.get("/api/v1/listings/x402/owned-lifecycle/lifecycle").json()
            for headers in ({"Origin": "https://attacker.example"}, {"Origin": "https://agent-tools.cloud"}):
                response = client.post("/api/v1/listings/x402/owned-lifecycle/retire",
                    json={"version": state["version"], "confirm": True},
                    headers={"Idempotency-Key": self.request_id, **headers})
                self.assertEqual(response.status_code, 403, response.text)
            response = client.post("/api/v1/listings/x402/owned-lifecycle/retire",
                json={"version": state["version"], "confirm": True}, headers={
                "Idempotency-Key": self.request_id, "Origin": "https://agent-tools.cloud", "X-CSRF-Token": state["csrf_token"]})
            self.assertEqual(response.status_code, 200, response.text)

    def test_manage_page_escapes_name_and_provides_signed_confirmation(self):
        with db.writer() as conn:
            conn.execute("UPDATE services SET name='<script>fixture</script>' WHERE id=?", (self.lid,))
        with patch.dict("os.environ", {"SESSION_SECRET": "offline-session-fixture"}), self.client() as client, \
                patch.object(auth, "current_user", return_value={"id": self.uid}):
            response = client.get("/listings/x402/owned-lifecycle/manage")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("&lt;script&gt;fixture&lt;/script&gt;", response.text)
            self.assertIn('name="csrf_token"', response.text)
            self.assertIn('name="confirm"', response.text)
            self.assertIn("private, no-store", response.headers["cache-control"])

    def test_account_lists_active_and_owner_withdrawn_entries(self):
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(lifecycle.managed_listings(conn, self.uid)[0]["status"], "active")
        self.retire_fixture()
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(lifecycle.managed_listings(conn, self.uid)[0]["status"], "retired")

    def test_bearer_key_real_authorization_and_revocation(self):
        with db.writer() as conn:
            key = db.create_api_key(conn, self.uid, name="offline-lifecycle-fixture")
            stranger = conn.execute("INSERT INTO users(provider,provider_uid,status,created_at) "
                "VALUES('domain','stranger-lifecycle','active',1)").lastrowid
            stranger_key = db.create_api_key(conn, stranger, name="offline-stranger")
        with self.client() as client:
            base = "/api/v1/listings/x402/owned-lifecycle"
            self.assertEqual(client.get(base + "/lifecycle", headers={
                "Authorization": "Bearer " + stranger_key}).status_code, 403)
            state = client.get(base + "/lifecycle", headers={"Authorization": "Bearer " + key})
            self.assertEqual(state.status_code, 200, state.text)
            with db.writer() as conn:
                entry = db.api_key_row(conn, key)
                db.revoke_api_key(conn, entry["id"], self.uid)
            response = client.post(base + "/retire", json={"version": state.json()["version"], "confirm": True},
                headers={"Authorization": "Bearer " + key, "Idempotency-Key": self.request_id})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.state()["status"], "active")

    def test_api_rejects_missing_key_wrong_key_reuse_and_extra_fields(self):
        with self.client() as client, patch.object(auth, "current_user", return_value={"id": self.uid}):
            base = "/api/v1/listings/x402/owned-lifecycle"
            payload = {"version": self.state()["version"], "confirm": True}
            self.assertEqual(client.post(base + "/retire", json=payload).status_code, 422)
            self.assertEqual(client.post(base + "/retire", json={**payload, "permanent": True}).status_code, 422)
            response = client.post(base + "/retire", json=payload, headers={"Idempotency-Key": self.request_id})
            self.assertEqual(response.status_code, 200, response.text)
            with patch.object(auth.limits, "check_ip_limits", return_value=None):
                repeated = client.post(base + "/retire", json=payload, headers={"Idempotency-Key": self.request_id})
                self.assertEqual(repeated.json(), response.json())

    def test_notice_failure_rolls_back_withdrawal(self):
        from directory import notices
        with patch.object(notices, "record_lifecycle", side_effect=ValueError("offline notice failure")):
            with self.assertRaises(ValueError):
                lifecycle.retire("x402", "owned-lifecycle", self.uid, self.state()["version"], self.request_id)
        self.assertEqual(self.state()["status"], "active")

    def test_supplemental_binding_identity_is_blocked_after_withdrawal(self):
        with db.writer() as conn:
            conn.execute("INSERT INTO wellknown_endpoint_sources(kind,identity_hash,endpoint_hash,source_id,"
                         "listing_id,first_seen,last_seen) VALUES('x402','fixture','fixture','supplemental',?,1,1)", (self.lid,))
        self.retire_fixture()
        with closing(db.connect(read_only=True)) as conn:
            retired = db.get_retired_listing(conn, "x402", "owned-lifecycle")
            self.assertEqual(retired["snapshot"]["source_history"]["wellknown_endpoint_sources"][0]["source_id"], "supplemental")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wellknown_endpoint_sources").fetchone()[0], 0)
        with self.assertRaises(db.RetiredListingError), db.writer() as conn:
            db.assert_listing_not_retired(conn, "x402", {"source": "wellknown", "source_id": "supplemental"})

    def test_staff_can_list_and_close_request_without_touching_listings(self):
        with db.writer() as conn:
            db.upsert_service(conn, {"slug": "old-lifecycle", "name": "Old", "url": "https://old.example/ping", "source": "old"})
        lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
                                    "old-lifecycle", self.state()["version"], self.request_id)
        with closing(db.connect(read_only=True)) as conn:
            entry = lifecycle.review_requests(conn)[0]
        self.assertNotIn("user_id", entry)
        with db.writer(immediate=True) as conn:
            lifecycle.close_review(conn, entry["request_id"], entry["review_version"], "declined")
        with closing(db.connect(read_only=True)) as conn:
            self.assertFalse(lifecycle.review_requests(conn))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 2)

    def test_rate_limit_denies_before_network(self):
        with self.client() as client, patch.object(auth, "current_user", return_value={"id": self.uid}), \
                patch.object(auth.limits, "check_ip_limits", return_value={"retry_after": 60, "window": "minute", "limit": 2}), \
                patch.object(ownership, "probe") as probe:
            response = client.post("/api/v1/listings/x402/owned-lifecycle/retire", json={
                "version": self.state()["version"], "confirm": True}, headers={"Idempotency-Key": self.request_id})
            self.assertEqual(response.status_code, 429, response.text)
            probe.assert_not_called()

    def test_missing_token_blocks_restore_before_any_service_request(self):
        version = self.retire_fixture()
        with patch.object(ownership, "probe", return_value=(False, "fixture")), \
                patch.object(lifecycle, "observe_restore") as observe:
            with self.assertRaises(ownership.OwnershipBlocked):
                lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                  "2234567890abcdef1234567890abcdef")
        observe.assert_not_called()

    def test_actual_x402_restore_uses_only_unpaid_primary_challenge(self):
        version = self.retire_fixture()
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            self.assertNotIn("payment-signature", request.headers)
            self.assertNotIn("authorization", request.headers)
            if request.url.path in ("/.well-known/x402", "/openapi.json"):
                return httpx.Response(404)
            self.assertEqual(request.url.path, "/ping")
            return httpx.Response(402, json={"x402Version": 2,
                "resource": {"url": "https://owned.example/ping"}, "accepts": [{
                "scheme": "exact", "network": "eip155:8453", "amount": "1000",
                "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "payTo": "0x1111111111111111111111111111111111111111"}]})
        with patch.object(lifecycle.public_http, "client", side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)):
            lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                              "2234567890abcdef1234567890abcdef")
        self.assertEqual(calls[-1], ("GET", "/ping"))
        self.assertEqual(len(calls), 3)

    def test_actual_mcp_restore_sends_initialize_only(self):
        with db.writer() as conn:
            _, listing_id = db.upsert_mcp_server(conn, {"slug": "mcp-lifecycle", "name": "MCP fixture",
                "endpoint_url": "https://owned.example/mcp", "source": "mcp-fixture",
                "quality_score": 90, "tool_count": 7})
        with closing(db.connect(read_only=True)) as conn:
            version = lifecycle.state(conn, "mcp", "mcp-lifecycle", self.uid)["version"]
        lifecycle.retire("mcp", "mcp-lifecycle", self.uid, version, self.request_id)
        with closing(db.connect(read_only=True)) as conn:
            version = lifecycle.state(conn, "mcp", "mcp-lifecycle", self.uid)["version"]
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload["method"])
            self.assertEqual(payload["method"], "initialize")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "Fixture"}}})
        with patch.object(lifecycle.public_http, "client", side_effect=lambda **kwargs:
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)):
            lifecycle.restore("mcp", "mcp-lifecycle", self.uid, version,
                              "2234567890abcdef1234567890abcdef")
        self.assertEqual(calls, ["initialize"])
        with closing(db.connect(read_only=True)) as conn:
            row = dict(conn.execute("SELECT * FROM mcp_servers WHERE slug='mcp-lifecycle'").fetchone())
        self.assertNotEqual(row["id"], listing_id)
        self.assertIsNone(row["tool_count"])
        self.assertIsNone(row["quality_score"])
        self.assertEqual(row["health"], "ok")

    def test_actual_a2a_restore_requires_card_bound_to_current_endpoint(self):
        with db.writer() as conn:
            db.upsert_a2a_agent(conn, {"slug": "a2a-lifecycle", "name": "A2A fixture",
                "endpoint_url": "https://owned.example/a2a", "card_url": "https://owned.example/card.json",
                "source": "a2a-fixture", "quality_score": 90})
        with closing(db.connect(read_only=True)) as conn:
            version = lifecycle.state(conn, "a2a", "a2a-lifecycle", self.uid)["version"]
        lifecycle.retire("a2a", "a2a-lifecycle", self.uid, version, self.request_id)
        with closing(db.connect(read_only=True)) as conn:
            version = lifecycle.state(conn, "a2a", "a2a-lifecycle", self.uid)["version"]
        for target in ("https://foreign.example/a2a", "https://owned.example/a2a"):
            def handler(request):
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.path, "/card.json")
                return httpx.Response(200, json={"name": "Fixture", "url": target,
                                                "skills": [{"id": "fixture"}]})
            with patch.object(lifecycle.public_http, "client", side_effect=lambda **kwargs:
                    httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)):
                if "foreign" in target:
                    with self.assertRaises(lifecycle.LifecycleBlocked):
                        lifecycle.restore("a2a", "a2a-lifecycle", self.uid, version,
                                          "2234567890abcdef1234567890abcdef")
                else:
                    lifecycle.restore("a2a", "a2a-lifecycle", self.uid, version,
                                      "2234567890abcdef1234567890abcdef")
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(lifecycle.state(conn, "a2a", "a2a-lifecycle", self.uid)["status"], "active")

    def test_catalogue_revision_invalidates_both_worker_caches(self):
        with patch.object(routes, "_home_cache", {"at": 0.0, "payload": None}), \
                patch.object(routes, "_category_cache", {}):
            before = routes._home_payload()
            self.assertEqual(before["stats"]["total"], 1)
            categories = routes._category_rows("x402")
            self.retire_fixture()
            after = routes._home_payload()
            self.assertEqual(after["stats"]["total"], 0)
            self.assertIsNot(routes._category_rows("x402"), categories)

    def test_owned_detail_and_retirement_never_request_shared_cache(self):
        with self.client() as client:
            response = client.get("/services/owned-lifecycle")
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(response.headers["cloudflare-cdn-cache-control"], "no-store")
            self.retire_fixture()
            response = client.get("/services/owned-lifecycle")
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_post_restore_method_comes_from_saved_call_metadata(self):
        with db.writer() as conn:
            conn.execute("UPDATE services SET resource_samples=? WHERE id=?", (json.dumps([
                {"url": "https://owned.example/ping", "method": "POST", "probe_body": {}}]), self.lid))
        self.retire_fixture()
        with closing(db.connect(read_only=True)) as conn:
            row = db.get_retired_listing(conn, "x402", "owned-lifecycle")["snapshot"]["row"]
        def observe(current):
            self.assertEqual(lifecycle.owner_declarations.stored(current)["fields"]["method"], "POST")
            return {"status": "blocked"}
        with patch.object(lifecycle.owner_declarations, "observe", side_effect=observe):
            with self.assertRaises(lifecycle.LifecycleBlocked):
                lifecycle.observe_restore("x402", row)

    def test_claim_takeover_cannot_restore_previous_operators_withdrawal(self):
        version = self.retire_fixture()
        with db.writer() as conn:
            stranger = conn.execute("INSERT INTO users(provider,provider_uid,status,created_at) "
                "VALUES('domain','new-domain-controller','active',1)").lastrowid
            conn.execute("UPDATE domain_ownership SET user_id=? WHERE id=?", (stranger, self.claim))
        with patch.object(lifecycle, "observe_restore") as observe:
            with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
                lifecycle.restore("x402", "owned-lifecycle", stranger, version,
                                  "2234567890abcdef1234567890abcdef")
        self.assertEqual(caught.exception.code, "staff_review_required")
        observe.assert_not_called()

    def test_privacy_deletion_removes_owner_audit_and_source_keys(self):
        self.retire_fixture()
        with db.writer() as conn:
            retirement = db.get_retired_listing(conn, "x402", "owned-lifecycle")
            conn.execute("DELETE FROM listing_retirement_events WHERE retirement_id=?", (retirement["id"],))
            conn.execute("DELETE FROM retired_listing_urls WHERE retirement_id=?", (retirement["id"],))
            conn.execute("DELETE FROM retired_listings WHERE id=?", (retirement["id"],))
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_lifecycle_requests").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_retired_sources").fetchone()[0], 0)

    def test_restore_preserves_snapshot_bytes_and_can_be_replayed(self):
        version = self.retire_fixture()
        with closing(db.connect(read_only=True)) as conn:
            snapshot = conn.execute("SELECT snapshot FROM retired_listings").fetchone()[0]
        request_id = "2234567890abcdef1234567890abcdef"
        with patch.object(lifecycle, "observe_restore", return_value=self.restoration_observation()) as observe:
            result = lifecycle.restore("x402", "owned-lifecycle", self.uid, version, request_id)
            self.assertEqual(lifecycle.restore("x402", "owned-lifecycle", self.uid, version, request_id), result)
        observe.assert_called_once()
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT snapshot FROM retired_listings").fetchone()[0], snapshot)

    def test_migration_replay_survives_source_metadata_refresh(self):
        with db.writer() as conn:
            _, source_id = db.upsert_service(conn, {"slug": "old-lifecycle", "name": "Old", "source": "old",
                                                  "url": "https://old.example/ping"})
        version = self.state()["version"]
        result = lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
                                            "old-lifecycle", version, self.request_id)
        with db.writer() as conn:
            conn.execute("UPDATE services SET health='down' WHERE id=?", (source_id,))
        self.assertEqual(lifecycle.request_migration("x402", "owned-lifecycle", self.uid,
            "old-lifecycle", version, self.request_id), result)

    def test_web_withdrawal_and_restore_flow_uses_persistent_marker(self):
        import re
        with patch.dict("os.environ", {"SESSION_SECRET": "offline-session-fixture"}), self.client() as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": self.uid}, 600))
            path = "/listings/x402/owned-lifecycle/manage"
            page = client.get(path)
            self.assertEqual(page.status_code, 200, page.text)
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page.text))
            fields.update(action="retire", confirm="yes")
            response = client.post(path, data=fields, headers={"Origin": "https://agent-tools.cloud"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Restore listing", response.text)
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text))
            fields.update(action="restore", confirm="yes")
            with patch.object(lifecycle, "observe_restore", return_value=self.restoration_observation()):
                response = client.post(path, data=fields, headers={"Origin": "https://agent-tools.cloud"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.state()["status"], "active")
            self.assertIn("Withdraw listing", response.text)

    def test_disabled_release_gate_denies_before_network(self):
        with db.writer() as conn:
            db.set_meta(conn, "owner_lifecycle_enabled", "0")
        with patch.object(ownership, "probe") as probe:
            with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
                lifecycle.retire("x402", "owned-lifecycle", self.uid, self.state()["version"], self.request_id)
        self.assertEqual(caught.exception.code, "lifecycle_unavailable")
        probe.assert_not_called()

    def test_key_revoked_during_token_read_is_refused_inside_writer(self):
        with db.writer() as conn:
            key = db.create_api_key(conn, self.uid, name="offline-inflight-key")
            key_id = db.api_key_row(conn, key)["id"]
        def revoke(*args):
            with db.writer() as conn:
                db.revoke_api_key(conn, key_id, self.uid)
            return True, "fixture"
        with self.client() as client, patch.object(ownership, "probe", side_effect=revoke):
            response = client.post("/api/v1/listings/x402/owned-lifecycle/retire", json={
                "version": self.state()["version"], "confirm": True}, headers={
                "Authorization": "Bearer " + key, "Idempotency-Key": self.request_id})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.state()["status"], "active")

    def test_reused_original_id_does_not_hide_an_endpoint_collision(self):
        version = self.retire_fixture()
        with db.writer() as conn:
            conn.execute("INSERT INTO services(id,slug,name,url,source,source_id,created_at,updated_at) "
                         "VALUES(?,'legacy-collision','Collision','https://owned.example/ping',"
                         "'legacy','collision',1,1)", (self.lid,))
        with patch.object(lifecycle, "observe_restore", return_value=self.restoration_observation()):
            with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
                lifecycle.restore("x402", "owned-lifecycle", self.uid, version,
                                  "2234567890abcdef1234567890abcdef")
        self.assertEqual(caught.exception.code, "shared_endpoint")
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNotNone(db.get_retired_listing(conn, "x402", "owned-lifecycle"))


class OwnerMergeTests(OwnershipFixture):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            db.set_meta(conn, "owner_lifecycle_enabled", "1")
            _, self.lid = db.upsert_service(conn, {
                "slug": "owned-lifecycle", "name": "Owned lifecycle", "source": "fixture",
                "source_id": "owned-lifecycle", "url": "https://owned.example/ping",
                "health": "ok", "quality_score": 90, "tx_30d": 7})
            conn.execute("UPDATE services SET owner_verified=1 WHERE id=?", (self.lid,))
            self.old_claim = conn.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,token,"
                "status,created_at,verified_at) VALUES(?,'old.example','descriptor',?,?,'verified',1,1)",
                (self.uid, ownership.token_hash("old-fixture-marker"), "old-fixture-marker")).lastrowid
            _, self.old_id = db.upsert_service(conn, {"slug": "old-owned", "name": "Old fixture",
                "url": "https://old.example/ping", "source": "old-fixture", "source_id": "old-owned",
                "quality_score": 20, "tx_30d": 99})
            conn.execute("UPDATE services SET owner_verified=1 WHERE id=?", (self.old_id,))
        self.request_id = "1234567890abcdef1234567890abcdef"
        self.enterContext(patch.object(ownership, "probe", return_value=(True, "fixture")))
        self.observation = {"checked_at": int(time.time())}
        self.enterContext(patch.object(lifecycle, "observe_restore", return_value=self.observation))

    def state(self):
        with closing(db.connect(read_only=True)) as conn:
            return lifecycle.state(conn, "x402", "owned-lifecycle", self.uid)

    def versions(self):
        with closing(db.connect(read_only=True)) as conn:
            return (lifecycle.state(conn, "x402", "old-owned", self.uid)["version"], self.state()["version"])

    def merge(self, versions=None):
        source_version, target_version = versions or self.versions()
        return lifecycle.merge("x402", "old-owned", self.uid, source_version, self.request_id,
                               "owned-lifecycle", target_version)

    def test_owned_merge_retires_old_identity_and_does_not_change_primary(self):
        with closing(db.connect(read_only=True)) as conn:
            before = dict(conn.execute("SELECT * FROM services WHERE id=?", (self.lid,)).fetchone())
        versions = self.versions()
        result = self.merge(versions)
        self.assertEqual(result["status"], "merged")
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM services WHERE id=?", (self.lid,)).fetchone()), before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 1)
            self.assertEqual(db.get_retired_listing(conn, "x402", "old-owned")["snapshot"]["row"]["tx_30d"], 99)
        self.assertEqual(self.merge(versions), result)
        with self.assertRaises(db.RetiredListingError), db.writer() as conn:
            db.upsert_service(conn, {"slug": "moved-old", "name": "Old", "source": "old-fixture",
                                    "source_id": "old-owned", "url": "https://old.example/moved"})

    def test_both_domains_are_rechecked_with_their_existing_markers(self):
        with patch.object(ownership, "probe", return_value=(True, "fixture")) as probe:
            self.merge()
        self.assertEqual({call.args for call in probe.call_args_list}, {
            ("descriptor", "old.example", "old-fixture-marker"),
            ("descriptor", "owned.example", self.token)})

    def test_unverified_source_cannot_be_merged_by_the_target_owner(self):
        with db.writer() as conn:
            conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.old_claim,))
        with patch.object(lifecycle, "observe_restore") as observe:
            with self.assertRaises(ownership.OwnershipBlocked):
                self.merge(("a" * 64, self.state()["version"]))
        observe.assert_not_called()

    def test_target_claim_revoked_during_verification_refuses_atomic_merge(self):
        versions = self.versions()
        def revoke(*args):
            with db.writer() as conn:
                conn.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim,))
            return self.observation
        with patch.object(lifecycle, "observe_restore", side_effect=revoke), self.assertRaises(ownership.OwnershipBlocked):
            self.merge(versions)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_listing_merges").fetchone()[0], 0)

    def test_target_refresh_during_service_check_refuses_merge(self):
        versions = self.versions()
        def refresh(*args):
            with db.writer() as conn:
                conn.execute("UPDATE services SET description='changed' WHERE id=?", (self.lid,))
            return self.observation
        with patch.object(lifecycle, "observe_restore", side_effect=refresh), self.assertRaises(lifecycle.LifecycleBlocked):
            self.merge(versions)
        with closing(db.connect(read_only=True)) as conn:
            self.assertIsNone(db.get_retired_listing(conn, "x402", "old-owned"))

    def test_merge_notice_failure_rolls_back_old_listing_and_relationship(self):
        from directory import notices
        with patch.object(notices, "record_lifecycle", side_effect=ValueError("offline notice failure")):
            with self.assertRaises(ValueError):
                self.merge()
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM services").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_listing_merges").fetchone()[0], 0)

    def test_canonical_target_with_dependants_cannot_be_withdrawn(self):
        self.merge()
        with self.assertRaises(lifecycle.LifecycleBlocked) as caught:
            lifecycle.retire("x402", "owned-lifecycle", self.uid, self.state()["version"],
                             "3234567890abcdef1234567890abcdef")
        self.assertEqual(caught.exception.code, "merge_target_in_use")

    def test_merge_api_and_retired_detail_point_to_primary(self):
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        with db.writer() as conn:
            key = db.create_api_key(conn, self.uid, name="offline-merge-key")
        headers = {"Authorization": "Bearer " + key, "Idempotency-Key": self.request_id}
        versions = self.versions()
        with TestClient(app) as client:
            response = client.post("/api/v1/listings/x402/old-owned/merge", headers=headers, json={
                "version": versions[0], "target_version": versions[1],
                "target_slug": "owned-lifecycle", "confirm": True})
            self.assertEqual(response.status_code, 200, response.text)
            page = client.get("/services/old-owned")
            self.assertEqual(page.status_code, 410, page.text)
            self.assertIn('href="/services/owned-lifecycle"', page.text)
            self.assertIn('href="/listings/x402/old-owned/manage"', page.text)

    def test_manage_page_lists_only_owned_merge_targets(self):
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        with patch.dict("os.environ", {"SESSION_SECRET": "offline-session-fixture"}), TestClient(app) as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": self.uid}, 600))
            page = client.get("/listings/x402/old-owned/manage")
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn('name="merge_target"', page.text)
            self.assertIn('value="owned-lifecycle:', page.text)

    def test_restore_undoes_merge_without_touching_primary(self):
        self.merge()
        with closing(db.connect(read_only=True)) as conn:
            version = lifecycle.state(conn, "x402", "old-owned", self.uid)["version"]
            before = dict(conn.execute("SELECT * FROM services WHERE id=?", (self.lid,)).fetchone())
        checked = int(time.time())
        observation = {"checked_at": checked, "declaration": {
            "version": 1, "revision": "a" * 32, "status": "pending",
            "primary_url": "https://old.example/ping", "fields": {"method": "GET"}},
            "validation": {"status": "verified", "checked_at": checked, "method": "GET",
                "payment": {"network": "eip155:8453", "currency": "USDC", "max_amount_usdc": 0.001},
                "latency_ms": 10, "well_known_url": None, "openapi_url": None,
                "resource_count": 1, "resource_samples": [{"url": "https://old.example/ping", "method": "GET"}]}}
        with patch.object(lifecycle, "observe_restore", return_value=observation):
            lifecycle.restore("x402", "old-owned", self.uid, version, "2234567890abcdef1234567890abcdef")
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_listing_merges").fetchone()[0], 0)
            self.assertEqual(dict(conn.execute("SELECT * FROM services WHERE id=?", (self.lid,)).fetchone()), before)
            self.assertEqual(lifecycle.state(conn, "x402", "old-owned", self.uid)["status"], "active")

    def test_privacy_impact_contains_merge_and_request_references(self):
        self.merge()
        with closing(db.connect(read_only=True)) as conn:
            impact = db._privacy_impact_plan(conn, db._KIND_TABLE,
                {"x402": [self.lid], "mcp": [], "a2a": []}, {"owned.example"}, {"owned-lifecycle"}, [], [])
            self.assertTrue(impact["rows"]["owner_listing_merges"])
            self.assertTrue(impact["rows"]["owner_lifecycle_requests"])