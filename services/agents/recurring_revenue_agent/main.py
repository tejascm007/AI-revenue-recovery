"""A2A server entrypoint for the Recurring Revenue Agent.

Run:
    uv run python services/agents/recurring_revenue_agent/main.py
"""

import sys
from pathlib import Path

from fastapi import FastAPI

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

from agent_executor import RecurringRevenueAgentExecutor  # noqa: E402

PORT = 9003

AGENT_CARD = AgentCard(
    name="Recurring Revenue Agent",
    description=(
        "Handles Problems 5/6 of the AI Revenue Recovery Engine: hard-decline "
        "subscription salvage and the penalty-aware dunning sequence for "
        "soft/NSF declines."
    ),
    version="0.1.0",
    supported_interfaces=[
        AgentInterface(url=f"http://localhost:{PORT}/", protocol_binding=TransportProtocol.JSONRPC),
    ],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False),
    skills=[
        AgentSkill(
            id="recurring_revenue",
            name="Recurring Revenue Recovery",
            description="Classify and recover hard/soft subscription payment declines.",
            tags=["subscriptions", "recovery", "dunning"],
        ),
    ],
    default_input_modes=["text"],
    default_output_modes=["text"],
)


def build_app() -> FastAPI:
    executor = RecurringRevenueAgentExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=AGENT_CARD
    )

    app = FastAPI(title="Recurring Revenue Agent")
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
