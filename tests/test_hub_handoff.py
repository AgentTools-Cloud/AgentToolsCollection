"""Hub sign-in handoff: the hub credential is separate and only reaches the hub.

Run: PYTHONPATH=/opt/mcpserver .venv/bin/python tests/test_hub_handoff.py
"""
import os
import sqlite3
import sys
import tempfile

os.environ["SESSION_SECRET"] = "test-secret-not-production"
os.environ.pop("HUB_COOKIE_DOMAIN", None)
os.environ.pop("HUB_PUBLIC_URL", None)
DB = tempfile.mktemp(suffix=".db")
os.environ["AGENT_TOOLS_DB_PATH"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import auth, db  # noqa: E402

db.init_db(DB)
with db.writer() as conn:
    UID = db.upsert_user(conn, provider="github", provider_uid="42",
                         login="tester")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(auth.router)
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %-60s %s" % (label, extra))
    else:
        fail += 1
        print("  FAIL %-60s %s" % (label, extra))


HUB = "https://hub.agent-tools.cloud"
check("hub URL is an allowed return target", auth._safe_next(HUB + "/my") == HUB + "/my")
for bad in ("https://evil.example/my", "https://hub.agent-tools.cloud.evil.example/",
            "https://user@hub.agent-tools.cloud/", "https://hub.agent-tools.cloud:8443/",
            "http://hub.agent-tools.cloud/", "//evil.example", "/\\evil.example"):
    check("rejects open redirect " + bad, auth._safe_next(bad) == "/")
check("relative paths still work", auth._safe_next("/account") == "/account")

c = TestClient(app, base_url="https://agent-tools.cloud")
r = c.get("/auth/hub", follow_redirects=False)
check("anonymous handoff goes through GitHub login", r.status_code == 302
      and r.headers["location"].startswith("/auth/github/login?next=%2Fauth%2Fhub"))
check("anonymous handoff sets no hub cookie", "atc_hub" not in r.headers.get("set-cookie", ""))

c.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": UID}, 3600))
r = c.get("/auth/hub", params={"next": HUB + "/my"}, follow_redirects=False)
cookie = r.headers.get("set-cookie", "")
check("signed-in handoff redirects to the hub", r.status_code == 302
      and r.headers["location"] == HUB + "/my")
check("hub cookie is scoped to the parent domain",
      "atc_hub=" in cookie and "Domain=.agent-tools.cloud" in cookie)
check("hub cookie is HttpOnly, Secure and Lax",
      "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie)
token = cookie.split("atc_hub=", 1)[1].split(";", 1)[0].strip('"')
data = auth.unsign(token)
check("hub cookie carries uid and hub audience",
      data and data.get("uid") == UID and data.get("aud") == auth.HUB_AUDIENCE)
check("handoff response is not cacheable", r.headers.get("cache-control") == "private, no-store")

c2 = TestClient(app, base_url="https://agent-tools.cloud")
c2.cookies.set(auth.SESSION_COOKIE, token)
r = c2.get("/auth/hub", follow_redirects=False)
check("hub credential cannot act as a main-site session",
      r.headers["location"].startswith("/auth/github/login"))

c3 = TestClient(app, base_url="https://agent-tools.cloud")
c3.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": UID}, 3600))
r = c3.get("/auth/logout", follow_redirects=False)
cookies = r.headers.get_list("set-cookie")
check("logout clears the main session", any(x.startswith("atc_session=") for x in cookies))
check("logout clears the hub credential on the parent domain",
      any(x.startswith("atc_hub=") and "Domain=.agent-tools.cloud" in x for x in cookies))

with sqlite3.connect(DB) as conn:
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (UID,))
c4 = TestClient(app, base_url="https://agent-tools.cloud")
c4.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": UID}, 3600))
r = c4.get("/auth/hub", follow_redirects=False)
check("blocked users get no hub credential", "atc_hub" not in r.headers.get("set-cookie", ""))

os.unlink(DB)
print("\n%d passed, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
