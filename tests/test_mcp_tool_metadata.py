import asyncio

from directory.mcp_app import discover_mcp

WRITE_TOOLS = {"register", "scan_mcp_safety", "prepare_paid_verification",
               "execute_paid_verification"}


def _tools():
    return {t.name: t for t in asyncio.run(discover_mcp.list_tools())}


def test_every_tool_is_annotated():
    tools = _tools()
    assert len(tools) == 14
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, name
        assert ann.readOnlyHint is (name not in WRITE_TOOLS), name
    assert tools["execute_paid_verification"].annotations.destructiveHint is True
    assert tools["register"].annotations.destructiveHint is False


def test_every_parameter_has_a_description():
    for name, tool in _tools().items():
        for param, schema in (tool.inputSchema.get("properties") or {}).items():
            assert schema.get("description"), f"{name}.{param}"


def test_multiline_arg_description_is_joined():
    desc = _tools()["search"].inputSchema["properties"]["has_mcp"]["description"]
    assert desc.startswith("When true, return only services with a callable MCP endpoint.")
    assert "Returns" not in desc
