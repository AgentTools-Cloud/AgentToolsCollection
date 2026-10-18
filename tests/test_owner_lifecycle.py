"""Owner lifecycle authorization and tiny-database regressions."""

from contextlib import closing
from dataclasses import replace
import json
import time
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import auth, routes
from directory import db, owner_lifecycle as lifecycle, ownership
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