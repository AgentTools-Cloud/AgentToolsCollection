"""Anonymous MCP authorization discovery: what is checked, only through public_http,
and how the daily health job stores it."""
import json
import os
import sys
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import crawlers, db, jobs, mcp_auth_discovery as D, public_http  # noqa: E402
from test_ingestion_pipeline import TemporaryDatabaseTestCase  # noqa: E402

ENDPOINT = "https://mcp.example.com/mcp"
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
ROOT_PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"
ISSUER = "https://auth.example.com/tenant"
AS_URLS = ("https://auth.example.com/.well-known/oauth-authorization-server/tenant",
           "https://auth.example.com/.well-known/openid-configuration/tenant",
           "https://auth.example.com/tenant/.well-known/openid-configuration")
PRM = {"resource": ENDPOINT, "authorization_servers": [ISSUER]}
AS = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
      "token_endpoint": ISSUER + "/token", "code_challenge_methods_supported": ["S256"],
      "registration_endpoint": ISSUER + "/register"}
CHALLENGE = f'Bearer resource_metadata="{PRM_URL}", scope="mcp"'


class FakeHosts(unittest.TestCase):
    """public_http.client answers from self.routes (URL -> JSON body, else 404)."""

    def setUp(self):
        D._cache.clear()
        self.addCleanup(D._cache.clear)
        self.routes, self.seen, self.clients = {}, [], []

        def factory(**kwargs):
            self.clients.append(kwargs)
            return httpx.Client(transport=httpx.MockTransport(self.handle), **kwargs)

        self.enterContext(patch.object(public_http, "client", side_effect=factory))

    def handle(self, request):
        url = str(request.url)
        self.seen.append((request.method, url))
        body = self.routes.get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, json=body, request=request)


class Checks(FakeHosts):
    def test_spec_compliant_server_passes_every_check(self):
        self.routes.update({PRM_URL: PRM, AS_URLS[0]: AS})
        out = D.check(ENDPOINT, CHALLENGE, 401)
        self.assertEqual({k: out[k] for k in D.CHECKS}, dict.fromkeys(D.CHECKS, True))
        self.assertEqual(out["v"], D.VERSION)
        self.assertEqual(self.seen, [("GET", PRM_URL), ("GET", AS_URLS[0])])
        self.assertEqual(len(self.clients), 1)

    def test_well_known_fallback_parent_resource_and_openid(self):
        self.routes.update({
            ROOT_PRM_URL: {"resource": "https://mcp.example.com",
                           "authorization_servers": [ISSUER]},
            AS_URLS[2]: {**AS, "registration_endpoint": None,
                         "client_id_metadata_document_supported": True}})
        out = D.check(ENDPOINT, 'Bearer realm="mcp"', 401)
        self.assertTrue(out["challenge"] and out["resource_metadata"] and out["server_metadata"])
        self.assertTrue(out["registration"])
        self.assertEqual([u for _, u in self.seen],
                         [PRM_URL, ROOT_PRM_URL, *AS_URLS])

    def test_challenge_needs_401_bearer_and_resource_must_name_endpoint(self):
        self.routes.update({PRM_URL: {**PRM, "resource": "https://other.example.com/mcp"},
                            AS_URLS[0]: AS})
        out = D.check(ENDPOINT, CHALLENGE, 401)
        self.assertTrue(out["challenge"])
        self.assertFalse(out["resource_metadata"])
        self.assertTrue(out["server_metadata"])
        self.assertFalse(D.check(ENDPOINT, 'Basic realm="x"', 401)["challenge"])
        self.assertFalse(D.check(ENDPOINT, CHALLENGE, 403)["challenge"])
        self.assertTrue(D.check(ENDPOINT, 'Basic realm="x", Bearer realm="y"', 401)["challenge"])
        self.assertTrue(D.resource_matches("https://MCP.example.com:443/mcp/", ENDPOINT))
        for other in ("https://mcp.example.com/m", "http://mcp.example.com/mcp",
                      "https://mcp.example.com:8443/mcp", "/mcp", None):
            self.assertFalse(D.resource_matches(other, ENDPOINT), other)

    def test_incomplete_metadata_and_errors_score_nothing(self):
        self.routes.update({PRM_URL: PRM,
                            AS_URLS[0]: {**AS, "token_endpoint": None},
                            AS_URLS[1]: {**AS, "code_challenge_methods_supported": ["plain"],
                                         "registration_endpoint": None}})
        out = D.check(ENDPOINT, CHALLENGE, 401)
        self.assertTrue(out["server_metadata"])
        self.assertFalse(out["pkce"] or out["registration"])
        D._cache.clear()
        self.routes = {PRM_URL: httpx.ConnectError("refused"), ROOT_PRM_URL: ["not", "an", "object"]}
        out = D.check(ENDPOINT, "Bearer", 401)
        self.assertEqual([k for k in D.CHECKS if out[k]], ["challenge"])
        self.assertEqual([u for _, u in self.seen[-2:]], [PRM_URL, ROOT_PRM_URL])

    def test_found_metadata_is_shared_failures_are_not(self):
        other = "https://mcp.example.com/other"
        self.routes.update({PRM_URL: PRM, AS_URLS[0]: AS,
                            ROOT_PRM_URL + "/other": {**PRM, "resource": other}})
        D.check(ENDPOINT, CHALLENGE, 401)
        D.check(other, "Bearer", 401)
        self.assertEqual([u for _, u in self.seen].count(AS_URLS[0]), 1)
        D.check(ENDPOINT, CHALLENGE, 401)
        self.assertEqual([u for _, u in self.seen].count(PRM_URL), 1)
        self.routes.pop(PRM_URL)
        D._cache.clear()
        D.check(ENDPOINT, CHALLENGE, 401)
        D.check(ENDPOINT, CHALLENGE, 401)
        self.assertEqual([u for _, u in self.seen].count(PRM_URL), 3)

    def test_probe_keeps_the_challenge(self):
        def handle(request):
            return httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE}, request=request)
        self.handle = handle
        res = crawlers.probe_mcp_health(ENDPOINT)
        self.assertEqual((res["status"], res["http_status"]), ("ok", 401))
        self.assertEqual(res["www_authenticate"], CHALLENGE)


