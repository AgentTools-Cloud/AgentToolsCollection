"""Endpoint collisions created by owner edits.

`upsert_service` dedups on the normalised endpoint, so crawls cannot put two
rows on one URL. An owner edit can: `apply_listing_edits` checks that the new
endpoint sits on a host the account has verified, not that the endpoint is
still free.

Applying the edit is safe -- the worst it produces is a duplicate, and the
directory carried 2,192 duplicate groups for months. Deleting a row is not.
So the edit always goes through, and the collision is settled afterwards:
provenance decides the cases it can, an adjudicator model looks at the rest,
and every outcome is recorded with what could not be established.

Deliberately imports nothing from `directory` so `db` can call into it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

PENDING = "pending"
MERGE = "merge"
HOLD = "hold"

_GENERIC_CATS = {"", "general", "other", "uncategorized"}
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoint_collisions (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL,
    listing_id   INTEGER NOT NULL,
    occupant_id  INTEGER NOT NULL,
    user_id      INTEGER,
    field        TEXT    NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    decision     TEXT    NOT NULL,
    reason_code  TEXT,
    evidence     TEXT,
    uncertainty  TEXT,
    survivor_id  INTEGER,
    absorbed_id  INTEGER,
    created_at   INTEGER NOT NULL,
    resolved_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_endpoint_collisions_open
    ON endpoint_collisions(created_at) WHERE resolved_at IS NULL;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _jlist(v):
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except Exception:
        return []


# --------------------------------------------------------------------------
# detection


def find_occupant(conn: sqlite3.Connection, listing_id: int,
                  endpoint_key: str) -> Optional[sqlite3.Row]:
    """Another services row already registering `endpoint_key`.

    `endpoint_key` must come from the caller's own normaliser so this stays a
    single definition of what "the same endpoint" means.
    """
    if not endpoint_key:
        return None
    return conn.execute(
        "SELECT * FROM services WHERE lower(rtrim(url, '/'))=? AND id<>? "
        "ORDER BY id LIMIT 1",
        (endpoint_key, int(listing_id))).fetchone()


# --------------------------------------------------------------------------
# survivor choice -- same order as ops/merge_x402_duplicates.py


def _rank(r: sqlite3.Row, ignore_field: Optional[str] = None):
    curated = [f for f in _jlist(r["owner_edited"]) if f != ignore_field]
    return (1 if curated else 0,
            int(r["owner_verified"] or 0),
            1 if r["health"] == "ok" else 0,
            int(r["x402_ok"] or 0),
            float(r["quality_score"] or 0),
            int(r["resource_count"] or 0),
            -int(r["id"]))


_RANK_STEPS = ("owner_edited", "owner_verified", "health=ok", "x402_ok",
               "quality_score", "resource_count", "lower id")


def pick_survivor(a: sqlite3.Row, b: sqlite3.Row,
                  ignore_field: Optional[str] = None) -> tuple:
    """(survivor, absorbed, which rank step decided it).

    `ignore_field` drops one entry from owner_edited. The edit that caused the
    collision is recorded before we get here, so without this the row being
    edited always wins step 1 and a corrected placeholder would swallow the
    established listing it was corrected onto.
    """
    ra, rb = _rank(a, ignore_field), _rank(b, ignore_field)
    step = next((_RANK_STEPS[i] for i in range(len(ra)) if ra[i] != rb[i]),
                "lower id")
    return (a, b, step) if ra > rb else (b, a, step)


# --------------------------------------------------------------------------
# provenance: settle what can be settled without a model


def _live_owners(conn: sqlite3.Connection, host: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, user_id FROM domain_ownership "
        "WHERE host=? AND status='verified'", (host,))]


def _paytos(conn: sqlite3.Connection, service_id: int) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT lower(address) FROM service_paytos WHERE service_id=?",
        (int(service_id),)) if r[0]}


def classify(conn: sqlite3.Connection, host: str, user_id,
             listing_id: int, occupant_id: int,
             shared_host: bool = False) -> tuple:
    """(verdict, reason_code, evidence). verdict is MERGE or PENDING.

    PENDING means provenance was not enough, not that something is wrong.
    """
    if shared_host:
        # On a multi-tenant gateway, holding the host says nothing about who
        # owns a given path. ownership.claim already refuses these, so this is
        # a second line: never fold one tenant's listing into another's.
        return (PENDING, "shared_host",
                ["%s is a shared host, so host control does not identify the "
                 "operator of either endpoint" % host])

    mine, theirs = _paytos(conn, listing_id), _paytos(conn, occupant_id)
    if mine and theirs and mine != theirs:
        return (PENDING, "conflicting_payto",
                ["service_paytos differ: %s vs %s"
                 % (sorted(mine), sorted(theirs))])

    owners = _live_owners(conn, host)
    if not owners:
        return (MERGE, "occupant_unclaimed",
                ["no domain_ownership row for %s is currently verified" % host])

    same = [o for o in owners if user_id is not None and o["user_id"] == user_id]
    if same:
        return (MERGE, "same_account",
                ["domain_ownership %d for %s is verified and held by the "
                 "editing user %s" % (same[0]["id"], host, user_id)])

    # Different account holds the host now. That is not proof of a different
    # operator: an account is minted per API key, so one person routinely
    # shows up under several user_ids. Provenance cannot tell them apart.
    return (PENDING, "different_account_holds_host",
            ["%s is verified under user_id %s, the edit came from user_id %s"
             % (host, owners[0]["user_id"], user_id)])


# --------------------------------------------------------------------------
# merge -- same absorb rules as ops/merge_x402_duplicates.py


_MAXABLE = ("x402_ok", "resource_count", "tx_30d", "payto_tx_30d",
            "payto_payers_30d", "payto_checked", "confidence")


def merge_pair(conn: sqlite3.Connection, survivor_id: int,
               absorbed_id: int) -> dict:
    keep = conn.execute("SELECT * FROM services WHERE id=?",
                        (survivor_id,)).fetchone()
    drop = conn.execute("SELECT * FROM services WHERE id=?",
                        (absorbed_id,)).fetchone()
    if keep is None or drop is None:
        raise ValueError("merge_pair: %s/%s not both present"
                         % (survivor_id, absorbed_id))
    rows = (keep, drop)

    upd: dict = {"owner_verified": max(int(r["owner_verified"] or 0)
                                       for r in rows)}
    edited: list = []
    for r in rows:
        for f in _jlist(r["owner_edited"]):
            if f not in edited:
                edited.append(f)
    upd["owner_edited"] = json.dumps(edited) if edited else None
    for col in _MAXABLE:
        vals = [r[col] for r in rows if r[col] is not None]
        if vals:
            upd[col] = max(vals)
    rich = max(rows, key=lambda r: int(r["resource_count"] or 0))
    if rich["resource_samples"]:
        upd["resource_samples"] = rich["resource_samples"]
    chain = max(rows, key=lambda r: len(_jlist(r["chains"])))
    if _jlist(chain["chains"]):
        upd["chains"] = chain["chains"]
    lo = [r["price_min"] for r in rows if r["price_min"] is not None]
    hi = [r["price_max"] for r in rows if r["price_max"] is not None]
    if lo:
        upd["price_min"] = min(lo)
    if hi:
        upd["price_max"] = max(hi)
    if (keep["category"] or "").lower() in _GENERIC_CATS:
        for r in rows:
            if (r["category"] or "").lower() not in _GENERIC_CATS:
                upd["category"] = r["category"]
                break
    if not keep["payment"]:
        for r in rows:
            if r["payment"]:
                upd["payment"] = r["payment"]
                break
    born = [int(r["created_at"]) for r in rows if r["created_at"]]
    if born:
        upd["created_at"] = min(born)
    upd["updated_at"] = int(time.time())

    conn.execute("UPDATE services SET %s WHERE id=?"
                 % ", ".join("%s=?" % c for c in upd),
                 list(upd.values()) + [survivor_id])

    repointed = 0
    for table, col, kinded in (("listing_sources", "listing_id", True),
                               ("listing_edits", "listing_id", True),
                               ("service_paytos", "service_id", False)):
        where = "%s=?" % col + (" AND kind='x402'" if kinded else "")
        r = conn.execute("UPDATE OR IGNORE %s SET %s=? WHERE %s"
                         % (table, col, where), (survivor_id, absorbed_id))
        repointed += r.rowcount
        # UPDATE OR IGNORE skips rows that would collide with the survivor's
        # own; those are duplicates of what it already has.
        conn.execute("DELETE FROM %s WHERE %s" % (table, where),
                     (absorbed_id,))
    hist = conn.execute("DELETE FROM health_history WHERE service_id=?",
                        (absorbed_id,)).rowcount
    conn.execute("DELETE FROM services WHERE id=?", (absorbed_id,))
    return {"survivor_id": survivor_id, "absorbed_id": absorbed_id,
            "repointed": repointed, "health_history_deleted": hist,
            "absorbed_slug": drop["slug"], "survivor_slug": keep["slug"]}


# --------------------------------------------------------------------------
# ledger


def record(conn: sqlite3.Connection, *, listing_id: int, occupant_id: int,
           user_id, field: str, old_value, new_value, decision: str,
           reason_code: str, evidence, uncertainty=None,
           survivor_id=None, absorbed_id=None, kind: str = "x402") -> int:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO endpoint_collisions "
        "(kind, listing_id, occupant_id, user_id, field, old_value, new_value,"
        " decision, reason_code, evidence, uncertainty, survivor_id,"
        " absorbed_id, created_at, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, int(listing_id), int(occupant_id), user_id, field, old_value,
         new_value, decision, reason_code,
         json.dumps(list(evidence or []), ensure_ascii=False),
         json.dumps(list(uncertainty or []), ensure_ascii=False),
         survivor_id, absorbed_id, now,
         now if decision != PENDING else None))
    return int(cur.lastrowid)


def open_rows(conn: sqlite3.Connection, limit: int = 50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM endpoint_collisions WHERE resolved_at IS NULL "
        "ORDER BY id LIMIT ?", (int(limit),))]


def close_row(conn: sqlite3.Connection, row_id: int, *, decision: str,
              reason_code: str, evidence, uncertainty,
              survivor_id=None, absorbed_id=None) -> None:
    conn.execute(
        "UPDATE endpoint_collisions SET decision=?, reason_code=?, evidence=?,"
        " uncertainty=?, survivor_id=?, absorbed_id=?, resolved_at=? "
        "WHERE id=?",
        (decision, reason_code,
         json.dumps(list(evidence or []), ensure_ascii=False),
         json.dumps(list(uncertainty or []), ensure_ascii=False),
         survivor_id, absorbed_id, int(time.time()), int(row_id)))


# --------------------------------------------------------------------------
# adjudicator


_SYSTEM = """\
You are the duplicate-endpoint adjudicator for agent-tools.cloud, a free public
directory of x402 / MCP / A2A endpoints.

