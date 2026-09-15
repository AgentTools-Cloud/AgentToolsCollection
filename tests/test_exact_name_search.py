"""Strict duplicate names stay together and identify each URL."""
import os
import sqlite3
import sys

SOURCE_DB = os.environ.get(
    "AGENT_TOOLS_SOURCE_DB", "/opt/mcpserver/data/agent-tools.db")
if not os.path.exists(SOURCE_DB):
    print("SKIP: no database at %s" % SOURCE_DB)
    raise SystemExit(0)

COPY = "/tmp/test-exact-name-%d.db" % os.getpid()
sqlite3.connect(SOURCE_DB).backup(sqlite3.connect(COPY))
os.environ["AGENT_TOOLS_DB_PATH"] = COPY
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import db, resources  # noqa: E402

db.init_db()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from directory.routes import router  # noqa: E402

app = FastAPI()
app.include_router(router)
client = TestClient(app)
passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %-60s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %-60s %s" % (label, detail))


with db.connect(read_only=True) as conn:
    group = resources.exact_name_matches(conn, " Agent Trust API ")
    unique = resources.exact_name_matches(
        conn, "Agent Trust API — Delegated Payment Authority Verification")
    collapsed = resources.exact_name_matches(conn, "builda.company")
    two_urls = resources.exact_name_matches(conn, "2s.io")

expected = {
    "https://agent-trust-api-496e.onrender.com",
    "https://agent-trust-api-production.up.railway.app",
}
check("case/space-normalized exact query returns a group", group is not None)
check("all distinct products are returned", group and group["count"] == 2)
check("the two known URLs identify the products",
      group and {x["url"] for x in group["matches"]} == expected)
check("each product has host, protocol and listing link",
      group and all(x["host"] and x["protocols"] and x["listings"]
                    for x in group["matches"]))
check("the response promises a complete group", group and group["complete"] is True)
check("a unique full name does not claim ambiguity", unique is None)
check("1916 rows at one URL collapse to one product/no warning", collapsed is None)
check("884 rows at two URLs return two products, not 884",
      two_urls and two_urls["count"] == 2 and two_urls["listing_count"] == 884)

with db.connect(read_only=True) as conn:
    discovered = resources.exact_name_groups(
        conn, "npm package risk", ["Agent Trust API"])
check("an ordinary hit expands every strict duplicate-name URL",
      len(discovered) == 1 and discovered[0]["count"] == 2)
check("a group discovered through a hit is not marked query_exact",
      discovered and discovered[0]["query_exact"] is False)
with db.connect(read_only=True) as conn:
    no_query = resources.exact_name_groups(
        conn, None, ["Agent Trust API"])
check("directory browsing without a query does not show ambiguity groups",
      no_query == [])

print("\n=== APIs ===")
for path, result_key in (
        ("/api/v1/search", "services"),
        ("/api/v1/mcp/search", "servers"),
        ("/api/v1/a2a/search", "agents"),
        ("/api/v1/resources/search", "resources")):
    response = client.get(path, params={"q": "Agent Trust API", "limit": 1})
    body = response.json()
    exact = body.get("exact_name_matches")
    check(path + " returns 200", response.status_code == 200)
    check(path + " keeps the ranked window at limit=1",
          len(body.get(result_key, [])) == 1)
    check(path + " returns the complete exact group",
          exact and exact["count"] == 2 and exact["complete"] is True)
    check(path + " exposes URL on every exact match",
          exact and all(x.get("url") for x in exact["matches"]))
    groups = body.get("exact_name_groups", [])
    check(path + " also exposes the plural group collection",
          any(group["name"] == "Agent Trust API" and group["count"] == 2
              for group in groups), len(groups))

print("\n=== hit-triggered groups ===")
# The query itself need not equal the duplicated name. Once a ranked result
# hits that name, the complete group must accompany it.
with db.connect(read_only=True) as conn:
    groups = resources.exact_name_groups(
        conn, "npm package risk", ["Agent Trust API"])
match = next((group for group in groups
              if group["name"] == "Agent Trust API"), None)
check("ordinary search hits expand the complete duplicate-name group",
      match and match["count"] == 2 and match["query_exact"] is False)

response = client.get("/api/v1/a2a/search",
                      params={"q": "on-chain reputation wallets", "limit": 100})
body = response.json()
agent_names = [agent.get("name") for agent in body.get("agents", [])]
match = next((group for group in body.get("exact_name_groups", [])
              if group["name"] == "Agent Trust API"), None)
check("a real non-name query ranks one Agent Trust API result",
      "Agent Trust API" in agent_names)
check("that hit expands both exact-name URLs outside normal pagination",
      match and match["count"] == 2 and match["query_exact"] is False)

print("\n=== browser + HTMX surfaces ===")
for path in ("/x402", "/mcp", "/a2a"):
    response = client.get(path)
    check(path + " has no ambiguity banner without a query",
          "different URLs" not in response.text)

for path in ("/x402", "/mcp", "/a2a",
             "/_partials/services", "/_partials/mcp", "/_partials/a2a"):
    response = client.get(path, params={"q": "Agent Trust API"})
    text = response.text
    check(path + " returns 200", response.status_code == 200)
    check(path + " labels the exact-name ambiguity",
          "is shared by 2 different URLs" in text)
    check(path + " prints both distinguishing URLs",
          all(url in text for url in expected))

print("\n%d passed, %d failed" % (passed, failed))
os.unlink(COPY)
raise SystemExit(1 if failed else 0)
