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
| `GET /api/v1/*` | free | JSON API: services, categories, stats |
| `POST /mcp-discovery/` | free | **MCP streamable-http** discovery server (search/get/list_categories/stats) |
| `GET /healthz` | free | Liveness |
| `GET /v1/models` | free | Upstream model listing (read-only) |
| `GET /.well-known/x402` | free | x402 v0.4 self-description (free-only) |
| `GET /.well-known/mcp.json` | free | MCP self-description |

The MCP discovery server is also published as a standalone PyPI package:
[`agent-tools-mcp`](https://pypi.org/project/agent-tools-mcp/)
([repo](https://github.com/JoursBleu/agent-tools-mcp)).

## MCP discovery tools

```
search(intent, top_k=5, category=None, max_price_usd=None, has_mcp=None)
get(slug)
list_categories()
stats()
```

`search` accepts natural-language intent and ranks by FTS5 + popularity +
health. Each result carries a `match_reason` and a `confidence` score.

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

- `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` — only used by `/v1/models`
- `X402_PAY_TO` — kept for `.well-known/x402` self-description
- `AGENT_TOOLS_DB_PATH` — SQLite path for the directory

## Smoke test

```bash
# Liveness
curl https://agent-tools.cloud/healthz

# Directory stats
curl https://agent-tools.cloud/api/v1/stats

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

MIT
