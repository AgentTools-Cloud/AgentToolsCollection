"""Sponsored paid verification exposed as MCP tools on /mcp-discovery."""
import asyncio
from contextlib import closing
from unittest.mock import patch

from directory import db, mcp_app, paid_verify as pv, public_http
from test_ingestion_pipeline import TemporaryDatabaseTestCase
from test_paid_verify import ACCOUNT, FakeEndpoint, URL, challenge, sign


def run(coro):
    return asyncio.run(coro)


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
