"""MCP-only clients can mint an account key and use it as a tool argument."""
import asyncio
from unittest.mock import patch

from directory import db, mcp_app
from test_ingestion_pipeline import TemporaryDatabaseTestCase


def run(coro):
    return asyncio.run(coro)


class McpCreateApiKeyTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(mcp_app, "DB_PATH", self.path))
        self.limit = self.enterContext(
            patch.object(mcp_app.limits, "check_ip_limits", return_value=None))
        self.logged = self.enterContext(patch.object(mcp_app, "_log_call"))

    def test_mints_a_working_key_and_never_logs_it(self):
        out = run(mcp_app.create_api_key(name="my-agent"))
        key = out["api_key"]
        self.assertTrue(out["prefix"] and key.startswith(out["prefix"]))
        self.assertIsNotNone(mcp_app._pv_user_id(key))
        with db.connect(read_only=True) as conn:
            row = db.api_key_row(conn, key)
        self.assertEqual(row["name"], "my-agent")
        self.assertEqual(self.limit.call_args.args[1], "mint-key")
        for call in self.logged.call_args_list:
            self.assertNotIn(key, repr(call))
            self.assertNotIn("my-agent", repr(call))

    def test_each_call_is_a_new_anonymous_account(self):
        a = run(mcp_app.create_api_key())["api_key"]
        b = run(mcp_app.create_api_key())["api_key"]
        self.assertNotEqual(mcp_app._pv_user_id(a), mcp_app._pv_user_id(b))

    def test_rate_limit_is_shared_with_http(self):
        self.limit.return_value = {"window": "day", "limit": 5, "retry_after": 30}
        out = run(mcp_app.create_api_key())
        self.assertEqual(out["error"], "rate_limited")
        self.assertNotIn("api_key", out)
        self.assertEqual(out["retry_after_seconds"], 30)

    def test_stats_accepts_key_argument(self):
        key = run(mcp_app.create_api_key())["api_key"]
        seen = []

        def summary(snap, view, full=False):
            seen.append(full)
            return {"view": view, "kpis": {}}

        with patch("directory.stats.latest", return_value={}), \
                patch("directory.stats.summary", side_effect=summary):
            run(mcp_app.directory_stats())
            run(mcp_app.directory_stats(api_key="atk_not_a_real_key"))
            run(mcp_app.directory_stats(api_key=key))
        self.assertEqual(seen, [False, False, True])
