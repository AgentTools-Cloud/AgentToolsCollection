# agent-tools-mcp

[![smithery badge](https://smithery.ai/badge/kangletian/agent-tools-x402-directory)](https://smithery.ai/servers/kangletian/agent-tools-x402-directory)
[![MCP](https://img.shields.io/badge/MCP-Server-blue)](https://modelcontextprotocol.io)
[![PyPI](https://img.shields.io/pypi/v/agent-tools-mcp)](https://pypi.org/project/agent-tools-mcp/)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](https://opensource.org/licenses/Apache-2.0)

Discover and call **x402 paid services** from any MCP-compatible agent (Claude, Cursor, Cline, Continue, …).

Also listed on [Smithery](https://smithery.ai/servers/kangletian/agent-tools-x402-directory) for one-click hosted install.

Backed by [agent-tools.cloud](https://agent-tools.cloud), an open directory of **2,000+ x402 paid APIs** (`awesome-x402`, `x402scan`, `x402.org/ecosystem`, …). Call the `stats` tool for live counts.

## Tools

| Tool | What it does |
|---|---|
| `search(intent, top_k, max_price_usd, category)` | Natural-language search across the directory |
| `get(slug)` | Full details (URL, price, call template) of a service |
| `list_categories()` | Browse categories |
| `stats()` | Directory size & health snapshot |

## What agents get back

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

Third-party entries scraped from `awesome-x402` / `x402scan` / `x402.org` are
passed through as-is and may or may not include bazaar metadata.

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

<!-- mcp-name: io.github.JoursBleu/agent-tools-mcp -->
