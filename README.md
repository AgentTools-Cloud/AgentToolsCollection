# mcpserver — agent-tools.cloud directory + MCP discovery

[![smithery badge](https://smithery.ai/badge/kangletian/agent-tools-x402-directory)](https://smithery.ai/servers/kangletian/agent-tools-x402-directory)

The server that powers [agent-tools.cloud](https://agent-tools.cloud) — an open
directory of **x402 paid services** with a free MCP discovery endpoint.
Also listed on [Smithery](https://smithery.ai/servers/kangletian/agent-tools-x402-directory).

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
| `POST /api/v1/submit` | free, rate-limited | Pending service submission with dedupe |
| `POST /mcp-discovery/` | free | **MCP streamable-http** discovery server |
| `POST /a2a` | free | **A2A JSON-RPC** agent (`message/send`): directory search + MCP safety scan |
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
search_mcp_servers(intent, top_k=5, chain=None, require_healthy=False)
get_mcp_server(slug)
search_a2a_agents(intent, top_k=5, x402_only=False)
search_resources(intent, protocol=None, top_k=10)
scan_mcp_safety(endpoint_url, name="", description="", tools_text="")
register(url, name=None, description=None, mcp_url=None, category=None)
```

`search` accepts natural-language intent and ranks by FTS5 + popularity +
health. Each result carries a `match_reason` and a `confidence` score.
`ask_services` uses the same retrieval-first / LLM-rerank flow as `/api/v1/ask`.

`scan_mcp_safety` vets an MCP server (by endpoint URL) for malware /
prompt-injection lures before you connect: an already-indexed server returns our
latest stored verdict, an unknown one is probed live, scanned, and added to the
directory. The verdict comes from deterministic static rules (no code execution);
each live call also runs a frontier-LLM second opinion as an advisory dimension.
It is also exposed as an A2A skill on `POST /a2a`. The hosted server carries the
full tool set above; the stdio `agent-tools-mcp` PyPI package ships the core
search tools only.

## Deploy

Currently deployed on `latex-tools` behind nginx (vhost: `agent-tools.cloud`).

```bash
cd /opt/mcpserver
sudo git pull
sudo systemctl restart mcpserver
```

systemd units live in [`deploy/`](deploy/):

- `mcpserver.service` — the ASGI app (uvicorn, 127.0.0.1:9100)
- `agent-tools-crawl.{service,timer}` — 6h directory crawler
- `agent-tools-health.{service,timer}` — endpoint health checks

## Config

See [`.env.example`](.env.example). Required:

- `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` — used by `/v1/models`
- `AGENT_TOOLS_ASK_BASE_URL` / `AGENT_TOOLS_ASK_API_KEY` / `AGENT_TOOLS_ASK_MODEL` — LLM backend for `/api/v1/ask`
- `AGENT_TOOLS_SAFETY_BASE_URL` / `AGENT_TOOLS_SAFETY_API_KEY` / `AGENT_TOOLS_SAFETY_MODEL` — optional override for the `scan_mcp_safety` advisory LLM (falls back to the `AGENT_TOOLS_ASK_*` backend)
- `AGENT_TOOLS_DB_PATH` — SQLite path for the directory
- `AGENT_TOOLS_ASK_RATE_LIMIT_PER_MINUTE` / `AGENT_TOOLS_ASK_RATE_LIMIT_PER_DAY` — public ask abuse limits
- `METRICS_BEARER_TOKEN` — optional remote access token for `/metrics`; without it metrics are local-only

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

