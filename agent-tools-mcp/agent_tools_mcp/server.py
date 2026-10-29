"""Stdio MCP server exposing the agent-tools.cloud agent resource directory.

agent-tools.cloud indexes three kinds of agent-callable resources:

- **x402 paid services** — HTTP APIs an agent can pay-per-call (USDC on Base etc.)
- **MCP servers** — tool/context servers an agent can connect to
- **A2A agents** — peer agents an agent can delegate tasks to

Tools:
- search(intent, top_k, max_price_usd, category)  - x402 paid-service discovery
- get(slug)                                        - full x402 service call template
- list_categories()                                - browse x402 categories
- search_mcp_servers(intent, top_k, chain)         - MCP server discovery
- get_mcp_server(slug)                             - full MCP server record
- search_agents(intent, top_k, x402_only)          - A2A agent discovery
- get_agent(slug)                                  - full A2A agent card
- search_all(intent, protocol, top_k)              - unified search across all three
- stats()                                          - directory-wide stats (all protocols)
- ask(question, ...)                               - natural-language answer + ranked x402/MCP picks
- recommend(intent, max_price_usd, chain, ...)     - scored x402 shortlist with price/pay hints
- prepare_paid_verification(slug, payer)           - quote a paid test of an x402 service
- execute_paid_verification(id, signature)         - run it once with your wallet signature
- get_paid_verification(id)                        - read the result

Tools never pay or sign on your behalf.

Reads `AGENT_TOOLS_API_BASE` (default https://agent-tools.cloud) and, for the
paid-verification tools, `AGENT_TOOLS_API_KEY`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

# mcp>=2 renamed FastMCP to MCPServer; keep both majors working.
try:  # pragma: no cover - exercised by whichever major is installed
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

log = logging.getLogger("agent_tools_mcp")

DEFAULT_API_BASE = "https://agent-tools.cloud"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _api_base() -> str:
    return os.environ.get("AGENT_TOOLS_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _user_agent() -> str:
    from . import __version__
    return f"agent-tools-mcp/{__version__} (+https://github.com/AgentTools-Cloud/AgentToolsCollection)"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT, headers={"User-Agent": _user_agent()}
    )


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with _client() as cx:
        r = await cx.get(f"{_api_base()}{path}", params=_clean(params or {}))
        if r.status_code == 410:
            return {"error": "retired", "detail": "This listing has been retired from the directory.",
                    "status": 410}
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    async with _client() as cx:
        r = await cx.post(f"{_api_base()}{path}", json=_clean(body), timeout=timeout)
        r.raise_for_status()
        return r.json()


_NEED_KEY = {
    "error": "unauthorized",
    "message": "API key required: POST https://agent-tools.cloud/api/v1/keys, "
               "then set AGENT_TOOLS_API_KEY or pass api_key.",
}


def _api_key(api_key: str | None) -> str | None:
    key = (api_key or os.environ.get("AGENT_TOOLS_API_KEY") or "").strip()
    return key or None


async def _authed(method: str, path: str, key: str, body: dict[str, Any] | None = None,
                  timeout: float = 90.0) -> dict[str, Any]:
    """Call an account-scoped endpoint; return API errors as data, not exceptions."""
    async with _client() as cx:
        r = await cx.request(method, f"{_api_base()}{path}", json=body,
                             headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {"error": "http_error", "message": r.text[:300]}
    if r.status_code >= 400 and isinstance(data, dict) and "error" not in data:
        detail = data.get("detail")
        data = {"error": "http_error", "status": r.status_code,
                "message": detail if isinstance(detail, str) else str(detail or data)[:300]}
    return data


def _version_kwargs(version: str) -> dict[str, Any]:
    """mcp>=2 accepts `version` directly; mcp 1.x FastMCP does not."""
    import inspect
    return {"version": version} if "version" in inspect.signature(_Server.__init__).parameters else {}


def _set_version(server: Any, version: str) -> None:
    """mcp 1.x reports the SDK version as serverInfo.version unless the low-level server has one."""
    low = getattr(server, "_mcp_server", None)
    if low is not None and getattr(low, "version", None) in (None, ""):
        low.version = version


def build_server() -> Any:
    from . import __version__
    mcp = _Server(
        name="agent-tools",
        instructions=(
            "Discover agent-callable resources from the agent-tools.cloud directory: "
            "x402 paid services, MCP servers and A2A agents. "
            "Use `search` for x402 paid APIs, `search_mcp_servers` for MCP tool servers, "
            "`search_agents` for A2A peer agents, or `search_all` to look across all three. "
            "Use `ask` for a plain-language recommendation, or `recommend` for a scored "
            "x402 shortlist under a price cap. "
            "Then call the matching `get*` tool for the full call template. "
            "To test a paid x402 service once from your own wallet: "
            "`prepare_paid_verification` -> sign `typed_data` -> `execute_paid_verification`. "
            "Tools never pay or sign on your behalf."
        ),
        website_url="https://agent-tools.cloud",
        **_version_kwargs(__version__),
    )
    _set_version(mcp, __version__)

    # ------------------------------------------------------------------ x402
    @mcp.tool()
    async def search(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Find **x402 paid services** matching a natural-language intent.

        x402 services are HTTP APIs the agent pays for per call (USDC on Base etc.).

        Args:
            intent: What the agent wants to do, in plain English or Chinese
                (e.g. "fetch user tweets", "check on-chain whale activity").
            top_k: Max number of services to return (default 5, max 25).
            max_price_usd: Hard upper bound on per-call price in USD. Services
                whose cheapest call exceeds this are filtered out.
            category: Optional category filter (see `list_categories`).
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if category:
            params["category"] = category
        data = await _get("/api/v1/search", params)

        items = data.get("items") or data.get("services") or []
        if max_price_usd is not None:
            kept = []
            for s in items:
                price = s.get("price_min")
                if price is None:
                    price = s.get("price_max")
                if price is None or float(price) <= max_price_usd:
                    kept.append(s)
            items = kept
        return {
            "intent": intent,
            "count": len(items[:top_k]),
            "items": items[:top_k],
        }

    @mcp.tool()
    async def get(slug: str) -> dict[str, Any]:
        """Get full details (URL, price, schema, call template) of an x402 service by slug."""
        return await _get(f"/api/v1/services/{slug}")

    @mcp.tool()
    async def list_categories() -> dict[str, Any]:
        """List all available x402 service categories in the directory."""
        return await _get("/api/v1/categories")

    # ------------------------------------------------------------------- MCP
    @mcp.tool()
    async def search_mcp_servers(
        intent: str,
        top_k: int = 5,
        chain: str | None = None,
    ) -> dict[str, Any]:
        """Find **MCP servers** (tool/context servers) matching an intent.

        Use this when the agent wants extra tools or context via the Model
        Context Protocol, rather than a one-shot paid HTTP call.

        Args:
            intent: What capability the agent is looking for, plain language.
            top_k: Max number of servers to return (default 5, max 25).
            chain: Optional chain filter for x402-capable MCP servers
                (e.g. "base", "solana").
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if chain:
            params["chain"] = chain
        data = await _get("/api/v1/mcp/search", params)
        servers = data.get("servers") or []
        return {
            "intent": intent,
            "count": len(servers[:top_k]),
            "total_matched": data.get("total_matched"),
            "servers": servers[:top_k],
        }

    @mcp.tool()
    async def get_mcp_server(slug: str) -> dict[str, Any]:
        """Get full details (endpoint URL, transport, capabilities) of an MCP server by slug."""
        return await _get(f"/api/v1/mcp/servers/{slug}")

    # ------------------------------------------------------------------- A2A
    @mcp.tool()
    async def search_agents(
        intent: str,
        top_k: int = 5,
        x402_only: bool = False,
    ) -> dict[str, Any]:
        """Find **A2A agents** (peer agents) the agent can delegate a task to.

        A2A agents publish an agent-card and an A2A JSON-RPC endpoint; use this
        when the task is better handed off to another agent than called directly.

        Args:
            intent: The task or capability to delegate, plain language.
            top_k: Max number of agents to return (default 5, max 25).
            x402_only: If true, only return agents that accept x402 payment.
        """
        top_k = max(1, min(int(top_k), 25))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if x402_only:
            params["x402_only"] = "true"
        data = await _get("/api/v1/a2a/search", params)
        agents = data.get("agents") or []
        return {
            "intent": intent,
            "count": len(agents[:top_k]),
            "agents": agents[:top_k],
        }

    @mcp.tool()
    async def get_agent(slug: str) -> dict[str, Any]:
        """Get the full A2A agent card (endpoint, skills, auth, x402 info) by slug."""
        return await _get(f"/api/v1/a2a/agents/{slug}")

    # --------------------------------------------------------------- unified
    @mcp.tool()
    async def search_all(
        intent: str,
        protocol: str | None = None,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """Unified search across **x402 services, MCP servers and A2A agents**.

        Use this when you don't yet know which resource type fits best — it
        ranks all three together and tags each result with its `protocol`.

        Args:
            intent: What the agent wants to accomplish, plain language.
            protocol: Optional filter, one of "x402", "mcp", "a2a". Omit for all.
            top_k: Max number of results to return (default 8, max 50).
        """
        top_k = max(1, min(int(top_k), 50))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if protocol:
            params["protocol"] = protocol
        return await _get("/api/v1/resources/search", params)

    # ----------------------------------------------------------------- stats
    @mcp.tool()
    async def stats() -> dict[str, Any]:
        """Directory-wide stats across all protocols: x402 services, MCP servers, A2A agents."""
        out: dict[str, Any] = {}
        try:
            out["x402"] = await _get("/api/v1/stats")
        except Exception as e:  # noqa: BLE001
            out["x402"] = {"error": str(e)}
        try:
            out["mcp"] = await _get("/api/v1/mcp/stats")
        except Exception as e:  # noqa: BLE001
            out["mcp"] = {"error": str(e)}
        try:
            out["a2a"] = await _get("/api/v1/a2a/stats")
        except Exception as e:  # noqa: BLE001
            out["a2a"] = {"error": str(e)}
        return out

    # ------------------------------------------------------------- guidance
    @mcp.tool()
    async def ask(
        question: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        chain: str | None = None,
        category: str | None = None,
        require_healthy: bool = True,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        """Ask in plain language which **x402 / MCP services** best fit a task.

        Returns a short written `answer` plus the ranked services it is based on.
        Use this when you want a recommendation, not just a raw search list.

        Args:
            question: The task, e.g. "translate a document to Japanese under $0.05".
            top_k: Max number of recommended services (default 5, max 10).
            max_price_usd: Optional upper bound on per-call price in USD.
            chain: Optional settlement chain filter (e.g. "base", "solana").
            category: Optional category filter (see `list_categories`).
            require_healthy: Only consider services whose latest probe was healthy.
            use_llm: Set false for a faster deterministic ranking without the written answer.
        """
        return await _post("/api/v1/ask", {
            "query": question, "limit": max(1, min(int(top_k), 10)),
            "max_price_usd": max_price_usd, "chain": chain, "category": category,
            "require_healthy": require_healthy, "use_llm": use_llm,
        })

    @mcp.tool()
    async def recommend(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        chain: str | None = None,
        require_healthy: bool = True,
    ) -> dict[str, Any]:
        """Get a **scored shortlist of x402 services** with price and payment hints.

        Each recommendation carries a `score`, `price_hint` and `pay_hint`. This is
        discovery only: nothing is paid; settle with your own x402 client.

        Args:
            intent: What the agent wants to do, plain language.
            top_k: Max number of recommendations (default 5, max 20).
            max_price_usd: Optional upper bound on per-call price in USD.
            chain: Optional settlement chain filter (e.g. "base").
            require_healthy: Only include services whose latest probe was healthy.
        """
        return await _get("/api/v1/broker/recommend", {
            "q": intent, "limit": max(1, min(int(top_k), 20)),
            "max_price_usd": max_price_usd, "chain": chain,
            "require_healthy": "true" if require_healthy else "false",
        })

    # ------------------------------------------------- paid verification

    @mcp.tool()
    async def prepare_paid_verification(slug: str, payer: str,
                                        api_key: str | None = None) -> dict[str, Any]:
        """Quote a sponsored paid verification of an x402 service.

        You pay the service once from your own wallet (Base USDC, <= $1; funds go
        to its payTo). Sign the returned `typed_data` (EIP-712), then call
        `execute_paid_verification` before `valid_before`. Never signs or pays for you.

        Args:
            slug: Service slug from `search` / `get`.
            payer: 0x address of the paying wallet.
            api_key: Optional; defaults to AGENT_TOOLS_API_KEY.
        """
        key = _api_key(api_key)
        if not key:
            return dict(_NEED_KEY)
        return await _authed("POST", f"/api/v1/services/{slug}/paid-verifications",
                             key, {"payer": payer})

    @mcp.tool()
    async def execute_paid_verification(id: str, signature: str,
                                        api_key: str | None = None) -> dict[str, Any]:
        """Send the signed payment to the service once and score the result.

        Aborts without paying if price or recipient changed. Never retried; a
        timeout returns `indeterminate`.

        Args:
            id: `id` from `prepare_paid_verification`.
            signature: 0x EIP-712 signature of its `typed_data` by the payer.
            api_key: Optional; defaults to AGENT_TOOLS_API_KEY.
        """
        key = _api_key(api_key)
        if not key:
            return dict(_NEED_KEY)
        return await _authed("POST", f"/api/v1/paid-verifications/{id}/execute",
                             key, {"signature": signature})

    @mcp.tool()
    async def get_paid_verification(id: str, api_key: str | None = None) -> dict[str, Any]:
        """Read one of your own paid verifications.

        Args:
            id: Verification id.
            api_key: Optional; defaults to AGENT_TOOLS_API_KEY.
        """
        key = _api_key(api_key)
        if not key:
            return dict(_NEED_KEY)
        return await _authed("GET", f"/api/v1/paid-verifications/{id}", key)

    log.info("agent-tools-mcp %s server built (api_base=%s)", __version__, _api_base())
    return mcp
