"""A2A dispatch — sends one event to its owning sub-agent and returns the result.

Timeout is generous (60s) because verified testing showed a real round trip
involves the target agent spawning up to 3 MCP subprocess servers via `uv run
fastmcp run ...` before it can even attempt the LLM call — the default httpx
timeout (a few seconds) was observed to fail on this alone, well before ever
reaching the (separately expected) missing-API-key error.
"""

import json

import httpx

import a2a.types as t
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_message_text, new_text_message

DISPATCH_TIMEOUT_SECONDS = 60.0


def build_instruction(event_type: str, problem_id: int, payload: dict) -> str:
    return (
        f"Event: {event_type}. Problem: {problem_id}. "
        f"Details: {json.dumps(payload)}. "
        "Diagnose this situation and take the appropriate action using your tools."
    )


async def dispatch(agent_url: str, instruction: str) -> str:
    """Sends `instruction` to the agent at `agent_url` and returns its final
    text response. Known limitation (2026-09-03, not yet built): a sub-agent
    that needs the two-hop delegation pattern (e.g. Problem 9's execute_action
    returning a "pending_two_hop_delegation" artifact for a WhatsApp send)
    currently has that structured signal absorbed into the LLM's own final
    text summary here, rather than surfaced as a proper A2A Data Part the
    Orchestrator could act on programmatically. The routing/dispatch
    mechanism below is real and verified; the two-hop follow-up call is the
    next piece to build, not yet wired.
    """
    # resolver_http_kwargs only covers the agent-card-fetching client; the actual
    # send_message transport is a *separate* httpx.AsyncClient set via
    # ClientConfig.httpx_client (verified directly against the installed SDK
    # after resolver_http_kwargs alone was observed to still time out at the
    # default ~5s httpx timeout on the real send_message call).
    client = await create_client(
        agent_url,
        resolver_http_kwargs={"timeout": DISPATCH_TIMEOUT_SECONDS},
        client_config=ClientConfig(httpx_client=httpx.AsyncClient(timeout=DISPATCH_TIMEOUT_SECONDS)),
    )
    request = t.SendMessageRequest(message=new_text_message(instruction))
    async for response in client.send_message(request):
        # StreamResponse is a oneof of {task, message, status_update, artifact_update}
        # (verified directly, not assumed) — artifact_update is the correct future
        # home for the two-hop delegation payload noted above, once an agent
        # actually publishes one instead of folding it into its text response.
        if response.WhichOneof("payload") == "message":
            return get_message_text(response.message)
    return ""
