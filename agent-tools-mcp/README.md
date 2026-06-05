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

This is a **discovery layer, not a facilitator** — your agent keeps full custody of payment. The tools never auto-pay; they only find resources and return call templates. Call the `stats` tool for live counts across all three protocols.

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

Typical flow: start with `search_all` if you don't know which resource type fits, or
go straight to the protocol-specific search (`search` / `search_mcp_servers` /
`search_agents`), then call the matching `get*` tool for the full call template.

## What agents get back (x402 example)

`search()` and `get()` return a normalised service record. For services hosted
directly on **agent-tools.cloud**, every paid endpoint advertises the full x402 v2
`extensions.bazaar` block in its 402 challenge — including JSON Schema and a worked
request body example — so the agent can call without trial-and-error:

```jsonc
// shape of get("agent-tools-cloud-signal-token")
{
  "slug": "agent-tools-cloud-signal-token",
  "endpoint": "https://agent-tools.cloud/v1/signal/token",
  "method": "POST",
  "price_usd": 0.01,
  "network": "base-mainnet",
  "asset": "USDC",
  "bazaar": {
    "info": {
      "input":  { "type": "http", "method": "POST", "bodyType": "json",
                  "body":   { "chain": "base", "token": "0x4200...0006" } },
      "output": { "type": "json",
                  "example": { "signal": "buy", "score": 0.78, "confidence": 0.62 } }
    },
    "schema": { "type": "object", "properties": { "body": { "properties":
                  { "chain": {"type":"string","enum":["base","ethereum","solana"]},
                    "token": {"type":"string"} },
                  "required": ["chain", "token"] } } }
  }
}
```

The response also passes through `payment-required` (challenge) and `payment-response`
headers, both exposed via `Access-Control-Expose-Headers` so a browser-side
`fetch()` (e.g. `x402-fetch`) can read them.

Third-party entries scraped from `awesome-x402` / `x402scan` / `x402.org`, the
MCP registry / PulseMCP, and public A2A agent-cards are passed through as-is and
may or may not include full call metadata.

## Quick Start

### Claude Code CLI

```bash
claude mcp add agent-tools -- uvx agent-tools-mcp
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
