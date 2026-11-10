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
| `POST /api/v1/keys` | free | Mint an API key (no signup) for account-scoped actions |
| `POST /api/v1/services/{slug}/paid-verifications` | API key | Sponsored paid verification: quote, then `/api/v1/paid-verifications/{id}/execute` |
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
prepare_paid_verification(slug, payer, api_key=None)
execute_paid_verification(id, signature, api_key=None)
get_paid_verification(id, api_key=None)
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

## 文章系统

此候选包含文章阅读、普通作者投稿和编辑审核，默认关闭，不代表 hosted 站点已开放。设置
`ARTICLES_ENABLED=1` 后启用 `/articles`、详情、RSS、Markdown 镜像和
`/account/articles`、`/admin/articles`；`ARTICLE_DB_PATH` 使用独立私有 SQLite 文件。
网页与 API 投稿均要求正常有效账号已验证认领至少一个站内工具。未认领、认领待验证
或认领已撤销的账号不能写稿、保存、预览、上传或提交；不另设作者白名单。
网页工作台接受本站既有签名会话或同一已认领账号的 Bearer key；`domain` 账号不必
另行使用 GitHub 登录。API 只接受 `Authorization: Bearer <api_key>`，不接受 Cookie
冒充 API 身份；无效 key 也不能回退到其他 Cookie 账号。

`ARTICLE_EDITOR_IDS` 仅决定审核管理资格，留空不影响已认领作者投稿，但无人能通过
后台审核发布。内部编辑发稿同样检查工具认领，发布前另核对稿件最初作者的资格，
不能用编辑修订转移作者身份。所有稿件必须审核，作者只能操作本人的稿件和附件，不能自行发布；可读取审核意见、
修订、重新提交及撤回送审。普通稿免费，套餐、文章订单、收款和通知投递仍未开放，
相应入口及接口返回 404。公开输出只读取发布版本，保存或提交新稿不会立即公开，
已发布文章的修订也不会改变当前公开版本。首发文章需经批准发布，
不会在启动时自动导入。工具栏图标来自 `lucide-static` 1.70.0，原许可保留在
`directory/article_assets/LICENSE`。

API 提供 `POST /api/v1/articles` 创建私有草稿、`GET /api/v1/articles` 列出本人稿件、
`GET /api/v1/articles/{id}` 读取稿件和审核意见，以及对应的 `/save`、`/submit` 和
`/cancel-submission` 操作。创建正文使用 `{slug, snapshot}`，保存使用
`{expected_version, snapshot}`，提交及撤回使用 `{expected_version}`；快照字段与网页
一致。另有 `/api/v1/articles/preview` 与 `/api/v1/articles/media`，图片仅本人及审核员
可在发布前读取。没有作者 `/publish` 接口，API key 也不能获得审核权限；完整请求结构与
Bearer 安全方案进入 OpenAPI。当前仅实现普通稿，赞助套餐、订单与收款仍关闭。

本地隔离预览使用临时库、两个已认领账号及一个未认领账号和独立样例，不读取生产库或
发起模型、支付及外部探测。退出后临时数据删除；预览登录入口不属于生产应用。

```bash
python -m deploy.preview_articles --check
python -m deploy.preview_articles --port 0
```

进程输出本机阅读、作者、审核员及未认领账号 URL。41 项隔离测试和三账号自检通过；
独立 Edge 桌面、手机检查覆盖投稿审核、版本隔离及未认领拒绝，不代表生产已经可用。
正式发布仍须明确启用文章开关、落实审核权限、使用已批准首发内容，
并将独立文章库纳入现行加密备份；库内包括图片，备份须使用 SQLite backup 接口，
不能只复制运行中的主文件而遗漏 WAL。预览示意图是原创概念图，不是生产服务实测。

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

