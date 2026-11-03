"""Review every x402 origin that holds more than one listing; fold the ones
that are a single seller's API (same origin + same payTo), keep the rest.

Usage:
  python -m directory.fold_review [--report out.json] [--apply plan.json]

Without --apply it only reads the database and writes a decision per origin
with the reasons. --apply executes the fold decisions of a reviewed plan
file, origin by origin, each in its own transaction.

Decision rules, in order (first match wins):
  keep  shared host            github.com, npm ... (reviewed as shared)
  keep  gateway                the MCP or A2A directory lists >= 3 separate
                               servers/agents on this origin (pipeworx ...)
  keep  several payTo          more than one current payTo: maybe several
                               sellers or a payout split; not ours to merge
  keep  no payTo               no row carries a payTo: identity unproven
  keep  owner split            an owner verified some rows but not others
  fold  single seller          everything else: one origin, one payTo
Rows with no payTo on a single-seller origin are folded with it (they are
the crawler's root/summary rows of the same service).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time

from . import crawlers, db, fold_origin


_B32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _family(addr: str) -> str:
    """Address family by format. A multi-chain seller has one address per
    family (EVM, Solana, Stellar, Algorand ...); two in one family is two
    payees."""
    a = addr.strip()
    if a.lower().startswith("0x") and len(a) == 42:
        return "evm"
    if len(a) == 58 and set(a) <= _B32:
        return "algorand"
    if len(a) == 56 and a[0] == "G" and set(a) <= _B32:
        return "stellar"
    if 32 <= len(a) <= 44 and set(a) <= _B58:
        # Solana and XRP (r...) share base58; XRP classic addresses start 'r'.
        return "xrp" if a.startswith("r") and 25 <= len(a) <= 35 else "solana"
    if 25 <= len(a) <= 35 and a.startswith("r") and set(a) <= _B58:
        return "xrp"
    if a.startswith("nano_") or a.startswith("xrb_"):
        return "nano"
    if a.startswith(("SP", "SM")) and len(a) in (40, 41) and a.isalnum():
        return "stacks"
    if a.lower().startswith("0x") and len(a) == 66:
        return "move"          # Aptos/Sui 32-byte addresses
    if a.startswith("u1") and len(a) > 60:
        return "zcash"
    if "::" in a:
        return a.split("::", 1)[0]
    return "other"


def _paytos(payment) -> set:
    try:
        d = json.loads(payment or "null") or {}
    except (TypeError, ValueError):
        return set()
    if not isinstance(d, dict):
        return set()
    seen = {str(d.get("pay_to") or "")}
    seen |= {str(a.get("payTo") or a.get("pay_to") or "")
             for a in d.get("accepts") or [] if isinstance(a, dict)}
    seen.discard("")
    # EVM addresses are case-insensitive; Solana base58 is not.
    return {a.lower() if _family(a) == "evm" else a for a in seen}


def review(conn) -> list[dict]:
    by: dict[str, list] = collections.defaultdict(list)
    for r in conn.execute(
            "SELECT id, slug, url, source, owner_verified, payment, health, "
            "x402_ok, name FROM services"):
        o = db._endpoint_authority(r["url"] or "")
        if o:
            by[o].append(dict(r))
    out = []
    for origin, rows in sorted(by.items()):
        if len(rows) < 2:
            continue
        per_row = [_paytos(r["payment"]) for r in rows]
        flat = set().union(*per_row)
        fams = collections.defaultdict(set)
        for a in flat:
            fams[_family(a)].add(a)
        verified = sum(1 for r in rows if int(r["owner_verified"] or 0))
        item = {
            "origin": origin, "rows": len(rows),
            "sources": dict(collections.Counter(r["source"] for r in rows)),
            "paytos": sorted(flat), "rows_without_payto": sum(1 for x in per_row if not x),
            "owner_verified": verified,
            "sample": [(r["url"][len(origin):] or "/")[:60] for r in rows[:4]],
        }
        if db.is_gateway_origin(conn, origin):
            host = origin.split("://", 1)[1]
            shared = conn.execute("SELECT verdict FROM shared_hosts WHERE host=?",
                                  (host,)).fetchone()
            item["decision"], item["reason"] = "keep", (
                "shared host" if shared and shared["verdict"] == "shared"
                else "gateway: MCP/A2A list several services here")
        elif not flat:
            item["decision"], item["reason"] = "keep", "no payTo on any row"
        elif any(len(v) > 1 for v in fams.values()) or "other" in fams:
            item["decision"], item["reason"] = "keep", "several payTo addresses on one chain family"
        elif verified and verified != len(rows):
            item["decision"], item["reason"] = "keep", "owner verified only some rows"
        else:
            item["decision"] = "fold"
            item["reason"] = ("single seller (one origin, one payTo per chain family: %s)"
                              % "+".join(sorted(fams)))
            item["pay_to"] = sorted(fams.get("evm") or next(iter(fams.values())))[0]
        out.append(item)
    return out


def apply(plan: list[dict], log=print) -> dict:
    done = skipped = absorbed = 0
    for item in plan:
        if item.get("decision") != "fold":
            continue
        origin, pay_to = item["origin"], item["pay_to"]
        try:
            resources = crawlers.fetch_wellknown_resources(origin)
        except Exception:  # noqa: BLE001 - best effort; folded paths are the fallback
            resources = None
        if not (resources and resources.get("descriptor_observed")):
            resources = None

        def op():
            with db.writer(immediate=True) as conn:
                return fold_origin.fold(conn, origin, pay_to, resources,
                                        include_unpaid=True)
        res = db.with_retry(op)
        if res.get("skipped"):
            skipped += 1
        else:
            done += 1
            absorbed += int(res.get("absorbed") or 0)
        log(json.dumps({"origin": origin, **res}, ensure_ascii=False))
        time.sleep(0.05)
    return {"folded_origins": done, "skipped": skipped, "absorbed_rows": absorbed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="-")
    ap.add_argument("--apply")
    a = ap.parse_args(argv)
    if a.apply:
        plan = json.load(open(a.apply))
        print(json.dumps(apply(plan)))
        return 0
    with db.connect(read_only=True) as conn:
        items = review(conn)
    summary = collections.Counter((i["decision"], i["reason"]) for i in items)
    data = json.dumps(items, ensure_ascii=False, indent=1)
    if a.report == "-":
        print(data)
    else:
        open(a.report, "w").write(data)
    for (d, r), n in summary.most_common():
        print("%-5s %-45s origins=%d rows=%d" % (
            d, r, n, sum(i["rows"] for i in items if (i["decision"], i["reason"]) == (d, r))),
            file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
