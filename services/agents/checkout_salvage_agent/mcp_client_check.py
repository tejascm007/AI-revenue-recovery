"""Connectivity check for the Checkout Salvage Agent's MCP client wiring.

Not the full agent yet (no A2A server, no LLM reasoning loop) — this proves
the LangChain MultiServerMCPClient -> prob2_route FastMCP server path works,
following the same MultiServerMCPClient pattern used elsewhere for this
project, scoped down to just our own server.

Requires Redis running (the tools' actual logic reads Redis) — if it's not
up yet, tool discovery below still succeeds, only the tool *call* will report
a connection error, which this script surfaces clearly rather than crashing.

Run:
    uv run python services/agents/checkout_salvage_agent/mcp_client_check.py
"""

import asyncio
import sys
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

_CODES_ROOT = Path(__file__).resolve().parents[3]
PROB2_SERVER = _CODES_ROOT / "services" / "mcp-servers" / "prob2_route" / "server.py"

SERVERS = {
    "prob2_route": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run", str(PROB2_SERVER)],
    },
}


async def main():
    client = MultiServerMCPClient(SERVERS)
    tools = await client.get_tools()
    named_tools = {t.name: t for t in tools}
    print("Available tools:", list(named_tools.keys()))

    try:
        result = await named_tools["get_route_status"].ainvoke(
            {"method": "upi", "instrument_key": "hdfc"}
        )
        print("get_route_status(upi, hdfc) ->", result)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: this is a connectivity check
        print(
            "Tool call failed (expected if Redis isn't running yet):",
            f"{type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    asyncio.run(main())
