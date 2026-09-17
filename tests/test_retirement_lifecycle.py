#!/usr/bin/env python3
"""Integration coverage for retirement lifecycle and public archive behavior."""
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid

SOURCE=os.environ.get("AGENT_TOOLS_SOURCE_DB","/opt/mcpserver/data/agent-tools.db")
PREVIOUS_DB_ENV=os.environ.get("AGENT_TOOLS_DB_PATH")
tmp=tempfile.mkdtemp(prefix="retirement-lifecycle-")
path=os.path.join(tmp,"test.db")
s=sqlite3.connect(SOURCE); d=sqlite3.connect(path); s.backup(d); d.close(); s.close()
os.environ["AGENT_TOOLS_DB_PATH"]=path
sys.path.insert(0,"/opt/mcpserver")
from directory import db
db=importlib.reload(db)

db.init_db()
from directory import a2a
from fastapi.testclient import TestClient
from server import app
client=TestClient(app)

passed=failed=0
def check(label,ok,detail=""):
 global passed,failed
 if ok: passed+=1; print("  ok  ",label,detail)
 else: failed+=1; print("  FAIL",label,detail)

# Create one listing of each kind and preserve dependencies.
run_id=uuid.uuid4().hex[:12]
base_url="https://%s.retire.example"%run_id
fixtures={
 "x402":{"slug":"retire-x402-"+run_id,"name":"Retire X402 "+run_id,"url":base_url+"/api","source":"test","source_id":"x-"+run_id},
 "mcp":{"slug":"retire-mcp-"+run_id,"name":"Retire MCP "+run_id,"endpoint_url":base_url+"/mcp","source":"test","source_id":"m-"+run_id},
 "a2a":{"slug":"retire-a2a-"+run_id,"name":"Retire A2A "+run_id,"endpoint_url":base_url+"/a2a","card_url":base_url+"/.well-known/agent-card.json","source":"test","source_id":"a-"+run_id},
}
ids={}
with db.writer() as c:
 _,ids["x402"]=db.upsert_service(c,dict(fixtures["x402"]))
 _,ids["mcp"]=db.upsert_mcp_server(c,dict(fixtures["mcp"]))
 _,ids["a2a"]=db.upsert_a2a_agent(c,dict(fixtures["a2a"]))
 c.execute("insert into health_history(service_id,checked_at,status) values(?,?,?)",(ids["x402"],1,"ok"))
 c.execute("insert into mcp_health_history(server_id,checked_at,status) values(?,?,?)",(ids["mcp"],1,"ok"))

for kind in ("x402","mcp","a2a"):
 with db.writer() as c:
  row=db.retire_listing(c,kind,fixtures[kind]["slug"],"operator request","test-admin",request_email="owner@example.com")
 check(kind+" active tombstone",row["status"]=="active")
 route={"x402":"/services/","mcp":"/mcp/servers/","a2a":"/a2a/agents/"}[kind]+fixtures[kind]["slug"]
 response=client.get(route)
 check(kind+" detail is 410",response.status_code==410)
 check(kind+" detail is noindex",'noindex,nofollow' in response.text.replace(" ",""))
 check(kind+" x-robots-tag",response.headers.get("x-robots-tag")=="noindex, nofollow")
 api_route={"x402":"/api/v1/services/","mcp":"/api/v1/mcp/servers/","a2a":"/api/v1/a2a/agents/"}[kind]+fixtures[kind]["slug"]
 api_response=client.get(api_route)
 check(kind+" JSON detail is 410",api_response.status_code==410)

# All writes are blocked centrally, regardless of call site.
for kind,fn in (("x402",db.upsert_service),("mcp",db.upsert_mcp_server),("a2a",db.upsert_a2a_agent)):
 try:
  with db.writer() as c: fn(c,dict(fixtures[kind]))
  check(kind+" central upsert blocked",False)
 except db.RetiredListingError:
  check(kind+" central upsert blocked",True)

try:
 with db.writer() as c:
  db.upsert_a2a_agent(c,{"slug":"cross-protocol-"+run_id,
         "name":"Cross Protocol "+run_id,
         "endpoint_url":fixtures["mcp"]["endpoint_url"],
         "source":"test","source_id":"cross"})
 check("cross-protocol URL blocked",False)
except db.RetiredListingError:
 check("cross-protocol URL blocked",True)

# A retired row must not roll back other agents from the same crawler batch.
batch_row=dict(fixtures["a2a"])
batch_row.update(slug="batch-a2a-peer-"+run_id,
         endpoint_url=base_url+"/a2a-peer",
         card_url=base_url+"/a2a-peer/card.json",
                 source_id="a-peer-"+run_id)
inserted,updated=a2a._upsert_rows([dict(fixtures["a2a"]),batch_row])
check("A2A crawler skips retired row",inserted==1 and updated==0,
  "inserted=%s updated=%s"%(inserted,updated))
with db.connect(read_only=True) as c:
 check("A2A crawler keeps batch peer",
   db.get_a2a_by_slug(c,batch_row["slug"]) is not None)

# Submission endpoints return a loud retirement response.
r=client.post("/api/v1/submit",json={"name":"x","url":fixtures["x402"]["url"],"contact":"x@example.com"})
check("x402 resubmit is 410",r.status_code==410)
r=client.post("/api/v1/mcp/submit",json={"url":fixtures["mcp"]["endpoint_url"],"contact":"x@example.com"})
check("mcp resubmit is 410",r.status_code==410)
r=client.post("/api/v1/a2a/submit",json={"url":fixtures["a2a"]["endpoint_url"],"contact":"x@example.com"})
check("a2a resubmit is 410",r.status_code==410)

# Retired rows cannot appear in search or sitemap.
for p,key in (("/api/v1/search","services"),("/api/v1/mcp/search","servers"),("/api/v1/a2a/search","agents"),("/api/v1/resources/search","resources")):
 body=client.get(p,params={"q":"Retire","limit":100}).json()
 values=body.get(key,[])
 retired_slugs={fixtures[kind]["slug"] for kind in fixtures}
 check(p+" excludes retired",not any(x.get("slug") in retired_slugs for x in values))
sitemap=client.get("/sitemap.xml").text
check("sitemap excludes retired",not any(slug in sitemap for slug in retired_slugs))

# Restore is deliberate and auditable.
for kind in ("x402","mcp","a2a"):
 with db.writer() as c:
  restored=db.restore_retired_listing(c,kind,fixtures[kind]["slug"],"test-admin","returned")
 check(kind+" restored",restored["slug"]==fixtures[kind]["slug"])
with db.connect(read_only=True) as c:
 actions=[r[0] for r in c.execute(
  "select e.action from listing_retirement_events e "
  "join retired_listings r on r.id=e.retirement_id "
  "where r.slug in (?,?,?) order by e.id",
  tuple(fixtures[kind]["slug"] for kind in ("x402","mcp","a2a")),
 )]
 check("audit has retire and restore",actions==["retire"]*3+["restore"]*3,str(actions))
 check("x402 history restored",c.execute("select count(*) from health_history where service_id=?",(ids["x402"],)).fetchone()[0]==1)
 check("mcp history restored",c.execute("select count(*) from mcp_health_history where server_id=?",(ids["mcp"],)).fetchone()[0]==1)

print("\n%d passed, %d failed"%(passed,failed))
if PREVIOUS_DB_ENV is None:
 os.environ.pop("AGENT_TOOLS_DB_PATH",None)
else:
 os.environ["AGENT_TOOLS_DB_PATH"]=PREVIOUS_DB_ENV
importlib.reload(db)
shutil.rmtree(tmp)
raise SystemExit(bool(failed))
