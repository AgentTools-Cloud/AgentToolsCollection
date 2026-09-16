"""Search callers can choose a stable ordering without changing the default."""
import os
import sqlite3
import sys

SOURCE_DB = os.environ.get("AGENT_TOOLS_SOURCE_DB", "/opt/mcpserver/data/agent-tools.db")
if not os.path.exists(SOURCE_DB):
    print("SKIP: no database at %s" % SOURCE_DB)
    raise SystemExit(0)
COPY = "/tmp/test-search-sort-%d.db" % os.getpid()
sqlite3.connect(SOURCE_DB).backup(sqlite3.connect(COPY))
os.environ["AGENT_TOOLS_DB_PATH"] = COPY
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directory import db

db.init_db()
from fastapi import FastAPI
from fastapi.testclient import TestClient
from directory.routes import router

app=FastAPI(); app.include_router(router); client=TestClient(app)
passed=failed=0

def check(label, ok, detail=""):
    global passed,failed
    if ok: passed+=1; print("  ok  ",label,detail)
    else: failed+=1; print("  FAIL",label,detail)

with db.connect(read_only=True) as c:
    before=[r["slug"] for r in db.search(c,q="Agent Trust API",limit=100)]
    explicit=[r["slug"] for r in db.search(c,q="Agent Trust API",sort="default",limit=100)]
    rel=[r["slug"] for r in db.search(c,q="Agent Trust API",sort="relevance",limit=20)]
    quality=db.search(c,q="Agent Trust API",sort="quality",limit=30)
    newest=db.search(c,q="Agent Trust API",sort="newest",limit=30)
    names=db.search(c,q="Agent Trust API",sort="name",limit=30)

target="api-agenttrustapi-com-sub769"
check("default remains byte-for-byte order compatible",before==explicit)
check("relevance puts reported listing on first page",target in rel,
      "position=%s" % (rel.index(target)+1 if target in rel else None))
check("quality is descending",all((quality[i].get("quality_score") or -1)>=(quality[i+1].get("quality_score") or -1) for i in range(len(quality)-1)))
check("newest is descending",all((newest[i].get("created_at") or 0)>=(newest[i+1].get("created_at") or 0) for i in range(len(newest)-1)))
check("name is alphabetical",[r.get("name") or "" for r in names]==sorted([r.get("name") or "" for r in names],key=str.casefold))

paths=(("/api/v1/search","services"),("/api/v1/mcp/search","servers"),
       ("/api/v1/a2a/search","agents"),("/api/v1/resources/search","resources"))
for path,key in paths:
    for sort in db.SEARCH_SORTS:
        res=client.get(path,params={"q":"Agent Trust API","sort":sort,"limit":5})
        body=res.json()
        check(path+" accepts "+sort,res.status_code==200)
        check(path+" echoes "+sort,body.get("sort")==sort)
        check(path+" honors limit "+sort,len(body.get(key,[]))<=5)
    bad=client.get(path,params={"q":"x","sort":"drop table"})
    check(path+" rejects invalid sort",bad.status_code==422)

for path in ("/x402","/mcp","/a2a"):
    res=client.get(path,params={"q":"Agent Trust API","sort":"relevance"})
    check(path+" renders sort control",res.status_code==200 and 'name="sort"' in res.text)
    check(path+" keeps selection",'value="relevance" selected' in res.text)

# Merged searches must use the same effective order before and after slicing.
# Otherwise increasing the per-source candidate window for page 2 reshuffles
# page 1 and produces duplicate rows across offsets.
def result_items(body):
    for key in ("services", "servers", "agents", "resources"):
        if isinstance(body.get(key), list):
            return body[key]
    return []

for path in ("/api/v1/search", "/api/v1/mcp/search",
             "/api/v1/a2a/search", "/api/v1/resources/search"):
    default_body=client.get(path,params={"sort":"default","limit":10}).json()
    fallback_body=client.get(path,params={"sort":"relevance","limit":10}).json()
    signature=lambda body:[(item.get("type"),item.get("slug"))
                           for item in result_items(body)]
    check(path+" relevance without q falls back to default",
          signature(default_body)==signature(fallback_body))

    ranked=client.get(path,params={"q":"Agent Trust API","sort":"relevance","limit":5}).json()
    leaked={key for item in result_items(ranked) for key in item
            if key.startswith("_") or key=="search_rank"}
    check(path+" hides internal sort metadata",not leaked,str(sorted(leaked)))

    for mode in ("quality","newest","name","relevance"):
        params={"sort":mode,"limit":10}
        if mode=="relevance":
            params["q"]="trust"
        first=client.get(path,params={**params,"offset":0}).json()
        second=client.get(path,params={**params,"offset":10}).json()
        first_ids=set(signature(first)); second_ids=set(signature(second))
        check(path+" disjoint pages "+mode,not (first_ids & second_ids))

print("\n%d passed, %d failed"%(passed,failed))
os.unlink(COPY)
raise SystemExit(1 if failed else 0)
