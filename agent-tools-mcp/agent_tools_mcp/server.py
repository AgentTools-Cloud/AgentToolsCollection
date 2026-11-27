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
- create_api_key(name)                             - free account key (shown once)
- directory_stats(view, topic, month, api_key)     - daily ecosystem statistics / reports

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
    "message": "API key required: call create_api_key (or POST "
               "https://agent-tools.cloud/api/v1/keys), then set "
               "AGENT_TOOLS_API_KEY or pass api_key.",
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


def _tool_kwargs(title: str, *, read_only: bool = True, idempotent: bool = True,
                 destructive: bool = False) -> dict[str, Any]:
    """`title` and tool annotations, passed only when the installed mcp supports them."""
    import inspect
    params = inspect.signature(_Server.tool).parameters
    kw: dict[str, Any] = {}
    if "title" in params:
        kw["title"] = title
    if "annotations" in params:
        try:
            from mcp.types import ToolAnnotations
        except ImportError:  # pragma: no cover - very old mcp
            return kw
        kw["annotations"] = ToolAnnotations(
            title=title, readOnlyHint=read_only, destructiveHint=destructive,
            idempotentHint=idempotent, openWorldHint=True)
    return kw


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
    @mcp.tool(**_tool_kwargs("Search x402 paid APIs"))
    async def search(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Search **x402 paid HTTP APIs only** (pay-per-call in USDC) and return a raw ranked list.

        Pick this when you already know you want an x402 API. For MCP servers use
        `search_mcp_servers`, for A2A agents `search_agents`, for all three at once
        `search_all`; for a written recommendation use `ask`. Read-only; never pays.

        Args:
            intent: What the agent wants to do, in plain English or Chinese
                (e.g. "fetch user tweets", "check on-chain whale activity").
            top_k: Max number of services to return (default 5, max 25).
            max_price_usd: Hard upper bound on per-call price in USD. Services
                whose cheapest call exceeds this are filtered out.
            category: Optional category slug from `list_categories` (e.g. "search").

        Returns:
            `{intent, count, items}`. Each item has `slug`, `name`, `description`,
            `url`, `category`, `chains`, `price_min`/`price_max` (USD), `health`,
            `confidence`, `tx_30d` (EIP-3009 USDC payments in 30 days), `payment`
            and `call_info`. Pass `slug` to `get` for the full call template.
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

    @mcp.tool(**_tool_kwargs("Get x402 API details"))
    async def get(slug: str) -> dict[str, Any]:
        """Get the full record of one **x402 paid API** by its slug.

        Use after `search`, `recommend` or `ask` to learn exactly how to call and
        pay. For MCP servers use `get_mcp_server`, for A2A agents `get_agent`.
        Read-only; never pays.

        Args:
            slug: Service slug from a search result (e.g. "some-weather-api").

        Returns:
            The service card: `url`, `description`, `price_min`/`price_max`,
            `chains`, `payment` (scheme, network, asset, `pay_to`, `accepts`),
            `call` and `call_info` (how to call, sample resources), `health`,
            `latency_ms`, `quality`, `readiness_grade` and on-chain demand
            (`payto_payers_30d`, `payto_tx_30d`).
            A retired listing returns `{"error": "retired", "status": 410}`.
        """
        return await _get(f"/api/v1/services/{slug}")

    @mcp.tool(**_tool_kwargs("List x402 categories"))
    async def list_categories() -> dict[str, Any]:
        """List the category slugs used for **x402 paid APIs**, with how many services each has.

        Use it to pick a valid `category` for `search` or `ask`, or to browse
        what kinds of paid APIs exist. Takes no arguments. Read-only.

        Returns:
            `{categories: [{category, count}, ...]}`, largest first.
        """
        return await _get("/api/v1/categories")

    # ------------------------------------------------------------------- MCP
    @mcp.tool(**_tool_kwargs("Search MCP servers"))
    async def search_mcp_servers(
        intent: str,
        top_k: int = 5,
        chain: str | None = None,
    ) -> dict[str, Any]:
        """Search **remote MCP servers only** (tool/context servers you connect to) by intent.

        Use this when the agent wants extra tools via the Model Context Protocol
        rather than a one-shot paid HTTP call (`search`) or a peer agent
        (`search_agents`). Only servers with a reachable endpoint that we probe
        daily are returned. Read-only.

        Args:
            intent: What capability the agent is looking for, plain language.
            top_k: Max number of servers to return (default 5, max 25).
            chain: Optional chain filter for x402-capable MCP servers
                (e.g. "base", "solana").

        Returns:
            `{intent, count, total_matched, servers}`. Each server has `slug`,
            `name`, `description`, `endpoint_url`, `access` (open / key / x402),
            `health_status`, `confidence` and `call_hint`. Pass `slug` to
            `get_mcp_server` for transport and tool details.
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

    @mcp.tool(**_tool_kwargs("Get MCP server details"))
    async def get_mcp_server(slug: str) -> dict[str, Any]:
        """Get the full record of one **MCP server** by its slug.

        Use after `search_mcp_servers` (or a `search_all` result with protocol
        "mcp") to decide whether and how to connect. Read-only.

        Args:
            slug: MCP server slug from a search result.

        Returns:
            `endpoint_url`, `transport`, `auth_method`, `tools` (names and
            schemas seen by our probe), `health`, `latency_ms`, `conformance`,
            `safety_verdict`/`safety_reasons`, `quality`, `readiness_grade`,
            `license` and source repository.
            A retired listing returns `{"error": "retired", "status": 410}`.
        """
        return await _get(f"/api/v1/mcp/servers/{slug}")

    # ------------------------------------------------------------------- A2A
    @mcp.tool(**_tool_kwargs("Search A2A agents"))
    async def search_agents(
        intent: str,
        top_k: int = 5,
        x402_only: bool = False,
    ) -> dict[str, Any]:
        """Search **A2A agents only** (peer agents you can delegate a task to) by intent.

        A2A agents publish an agent card and a JSON-RPC endpoint. Use this when
        the task is better handed to another agent than done with a single API
        call (`search`) or an MCP tool (`search_mcp_servers`). Read-only.

        Args:
            intent: The task or capability to delegate, plain language.
            top_k: Max number of agents to return (default 5, max 25).
            x402_only: If true, only return agents that accept x402 payment.

        Returns:
            `{intent, count, agents}`. Each agent has `slug`, `name`,
            `description`, `endpoint_url`, `card_url`, `skill_names`,
            `auth_schemes`, `x402_supported`, `health`, `latency_ms` and
            `conformance`. Pass `slug` to `get_agent` for the full card.
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

    @mcp.tool(**_tool_kwargs("Get A2A agent card"))
    async def get_agent(slug: str) -> dict[str, Any]:
        """Get the full record of one **A2A agent** by its slug.

        Use after `search_agents` (or a `search_all` result with protocol "a2a")
        to see how to send it a task. Read-only.

        Args:
            slug: A2A agent slug from a search result.

        Returns:
            `endpoint_url`, `card_url`, `protocol_version`, `preferred_transport`,
            `skills` (id, name, description, input/output modes), `auth_schemes`,
            x402 payment info (`x402_supported`, `price_hint_usd`, `payto`),
            latest health, latency and protocol conformance.
            A retired listing returns `{"error": "retired", "status": 410}`.
        """
        return await _get(f"/api/v1/a2a/agents/{slug}")

    # --------------------------------------------------------------- unified
    @mcp.tool(**_tool_kwargs("Search all resource types"))
    async def search_all(
        intent: str,
        protocol: str | None = None,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """Search **x402 APIs, MCP servers and A2A agents together** in one ranked list.

        Start here when you don't know which kind of resource fits. If you do,
        the typed tools (`search`, `search_mcp_servers`, `search_agents`) return
        richer per-type fields. Returns a list, not a written answer (see `ask`).
        Read-only.

        Args:
            intent: What the agent wants to accomplish, plain language.
            protocol: Optional filter, one of "x402", "mcp", "a2a". Omit for all.
            top_k: Max number of results to return (default 8, max 50).

        Returns:
            `{query, protocol, count, total_matched, resources}`. Each resource
            has `type` (x402 / mcp / a2a), `slug`, `name`, `description`,
            `endpoint_url`, `access`, `price_hint`, `health_status`, `confidence`
            and `call_hint`. Use `get`, `get_mcp_server` or `get_agent` by type.
        """
        top_k = max(1, min(int(top_k), 50))
        params: dict[str, Any] = {"q": intent, "limit": top_k}
        if protocol:
            params["protocol"] = protocol
        return await _get("/api/v1/resources/search", params)

    # ----------------------------------------------------------------- stats
    @mcp.tool(**_tool_kwargs("Current directory counts"))
    async def stats() -> dict[str, Any]:
        """Current size of the directory: live counts per protocol, no history or rankings.

        A quick check of how many x402 services, MCP servers and A2A agents are
        indexed and how many are healthy right now. For daily ecosystem
        statistics, findings, top searches, topic or monthly reports use
        `directory_stats` instead. Takes no arguments. Read-only.

        Returns:
            `{x402, mcp, a2a}`. `x402` has `total`, `healthy`, `by_chain`;
            `mcp` and `a2a` have `total`, `healthy`, `conformant`,
            `x402_capable`, `new_7d` and access counts. A part that fails to
            load is returned as `{"error": ...}` instead of failing the call.
        """
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
    @mcp.tool(**_tool_kwargs("Ask for a recommendation", idempotent=False))
    async def ask(
        question: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        chain: str | None = None,
        category: str | None = None,
        require_healthy: bool = True,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        """Get a **written answer** on which x402 / MCP services best fit a task.

        The only tool that returns prose: an LLM writes a short `answer` grounded
        in directory candidates, plus the ranked services it used. Use it when
        you want advice; use `search` / `search_all` for a plain list, or
        `recommend` for a scored x402 shortlist with payment hints. Rate-limited;
        read-only; never pays.

        Args:
            question: The task, e.g. "translate a document to Japanese under $0.05".
            top_k: Max number of recommended services (default 5, max 10).
            max_price_usd: Optional upper bound on per-call price in USD.
            chain: Optional settlement chain filter (e.g. "base", "solana").
            category: Optional category filter (see `list_categories`).
            require_healthy: Only consider services whose latest probe was healthy.
            use_llm: Set false for a faster deterministic ranking without the written answer.

        Returns:
            `{query, answer, recommendations, follow_up_questions, llm_used,
            candidate_count}`. Each recommendation has `slug`, `name`, `url`,
            `why`, `confidence`, `next_step` and a `service_card`. `answer` is
            empty when `use_llm` is false or the LLM is unavailable.
        """
        return await _post("/api/v1/ask", {
            "query": question, "limit": max(1, min(int(top_k), 10)),
            "max_price_usd": max_price_usd, "chain": chain, "category": category,
            "require_healthy": require_healthy, "use_llm": use_llm,
        })

    @mcp.tool(**_tool_kwargs("Shortlist x402 APIs to pay"))
    async def recommend(
        intent: str,
        top_k: int = 5,
        max_price_usd: float | None = None,
        chain: str | None = None,
        require_healthy: bool = True,
    ) -> dict[str, Any]:
        """Get a **scored x402 shortlist ready to pay**: score, price and payment hints per API.

        Use when you intend to call and pay an x402 API next and want the
        payment details up front. Unlike `search` it adds a `score` and
        `pay_hint`; unlike `ask` it writes no prose. Discovery only: nothing is
        paid; settle with your own x402 client. Read-only.

        Args:
            intent: What the agent wants to do, plain language.
            top_k: Max number of recommendations (default 5, max 20).
            max_price_usd: Optional upper bound on per-call price in USD.
            chain: Optional settlement chain filter (e.g. "base").
            require_healthy: Only include services whose latest probe was healthy.

        Returns:
            `{query, count, settlement, note, recommendations}`. Each has `slug`,
            `name`, `score`, `price_hint`, `pay_hint` (scheme, chains, price, flow),
            `call_hint`, `health_status`, `confidence`, `tx_30d` and `chains`.
        """
        return await _get("/api/v1/broker/recommend", {
            "q": intent, "limit": max(1, min(int(top_k), 20)),
            "max_price_usd": max_price_usd, "chain": chain,
            "require_healthy": "true" if require_healthy else "false",
        })

    # ------------------------------------------------- paid verification

    @mcp.tool(**_tool_kwargs("Quote a paid verification", read_only=False,
                             idempotent=False))
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

    @mcp.tool(**_tool_kwargs("Run a paid verification", read_only=False,
                             idempotent=False))
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

    @mcp.tool(**_tool_kwargs("Read a paid verification"))
    async def get_paid_verification(id: str, api_key: str | None = None) -> dict[str, Any]:
        """Read the status and result of one of **your own** paid verifications.

        Third step of the paid-verification flow (`prepare_paid_verification` ->
        `execute_paid_verification` -> this). Use it to re-check a result later,
        e.g. after an `indeterminate` timeout. Read-only; never pays. Needs the
        same account API key that created the verification.

        Args:
            id: The `id` returned by `prepare_paid_verification`.
            api_key: Optional; defaults to AGENT_TOOLS_API_KEY.

        Returns:
            `status` (prepared, dispatching, succeeded, delivery_failed,
            payment_rejected, failed_before_dispatch, indeterminate, expired),
            `amount_usd`, `settled`, `checks` (response and settlement checks)
            and the resulting score. Without a key returns
            `{"error": "unauthorized", ...}`.
        """
        key = _api_key(api_key)
        if not key:
            return dict(_NEED_KEY)
        return await _authed("GET", f"/api/v1/paid-verifications/{id}", key)

    # ------------------------------------------------------------ account
    @mcp.tool(**_tool_kwargs("Create a free API key", read_only=False,
                             idempotent=False))
    async def create_api_key(name: str | None = None) -> dict[str, Any]:
        """Create a free agent-tools.cloud account API key. No sign-up needed.

        The key is returned once and cannot be recovered. Store it (for example
        as AGENT_TOOLS_API_KEY); do not print it back to the user or into logs.
        It unlocks complete `directory_stats` lists and paid verification.
        A few keys per IP per day.

        Args:
            name: Optional label to recognise the key later, e.g. your agent name.
        """
        async with _client() as cx:
            r = await cx.post(f"{_api_base()}/api/v1/keys",
                              json=_clean({"name": (name or "").strip()[:60] or None}))
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            msg = detail.get("message") if isinstance(detail, dict) else detail
            return {"error": "rate_limited" if r.status_code == 429 else "http_error",
                    "status": r.status_code, "message": str(msg or r.text[:300])}
        return {"api_key": data.get("api_key"), "prefix": data.get("prefix"),
                "message": ("Store this now -- it is not shown again. Do not echo it "
                            "to the user or logs. Set AGENT_TOOLS_API_KEY or pass api_key.")}

    # ------------------------------------------------------- statistics
    @mcp.tool(**_tool_kwargs("Ecosystem statistics and reports"))
    async def directory_stats(view: str = "all", topic: str | None = None,
                              month: str | None = None,
                              api_key: str | None = None) -> dict[str, Any]:
        """Daily **ecosystem statistics and reports** from our probes (not live directory counts).

        The published daily snapshot: counts (indexed, reachable,
        protocol-verified, ready), findings, probe outcomes, top searches by
        distinct searchers; or one topic page; or one monthly report. For a
        quick live count of listings use `stats`. With an account key the topic
        and search lists are complete; without one they are shortened. Read-only.

        Args:
            view: "all", "mcp", "x402" or "a2a".
            topic: Optional topic word (e.g. "payment", "weather") for that topic instead.
            month: Optional "YYYY-MM" for that monthly report instead.
            api_key: Optional; defaults to AGENT_TOOLS_API_KEY.

        Returns:
            The summary, topic or monthly report as JSON (CC BY 4.0), with its
            snapshot date; on an HTTP error `{"error": "http_error", ...}`.
        """
        key = _api_key(api_key)
        if topic:
            path, params = f"/stats/topics/{topic.strip().lower()}.json", {}
        elif month:
            path, params = f"/stats/reports/{month.strip()}.json", {}
        else:
            path, params = "/api/v1/stats/summary", {"view": view}
        if key and not month:
            params["full"] = "true"
        async with _client() as cx:
            r = await cx.get(f"{_api_base()}{path}", params=params,
                             headers={"Authorization": f"Bearer {key}"} if key else None)
        try:
            data = r.json()
        except ValueError:
            data = {"message": r.text[:300]}
        if r.status_code >= 400:
            return {"error": "http_error", "status": r.status_code,
                    "message": str(data.get("detail") if isinstance(data, dict) else data)[:300]}
        return data

    log.info("agent-tools-mcp %s server built (api_base=%s)", __version__, _api_base())
    return mcp
