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


def _rows(conn, origin: str, pay_to: str | None):
    out = []
    for row in conn.execute(
            "SELECT * FROM services WHERE url = ? COLLATE NOCASE "
            "OR url LIKE ? COLLATE NOCASE ORDER BY id", (origin, origin + "/%")):
        if db._endpoint_authority(row["url"] or "") != origin:
            continue
        if pay_to:
            try:
                pay = json.loads(row["payment"] or "null") or {}
            except (TypeError, ValueError):
                pay = {}
            seen = {str(pay.get("pay_to") or "").lower()}
            seen |= {str(a.get("payTo") or a.get("pay_to") or "").lower()
                     for a in pay.get("accepts") or [] if isinstance(a, dict)}
            if pay_to.lower() not in seen:
                continue
        out.append(dict(row))
    return out


def fold(conn, origin: str, pay_to: str | None, resources: dict | None) -> dict:
    rows = _rows(conn, origin, pay_to)
    if len(rows) < 2:
        return {"rows": len(rows), "absorbed": 0}
    verified = [r for r in rows if int(r.get("owner_verified") or 0)]
    keep = (verified or rows)[0]
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
        # Keep the absorbed slug resolvable (301) from the survivor.
        conn.execute(
            "INSERT OR IGNORE INTO listing_sources (kind, listing_id, source, "
            "source_id, source_url, first_seen, last_seen) "
            "VALUES ('x402', ?, ?, ?, ?, ?, ?)",
            (sid, "x402-fold:" + res["absorbed_slug"], "x402:" + res["absorbed_slug"],
             r.get("url"), int(time.time()), int(time.time())))
        absorbed.append(res["absorbed_slug"])
    upd = {"url": origin, "updated_at": int(time.time())}
    if resources and resources.get("resource_count"):
        upd["resource_count"] = int(resources["resource_count"])
        upd["resource_samples"] = json.dumps(resources.get("resource_samples"))
        if resources.get("well_known_url"):
            upd["well_known_url"] = resources["well_known_url"]
    if db._find_exact_endpoint_row(conn, "services", ("url",), origin,
                                   exclude_id=sid) is not None:
        upd.pop("url")
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
