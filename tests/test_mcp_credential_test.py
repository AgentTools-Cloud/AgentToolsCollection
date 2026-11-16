"""MCP credential test: one authenticated initialize + tools/list, never a stored credential."""
import json
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import auth, db, listing_readiness as L, mcp_credential_test as mct, routes
from test_ingestion_pipeline import TemporaryDatabaseTestCase

ENDPOINT = "https://mcp.vendor.example/mcp"
SECRET = "sk_live_offline_fixture_secret_42"
TOOLS = [{"name": "get_skill", "description": "Fetch one skill"},
         {"name": "list_library", "description": ""},
         {"name": "get_skill_file", "description": "Fetch a file"}]


class FakeServer:
    """MockTransport handler for a Streamable HTTP MCP server that wants a credential."""

    def __init__(self, token="Bearer " + SECRET, header="authorization", sse=False,
                 redirect=None, paged=False, tools_error=False, big=False):
        self.token, self.header, self.sse = token, header, sse
        self.redirect, self.paged, self.tools_error, self.big = redirect, paged, tools_error, big
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), dict(request.headers), body))
        if self.redirect and str(request.url) == ENDPOINT:
            return httpx.Response(308, headers={"location": self.redirect})
        if request.headers.get(self.header) != self.token:
            return httpx.Response(401, json={"error": "Authentication required"})
        if request.method == "DELETE":
            return httpx.Response(204)
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return self.reply({"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"}}}, {"mcp-session-id": "s-1"})
        if method == "tools/list":
            if self.tools_error:
                return self.reply({"jsonrpc": "2.0", "id": body["id"],
                                   "error": {"code": -32601, "message": "nope"}})
            cursor = (body.get("params") or {}).get("cursor")
            if self.paged and not cursor:
                result = {"tools": TOOLS[:2], "nextCursor": "p2"}
            elif cursor == "p2":
                result = {"tools": TOOLS[2:]}
            else:
                result = {"tools": TOOLS}
            if self.big:
                result["padding"] = "x" * (mct.MAX_RESPONSE_BYTES + 10)
            return self.reply({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(400)

    def reply(self, msg, headers=None):
        if self.sse:
            text = "event: message\ndata: " + json.dumps(msg) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})},
                                  content=text.encode())
        return httpx.Response(200, json=msg, headers=headers or {})

    def methods(self):
        return [(m, (b or {}).get("method")) for m, _u, _h, b in self.calls]


_CLIENTS = []


def client_for(server):
    client = httpx.Client(transport=httpx.MockTransport(server), follow_redirects=False)
    _CLIENTS.append(client)
    return client


def tearDownModule():
    for client in _CLIENTS:
        client.close()


def bearer():
    return mct.credential_header(None, SECRET)


