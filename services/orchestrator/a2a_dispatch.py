"""A2A dispatch — sends one event to its owning sub-agent and returns the
result, including any two-hop delegation artifacts.

Timeout raised to 120s (2026-09-05, from an original 60s) because live
pre-demo testing with a free OpenRouter model (temporarily substituted while
the paid account's balance was exhausted - see .env) showed 60s genuinely
wasn't enough for the Conversational NLP Agent specifically: it owns 3 MCP
subprocess servers to spawn AND free-tier models measurably respond slower
than the paid gpt-4o this was originally tuned against. The original 60s
reasoning still holds otherwise - a real round trip involves the target
agent spawning up to 3 MCP subprocess servers via `uv run fastmcp run ...`
before it can even attempt the LLM call, and the default httpx timeout (a
few seconds) was observed to fail on this alone, well before ever reaching
the (separately expected) missing-API-key error.

Two-hop delegation fix (2026-09-03): closes the gap this module's own
docstring used to document. Verified directly against the installed SDK
(a throwaway test agent, since no OPENAI_API_KEY is available in this
environment to exercise the real LLM tool-calling path) that a response
collapses into exactly ONE of two shapes depending on what the target
executor enqueued:
  - a bare Message -> StreamResponse.WhichOneof("payload") == "message"
    (the ordinary case, no delegation needed - this was the only shape
    the original version of this function handled, and correctly so).
  - a TaskArtifactUpdateEvent followed by a TaskStatusUpdateEvent (required
    once any Task-scoped event is enqueued - a bare Message at that point is
    rejected with InvalidAgentResponseError) -> for a non-streaming agent
    (every agent in this project declares streaming=False) these collapse
    into a single "task" payload carrying both task.status.message (the
    final text) and task.artifacts[] (the delegation payload, recovered via
    get_data_parts). "artifact_update"/"status_update" payload kinds are
    also handled defensively in case a future agent declares streaming=True.
"""

import json

import httpx

import a2a.types as t
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_message_text, new_text_message

DISPATCH_TIMEOUT_SECONDS = 120.0


def build_instruction(event_type: str, problem_id: int, payload: dict) -> str:
    return (
        f"Event: {event_type}. Problem: {problem_id}. "
        f"Details: {json.dumps(payload)}. "
        "Diagnose this situation and take the appropriate action using your tools."
    )


def build_delegation_instruction(artifact: dict) -> str:
    """Builds the next-hop instruction for whichever agent a delegation
    artifact targets — states the exact arguments (or the exact lookup-then-
    act sequence) rather than re-describing the situation, so the receiving
    agent's LLM only needs to follow it, not re-derive intent from scratch.
    Four artifact shapes exist: "send_whatsapp" (template-based, from
    prob3/5/6/9's deterministic tools) and "send_whatsapp_freeform" (an
    LLM-composed FAQ reply, from prob0_policy_rag.build_faq_reply_delegation
    - free-form since it answers within an already-open Customer Service
    Window, not a business-initiated template send) both target the
    Conversational NLP Agent (gap fix, 2026-09-03); "flag_b2b_dispute" and
    "request_payment_link_resend" (gap fix, 2026-09-05) run the *reverse*
    direction - the Conversational NLP Agent delegating OUT to whichever
    agent owns the data (invoice or checkout state) it doesn't have."""
    action = artifact.get("action")
    if action == "request_payment_link_resend":
        return (
            "Another agent detected a customer asking to resend their payment "
            f"link and is delegating it to you, since you own checkout state. "
            f"customer_id={artifact.get('customer_id')!r}, phone={artifact.get('phone')!r}. "
            f"The customer's raw message: {artifact.get('raw_message')!r}. First "
            "call find_active_checkout_session_for_customer to resolve which "
            "order this is about. If one is found, call generate_recovery_link "
            "with the resolved order_id, amount, customer_name, and "
            "customer_contact to regenerate and send a fresh link. If none is "
            "found, do not guess an order_id - just note that no active "
            "checkout exists for this customer."
        )
    if action == "flag_b2b_dispute":
        return (
            "Another agent detected a possible billing dispute in an inbound "
            "customer message and is delegating it to you, since you own "
            f"invoice data. customer_id={artifact.get('customer_id')!r}. "
            f"The customer's raw message: {artifact.get('raw_message')!r} "
            f"(extracted reason: {artifact.get('dispute_reason')!r}). First call "
            "find_open_invoice_for_customer to resolve which invoice this is "
            "about. If one is found, call pause_for_dispute with the resolved "
            "invoice_id, an appropriate dispute_type derived from the reason "
            "given, and a concise description. If none is found, do not guess "
            "an invoice_id - just note that no open invoice exists for this "
            "customer."
        )
    if action == "send_whatsapp_freeform":
        return (
            "Another agent has already composed a reply to an open customer "
            "conversation and prepared its exact arguments. Call "
            "send_freeform_reply with EXACTLY these arguments, unmodified: "
            f"customer_id={artifact.get('customer_id')!r}, phone={artifact.get('phone')!r}, "
            f"text={artifact.get('text')!r}."
        )
    return (
        "Another agent has already decided a WhatsApp message must be sent "
        "and prepared its exact arguments. Call send_whatsapp_message with "
        "EXACTLY these arguments, unmodified: "
        f"customer_id={artifact.get('customer_id')!r}, phone={artifact.get('phone')!r}, "
        f"template_id={artifact.get('template_id')!r}, variables={artifact.get('variables')!r}."
    )


async def dispatch(agent_url: str, instruction: str) -> dict:
    """Sends `instruction` to the agent at `agent_url`. Returns
    {"text": str, "delegation_artifacts": list[dict]} - text is the agent's
    final response, delegation_artifacts is every pending_two_hop_delegation
    artifact it produced (usually 0 or 1, a list for generality)."""
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
    text = ""
    delegation_artifacts: list[dict] = []

    async for response in client.send_message(request):
        kind = response.WhichOneof("payload")
        if kind == "message":
            text = get_message_text(response.message)
        elif kind == "task":
            task = response.task
            if task.status.HasField("message"):
                text = get_message_text(task.status.message)
            for artifact in task.artifacts:
                delegation_artifacts.extend(get_data_parts(artifact.parts))
        elif kind == "status_update":
            status = response.status_update.status
            if status.HasField("message"):
                text = get_message_text(status.message)
        elif kind == "artifact_update":
            delegation_artifacts.extend(get_data_parts(response.artifact_update.artifact.parts))

    return {"text": text, "delegation_artifacts": delegation_artifacts}
