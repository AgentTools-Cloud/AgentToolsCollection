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
print("\n%d passed, %d failed" % (len(cases) + 1 - failed, failed))
raise SystemExit(bool(failed))
