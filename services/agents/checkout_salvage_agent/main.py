"""A2A server entrypoint for the Checkout Salvage Agent.

Mounts A2A's JSON-RPC/REST/agent-card routes onto a plain FastAPI app, per
a2a-sdk 1.1.2's actual current API (add_a2a_routes_to_fastapi in
a2a.server.routes — the pre-1.0 "Application wrapper" classes the original
design research referenced no longer exist, confirmed by reading the
installed package directly rather than trusting that research blindly).

Run:
    uv run python services/agents/checkout_salvage_agent/main.py
"""

import sys
from pathlib import Path

from fastapi import FastAPI

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling import: agent_executor

from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes import (  # noqa: E402
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore  # noqa: E402
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402
from a2a.utils import TransportProtocol  # noqa: E402

from agent_executor import CheckoutSalvageAgentExecutor  # noqa: E402

PORT = 9002

AGENT_CARD = AgentCard(
    name="Checkout Salvage Agent",
    description=(
        "Handles Problems 2/3/4 of the AI Revenue Recovery Engine: payment "
        "route degradation & smart switching, checkout drop-off & ghost-debit "
        "reconciliation, and BNPL/EMI decline salvage."
    ),
    version="0.1.0",
    supported_interfaces=[
        AgentInterface(url=f"http://localhost:{PORT}/", protocol_binding=TransportProtocol.JSONRPC),
    ],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False),
    skills=[
        AgentSkill(
            id="checkout_salvage",
            name="Checkout Salvage",
            description="Diagnose and recover payment degradation, checkout drop-off, and EMI/BNPL declines.",
            tags=["payments", "recovery", "checkout"],
        ),
    ],
    default_input_modes=["text"],
    default_output_modes=["text"],
)


def build_app() -> FastAPI:
    executor = CheckoutSalvageAgentExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=AGENT_CARD
    )

    app = FastAPI(title="Checkout Salvage Agent")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(AGENT_CARD),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        rest_routes=create_rest_routes(request_handler),
    )
    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
