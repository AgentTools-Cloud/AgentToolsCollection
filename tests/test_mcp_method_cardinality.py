import json

from directory.mcp_app import _methods_from_body


def test_unknown_jsonrpc_methods_are_bucketed_and_batch_is_capped():
    body = json.dumps([
        {"jsonrpc": "2.0", "method": "unknown/%d" % index}
        for index in range(2000)
    ]).encode()
    methods = _methods_from_body(body)
    assert len(methods) == 64
    assert set(methods) == {"_other"}


def test_known_jsonrpc_methods_keep_useful_labels():
    body = json.dumps([
        {"jsonrpc": "2.0", "method": "initialize"},
        {"jsonrpc": "2.0", "method": "tools/call"},
    ]).encode()
    assert _methods_from_body(body) == ["initialize", "tools/call"]