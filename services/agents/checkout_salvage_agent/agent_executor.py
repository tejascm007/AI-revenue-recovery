"""AgentExecutor for the Checkout Salvage Agent (Problems 2, 3, 4).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problems 2/3/4). This agent's own LLM reasoning is genuinely thin
per the design's own finding — Problem 2's diagnosis is deterministic rule
evaluation, Problem 4's cap enforcement is atomic Mongo, and Problem 3 is the
one place real judgment (which recovery path, what to say) matters. The LLM's
role here is mostly: pick the right tool from the three servers below, and
phrase the audit-log reasoning / any two-hop delegation Artifact — not decide
the underlying route/cap logic itself, which the tools already enforce.

API surface note: a2a-sdk 1.1.2 uses protobuf-generated types (a2a_pb2), a
real breaking change from the pre-1.0 SDK the original design research was
based on — Message/Part are proto messages, not plain Pydantic models, and
must be built via a2a.helpers.proto_helpers (new_text_message, get_message_text),
not by passing kwargs directly.
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))  # first use in this process: rzp_agent_kit below

from a2a.helpers.proto_helpers import (
    new_data_artifact_update_event,
    new_text_message,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from rzp_agent_kit.llm import get_chat_llm
from rzp_agent_kit.two_hop import find_delegation_artifact

MAX_TOOL_ROUNDS = 6  # safety cap against a runaway tool-calling loop

SERVERS = {
    "prob2_route": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob2_route" / "server.py")],
    },
    "prob3_otp_watch": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob3_otp_watch" / "server.py")],
    },
    "prob4_emi_salvage": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob4_emi_salvage" / "server.py")],
    },
}

SYSTEM_PROMPT = (
    "You are the Checkout Salvage Agent for a Razorpay merchant's revenue "
    "recovery system. You handle three problems: payment route degradation "
    "(Problem 2), checkout drop-off and ghost-debit reconciliation (Problem 3), "
    "and BNPL/EMI decline salvage (Problem 4). Your tools already enforce the "
    "deterministic rules (route-health thresholds, stopping caps) — your job is "
    "to pick the right tool for the situation described, and explain your "
    "reasoning concisely for the audit trail. Never invent a payment amount, "
    "provider name, or date that wasn't given to you. If a request is "
    "delegated to you asking to resend a customer's payment link (it only "
    "has customer_id - that's your data), call "
    "find_active_checkout_session_for_customer first to resolve the order "
    "before calling generate_recovery_link; never guess an order_id."
)


class CheckoutSalvageAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self._mcp_client = MultiServerMCPClient(SERVERS)
        self._llm: ChatOpenAI | None = None  # lazy — see _get_llm

    def _get_llm(self) -> ChatOpenAI:
        # Constructed lazily and cached, not at __init__ time: unlike every
        # other external client in this project (Mongo/Redis/Razorpay/Meta,
        # all lazy via lru_cache), ChatOpenAI validates its API key eagerly at
        # construction — building it in __init__ would make the whole A2A app
        # fail to even start without OPENROUTER_API_KEY set, rather than
        # failing only when an actual request needs the LLM. Routed through
        # OpenRouter (libs/rzp_agent_kit/llm.py), decided 2026-09-03.
        if self._llm is None:
            self._llm = get_chat_llm()
        return self._llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()

        tools = await self._mcp_client.get_tools()
        named_tools = {t.name: t for t in tools}
        llm_with_tools = self._get_llm().bind_tools(tools)

        # Multi-round loop (gap fix, 2026-09-03): the old single fixed round
        # of tool-calling couldn't express a multi-step decision (e.g.
        # diagnose, then act on the diagnosis) - this loop is a strict
        # superset of that behavior, since every existing single-tool-call
        # case here still terminates after one round on its own.
        messages = [("system", SYSTEM_PROMPT), ("human", user_input)]
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)
        artifact = None

        for _ in range(MAX_TOOL_ROUNDS):
            if not getattr(response, "tool_calls", None):
                break
            tool_messages = []
            raw_results = []
            for call in response.tool_calls:
                result = await named_tools[call["name"]].ainvoke(call["args"])
                raw_results.append(result)
                tool_messages.append(ToolMessage(tool_call_id=call["id"], content=str(result)))
            messages.extend(tool_messages)

            # Two-hop delegation fix (2026-09-03): extracted deterministically
            # from the tool's own structured result, not left to the LLM's
            # final-turn text to restate correctly - see libs/rzp_agent_kit/two_hop.py.
            found = find_delegation_artifact(raw_results)
            if found is not None:
                artifact = found
                await event_queue.enqueue_event(new_data_artifact_update_event(
                    task_id=context.task_id, context_id=context.context_id,
                    name="two_hop_delegation", data=artifact,
                ))

            response = await llm_with_tools.ainvoke(messages)
            messages.append(response)

        if artifact is not None:
            # Verified (2026-09-03): once a TaskArtifactUpdateEvent has been
            # enqueued, the SDK requires the response to complete via a Task
            # status event, not a bare Message - "Received Message object in
            # task mode" is a real InvalidAgentResponseError, not a guess.
            await event_queue.enqueue_event(new_text_status_update_event(
                task_id=context.task_id, context_id=context.context_id,
                state=TaskState.TASK_STATE_COMPLETED, text=response.content,
            ))
        else:
            await event_queue.enqueue_event(new_text_message(response.content))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # This agent's tool calls are all fast/synchronous (deterministic Mongo/
        # Redis operations, no long-running work) — there is nothing meaningful
        # to interrupt mid-flight, so cancellation just acknowledges the request.
        await event_queue.enqueue_event(new_text_message("Nothing in-flight to cancel."))