An owner edited a listing's endpoint field. The new endpoint is already held by
another listing. The edit has already been applied and both rows exist. You
decide whether they are one service that should be folded into a single row.

Your output is executed automatically. Emit one JSON object and nothing else.

## What happened before you were called

A deterministic layer already handled the cases that do not need judgement:

- The editor proved control of the host before the edit was accepted, so the
  new endpoint is never someone else's domain.
- If the occupying row is unclaimed, or the host's live ownership belongs to
  the editing account, it was merged without asking you.
- If the two rows carry different confirmed payTo addresses, it was held.

You see what is left: collisions where provenance alone did not settle it. So
do not expect a clean answer to exist. Reaching `hold` is a normal outcome, not
a failure.

## Input

- `edit`      - { user_id, field, old_value, new_value, via }
- `editing`   - the edited listing, current state
- `occupant`  - the listing that already held `new_value`
- `sources`   - listing_sources rows for both: which directories vouch for each
- `edits`     - listing_edits history for both, with the user_id behind each
- `ownership` - domain_ownership rows for the host, including pending and
                revoked, with `status` and `verified_at`
- `paytos`    - service_paytos rows for both: confirmed payout addresses
- `siblings`  - other listings on the same host, for context

Treat this as the complete evidence. Do not assume anything that is not in it.

