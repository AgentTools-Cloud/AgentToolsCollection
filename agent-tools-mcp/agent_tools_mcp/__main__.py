"""Entry point: `agent-tools-mcp` (CLI script) or `python -m agent_tools_mcp`.

Defaults to stdio transport so MCP clients (Claude Desktop, Cursor, Cline) can
spawn this process directly via the `command` field in their config.
"""
from __future__ import annotations

import logging
import os
import sys

from .server import build_server


def main() -> None:
    # CRITICAL: every log must go to stderr; stdout is reserved for JSON-RPC.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("AGENT_TOOLS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Mute noisy libraries by default; opt in via env if debugging.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(
            os.environ.get("AGENT_TOOLS_HTTP_LOG_LEVEL", "WARNING")
        )

    server = build_server()
    server.run()  # stdio transport by default


if __name__ == "__main__":
    main()
