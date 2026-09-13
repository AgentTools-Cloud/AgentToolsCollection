"""API-key ownership: the JSON door must grant exactly what the web form does.

Run: PYTHONPATH=/opt/mcpserver .venv/bin/python tests/test_api_keys.py

Everything happens on a throwaway copy of the live database, because the point
of the suite is to watch real listings refuse to move.
"""
import json
import os
import sqlite3
import sys

SRC = os.environ.get("AGENT_TOOLS_SOURCE_DB", "/opt/mcpserver/data/agent-tools.db")
if not os.path.exists(SRC):
    print("SKIP: no database at %s" % SRC)
    raise SystemExit(0)

CP = "/tmp/test-api-keys-%d.db" % os.getpid()
sqlite3.connect(SRC).backup(sqlite3.connect(CP))
os.environ["AGENT_TOOLS_DB_PATH"] = CP
os.environ["AGENT_TOOLS_KEY_MINT_PER_DAY"] = "200"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import db, ownership  # noqa: E402

db.init_db()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from directory.routes import router  # noqa: E402

app = FastAPI()
app.include_router(router)
c = TestClient(app)


def raw():
    return sqlite3.connect(CP)


ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %-56s %s" % (label, extra))
    else:
        fail += 1
        print("  FAIL %-56s %s" % (label, extra))


cols = [r[1] for r in raw().execute("PRAGMA table_info(listing_edits)")]
check("listing_edits.via is migrated onto an existing database", "via" in cols)

# A key that owns a host, and a key that owns nothing.
ownership.probe = lambda method, host, token: (True, "stubbed")
HOST = "agent-tools.cloud"
owner_key = c.post("/api/v1/keys", json={"name": "owner"}).json()["api_key"]
stranger_key = c.post("/api/v1/keys", json={"name": "stranger"}).json()["api_key"]
with db.connect(read_only=True) as conn:
    owner_uid = int(db.api_key_row(conn, owner_key)["user_id"])
with db.writer() as conn:
    cid, tok = ownership.issue(conn, owner_uid, HOST, "wellknown_file")
verified = c.post("/api/v1/claims/%d/verify" % cid, json={"token": tok},
                  headers={"Authorization": "Bearer " + owner_key})
assert verified.status_code == 200, verified.text
listing = verified.json()["listings"][0]
KIND, SLUG = listing["kind"], listing["slug"]
TABLE = db._KIND_TABLE[KIND]
OWN = {"Authorization": "Bearer " + owner_key}
STRANGER = {"Authorization": "Bearer " + stranger_key}
URL = "/api/v1/listings/%s/%s" % (KIND, SLUG)


def snapshot():
    """Per-row baseline. An aggregate would hide a single listing moving."""
    cur = raw().execute("SELECT * FROM %s WHERE slug=?" % TABLE, (SLUG,))
    names = [x[0] for x in cur.description]
    return dict(zip(names, cur.fetchone()))


base = snapshot()

check("a key is minted without credentials", owner_key.startswith("atc_live_"))
check("GET without credentials is 401", c.get(URL).status_code == 401)

r = c.get(URL, headers=STRANGER)
check("GET with a key that owns nothing is 403", r.status_code == 403)
check("the 403 says where to claim the host",
      "/api/v1/claims" in json.dumps(r.json()))

r = c.get(URL, headers=OWN)
check("the owner reads the fields PATCH expects back",
      r.status_code == 200
      and set(r.json()["fields"]) == set(db._EDITABLE_FIELDS[KIND]))

check("PATCH without credentials is 401",
      c.patch(URL, json={"name": "X"}).status_code == 401)
check("PATCH from a stranger is 403",
      c.patch(URL, json={"name": "HIJACKED"}, headers=STRANGER).status_code == 403)
check("a refused PATCH leaves the row untouched", snapshot()["name"] == base["name"])

r = c.patch(URL, json={"description": "Owner-written description."}, headers=OWN)
check("the owner's PATCH succeeds", r.status_code == 200)
check("and the value is really stored",
      snapshot()["description"] == "Owner-written description.")
check("the audit trail records it as an api edit",
      raw().execute("SELECT field, via, user_id FROM listing_edits "
                    "ORDER BY id DESC LIMIT 1").fetchone()
      == ("description", "api", owner_uid))

check("columns we measure are not editable",
      c.patch(URL, json={"quality_score": "999"}, headers=OWN).status_code == 422)
check("and stay as they were",
      snapshot().get("quality_score") == base.get("quality_score"))
check("an empty PATCH is refused", c.patch(URL, json={}, headers=OWN).status_code == 422)

endpoints = db._ENDPOINT_FIELDS.get(KIND, ())
if endpoints:
    field = endpoints[0]
    check("an endpoint cannot move to an unverified host",
          c.patch(URL, json={field: "https://evil.example/steal"},
                  headers=OWN).status_code == 422)
    check("and the endpoint is unchanged", snapshot()[field] == base[field])
    check("an endpoint may move within a verified host",
          c.patch(URL, json={field: "https://%s/moved" % HOST},
                  headers=OWN).status_code == 200)
    measured = [col for col in db._MEASURED_RESET.get(KIND, ())
                if col in base and base[col] is not None]
    after = snapshot()
    check("moving an endpoint drops the reputation earned at the old one",
          all(after.get(col) is None for col in measured),
          "cleared %d columns" % len(measured))

with db.connect(read_only=True) as conn:
    row = db.api_key_row(conn, stranger_key)
with db.writer() as conn:
    db.revoke_api_key(conn, row["id"], row["user_id"])
check("a revoked key is no longer a credential",
      c.get(URL, headers=STRANGER).status_code == 401)
check("an unknown slug is 404",
      c.get("/api/v1/listings/%s/no-such-slug" % KIND, headers=OWN).status_code == 404)
check("an unknown kind is 404",
      c.get("/api/v1/listings/bogus/x", headers=OWN).status_code == 404)

claimed = raw().execute(
    "SELECT endpoint_url, name FROM mcp_servers WHERE owner_verified=1 LIMIT 1"
).fetchone()
if claimed:
    r = c.post("/api/v1/mcp/submit",
               json={"url": claimed[0], "name": "ANONYMOUS OVERWRITE",
                     "contact": "someone@example.com"})
    check("anonymous submit still cannot rewrite a claimed listing",
          r.status_code == 409)
    check("and its name survives",
          raw().execute("SELECT name FROM mcp_servers WHERE endpoint_url=?",
                        (claimed[0],)).fetchone()[0] != "ANONYMOUS OVERWRITE")

os.unlink(CP)
print("%d ok, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