`owner_verified` on a listing is a projection refreshed once a day and can be a
day stale; a row can still read 1 after its ownership was revoked. Judge
ownership from `ownership[].status`, never from `owner_verified`.

An account is minted per API key, so one operator routinely appears under
several user_ids. Different user_ids are not evidence of different people;
matching contact addresses, shared edit history or continuous ownership
handover are.

## Decisions

- `merge` - you can establish that both rows describe the same service. Name
  `survivor_id`. The other row is deleted; its sources, payout addresses and
  edit history move onto the survivor, which absorbs the better value of every
  measured column.

- `hold` - you cannot. Both rows stay exactly as they are and the collision
  stays on the ledger.

There is no reject and no escalation, and nothing is blocked either way. The
edit can only produce a duplicate, and this directory carried 2,192 duplicate
groups for months without harm. Deletion is the only irreversible act here, so
`merge` is the only decision that needs confidence. Do not strain toward it.

## When to merge

Merge when the evidence positively identifies one service: the edit moves a
placeholder (site root, docs page, a bare `/`) onto the paid endpoint the
occupant already describes; both rows trace to one operator through
`edits[].user_id`, contact addresses or a continuous ownership handover; the
occupant's `resource_samples` already advertise the edited row's new URL.

Identity must be shown, not inferred from resemblance.

