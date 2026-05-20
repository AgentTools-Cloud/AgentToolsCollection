# mcpserver — agent-native LLM relay with x402 payment

A thin HTTP relay that exposes the **Qwen/Qwen3.6-35B-A3B** model (served by
天枢 llm-gateway behind the scenes) to overseas agents, gated by the
[x402](https://www.x402.org/) micropayment protocol on Base L2 (USDC).

No accounts. No website. No human UI. An autonomous agent discovers the
endpoint, gets a `402 Payment Required`, signs an EIP-3009 USDC transfer in
the `X-PAYMENT` header, and retries — that's it.

## SKU (v0)

| | |
|---|---|
| Endpoint | `POST /v1/chat/completions` — OpenAI-compatible |
| Model | `Qwen/Qwen3.6-35B-A3B` (AWQ on W7900D, via 天枢) |
| Price | `$0.001` USDC per call (flat, v0) |
| Chain | Base mainnet |
| Asset | USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) |
| Pay-to | `0xC445aa2AA0FA68db67Cd22fc04867773941f9CdF` |
| Facilitator | `https://facilitator.x402.org` |

## Endpoints

- `POST /v1/chat/completions` — OpenAI-compatible, **x402-gated**
- `GET  /v1/models`           — list (free)
- `GET  /healthz`             — health (free)
- `POST /mcp`                 — MCP streamable-http transport (planned, v1)

## Deploy (latex-tools)

```bash
ssh latex-tools
sudo bash /opt/mcpserver/deploy/install.sh
sudo systemctl status mcpserver
```

Listens on `0.0.0.0:9100`. Public:
`http://107.174.178.57:9100/v1/chat/completions`

## Config

See `.env.example`. Required env:

- `TIANSHU_BASE_URL` — upstream OpenAI-compatible endpoint
- `TIANSHU_API_KEY`  — `sk-...` for the upstream
- `X402_PAY_TO`      — EVM address to receive USDC
- `X402_PRICE_USD`   — price string like `"0.001"`

## Local smoke test

```bash
# 1. health
curl http://localhost:9100/healthz

# 2. unpaid call -> 402 with paymentRequirements
curl -i -X POST http://localhost:9100/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"Qwen/Qwen3.6-35B-A3B","messages":[{"role":"user","content":"hi"}]}'

# 3. paid call: agent SDKs (x402-axios / x402-fetch / x402 Python) handle the retry automatically
```

## License

MIT
