# mcpserver — agent-native LLM relay with x402 payment

A thin **REST + MCP** relay that exposes **Qwen/Qwen3.6-35B-A3B** (served by
OpenAI-compatible llm-gateway behind the scenes) to overseas agents, gated by the
[x402](https://www.x402.org/) micropayment protocol on Base L2 (USDC).

No accounts. No website. No human UI. An autonomous agent discovers the
endpoint, gets a `402 Payment Required`, signs an EIP-3009 USDC transfer in
the `PAYMENT-SIGNATURE` header, and retries — that's it.

## SKU (v0.2)

| | |
|---|---|
| Model | `Qwen/Qwen3.6-35B-A3B` (AWQ on W7900D) |
| Price | `$0.001` USDC per call (flat) |
| Chain | Base Sepolia testnet (`eip155:84532`) — switch to `eip155:8453` after setting up Coinbase CDP facilitator |
| Asset | USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) |
| Pay-to | `0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF` |
| Facilitator | `https://x402.org/facilitator` |

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET`  | `/healthz`              | free | liveness |
| `GET`  | `/v1/models`            | free | upstream `/v1/models`, filtered to ALLOWED_MODELS |
| `POST` | `/v1/chat/completions`  | **x402** | OpenAI-compatible (incl. SSE streaming) |
| `POST` | `/mcp`                  | **x402** | MCP streamable-http transport |

### MCP tool

- name: `qwen36_chat`
- args: `messages: list[dict]`, `max_tokens=512`, `temperature=0.7`, `top_p=0.95`
- returns: assistant message content (str)

> v0.2 caveat: the entire `POST /mcp` route is gated, so MCP `initialize` and
> `tools/list` also incur one payment each. A v0.3 will introspect JSON-RPC
> and only gate `tools/call` for the paid tools.

## Deploy (latex-tools)

```bash
ssh latex-tools
cd /opt
git clone https://github.com/JoursBleu/mcpserver.git
cd mcpserver
cp .env.example .env
$EDITOR .env                          # set TIANSHU_API_KEY
sudo bash deploy/install.sh
sudo systemctl status mcpserver
```

Listens on `0.0.0.0:9100`. Public:

- REST: `http://107.174.178.57:9100/v1/chat/completions`
- MCP:  `http://107.174.178.57:9100/mcp`

## Config

See `.env.example`. Required env:

- `TIANSHU_BASE_URL` — upstream OpenAI-compatible endpoint
- `TIANSHU_API_KEY`  — `sk-...` for the upstream
- `X402_PAY_TO`      — EVM address to receive USDC
- `X402_PRICE_USD`   — price string like `"0.001"`

## Smoke test

```bash
# 1. liveness
curl http://localhost:9100/healthz

# 2. unpaid REST -> 402 with paymentRequirements
curl -i -X POST http://localhost:9100/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"Qwen/Qwen3.6-35B-A3B","messages":[{"role":"user","content":"hi"}]}'

# 3. unpaid MCP -> 402
curl -i -X POST http://localhost:9100/mcp \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 4. paid: agent SDKs (x402-axios / x402-fetch / Python x402Client) handle the
#    402 -> sign -> retry round-trip automatically.
```

## License

MIT