## Choosing the survivor

Rank by, in order: has entries in `owner_edited`; live verified ownership;
`health = 'ok'`; `x402_ok`; `quality_score`; `resource_count`; lowest `id`.

Lowest `id` is last because it is the older row and the one more likely to be
linked from elsewhere - the loser's /services/<slug> stops resolving.

Say in `evidence` which rank step decided it.

## Not reasons to merge

- Same domain. Sibling endpoints on one domain are listed separately by design.
- Similar names or descriptions. Both are owner-supplied and unvalidated.
- One row scoring higher. Scores measure our own data completeness, not identity.
- Tidiness. A duplicate costs a row. A wrong merge destroys a listing someone
  built, and we cannot tell them what they lost.

## Output

{
  "decision": "merge" | "hold",
  "survivor_id": <int or null>,
  "absorbed_id": <int or null>,
  "reason_code": "<short_snake_case_tag>",
  "evidence": ["<a fact from the input that drove this>"],
  "uncertainty": ["<what you could not establish, and what would settle it>"]
}

`survivor_id` and `absorbed_id` are null unless the decision is `merge`, and
must be the two ids you were given.

Every `evidence` item names a field or row from the input. Do not restate these
rules; cite what you actually read.

`uncertainty` is never omitted. On `hold` it must be non-empty. On `merge` an
empty array claims you could point at a field in the input for every step of
the judgement -- if you assumed anything, list it even though you merged. Each
item says what is missing and what evidence would settle it, so someone can act
on it later. "Not sure" on its own is not an entry.
"""

_LISTING_FIELDS = ("id", "slug", "name", "description", "category", "url",
                   "mcp_url", "source", "source_id", "health", "x402_ok",
                   "quality_score", "resource_count", "resource_samples",
                   "tx_30d", "payto_payers_30d", "owner_verified",
                   "owner_edited", "created_at")


def build_payload(conn: sqlite3.Connection, listing_id: int, occupant_id: int,
                  host: str, edit: dict) -> dict:
    def listing(lid):
        r = conn.execute("SELECT * FROM services WHERE id=?", (lid,)).fetchone()
        if r is None:
            return None
        out = {}
        for f in _LISTING_FIELDS:
            v = r[f]
            if f == "resource_samples" and v:
                v = str(v)[:1500]
            out[f] = v
        return out

    def rows(sql, args):
        return [dict(x) for x in conn.execute(sql, args)]

    ids = (listing_id, occupant_id)
    return {
        "edit": edit,
        "editing": listing(listing_id),
        "occupant": listing(occupant_id),
        "sources": rows("SELECT listing_id, source, source_id FROM "
                        "listing_sources WHERE kind='x402' AND listing_id IN (?,?)",
                        ids),
        "edits": rows("SELECT listing_id, user_id, field, old_value, new_value,"
                      " via, applied_at FROM listing_edits WHERE kind='x402'"
                      " AND listing_id IN (?,?) ORDER BY id", ids),
        "ownership": rows("SELECT d.id, d.user_id, d.host, d.method, d.status,"
                          " d.verified_at, u.email FROM domain_ownership d"
                          " LEFT JOIN users u ON u.id=d.user_id"
                          " WHERE d.host=? ORDER BY d.id", (host,)),
        "paytos": rows("SELECT service_id, chain, address FROM service_paytos"
                       " WHERE service_id IN (?,?)", ids),
        "siblings": rows("SELECT id, slug, url FROM services"
                         " WHERE lower(url) LIKE ? AND id NOT IN (?,?) LIMIT 20",
                         ("%" + host.lower() + "%",) + ids),
    }


def _conf():
    base = (os.getenv("AGENT_TOOLS_ADJUDICATOR_BASE_URL") or "").rstrip("/")
    key = os.getenv("AGENT_TOOLS_ADJUDICATOR_API_KEY") or ""
    model = os.getenv("AGENT_TOOLS_ADJUDICATOR_MODEL") or ""
    return base, key, model


def adjudicate(payload: dict, timeout: float = 120.0) -> Optional[dict]:
    """Ask the adjudicator model. Returns the parsed verdict or None.

    None means we could not get an answer, which is treated as `hold`: on any
    failure the safe behaviour is to leave both rows alone.
    """
    base, key, model = _conf()
    if not (base and key and model):
        log.warning("collision adjudicator not configured")
        return None
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",
             "content": "An owner edit collided with an existing listing. "
                        "Adjudicate.\n\n"
                        + json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "temperature": 0.0,
        "max_completion_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    try:
        r = httpx.post("%s/v1/chat/completions" % base, json=body,
                       headers={"Authorization": "Bearer %s" % key},
                       timeout=httpx.Timeout(connect=10.0, read=timeout,
                                             write=10.0, pool=10.0))
        r.raise_for_status()
        content = ((r.json().get("choices") or [{}])[0]
                   .get("message", {}).get("content") or "")
    except Exception as e:  # noqa: BLE001 - a failed call means hold
        log.warning("collision adjudicator call failed: %r", e)
        return None

    try:
        verdict = json.loads(content)
    except Exception:
        m = _JSON_OBJ_RE.search(content)
        if not m:
            log.warning("collision adjudicator returned no JSON")
            return None
        try:
            verdict = json.loads(m.group(0))
        except Exception:
            log.warning("collision adjudicator returned unparseable JSON")
            return None
    if not isinstance(verdict, dict):
        return None
    return verdict


def check_verdict(verdict: dict, listing_id: int, occupant_id: int) -> tuple:
    """(ok, reason). A verdict we cannot execute verbatim is not executed."""
    if verdict.get("decision") not in (MERGE, HOLD):
        return False, "decision is %r" % verdict.get("decision")
    if not verdict.get("uncertainty") and verdict.get("decision") == HOLD:
        return False, "hold without uncertainty"
    if verdict["decision"] == HOLD:
        return True, ""
    pair = {int(listing_id), int(occupant_id)}
    try:
        named = {int(verdict.get("survivor_id")),
                 int(verdict.get("absorbed_id"))}
    except (TypeError, ValueError):
        return False, "merge without a usable id pair"
    if named != pair:
        return False, "merge names %s, the collision is %s" % (
            sorted(named), sorted(pair))
    return True, ""


# --------------------------------------------------------------------------
# entry point used by apply_listing_edits


def on_endpoint_changed(conn: sqlite3.Connection, *, listing_id: int,
                        endpoint_key: str, host: str, user_id, field: str,
                        old_value, new_value,
                        shared_host: bool = False) -> Optional[dict]:
    """Settle, or shelve, a collision the edit just created.

    Never raises: an owner edit must not fail because the bookkeeping did.
    """
    try:
        occupant = find_occupant(conn, listing_id, endpoint_key)
        if occupant is None:
            return None
        ensure_schema(conn)
        occupant_id = int(occupant["id"])
        verdict, reason, evidence = classify(conn, host, user_id, listing_id,
                                             occupant_id, shared_host)
        if verdict == MERGE:
            mine = conn.execute("SELECT * FROM services WHERE id=?",
                                (listing_id,)).fetchone()
            keep, drop, step = pick_survivor(mine, occupant, ignore_field=field)
            out = merge_pair(conn, int(keep["id"]), int(drop["id"]))
            record(conn, listing_id=listing_id, occupant_id=occupant_id,
                   user_id=user_id, field=field, old_value=old_value,
                   new_value=new_value, decision=MERGE, reason_code=reason,
                   evidence=list(evidence) + ["survivor decided by " + step],
                   survivor_id=out["survivor_id"],
                   absorbed_id=out["absorbed_id"])
            log.info("collision merged %s into %s (%s)",
                     out["absorbed_slug"], out["survivor_slug"], reason)
            return out
        record(conn, listing_id=listing_id, occupant_id=occupant_id,
               user_id=user_id, field=field, old_value=old_value,
               new_value=new_value, decision=PENDING, reason_code=reason,
               evidence=evidence)
        log.info("collision shelved: listing %s vs %s (%s)",
                 listing_id, occupant_id, reason)
        return {"pending": True, "occupant_id": occupant_id}
    except Exception as e:  # noqa: BLE001
        log.warning("collision handling failed for listing %s: %r",
                    listing_id, e)
        return None
