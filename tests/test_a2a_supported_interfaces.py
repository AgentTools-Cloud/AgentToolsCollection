#!/usr/bin/env python3
"""Regression test for A2A 1.0 supportedInterfaces endpoint discovery."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import a2a

cases = [
    ({"supportedInterfaces": [{"url": "https://api.interailabs.dev/a2a/v1", "protocolBinding": "JSONRPC"}]}, "https://api.interailabs.dev/a2a/v1", "A2A 1.0"),
    ({"additionalInterfaces": [{"url": "https://old.example/a2a"}]}, "https://old.example/a2a", "legacy additionalInterfaces"),
    ({"interfaces": [{"url": "https://older.example/a2a"}]}, "https://older.example/a2a", "legacy interfaces"),
    ({"url": "https://top.example/a2a", "supportedInterfaces": [{"url": "https://ignored.example/a2a"}]}, "https://top.example/a2a", "top-level URL precedence"),
]
failed = 0
for card, expected, label in cases:
    got = a2a._card_endpoint(card, "https://example/.well-known/agent-card.json")
    ok = got == expected
    print("  %s %-29s %s" % ("ok  " if ok else "FAIL", label, got))
    failed += not ok

row = a2a.card_to_row({
    "name": "InterAI Risk Oracle",
    "version": "0.1.3-beta.1",
    "provider": {"url": "https://api.interailabs.dev", "organization": "InterAI"},
    "supportedInterfaces": [{"url": "https://api.interailabs.dev/a2a/v1", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
}, "https://api.interailabs.dev/.well-known/agent-card.json", source="agenstry", source_id="api.interailabs.dev")
ok = row["endpoint_url"] == "https://api.interailabs.dev/a2a/v1"
print("  %s card_to_row endpoint_url       %s" % ("ok  " if ok else "FAIL", row["endpoint_url"]))
failed += not ok
ok = row["protocol_version"] == "1.0" and row["preferred_transport"] == "JSONRPC"
print("  %s card_to_row v1 version        %s %s" % ("ok  " if ok else "FAIL",
      row["protocol_version"], row["preferred_transport"]))
failed += not ok

version_cases = [
    ({"version": "2.3.0", "protocolVersion": "0.3.0", "preferredTransport": "JSONRPC"},
     "0.3.0", "JSONRPC", "0.3 top-level"),
    ({"version": "0.1.0", "supportedInterfaces": [
        {"url": "https://a/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
        {"url": "https://a/a2a/v1", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]},
     "1.0", "JSONRPC", "dual 0.3 + 1.0 picks highest"),
    ({"version": "0.3.0", "protocolVersion": "0.3.0",
      "supportedInterfaces": [{"url": "https://b/a2a", "protocolVersion": "1.0"}]},
     "1.0", None, "legacy top-level plus v1 interface"),
    ({"version": "1.4.2", "name": "x"}, None, None, "agent version is not protocol version"),
    ({"supportedInterfaces": [{"url": "https://c/a2a", "protocolVersion": "1.1"},
                              {"url": "https://c/a2a2", "protocolVersion": "1.0.1"}]},
     "1.1", None, "1.1 beats 1.0.1"),
    ({"supportedInterfaces": [{"url": "https://d/mcp", "protocolVersion": "2025-06-18"}]},
     "2025-06-18", None, "non-numeric kept verbatim"),
]
for card, want_v, want_t, label in version_cases:
    got_v = a2a._card_protocol_version(card)
    got_t = a2a._card_transport(card)
    ok = got_v == want_v and got_t == want_t
    print("  %s %-36s %s %s" % ("ok  " if ok else "FAIL", label, got_v, got_t))
    failed += not ok
total = len(cases) + 2 + len(version_cases)
print("\n%d passed, %d failed" % (total - failed, failed))
raise SystemExit(bool(failed))