class ProbeTests(TestCase):
    def test_bearer_handshake_session_and_tools(self):
        server = FakeServer()
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(server))
        self.assertEqual(out["status"], "succeeded", out)
        self.assertEqual((out["protocol_version"], out["tool_count"], out["described_count"]),
                         ("2025-06-18", 3, 2))
        self.assertEqual([t["name"] for t in out["tools"]], ["get_skill", "list_library", "get_skill_file"])
        self.assertEqual(server.methods(), [("POST", "initialize"), ("POST", "notifications/initialized"),
                                            ("POST", "tools/list"), ("DELETE", None)])
        tools_headers = server.calls[2][2]
        self.assertEqual(tools_headers["mcp-session-id"], "s-1")
        self.assertEqual(tools_headers["mcp-protocol-version"], "2025-06-18")
        self.assertNotIn(SECRET, json.dumps(out))

    def test_sse_replies_and_pagination(self):
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(FakeServer(sse=True, paged=True)))
        self.assertEqual((out["status"], out["tool_count"]), ("succeeded", 3))

    def test_custom_header_is_sent_as_is(self):
        name, value = mct.credential_header("X-API-Key", SECRET)
        self.assertEqual(value, SECRET)
        out = mct.probe(ENDPOINT, name, value,
                        client=client_for(FakeServer(token=SECRET, header="x-api-key")))
        self.assertEqual(out["status"], "succeeded")

    def test_rejected_credential(self):
        out = mct.probe(ENDPOINT, *mct.credential_header(None, "wrong"), client=client_for(FakeServer()))
        self.assertEqual((out["status"], out["http_status"]), ("auth_rejected", 401))

    def test_same_origin_redirect_is_followed(self):
        server = FakeServer(redirect="/api/mcp")
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(server))
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(server.calls[1][1], "https://mcp.vendor.example/api/mcp")

    def test_cross_origin_redirect_never_gets_the_credential(self):
        server = FakeServer(redirect="https://collector.example/mcp")
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(server))
        self.assertEqual((out["status"], out["error"]), ("failed", "redirect_other_origin"))
        self.assertEqual([u for _m, u, _h, _b in server.calls], [ENDPOINT])

    def test_tools_list_failure_is_partial(self):
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(FakeServer(tools_error=True)))
        self.assertEqual((out["status"], out["tool_count"]), ("partial", None))

    def test_oversized_reply_and_plain_http(self):
        out = mct.probe(ENDPOINT, *bearer(), client=client_for(FakeServer(big=True)))
        self.assertEqual(out["error"], "response_too_large")
        server = FakeServer()
        out = mct.probe("http://mcp.vendor.example/mcp", *bearer(), client=client_for(server))
        self.assertEqual(out["error"], "not_https")
        self.assertEqual(server.calls, [])

    def test_header_and_credential_validation(self):
        self.assertEqual(mct.credential_header(None, "abc"), ("Authorization", "Bearer abc"))
        self.assertEqual(mct.credential_header("authorization", "Basic abc"), ("authorization", "Basic abc"))
        for header, credential, code in (("Cookie", "x", "bad_header"), ("X Key", "x", "bad_header"),
                                         ("Mcp-Session-Id", "x", "bad_header"),
                                         (None, "", "bad_credential"), (None, "a\r\nX-Evil: 1", "bad_credential"),
                                         (None, "x" * (mct.MAX_CREDENTIAL + 1), "bad_credential")):
            with self.assertRaises(mct.CredentialTestError) as caught:
                mct.credential_header(header, credential)
            self.assertEqual(caught.exception.code, code)


def gated_row(**extra):
    row = {"id": 1, "health": "ok", "http_status": 401, "conformance": "fail", "latency_ms": 400,
           "latency_p95_ms": 500, "protocol_version": None, "tools_json": None, "tool_count": None,
           "source_code_url": "https://github.com/v/mcp", "transport": "streamable_http",
           "owner_verified": 0, "auth_method": "oauth", "endpoint_url": ENDPOINT, "health_checked": 1_800_000_000}
    row.update(extra)
    return row


def summary_json(**extra):
    summary = {"id": "t1", "at": 1_800_000_000, "status": "succeeded", "endpoint_url": ENDPOINT,
               "protocol_version": "2025-06-18", "tool_count": 3, "described_count": 2}
    summary.update(extra)
    return json.dumps(summary)


class ScoringTests(TestCase):
    def by_key(self, row):
        return {c["key"]: c for c in L.assess_mcp(row, uptime_ok=10, uptime_total=10, now=1_800_000_000)["criteria"]}

    def test_test_scores_the_three_gated_items(self):
        k = self.by_key(gated_row(credential_test_json=summary_json()))
        self.assertEqual((k["valid_card"]["points"], k["skills"]["points"]), (10, 8))
        self.assertEqual(k["protocol_version"]["points"], 6)
        for key in ("valid_card", "protocol_version", "skills"):
            self.assertTrue(k[key]["detail"].startswith("Measured with a credential supplied by a signed-in user"))
            self.assertNotIn("auth_gated", k[key])

    def test_partial_test_and_stale_endpoint(self):
        k = self.by_key(gated_row(credential_test_json=summary_json(status="partial", tool_count=None)))
        self.assertEqual((k["valid_card"]["points"], k["skills"]["points"]), (7, 0))
        k = self.by_key(gated_row(credential_test_json=summary_json(endpoint_url="https://old.example/mcp")))
        self.assertTrue(k["valid_card"]["auth_gated"])
        k = self.by_key(gated_row(credential_test_json=summary_json(status="auth_rejected")))
        self.assertTrue(k["skills"]["auth_gated"])

    def test_open_server_keeps_its_own_probe(self):
        k = self.by_key(gated_row(http_status=200, conformance="pass", tool_count=1,
                                  tools_json=json.dumps([{"name": "a", "description": "d"}]),
                                  credential_test_json=summary_json()))
        self.assertEqual(k["valid_card"]["detail"], "initialize and tools/list both answered.")
        self.assertEqual(k["skills"]["points"], 5)

    def test_quality_counts_like_an_open_server(self):
        bare = db.mcp_quality_score(gated_row())
        tested = db.mcp_quality_score(gated_row(credential_test_json=summary_json()))
        self.assertAlmostEqual(tested - bare, 25 + 8 + 5)
        partial = db.mcp_quality_score(gated_row(credential_test_json=summary_json(status="partial", tool_count=None)))
        self.assertAlmostEqual(partial - bare, 10)
        down = db.mcp_quality_score(gated_row(health="down", credential_test_json=summary_json()))
        self.assertEqual(down, db.mcp_quality_score(gated_row(health="down")))


