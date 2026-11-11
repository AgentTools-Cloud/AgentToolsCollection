# mcpserver — agent-tools.cloud directory + MCP discovery

[![smithery badge](https://smithery.ai/badge/kangletian/agent-tools-x402-directory)](https://smithery.ai/servers/kangletian/agent-tools-x402-directory)

The server that powers [agent-tools.cloud](https://agent-tools.cloud) — an open,
agent-readable directory of **x402 paid APIs, MCP servers and A2A agents**, probed
daily, graded and safety-scanned, with a free MCP discovery endpoint and an A2A agent.
Everything on the site (listings, daily statistics, scoring methodology, Blog) is
available as JSON and through MCP tools.
Also listed on [Smithery](https://smithery.ai/servers/kangletian/agent-tools-x402-directory).

> **Branches:** `main` is an older public snapshot. The code that runs the site
> today, including everything described below, is on the
> [`directory-site`](https://github.com/AgentTools-Cloud/AgentToolsCollection/tree/directory-site) branch.

> **Note (2026-05-25):** the previously hosted paid Qwen3.6 relay and the paid
> vertical endpoints (signal / onchain / defi / portfolio) were retired after
> 30 days of zero external conversions. This repository now serves the
> directory site and the free MCP discovery server only.

## What this serves today

| Path | Auth | Purpose |
|---|---|---|
| `GET /` (host = `agent-tools.cloud`) | free | Directory site (search / browse) |
| `GET /api/v1/search` | free | Faceted service search |
| `POST /api/v1/ask` | free, rate-limited | LLM-ranked recommendations grounded in directory candidates |
| `GET /api/v1/services/{slug}` | free | Service card with payment, call and quality metadata |
| `GET /api/v1/categories`, `/api/v1/stats` | free | Directory facets and aggregate stats |
| `GET /stats`, `/stats.json`, `/stats/daily.csv` | free | Daily ecosystem statistics page, full snapshot and history (CC BY 4.0) |
| `GET /api/v1/stats/summary?view=all\|mcp\|x402\|a2a` | free | Compact daily digest for agents |
| `GET /api/v1/methodology` | free | How quality grades, readiness scores and statistics are computed |
| `GET /api/v1/blog`, `/api/v1/blog/{slug}` | free | Published Blog articles (list; full Markdown) |
| `GET /llms.txt` | free | Plain-text guide for LLM agents |
| `POST /api/v1/submit` | free, rate-limited | Pending service submission with dedupe |
| `POST /api/v1/keys` | free | Mint an API key (no signup) for account-scoped actions |
| `POST /api/v1/services/{slug}/paid-verifications` | API key | Sponsored paid verification: quote, then `/api/v1/paid-verifications/{id}/execute` |
| `POST /mcp-discovery/` | free | **MCP streamable-http** discovery server |
| `POST /a2a` | free | **A2A JSON-RPC** agent (0.3 `message/send` and 1.0 `SendMessage`): directory search, MCP safety scan, daily statistics |
| `GET /healthz` | free | Liveness |
| `GET /v1/models` | free | Upstream model listing (read-only) |
| `GET /.well-known/agent-tools.json` | free | Agent discovery manifest |
| `GET /.well-known/x402` | free | x402 v0.4 self-description (free-only) |
| `GET /.well-known/mcp.json` | free | MCP self-description |

The MCP discovery server is also published as a standalone PyPI package:
[`agent-tools-mcp`](https://pypi.org/project/agent-tools-mcp/)
([repo](https://github.com/JoursBleu/agent-tools-mcp)).

## MCP discovery tools

```
search(intent, top_k=5, category=None, max_price_usd=None, has_mcp=None)
ask_services(intent, top_k=5, category=None, max_price_usd=None, use_llm=True)
get(slug)
list_categories()
stats()
directory_stats(view="all")
get_methodology()
list_articles(query="", tag="", page=1)
get_article(slug)
search_mcp_servers(intent, top_k=5, chain=None, require_healthy=False)
get_mcp_server(slug)
search_a2a_agents(intent, top_k=5, x402_only=False)
search_resources(intent, protocol=None, top_k=10)
scan_mcp_safety(endpoint_url, name="", description="", tools_text="")
register(url, name=None, description=None, mcp_url=None, category=None)
prepare_paid_verification(slug, payer, api_key=None)
execute_paid_verification(id, signature, api_key=None)
get_paid_verification(id, api_key=None)
claim_service_url(url, method=None)
verify_service_url(claim_id, token)
get_owned_listing(kind, slug)
edit_owned_listing(kind, slug, fields)
```

`*_paid_verification` (remote server only, not in the `agent-tools-mcp`
package): pay a listed x402 service once from your own wallet and record the
result. Needs an API key from `POST /api/v1/keys`. See
[`/about#paid-verification`](https://agent-tools.cloud/about#paid-verification).

`search` accepts natural-language intent and ranks by FTS5 + popularity +
health. Each result carries a `match_reason` and a `confidence` score.
`ask_services` uses the same retrieval-first / LLM-rerank flow as `/api/v1/ask`.

`scan_mcp_safety` vets an MCP server (by endpoint URL) for malware /
prompt-injection lures before you connect: an already-indexed server returns our
latest stored verdict, an unknown one is probed live, scanned, and added to the
directory if it answered or you supplied metadata (documentation hosts such as
`example.com`, `*.test`, `*.invalid` are scanned but never listed). The verdict comes from deterministic static rules (no code execution)
over the server's advertised name + description + tool docs. Rules cover
pipe-to-shell / base64-eval / PowerShell install cradles, bare-IP payload hosts,
prompt-injection phrasing, and MCP **tool-poisoning** coercion — "always call
this tool first", "before using any other tool you must…", hidden
`<IMPORTANT>` instructions, "list all API keys / include secrets in your
response", and coercion to read & forward `.key`/`.pem`/`.ssh`/`.env` files.
Source-code-oriented rules (SQL/command/code injection) are intentionally not
applied to natural-language descriptions to avoid false positives. Each live
call also runs a frontier-LLM second opinion as an advisory dimension.
It is also exposed as an A2A skill on `POST /a2a`. The hosted server carries the
full tool set above; the stdio `agent-tools-mcp` PyPI package ships the core
search tools only.

## Deploy

Runs behind nginx as a systemd service (see `deploy/`).

```bash
cd <install-dir>
sudo git pull
sudo systemctl restart mcpserver
```

systemd units live in [`deploy/`](deploy/):

- `mcpserver.service` — the ASGI app (uvicorn, 127.0.0.1:9100)
- `mcpserver-b.service` — second instance on 127.0.0.1:9101; nginx upstream `agent_tools_app` uses both, so deploy with `agent-tools-rolling-restart` instead of `systemctl restart mcpserver`
- `agent-tools-crawl.{service,timer}` — daily directory crawler at 00:30
- `agent-tools-health.{service,timer}` — daily endpoint health checks at 05:30

## Config

See [`.env.example`](.env.example). Required:

- `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` — used by `/v1/models`
- `AGENT_TOOLS_ASK_BASE_URL` / `AGENT_TOOLS_ASK_API_KEY` / `AGENT_TOOLS_ASK_MODEL` — LLM backend for `/api/v1/ask`
- `AGENT_TOOLS_SAFETY_BASE_URL` / `AGENT_TOOLS_SAFETY_API_KEY` / `AGENT_TOOLS_SAFETY_MODEL` — optional override for the `scan_mcp_safety` advisory LLM (falls back to the `AGENT_TOOLS_ASK_*` backend)
- `AGENT_TOOLS_DB_PATH` — SQLite path for the directory
- `AGENT_TOOLS_ASK_RATE_LIMIT_PER_MINUTE` / `AGENT_TOOLS_ASK_RATE_LIMIT_PER_DAY` — public ask abuse limits
- `METRICS_BEARER_TOKEN` — optional remote access token for `/metrics`; without it metrics are local-only

## Articles (optional)

Disabled by default. `ARTICLES_ENABLED=1` enables `/articles` (list, detail, RSS,
Markdown mirror), the author workspace `/account/articles` and editor review
`/admin/articles`. Articles live in a separate SQLite file (`ARTICLE_DB_PATH`,
default `articles.db` next to the main database); back it up with the SQLite
backup API so the WAL is not lost.

- Authors must hold at least one verified listing claim. The REST API
  (`/api/v1/articles`, `/save`, `/submit`, `/cancel-submission`, `/preview`,
  `/media`) accepts only `Authorization: Bearer <api_key>`.
- Every submission is reviewed; only accounts in `ARTICLE_EDITOR_IDS` can
  publish. Public pages read the published version only.
- Toolbar icons come from `lucide-static` 1.70.0 (licence kept in
  `directory/article_assets/LICENSE`).

### Sponsored publication (optional)

Paid sponsored posts use a NOWPayments hosted invoice plus a signed IPN
(HMAC-SHA512 over the sorted JSON body). The private JSON config is read from
`NOWPAYMENTS_ARTICLES_CONFIG` (default `/etc/agent-tools/nowpayments-articles.json`,
keep it mode 0600). Order, pricing and IPN endpoints return 404 unless the
config is complete and `sponsored_articles_enabled` is `true`. Sponsored posts
carry a visible "Sponsored" disclosure and their links get `rel="sponsored"`;
payment never changes directory ranking.

## Smoke test

```bash
# Liveness
curl https://agent-tools.cloud/healthz

# Directory stats
curl https://agent-tools.cloud/api/v1/stats

# Intent-level service recommendation
curl -s -X POST https://agent-tools.cloud/api/v1/ask \
  -H 'content-type: application/json' \
  -d '{"query":"find a weather API that accepts x402","limit":2}'

# Service card
curl https://agent-tools.cloud/api/v1/services/weather-hugen-tokyo-scan

# MCP discovery handshake
curl -s -X POST https://agent-tools.cloud/mcp-discovery/ \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"smoke","version":"1"}}}'

# Retired paid path -> 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://agent-tools.cloud/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"Qwen/Qwen3.6-35B-A3B","messages":[{"role":"user","content":"hi"}]}'
# => 404
```

## License

The agent-tools.cloud **server and directory code** in this repository is
licensed under the **[PolyForm Noncommercial License 1.0.0](LICENSE)** — you may
use, modify, and share it for any **noncommercial** purpose; commercial use is
not permitted.

The standalone MCP client package in [`agent-tools-mcp/`](agent-tools-mcp/)
(published to PyPI as `agent-tools-mcp`) is licensed separately under
**[Apache-2.0](agent-tools-mcp/LICENSE)**, so any agent — including commercial
ones — can install and call the hosted service freely.

Using the hosted service at agent-tools.cloud is governed by its terms of
service, not this code license.

