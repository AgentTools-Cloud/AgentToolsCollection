"""Split one chain's 30-day bulk log sweep across several hosts.

Each host has its own egress IP, so the public node's per-IP pacing applies
to each slice separately.

    plan   (main host)   python -m directory.onchain_shard plan base 4 plan.json
    run    (any host)    python -m directory.onchain_shard run plan.json 2 out-2.json
    merge  (main host)   python -m directory.onchain_shard merge plan.json out-*.json

`run` needs no database. `merge` refuses unless every slice is present and
complete, then writes per-address rows (payto_activity) only; listing demand
is left to the regular onchain job.
"""

from __future__ import annotations

import json
import logging
import sys
import time

import httpx

from . import crawlers

log = logging.getLogger(__name__)


def plan(chain: str, parts: int, out: str, days: int = 30) -> dict:
    from . import db
    rpc_url, _contract, block_secs, block_window, _batch = crawlers.BULK_LOG_CHAINS[chain]
    now = int(time.time())
    with db.connect(read_only=True) as c:
        addrs = [r[0] for r in c.execute(
            "SELECT DISTINCT address FROM service_paytos WHERE chain=? AND last_seen>=?",
            (chain, now - days * 86400))]
    with httpx.Client(timeout=30, headers={"User-Agent": crawlers.UA}) as c:
        head = int(crawlers._rpc(c, rpc_url, "eth_blockNumber", []), 16)
        probe = 100000
        t_head = int(crawlers._rpc(c, rpc_url, "eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
        t_old = int(crawlers._rpc(c, rpc_url, "eth_getBlockByNumber", [hex(head - probe), False])["timestamp"], 16)
    if t_head > t_old:
        block_secs = (t_head - t_old) / probe
    start = max(0, head - int(days * 86400 / block_secs))
    windows = list(range(start, head + 1, block_window))
    size = -(-len(windows) // parts)
    slices = []
    for i in range(parts):
        chunk = windows[i * size:(i + 1) * size]
        if chunk:
            last = min(chunk[-1] + block_window - 1, head)
            slices.append([chunk[0], last])
    doc = {"chain": chain, "now": now, "head": head, "start": start,
           "block_secs": block_secs, "addresses": addrs, "slices": slices}
    with open(out, "w") as f:
        json.dump(doc, f)
    print(f"{chain}: {len(addrs)} addresses, blocks {start}-{head}, "
          f"{len(windows)} windows in {len(slices)} slices")
    return doc


def run(plan_path: str, index: int, out: str) -> bool:
    with open(plan_path) as f:
        doc = json.load(f)
    lo, hi = doc["slices"][index]
    t0 = time.time()
    res, ok = crawlers.fetch_chain_activity_bulk(doc["addresses"], chain=doc["chain"],
                                                block_range=(lo, hi))
    with open(out, "w") as f:
        json.dump({"index": index, "slice": [lo, hi], "ok": ok, "now": doc["now"],
                   "seconds": round(time.time() - t0), "results": res}, f)
    print(f"slice {index} [{lo},{hi}] ok={ok} in {round(time.time() - t0)}s")
    return ok


def merge(plan_path: str, outs: list[str], write: bool = True) -> dict:
    from . import db
    with open(plan_path) as f:
        doc = json.load(f)
    got = {}
    for p in outs:
        with open(p) as f:
            part = json.load(f)
        if not part["ok"] or part["slice"] != doc["slices"][part["index"]]:
            raise SystemExit(f"{p}: incomplete or for another plan")
        got[part["index"]] = part["results"]
    if sorted(got) != list(range(len(doc["slices"]))):
        raise SystemExit(f"missing slices: have {sorted(got)}")
    merged = {}
    for a in doc["addresses"]:
        key = a.lower()
        tx = raw = 0
        payers: set = set()
        for res in got.values():
            r = res.get(key) or {}
            tx += int(r.get("tx") or 0)
            raw += int(r.get("raw_tx") or 0)
            payers.update(r.get("payer_ids") or [])
        merged[(doc["chain"], a)] = {"tx": tx, "payers": len(payers), "raw_tx": raw,
                                     "ok": True}
    paid = sum(1 for v in merged.values() if v["tx"])
    print(f"merged {len(merged)} addresses, {paid} with payments, "
          f"{sum(v['tx'] for v in merged.values())} payments, "
          f"{sum(v['raw_tx'] for v in merged.values())} raw transfers")
    if write:
        def op():
            with db.writer(immediate=True) as c:
                return db.record_payto_activity(c, merged, int(doc["now"]))
        print("rows written:", db.with_retry(op))
    return merged


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["plan"]:
        plan(a[1], int(a[2]), a[3])
    elif a[:1] == ["run"]:
        return 0 if run(a[1], int(a[2]), a[3]) else 1
    elif a[:1] == ["merge"]:
        dry = "--dry-run" in a
        merge(a[1], [x for x in a[2:] if x != "--dry-run"], write=not dry)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
