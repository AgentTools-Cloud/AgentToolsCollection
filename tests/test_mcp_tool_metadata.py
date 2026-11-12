"""Every MCP tool carries annotations and every parameter a description.

Unittest-style so `python -m unittest discover -s tests` runs it. No hard-coded
tool count: new tools only have to be classified below when they write.
"""
import asyncio
import unittest

from directory.mcp_app import discover_mcp

# Tools that change state (directory listings, claims, payments). Everything
# else must be marked read-only.
WRITE_TOOLS = {"register", "scan_mcp_safety", "prepare_paid_verification",
               "execute_paid_verification", "claim_service_url",
               "verify_service_url", "edit_owned_listing"}
DESTRUCTIVE_TOOLS = {"execute_paid_verification", "verify_service_url",
                     "edit_owned_listing"}


def _tools():
    return {t.name: t for t in asyncio.run(discover_mcp.list_tools())}


class McpToolMetadataTests(unittest.TestCase):
    def test_every_tool_is_annotated(self):
        tools = _tools()
        self.assertTrue(WRITE_TOOLS <= set(tools), WRITE_TOOLS - set(tools))
        for name, tool in tools.items():
            ann = tool.annotations
            self.assertIsNotNone(ann, name)
            self.assertIs(ann.readOnlyHint, name not in WRITE_TOOLS, name)
            if name in WRITE_TOOLS:
                self.assertIs(ann.destructiveHint, name in DESTRUCTIVE_TOOLS, name)

    def test_every_parameter_has_a_description(self):
        for name, tool in _tools().items():
            for param, schema in (tool.inputSchema.get("properties") or {}).items():
                self.assertTrue(schema.get("description"), f"{name}.{param}")

    def test_multiline_arg_description_is_joined(self):
        desc = _tools()["search"].inputSchema["properties"]["has_mcp"]["description"]
        self.assertTrue(desc.startswith("When true, return only services with a callable MCP endpoint."))
        self.assertNotIn("Returns", desc)

    def test_stats_tools_present(self):
        tools = _tools()
        self.assertIn("directory_stats", tools)
        self.assertEqual(set(tools["directory_stats"].inputSchema["properties"]), {"view", "topic", "month"})


if __name__ == "__main__":
    unittest.main()
