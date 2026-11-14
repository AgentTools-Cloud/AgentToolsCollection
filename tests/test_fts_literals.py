#!/usr/bin/env python3
"""Regression tests for punctuation in FTS synonym expansions."""
import os
import sqlite3
import sys

SOURCE_DB = os.environ.get("AGENT_TOOLS_SOURCE_DB", "/opt/mcpserver/data/agent-tools.db")
if not os.path.exists(SOURCE_DB):
    print("SKIP: no database at %s" % SOURCE_DB)
    raise SystemExit(0)

COPY = "/tmp/test-fts-literals-%d.db" % os.getpid()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_copy_guard import assert_on_copy, refuse_if_imported  # noqa: E402
refuse_if_imported()
sqlite3.connect(SOURCE_DB).backup(sqlite3.connect(COPY))
os.environ["AGENT_TOOLS_DB_PATH"] = COPY
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import db  # noqa: E402
assert_on_copy(COPY)

c=db.connect(read_only=True)
queries=(
    "NFT",
    "send an NFT to Base address",
    "mint BotPay NFT with inscription",
    "on-chain token activity",
    "onchain token activity",
    "LLM text generation",
    "language model text generation",
)
failed=0
for query in queries:
    try:
        rows=db.search(c,q=query,limit=10)
        print("  ok   %-42s %d rows"%(query,len(rows)))
    except Exception as exc:
        failed+=1
        print("  FAIL %-42s %s: %s"%(query,type(exc).__name__,exc))

for value in ('non-fungible','on-chain','language model','say "hello"'):
    literal=db._fts_literal(value)
    ok=literal.startswith('"') and literal.endswith('"') and value.replace('"','""') in literal
    failed+=not ok
    print("  %s literal %-27s -> %s"%("ok  " if ok else "FAIL",value,literal))

print("\n%d passed, %d failed"%(len(queries)+4-failed,failed))
c.close()
os.remove(COPY)
raise SystemExit(bool(failed))
