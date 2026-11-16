"""Sponsored paid verification exposed as MCP tools on /mcp-discovery."""
import asyncio
from contextlib import closing
import json
from unittest.mock import patch

from directory import db, mcp_app, ownership, paid_verify as pv, public_http
from test_ingestion_pipeline import TemporaryDatabaseTestCase
from test_paid_verify import ACCOUNT, FakeEndpoint, URL, challenge, sign


def run(coro):
    return asyncio.run(coro)


class McpOwnershipClaimTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(mcp_app, "DB_PATH", self.path))
        self.enterContext(patch.object(mcp_app.limits, "check_ip_limits", return_value=None))
        with db.writer() as conn:
            self.uid = db.upsert_user(conn, "github", "mcp-claim-fixture", login="mcp-claim-fixture")
            self.other = db.upsert_user(conn, "github", "mcp-claim-other", login="mcp-claim-other")
            self.key = db.create_api_key(conn, self.uid, "fixture")
            self.other_key = db.create_api_key(conn, self.other, "fixture")
            for slug, target in (("claimed-service", "https://platform.example/team/one/mcp"),
                                 ("sibling-service", "https://platform.example/team/two/mcp")):
                db.upsert_mcp_server(conn, {"slug": slug, "name": slug, "endpoint_url": target,
                                           "source": "fixture", "source_id": slug})

    def call(self, function, *args, key=None, **kwargs):
        context = mcp_app._current_authorization.set("Bearer " + (key or self.key))
        try:
            return run(function(*args, **kwargs))
        finally:
            mcp_app._current_authorization.reset(context)

    def test_claim_tools_are_registered_with_url_only_input(self):
        tools = {tool.name: tool for tool in run(mcp_app.discover_mcp.list_tools())}
        self.assertTrue({"claim_service_url", "verify_service_url", "get_owned_listing", "edit_owned_listing"} <= set(tools))
        fields = tools["claim_service_url"].inputSchema["properties"]
        self.assertIn("url", fields)
        self.assertNotIn("platform", fields)
        self.assertNotIn("suffix", fields)
        self.assertNotIn("api_key", fields)

    def test_claim_tools_require_the_bearer_account_header(self):
        for function, args in (
                (mcp_app.claim_service_url, ("https://platform.example/one",)),
                (mcp_app.verify_service_url, (1, "not-a-real-token")),
                (mcp_app.get_owned_listing, ("mcp", "claimed-service")),
                (mcp_app.edit_owned_listing, ("mcp", "claimed-service", {"description": "No rights"}))):
            with self.subTest(function=function.__name__):
                self.assertEqual(run(function(*args))["error"], "unauthorized")
        self.assertEqual(self.call(mcp_app.claim_service_url, "https://platform.example/one", key="invalid-fixture-key")["error"], "unauthorized")

    def test_mcp_claim_verify_read_and_edit_use_the_exact_url(self):
        opened = self.call(mcp_app.claim_service_url, "https://platform.example/team/one/mcp", method="url_descriptor")
        self.assertNotIn("error", opened, opened)
        self.assertEqual(opened["mcp_next"]["tool"], "verify_service_url")
        proof = ownership._FetchResponse(200, json.dumps(opened["document"]).encode(), {})
        with patch.object(ownership, "_read_https", return_value=proof):
            verified = self.call(mcp_app.verify_service_url, opened["claim_id"], opened["token"])
        self.assertEqual(verified["status"], "verified", verified)
        self.assertEqual([listing["slug"] for listing in verified["listings"]], ["claimed-service"])
        got = self.call(mcp_app.get_owned_listing, "mcp", "claimed-service")
        self.assertNotIn("error", got, got)
        edited = self.call(mcp_app.edit_owned_listing, "mcp", "claimed-service", {"description": "MCP owner correction"})
        self.assertEqual(edited["applied"], ["description"], edited)
        refused = self.call(mcp_app.edit_owned_listing, "mcp", "sibling-service", {"description": "Not owned"})
        self.assertEqual(refused["status_code"], 403, refused)
        self.assertIsNone(mcp_app._current_authorization.get())

    def test_other_accounts_and_unverified_destination_urls_are_rejected(self):
        opened = self.call(mcp_app.claim_service_url, "https://platform.example/team/one/mcp", method="url_descriptor")
        with patch.object(ownership, "_read_https") as reader:
            refused = self.call(mcp_app.verify_service_url, opened["claim_id"], opened["token"], key=self.other_key)
            self.assertEqual(refused["status_code"], 403, refused)
            reader.assert_not_called()
        proof = ownership._FetchResponse(200, json.dumps(opened["document"]).encode(), {})
        with patch.object(ownership, "_read_https", return_value=proof):
            self.call(mcp_app.verify_service_url, opened["claim_id"], opened["token"])
        refused = self.call(mcp_app.edit_owned_listing, "mcp", "claimed-service", {"endpoint_url": "https://platform.example/team/three/mcp"})
        self.assertEqual(refused["status_code"], 422, refused)

    def test_mcp_claim_logs_do_not_store_keys_or_verification_tokens(self):
        opened = self.call(mcp_app.claim_service_url, "https://platform.example/team/one/mcp", method="url_descriptor")
        proof = ownership._FetchResponse(200, json.dumps(opened["document"]).encode(), {})
        with patch.object(ownership, "_read_https", return_value=proof):
            self.call(mcp_app.verify_service_url, opened["claim_id"], opened["token"])
        with closing(db.connect(read_only=True)) as conn:
            log = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM tool_calls"))
        self.assertIn("claim_service_url", log)
        self.assertIn("verify_service_url", log)
        self.assertNotIn(self.key, log)
        self.assertNotIn(opened["token"], log)

    def test_mcp_http_protocol_supports_claim_verification_and_editing(self):
        import httpx
        from fastapi import FastAPI
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def flow():
            http_server = mcp_app.FastMCP("claims-http-fixture", stateless_http=True,
                                         streamable_http_path="/", transport_security=mcp_app._TS)
            for handler in (mcp_app.claim_service_url, mcp_app.verify_service_url,
                            mcp_app.get_owned_listing, mcp_app.edit_owned_listing):
                http_server.add_tool(handler)
            app = FastAPI()
            app.mount("/mcp-discovery", mcp_app.wrap_with_client_capture(
                http_server.streamable_http_app()))

            def client_factory(**options):
                return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         headers=options.get("headers"), timeout=10.0,
                                         follow_redirects=False, trust_env=False)

            def unpack(result):
                if result.structuredContent is not None:
                    return result.structuredContent
                return json.loads(next(content.text for content in result.content if content.type == "text"))

            async with http_server.session_manager.run():
                async with streamablehttp_client(
                        "https://agent-tools.cloud/mcp-discovery/", headers={"Authorization": "Bearer " + self.key},
                        httpx_client_factory=client_factory) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertIn("claim_service_url", {tool.name for tool in tools.tools})
                        opened = unpack(await session.call_tool("claim_service_url", {
                            "url": "https://platform.example/team/one/mcp", "method": "url_descriptor"}))
                        self.assertNotIn("error", opened, opened)
                        proof = ownership._FetchResponse(200, json.dumps(opened["document"]).encode(), {})
                        with patch.object(ownership, "_read_https", return_value=proof):
                            verified = unpack(await session.call_tool("verify_service_url", {
                                "claim_id": opened["claim_id"], "token": opened["token"]}))
                        self.assertEqual(verified["status"], "verified", verified)
                        self.assertEqual([item["slug"] for item in verified["listings"]], ["claimed-service"])
                        changed = unpack(await session.call_tool("edit_owned_listing", {
                            "kind": "mcp", "slug": "claimed-service", "fields": {"description": "SDK correction"}}))
                        self.assertEqual(changed["applied"], ["description"], changed)
                        refused = unpack(await session.call_tool("edit_owned_listing", {
                            "kind": "mcp", "slug": "sibling-service", "fields": {"description": "Not owned"}}))
                        self.assertEqual(refused["status_code"], 403, refused)

        run(flow())
        self.assertIsNone(mcp_app._current_authorization.get())

    def public_document_client(self, root=None):
        import importlib.util
        import os
        from pathlib import Path
        from fastapi.testclient import TestClient

        root = root or Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test_mcp_claims", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        return TestClient(server.app, base_url="https://agent-tools.cloud")

    def test_claim_tools_are_documented_in_public_discovery_and_http_guidance(self):
        client = self.public_document_client()
        names = {tool["name"] for tool in client.get("/.well-known/mcp.json").json()["tools"]}
        expected = {"claim_service_url", "verify_service_url", "get_owned_listing", "edit_owned_listing"}
        self.assertTrue(expected <= names)
        llms = client.get("/llms.txt").text
        guidance = client.get("/openapi.json").json()["info"]["x-guidance"]
        page = client.get("/docs/claim").text
        for name in expected:
            self.assertIn(name, llms)
            self.assertIn(name, guidance)
            self.assertIn(name, page)

    def test_public_claim_page_is_short_url_first_and_has_current_fields(self):
        from html.parser import HTMLParser
        class VisibleText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.hidden = 0
                self.parts = []
            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style'):
                    self.hidden += 1
            def handle_endtag(self, tag):
                if tag in ('script', 'style'):
                    self.hidden -= 1
            def handle_data(self, data):
                if not self.hidden:
                    self.parts.append(data)
        page = self.public_document_client().get('/docs/claim').text
        parsed = VisibleText()
        parsed.feed(page)
        text = ' '.join(parsed.parts)
        self.assertLessEqual(len(text.split()), 750)
        self.assertLess(text.index('Claim a service URL'), text.index('Claim a whole host'))
        for value in ('agentToolsVerify', 'agent-tools://ownership', '.well-known/agent-tools.json',
                      '24 hours', 'cost_hint', 'auth_method', 'pricing', 'Authorization: Bearer'):
            self.assertIn(value, text)
        for value in ('+3 points', 'atc_R7xK2mQ9pL4vB8nT6wZ3yF5c', 'verification_revision',
                      'domain_ownership', '/opt/mcpserver', '/root/agent-tools', '36609', '26005'):
            self.assertNotIn(value, page)

    def test_public_machine_guidance_is_short_and_omits_internal_history(self):
        client = self.public_document_client()
        llms = client.get('/llms.txt').text
        info = client.get('/openapi.json').json()['info']
        self.assertLessEqual(len(llms.split()), 420)
        self.assertLessEqual(len(info['description'].split()), 55)
        self.assertLessEqual(len(info['x-guidance'].split()), 160)
        text = llms + info['description'] + info['x-guidance']
        for value in ('Qwen', 'crypto verticals', 'domain_ownership', 'verification_revision',
                      '/root/agent-tools', '/opt/mcpserver', '+3 points'):
            self.assertNotIn(value, text)
        for name in ('claim_service_url', 'verify_service_url', 'get_owned_listing', 'edit_owned_listing'):
            self.assertIn(name, llms)
            self.assertIn(name, info['x-guidance'])
        self.assertIn('Authorization: Bearer', text)
        self.assertIn('Pending tokens expire after 24 hours', llms)

    def test_public_doc_edit_does_not_change_api_contract(self):
        schema = self.public_document_client().get('/openapi.json').json()
        request = schema['components']['schemas']['ServiceClaimRequest']
        self.assertEqual(set(request['properties']), {'url', 'method'})
        self.assertEqual(request['required'], ['url'])
        self.assertFalse(request['additionalProperties'])
        opened = schema['paths']['/api/v1/listing-claims']['post']
        verified = schema['paths']['/api/v1/listing-claims/{claim_id}/verify']['post']
        self.assertIn('201', opened['responses'])
        self.assertIn('200', verified['responses'])
        self.assertEqual(opened['security'], [{'AccountAPIKey': []}])
        self.assertEqual(verified['security'], [{'AccountAPIKey': []}])


class McpPaidVerifyTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(mcp_app, "DB_PATH", self.path))
        self.enterContext(patch.object(public_http, "validate_url", return_value=None))
        self.enterContext(patch.object(pv, "verify_onchain_settlement",
                                       return_value=(True, "verified")))
        with db.writer() as conn:
            self.uid = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','mpv','mpv','active',1)").lastrowid
            self.other = conn.execute(
                "INSERT INTO users(provider,provider_uid,login,status,created_at) "
                "VALUES('github','mpv2','mpv2','active',1)").lastrowid
            self.key = db.create_api_key(conn, self.uid, "t")
            self.other_key = db.create_api_key(conn, self.other, "t2")
            conn.execute(
                "INSERT INTO services(slug,name,url,source,source_id,created_at,updated_at) "
                "VALUES('vendor','Vendor',?,'manual','vendor',1,1)", (URL,))

    def test_tools_are_registered(self):
        names = {t.name for t in run(mcp_app.discover_mcp.list_tools())}
        self.assertTrue({"prepare_paid_verification", "execute_paid_verification",
                         "get_paid_verification"} <= names)

    def test_requires_api_key(self):
        out = run(mcp_app.prepare_paid_verification("vendor", ACCOUNT.address))
        self.assertEqual(out["error"], "unauthorized")
        out = run(mcp_app.prepare_paid_verification("vendor", ACCOUNT.address,
                                                    api_key="atc_live_bogus"))
        self.assertEqual(out["error"], "unauthorized")
        self.assertEqual(run(mcp_app.get_paid_verification("x"))["error"], "unauthorized")

    def test_full_flow_with_argument_key(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            q = run(mcp_app.prepare_paid_verification("vendor", ACCOUNT.address,
                                                      api_key=self.key))
            self.assertNotIn("error", q, q)
            out = run(mcp_app.execute_paid_verification(q["id"], sign(q["typed_data"]),
                                                        api_key=self.key))
        self.assertEqual(out["status"], "succeeded", out)
        got = run(mcp_app.get_paid_verification(q["id"], api_key=self.key))
        self.assertEqual(got["status"], "succeeded")
        # Another account can neither read nor execute it.
        self.assertIn("error", run(mcp_app.get_paid_verification(q["id"], api_key=self.other_key)))

    def test_bearer_header_is_accepted(self):
        token = mcp_app._current_authorization.set("Bearer " + self.key)
        try:
            with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
                q = run(mcp_app.prepare_paid_verification("vendor", ACCOUNT.address))
        finally:
            mcp_app._current_authorization.reset(token)
        self.assertIn("typed_data", q, q)

    def test_unknown_service_and_bad_signature(self):
        self.assertEqual(run(mcp_app.prepare_paid_verification(
            "nope", ACCOUNT.address, api_key=self.key))["error"], "not_found")
        self.assertEqual(run(mcp_app.execute_paid_verification(
            "x", "0x12", api_key=self.key))["error"], "bad_signature")

    def test_secrets_are_not_logged(self):
        with patch.object(pv, "_fetch", FakeEndpoint([challenge()])):
            q = run(mcp_app.prepare_paid_verification("vendor", ACCOUNT.address,
                                                      api_key=self.key))
            sig = sign(q["typed_data"])
            run(mcp_app.execute_paid_verification(q["id"], sig, api_key=self.key))
        with closing(db.connect(read_only=True)) as conn:
            dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM tool_calls"))
        self.assertIn("prepare_paid_verification", dump)
        self.assertNotIn(self.key, dump)
        self.assertNotIn(sig[2:40], dump)

    def test_header_middleware_sets_and_resets(self):
        seen = {}

        async def app(scope, receive, send):
            seen["auth"] = mcp_app._current_authorization.get()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "method": "GET", "headers": [(b"authorization", b"Bearer k")]}
        run(mcp_app.wrap_with_client_capture(app)(scope, receive, None))
        self.assertEqual(seen["auth"], "Bearer k")
        self.assertIsNone(mcp_app._current_authorization.get())

    def test_public_docs_list_the_tools(self):
        import os
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test_mcp", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        from fastapi.testclient import TestClient
        client = TestClient(server.app, base_url="https://agent-tools.cloud")
        llms = client.get("/llms.txt").text
        self.assertIn("prepare_paid_verification(slug, payer)", llms)
        names = {t["name"] for t in client.get("/.well-known/mcp.json").json()["tools"]}
        self.assertIn("execute_paid_verification", names)
        self.assertIn("get_paid_verification",
                      client.get("/openapi.json").json()["info"]["x-guidance"])

    def test_human_and_manifest_docs_mention_mcp_tools(self):
        import os
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test_mcp2", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        from fastapi.testclient import TestClient
        client = TestClient(server.app, base_url="https://agent-tools.cloud")
        manifest = client.get("/.well-known/agent-tools.json").json()
        self.assertIn("prepare_paid_verification",
                      manifest["endpoints"]["paid_verification"]["mcp"]["tools"])
        self.assertIn("execute_paid_verification(id, signature)",
                      (root / "directory/templates/about.html").read_text())
        self.assertIn("get_paid_verification(id, api_key=None)", (root / "README.md").read_text())
