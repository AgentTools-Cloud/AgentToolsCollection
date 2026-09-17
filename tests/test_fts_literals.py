#!/usr/bin/env python3
"""Regression tests for punctuation in FTS synonym expansions."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import db

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
raise SystemExit(bool(failed))
