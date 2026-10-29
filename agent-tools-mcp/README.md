# agent-tools-mcp

[![smithery badge](https://smithery.ai/badge/kangletian/agent-tools-mcp)](https://smithery.ai/servers/kangletian/agent-tools-mcp)
[![MCP](https://img.shields.io/badge/MCP-Server-blue)](https://modelcontextprotocol.io)
[![PyPI](https://img.shields.io/pypi/v/agent-tools-mcp)](https://pypi.org/project/agent-tools-mcp/)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](https://opensource.org/licenses/Apache-2.0)

Discover agent-callable resources from any MCP-compatible agent (Claude, Cursor, Cline, Continue, …).

Backed by [agent-tools.cloud](https://agent-tools.cloud), an open directory that indexes **three** kinds of agent-callable resources:

| Resource | What it solves | Typical object |
|---|---|---|
| **x402 service** | agent pays per call for an HTTP API | `/.well-known/x402`, 402 endpoint, payTo |
| **MCP server** | agent gains external tools & context | Streamable-HTTP / stdio MCP servers |
| **A2A agent** | agent delegates a task to another agent | `/.well-known/agent-card.json`, A2A JSON-RPC |

> A2A tells agents *who* can do the job. x402 lets them *pay* for it. MCP gives them the *tools* to do it.

Tools never pay or sign on your behalf. Call `stats` for live counts.

Also listed on [Smithery](https://smithery.ai/servers/kangletian/agent-tools-mcp) for one-click hosted install.

## Tools

| Tool | What it does |
|---|---|
| `search(intent, top_k, max_price_usd, category)` | Natural-language search across **x402 paid services** |
| `get(slug)` | Full details (URL, price, call template) of an x402 service |
| `list_categories()` | Browse x402 categories |
| `search_mcp_servers(intent, top_k, chain)` | Find **MCP servers** by intent |
| `get_mcp_server(slug)` | Full MCP server record (endpoint, transport, capabilities) |
| `search_agents(intent, top_k, x402_only)` | Find **A2A agents** to delegate a task to |
| `get_agent(slug)` | Full A2A agent card (endpoint, skills, auth, x402 info) |
| `search_all(intent, protocol, top_k)` | **Unified** search across all three; each result tagged with `protocol` |
| `stats()` | Directory size & health across x402 / MCP / A2A |
| `ask(question, top_k, max_price_usd, chain, category)` | Plain-language **recommendation**: a written answer plus the ranked x402/MCP services behind it |
| `recommend(intent, top_k, max_price_usd, chain)` | **Scored x402 shortlist** with `price_hint` / `pay_hint` (discovery only, never pays) |
| `prepare_paid_verification(slug, payer)` | Quote a one-off paid test of an x402 service; returns EIP-712 `typed_data` to sign in your wallet |
| `execute_paid_verification(id, signature)` | Pay the service once (Base USDC, <= $1, straight to its payTo) and score the result |
| `get_paid_verification(id)` | Read your result |

Paid-verification tools need a free API key: `POST https://agent-tools.cloud/api/v1/keys`, then set `AGENT_TOOLS_API_KEY`.

Typical flow: ask `ask` for a recommendation, start with `search_all` if you don't know which resource type fits, or
go straight to the protocol-specific search (`search` / `search_mcp_servers` /
`search_agents`), then call the matching `get*` tool for the full call template.

## What agents get back

`search()` / `get()` return a normalised service record: endpoint URL, method, price range,
settlement network/asset, `payTo`, health and quality signals, plus whatever call metadata
the provider publishes (x402 v2 `extensions.bazaar` input/output schema, OpenAPI, or an
MCP `tools/list`). Entries come from the provider's own `/.well-known/x402`, x402scan,
x402.org, the MCP Registry, PulseMCP and public A2A agent-cards; agent-tools.cloud
re-probes them and adds health, latency and on-chain activity, but does not proxy or
host the paid endpoints.

Retired listings return `{"error": "retired", "status": 410}` instead of raising.

## Quick Start

### Claude Code CLI

```bash
claude mcp add agent-tools -- uvx agent-tools-mcp@latest
```

### Claude Desktop / Cursor / Cline

Add to your MCP config (`~/.config/Claude/claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "agent-tools": {
      "command": "uvx",
      "args": ["agent-tools-mcp"]
    }
  }
}
```

### Remote (no install)

Most clients also accept a `url`-based remote MCP server (Streamable HTTP; the client must send `Accept: application/json, text/event-stream`):

```json
{
  "mcpServers": {
    "agent-tools": { "url": "https://agent-tools.cloud/mcp-discovery/" }
  }
}
```

### From source

```bash
pip install agent-tools-mcp        # or `uv tool install agent-tools-mcp`
agent-tools-mcp                    # stdio server, ready for an MCP client
```

## Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `AGENT_TOOLS_API_BASE` | `https://agent-tools.cloud` | Point at a different deployment (e.g. self-hosted) |
| `AGENT_TOOLS_LOG_LEVEL` | `INFO` | Server log level (stderr only) |
| `AGENT_TOOLS_HTTP_LOG_LEVEL` | `WARNING` | httpx/httpcore log level |
| `AGENT_TOOLS_API_KEY` | — | API key for the paid-verification tools |

## Debugging

```bash
# Probe with the official MCP Inspector
npx -y @modelcontextprotocol/inspector uvx agent-tools-mcp

# Or raw JSON-RPC
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | uvx agent-tools-mcp
```

## License

Apache-2.0. See [LICENSE](LICENSE).

## Related

- Directory site: <https://agent-tools.cloud>
- x402 spec: <https://x402.org>
- MCP spec: <https://modelcontextprotocol.io>
- A2A spec: <https://a2a.dev>

<!-- mcp-name: io.github.AgentTools-Cloud/agent-tools-mcp -->