class CredentialFixture(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','ct','ct','active',1)").lastrowid
            self.sid = conn.execute(
                "INSERT INTO mcp_servers(slug,name,endpoint_url,source,source_id,created_at,updated_at,"
                "health,http_status,conformance,latency_p95_ms,transport,source_code_url,quality_score) "
                "VALUES('vendor-mcp','Vendor',?,'fixture','vendor',1,1,'ok',401,'fail',500,"
                "'streamable_http','https://github.com/v/mcp',0)", (ENDPOINT,)).lastrowid
        self.server = {"id": self.sid, "slug": "vendor-mcp", "endpoint_url": ENDPOINT}

    def row(self):
        return self.query("SELECT * FROM mcp_servers WHERE id=?", (self.sid,))[0]

    def run_test(self, credential=SECRET, publish=True, server=None):
        return mct.run(self.uid, self.server, credential, None, publish,
                       client=client_for(server or FakeServer()))


class RunTests(CredentialFixture):
    def test_success_is_public_scored_and_never_stores_the_credential(self):
        out = self.run_test()
        self.assertEqual((out["status"], out["tool_count"], out["tools_public"]), ("succeeded", 3, True))
        row = self.row()
        self.assertEqual(json.loads(row["credential_test_json"])["id"], out["id"])
        self.assertGreater(row["quality_score"], 0)
        self.assertIsNotNone(row["readiness_score"])
        with closing(db.connect(read_only=True)) as conn:
            dump = "\n".join(conn.iterdump())
            detail = db.get_mcp_by_slug(conn, "vendor-mcp")
        self.assertNotIn(SECRET, dump)
        self.assertEqual(detail["credential_tests"][0]["tools"][0]["name"], "get_skill")
        valid = {c["key"]: c for c in detail["readiness"]["criteria"]}["valid_card"]
        self.assertEqual(valid["points"], 10)
        self.assertIn("tools/list returned 3 tools", " ".join(detail["measured"]["sentences"]))

    def test_count_only_hides_names_but_scores_the_same(self):
        out = self.run_test(publish=False)
        self.assertEqual((out["tools"], out["tool_count"], out["tools_public"]), (None, 3, False))
        stored = self.query("SELECT tools_json FROM mcp_credential_tests")[0]
        self.assertIsNone(stored["tools_json"])
        hidden = self.row()["quality_score"]
        self.run_test(publish=True)
        self.assertEqual(self.row()["quality_score"], hidden)

    def test_failed_test_is_listed_but_keeps_the_last_success(self):
        first = self.run_test()
        failed = self.run_test(credential="wrong")
        self.assertEqual(failed["status"], "auth_rejected")
        self.assertEqual(json.loads(self.row()["credential_test_json"])["id"], first["id"])
        with closing(db.connect(read_only=True)) as conn:
            history = mct.history(conn, self.sid)
        self.assertEqual([t["status"] for t in history], ["auth_rejected", "succeeded"])

    def test_account_daily_limit(self):
        with db.writer() as conn:
            for i in range(mct.ACCOUNT_DAILY_LIMIT):
                conn.execute(
                    "INSERT INTO mcp_credential_tests(id,server_id,user_id,created_at,endpoint_url,"
                    "header_name,status,publish_tools) VALUES(?,?,?,strftime('%s','now'),?,"
                    "'Authorization','failed',1)", (f"t{i}", self.sid, self.uid, ENDPOINT))
        with self.assertRaises(mct.CredentialTestError) as caught:
            self.run_test()
        self.assertEqual(caught.exception.status, 429)


class RouteTests(CredentialFixture):
    def setUp(self):
        super().setUp()
        import os
        from directory import mcp_credential_test_routes
        self.enterContext(patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret",
                                                  "PUBLIC_BASE_URL": "https://agent-tools.cloud"}))
        app = FastAPI()
        app.include_router(mcp_credential_test_routes.router)
        app.include_router(routes.router)
        self.client = TestClient(app, base_url="https://agent-tools.cloud")

    def cookie(self):
        return {auth.SESSION_COOKIE: auth.sign({"uid": self.uid}, 600)}

    def post(self, slug="vendor-mcp", **extra):
        return self.client.post(f"/api/v1/mcp/servers/{slug}/credential-tests",
                                json={"credential": SECRET, **extra}, cookies=self.cookie(),
                                headers={"Origin": "https://agent-tools.cloud"})

    def test_requires_sign_in_and_same_origin(self):
        url = "/api/v1/mcp/servers/vendor-mcp/credential-tests"
        self.assertEqual(self.client.post(url, json={"credential": SECRET}).status_code, 401)
        r = self.client.post(url, json={"credential": SECRET}, cookies=self.cookie(),
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_run_over_http_then_public_history_and_page(self):
        real = mct.probe
        with patch.object(mct, "probe", side_effect=lambda e, n, v, client=None: real(
                e, n, v, client=client_for(FakeServer()))):
            r = self.post(publish_tools=False)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.headers["cache-control"], "private, no-store")
        self.assertNotIn(SECRET, r.text)
        history = self.client.get("/api/v1/mcp/servers/vendor-mcp/credential-tests").json()
        self.assertEqual(history["tests"][0]["tool_count"], 3)
        self.assertNotIn("user_id", history["tests"][0])
        page = self.client.get("/mcp/servers/vendor-mcp")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="credential-test"', page.text)
        self.assertIn("names not published", page.text)
        self.assertNotIn("get_skill_file", page.text)
        self.assertNotIn("<form", page.text.split('id="credential-test"')[1].split("</section>")[0])

    def test_unknown_or_bad_input(self):
        self.assertEqual(self.post(slug="nope").status_code, 404)
        r = self.post(header="Cookie")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_header"))


