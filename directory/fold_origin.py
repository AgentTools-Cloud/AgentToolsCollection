"""Fold every x402 row of one origin that shares one payTo into a single listing.

Usage:
  python -m directory.fold_origin --origin https://host [--pay-to 0x...] [--apply]

Dry run by default. The survivor is the oldest row (lowest id) unless an
owner-verified row exists, in which case that one is kept. Each absorbed row
goes through collisions.merge_pair (provenance, payTo history and edits are
repointed; health history of the absorbed row is dropped), and its slug is
recorded on the survivor as source 'x402' / 'x402:<slug>' so
/services/<old-slug> answers 301. Supplemental well-known bindings that
pointed at an absorbed row are repointed to the survivor.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import collisions, crawlers, db


def _rows(conn, origin: str, pay_to: str | None, include_unpaid: bool = False):
    out = []
    for row in conn.execute(
            "SELECT * FROM services WHERE url = ? COLLATE NOCASE "
            "OR url LIKE ? COLLATE NOCASE ORDER BY id", (origin, origin + "/%")):
        if db._endpoint_authority(row["url"] or "") != origin:
            continue
        if pay_to:
            from .fold_review import _paytos
            seen = {a.lower() for a in _paytos(row["payment"])}
            # A row paying the seller's address on another chain family
            # (EVM + Solana) belongs to the same seller; the caller has
            # checked there is only one address per family on this origin.
            if (pay_to.lower() not in seen and seen and not include_unpaid):
                continue
            if not seen and not include_unpaid:
                continue
        out.append(dict(row))
    return out


def fold(conn, origin: str, pay_to: str | None, resources: dict | None,
         include_unpaid: bool = False, allow_gateway: bool = False) -> dict:
    if not allow_gateway and db.is_gateway_origin(conn, origin):
        return {"rows": 0, "absorbed": 0, "skipped": "gateway"}
    rows = _rows(conn, origin, pay_to, include_unpaid)
    if len(rows) < 2:
        return {"rows": len(rows), "absorbed": 0}
    verified = [r for r in rows if int(r.get("owner_verified") or 0)]
    # Survivor: an owner-verified row, else one that is a working paid
    # endpoint (a bare root often answers 200 and reads as not-x402), else
    # the oldest.
    working = [r for r in rows if int(r.get("x402_ok") or 0)
               and (r.get("health") or "") == "ok"]
    keep = (verified or working or rows)[0]
    sid = int(keep["id"])
    absorbed = []
    for r in rows:
        if int(r["id"]) == sid:
            continue
        rid = int(r["id"])
        conn.execute(
            "UPDATE OR IGNORE wellknown_endpoint_sources SET listing_id=? "
            "WHERE kind='x402' AND listing_id=?", (sid, rid))
        conn.execute("UPDATE OR IGNORE wellknown_reviewed_aliases SET listing_id=? "
                     "WHERE kind='x402' AND listing_id=?", (sid, rid))
        res = collisions.merge_pair(conn, sid, rid)
        # Keep the absorbed slug resolvable (301) and its endpoint attached.
        conn.execute(
            "INSERT OR REPLACE INTO service_folds (absorbed_slug, absorbed_url, "
            "survivor_id, folded_at) VALUES (?, ?, ?, ?)",
            (res["absorbed_slug"], db._exact_endpoint(r.get("url") or ""), sid,
             int(time.time())))
        conn.execute("UPDATE service_folds SET survivor_id=? WHERE survivor_id=?",
                     (sid, rid))
        absorbed.append(res["absorbed_slug"])
    # The survivor keeps its own paid endpoint as url: the bare origin often
    # answers 200, and re-verification would then read it as not x402.
    upd = {"updated_at": int(time.time())}
    if not (resources and resources.get("resource_count")):
        # Descriptor unreadable (e.g. over the size cap): the folded rows'
        # own verified paths are the resource list.
        paths = sorted({r["url"] for r in rows if r.get("url")})
        resources = {"resource_count": len(paths),
                     "resource_samples": [{"url": u, "kind": "x402-resource"}
                                          for u in paths[:50]]}
    if int(resources["resource_count"]) > int(keep.get("resource_count") or 0):
        upd["resource_count"] = int(resources["resource_count"])
        upd["resource_samples"] = json.dumps(resources.get("resource_samples"))
        if resources.get("well_known_url"):
            upd["well_known_url"] = resources["well_known_url"]
    conn.execute("UPDATE services SET %s WHERE id=?"
                 % ", ".join("%s=?" % k for k in upd), [*upd.values(), sid])
    return {"rows": len(rows), "survivor_id": sid, "survivor_slug": keep["slug"],
            "absorbed": len(absorbed), "sample_absorbed": absorbed[:3]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--pay-to")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    origin = db._endpoint_authority(a.origin)
    if not origin:
        print("invalid origin", file=sys.stderr)
        return 2
    try:
        resources = crawlers.fetch_wellknown_resources(origin)
    except Exception as e:  # noqa: BLE001
        print("resource discovery failed: %r" % e, file=sys.stderr)
        resources = None
    with db.writer(immediate=True) as conn:
        out = fold(conn, origin, a.pay_to, resources)
        out["resource_count"] = (resources or {}).get("resource_count")
        if not a.apply:
            conn.rollback()
            out["dry_run"] = True
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
