---
name: agent-tools
description: Discover and call x402 paid services (token signals, on-chain Q&A, DeFi plans, LLM chat, etc.) via the agent-tools.cloud directory. Use when the user wants on-demand paid APIs that settle in USDC on Base — no signup, no API keys, pay per call. Tools - `search(intent)` finds matching services by natural language, `get(slug)` returns the full call template (URL, schema, price, x402 bazaar info), `list_categories()` browses, `stats()` shows directory size.
metadata: {"openclaw": {"emoji": "🛒", "homepage": "https://agent-tools.cloud", "requires": {"bins": ["uvx"]}, "install": [{"id": "uv", "kind": "uv", "package": "agent-tools-mcp", "bins": ["agent-tools-mcp"], "label": "Install via uv (recommended)"}, {"id": "pip", "kind": "pip", "package": "agent-tools-mcp", "bins": ["agent-tools-mcp"], "label": "Install via pip"}]}}
---

# agent-tools

Discovery + payment shim for the [x402](https://x402.org) paid-API ecosystem.
Lets an agent find a paid service by intent, read its full call template, and call
it (pay-per-call USDC on Base).

Backed by [agent-tools.cloud](https://agent-tools.cloud), an open directory of
**2 000+** paid x402 endpoints aggregated from `awesome-x402`, `x402scan`,
`x402.org/ecosystem`, and a handful of self-hosted services on the same host
(LLM chat, token signal, on-chain Q&A, DeFi planner …).

## When to use

Use this skill when the user asks for any of:

- "find an API that does X" / "is there a paid service for Y"
- "call a token signal / on-chain query / DeFi planner / chat completion"
- "what's available on x402" / "what x402 services exist"
- Any task that needs a paid third-party API and the user has not pinned a
  specific provider — search first, then decide.

Do **not** use this skill for:

- Free, well-known APIs (CoinGecko, Defillama public). Call those directly.
- The user explicitly named a non-x402 provider (OpenAI, Anthropic, etc.).

## How to use

### 1. Find a service

The MCP server (installed via the installer specs above) exposes four tools:

| Tool | Purpose |
|---|---|
| `search(intent, top_k?, max_price_usd?, category?)` | Natural-language search across the directory |
| `get(slug)` | Full record for one service (URL, schema, price, x402 bazaar info) |
| `list_categories()` | Browse available categories |
| `stats()` | Live directory size + health |

Most workflows start with `search`, pick the highest-ranked result whose `price_usd`
fits the user's budget, then `get(slug)` to read the full call template.

### 2. Read the call template

For services hosted on **agent-tools.cloud** the `get()` payload includes the full
x402 v2 `extensions.bazaar` block — request body example, JSON Schema, output
example — so the agent can construct a request without trial-and-error.

Third-party entries are passed through as scraped; some include bazaar metadata,
some don't. When in doubt, call the endpoint with an empty body and read the 402
challenge for guidance.

### 3. Call + pay

1. POST to the endpoint with no payment header → receive HTTP 402 + `payment-required`
   header (base64-encoded x402 v2 challenge).
2. Decode, sign with a Base-mainnet USDC wallet, attach as `X-Payment` header.
3. Re-POST → receive 200 + the actual response.

Use any x402-compatible payment lib (`x402-axios`, `x402-fetch`, the Python
`x402.payment` helpers, …) to handle steps 1–3.

## Configuration

Set in `~/.openclaw/openclaw.json` (optional):

```json
{
  "skills": {
    "entries": {
      "agent-tools": {
        "enabled": true,
        "env": {
          "AGENT_TOOLS_API_BASE": "https://agent-tools.cloud"
        }
      }
    }
  }
}
```

`AGENT_TOOLS_API_BASE` lets you point at a self-hosted directory if you ever
deploy your own.

## Cost

The directory itself is **free** (`search`, `get`, `list_categories`, `stats` all
unauthenticated, no rate limit announced). Only the underlying paid services
charge — typically $0.001–$0.50 per call.

## Safety

- The skill never auto-pays. It only **discovers** and returns call templates.
  The agent / user is in full control of which 402 challenge to actually settle.
- Always show `price_usd` to the user before paying.
- Cap `max_price_usd` in `search()` if the user mentioned a budget.

## Related

- Project home: <https://agent-tools.cloud>
- Source: <https://github.com/JoursBleu/agent-tools-mcp>
- PyPI: <https://pypi.org/project/agent-tools-mcp/>
- x402 spec: <https://x402.org>