class PublicOnly(unittest.TestCase):
    def test_metadata_urls_go_through_the_public_only_transport(self):
        D._cache.clear()
        seen = []
        original = public_http.PublicTransport.handle_request

        def spy(transport, request):
            seen.append(str(request.url))
            try:
                return original(transport, request)
            except public_http.UnsafeURL:
                seen.append("blocked")
                raise

        with patch.object(public_http.PublicTransport, "handle_request", spy):
            out = D.check("https://127.0.0.1/mcp",
                          'Bearer resource_metadata="http://169.254.169.254/latest"', 401)
        self.assertEqual([k for k in D.CHECKS if out[k]], ["challenge"])
        self.assertEqual(seen, ["http://169.254.169.254/latest", "blocked"])


class Schedule(unittest.TestCase):
    def test_weekly_on_each_listings_own_day(self):
        now = 20_000 * 86400 + 3600
        self.assertTrue(D.due(None, 5, now))
        self.assertFalse(D.due(now - 3600, 5, now))
        self.assertTrue(D.due(now - 8 * 86400, 5, now))
        slot = next(i for i in range(7) if (now // 86400 + i) % 7 == 0)
        self.assertTrue(D.due(now - 2 * 86400, slot, now))
        self.assertFalse(D.due(now - 2 * 86400, slot + 1, now))
        checked, days = now - 3 * 86400, []
        for d in range(21):
            t = now + d * 86400
            if D.due(checked, slot, t):
                days.append(d)
                checked = t
        self.assertEqual(days, [0, 7, 14])


class HealthJob(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            conn.execute("INSERT INTO mcp_servers(slug, name, source, endpoint_url, created_at, "
                         "updated_at) VALUES(?,?,?,?,?,?)",
                         ("gated", "Gated", "fixture", ENDPOINT, 1, 1))
        self.enterContext(patch.object(jobs, "_wait_for_host"))
        self.status = 401
        self.enterContext(patch.object(jobs.crawlers, "probe_mcp_health", side_effect=self.probe))
        self.check = self.enterContext(patch.object(
            jobs.mcp_auth_discovery, "check", return_value={"challenge": True, "v": 1}))

    def probe(self, url, discover_modern=False):
        return {"status": "ok", "latency_ms": 50, "http_status": self.status,
                "conformance": "fail", "tool_count": None, "tools": None,
                "protocol_version": None, "www_authenticate": CHALLENGE}

    def stored(self):
        return self.query("SELECT auth_discovery_json, auth_discovery_checked FROM mcp_servers")[0]

    def test_read_weekly_and_kept_in_between(self):
        jobs.cmd_health_mcp()
        self.check.assert_called_once_with(ENDPOINT, CHALLENGE, 401)
        first = self.stored()
        self.assertEqual(json.loads(first["auth_discovery_json"]), {"challenge": True, "v": 1})
        self.assertAlmostEqual(first["auth_discovery_checked"], time.time(), delta=60)
        jobs.cmd_health_mcp()
        self.status = 200
        jobs.cmd_health_mcp()
        self.assertEqual(self.check.call_count, 1)
        self.assertEqual(self.stored(), first)
        with closing(db.connect(read_only=True)) as conn:
            row = db.mcp_row_to_dict(conn.execute("SELECT * FROM mcp_servers").fetchone())
        self.assertEqual(row["auth_discovery"], {"challenge": True, "v": 1})
        self.assertNotIn("auth_discovery_json", row)

    def test_a_failed_read_does_not_stop_the_run(self):
        self.check.side_effect = RuntimeError("boom")
        with self.assertLogs(jobs.log, "ERROR"):
            self.assertEqual(jobs.cmd_health_mcp(), 0)
        self.assertEqual(self.query("SELECT health FROM mcp_servers")[0]["health"], "ok")
        self.assertIsNone(self.stored()["auth_discovery_checked"])


if __name__ == "__main__":
    unittest.main()
