"""/.well-known/mcp-registry-auth serves the MCP Registry HTTP-auth proof record."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ingestion_pipeline import TemporaryDatabaseTestCase

RECORD = "v=MCPv1; k=ed25519; p=CmELkdW27Q+pySZRD1ezkXAG/SbeDhGWAjtuKKMKixg="


class McpRegistryAuthTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("server_under_test_auth", root / "server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"SESSION_SECRET": "offline-test-session-secret"}):
            spec.loader.exec_module(server)
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app, base_url="https://agent-tools.cloud")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mcp-registry-auth")

    def get(self):
        with patch.dict(os.environ, {"AGENT_TOOLS_MCP_REGISTRY_AUTH_FILE": self.path}):
            return self.client.get("/.well-known/mcp-registry-auth",
                                   headers={"Accept": "text/plain", "User-Agent": "mcp-registry/1.0"},
                                   follow_redirects=False)

    def test_serves_only_proof_lines_as_plain_text(self):
        with open(self.path, "w") as fh:
            fh.write("# comment\n" + RECORD + "\n\n")
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(r.text, RECORD + "\n")

    def test_missing_or_empty_file_is_404(self):
        self.assertEqual(self.get().status_code, 404)
        with open(self.path, "w") as fh:
            fh.write("not a record\n")
        self.assertEqual(self.get().status_code, 404)

    def test_oversized_file_is_404(self):
        with open(self.path, "w") as fh:
            fh.write((RECORD + "\n") * 80)
        self.assertEqual(self.get().status_code, 404)


if __name__ == "__main__":
    unittest.main()
