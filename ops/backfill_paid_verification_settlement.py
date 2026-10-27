#!/usr/bin/env python3
"""Re-verify legacy sponsored settlements against Base and update their evidence."""
from __future__ import annotations

import argparse
import json
import time

from dotenv import load_dotenv

load_dotenv()

from directory import db, paid_verify as pv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write verified results")
    args = parser.parse_args()

    db.init_db()
    with db.connect(read_only=True) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM paid_verifications WHERE settled=1 AND tx_hash IS NOT NULL "
            "ORDER BY created_at")]

    updates: list[tuple[dict, bool, str, dict, int]] = []
    for row in rows:
        try:
            checks = json.loads(row.get("checks") or "{}")
        except (TypeError, ValueError, RecursionError):
            checks = {}
        if checks.get("settlement_check") == "verified":
            continue
        try:
            req, _ = pv._stored_requirements(row["requirements"])
            verified, reason = pv.verify_onchain_settlement(
                req, row["payer"], row["nonce"], row["tx_hash"])
        except pv.VerifyError:
            verified, reason = False, "invalid_verification"
        checks["settled"] = bool(verified)
        checks["settlement_check"] = reason
        checks["settlement_checked_at"] = int(time.time())
        score = int(row.get("score") or 0)
        if not verified:
            score = max(0, score - 20)
        updates.append((row, verified, reason, checks, score))
        print(json.dumps({
            "id": row["id"], "tx_hash": row["tx_hash"],
            "verified": verified, "reason": reason,
            "action": "update" if args.apply else "dry-run",
        }, separators=(",", ":")))

    if args.apply:
        with db.writer(immediate=True) as conn:
            for row, verified, _reason, checks, score in updates:
                conn.execute(
                    "UPDATE paid_verifications SET settled=?,score=?,checks=? WHERE id=?",
                    (int(verified), score, json.dumps(checks, separators=(",", ":")), row["id"]))
    print(json.dumps({"rows": len(rows), "updated": len(updates), "applied": args.apply},
                     separators=(",", ":")))
    return 0 if all(item[1] for item in updates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