class McpToolTests(CredentialFixture):
    def setUp(self):
        super().setUp()
        import asyncio
        from directory import mcp_app
        self.mcp_app = mcp_app
        self.run_async = asyncio.run
        self.enterContext(patch.object(mcp_app, "DB_PATH", self.path))

    def test_tools_never_take_a_credential(self):
        tools = {t.name: t for t in self.run_async(self.mcp_app.discover_mcp.list_tools())}
        self.assertEqual(set(tools["get_mcp_credential_test_link"].inputSchema["properties"]), {"slug"})
        self.assertEqual(set(tools["list_mcp_credential_tests"].inputSchema["properties"]), {"slug", "since"})
        self.assertIn("Never ask the user for the credential", tools["get_mcp_credential_test_link"].description)
        self.assertTrue(tools["list_mcp_credential_tests"].annotations.readOnlyHint)

    def test_link_then_results(self):
        link = self.run_async(self.mcp_app.get_mcp_credential_test_link("vendor-mcp"))
        self.assertEqual(link["url"], "https://agent-tools.cloud/mcp/servers/vendor-mcp#credential-test")
        self.assertTrue(link["asks_for_credential"])
        self.assertEqual(self.run_async(self.mcp_app.list_mcp_credential_tests("vendor-mcp"))["count"], 0)
        self.run_test(publish=False)
        out = self.run_async(self.mcp_app.list_mcp_credential_tests("vendor-mcp", since=link["issued_at"]))
        self.assertEqual((out["count"], out["tests"][0]["tool_count"], out["tests"][0]["tools"]), (1, 3, None))
        later = self.run_async(self.mcp_app.list_mcp_credential_tests("vendor-mcp", since=link["issued_at"] + 3600))
        self.assertEqual(later["count"], 0)
        self.assertEqual(self.run_async(self.mcp_app.get_mcp_credential_test_link("nope"))["error"], "not_found")

    def test_public_docs_list_the_tools(self):
        import importlib.util
        import os
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test_credential", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        client = TestClient(server.app, base_url="https://agent-tools.cloud")
        names = {t["name"] for t in client.get("/.well-known/mcp.json").json()["tools"]}
        llms = client.get("/llms.txt").text
        guidance = client.get("/openapi.json").json()["info"]["x-guidance"]
        manifest = client.get("/.well-known/agent-tools.json").json()
        about = (root / "directory/templates/about.html").read_text()
        readme = (root / "README.md").read_text()
        for name in ("get_mcp_credential_test_link", "list_mcp_credential_tests"):
            self.assertIn(name, names)
            for text in (llms, guidance, about, readme):
                self.assertIn(name, text)
            self.assertIn(name, manifest["endpoints"]["mcp_credential_test"]["mcp"]["tools"])
