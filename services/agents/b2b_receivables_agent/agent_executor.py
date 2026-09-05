"""AgentExecutor for the B2B Receivables Agent (Problem 9).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problem 9). The only agent connected to Srv9 (prob9_recon) —
enforces the fixed action set via execute_action before anything happens;
this agent's LLM only ever proposes an action from gather_decision_context,
it never executes state changes directly.
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
    "prob9_recon": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob9_recon" / "server.py")],
    },
    "prob0_policy_rag": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob0_policy_rag" / "server.py")],
    },
}

SYSTEM_PROMPT = (
    "You are the B2B Receivables Agent for a Razorpay merchant's revenue "
    "recovery system (Problem 9). You understand why an enterprise invoice "
    "is unpaid, detect payment intent and disputes, and choose the "
    "least-friction recovery action. You must ALWAYS call "
    "gather_decision_context first to see the invoice's current state and "
    "which actions the deterministic escalation ceiling currently permits, "
    "then call execute_action with exactly one action from that allowed "
    "list plus your reasoning. Never propose an action outside the allowed "
    "list - execute_action will reject it anyway, but you should reason "
    "about the right action within what's actually permitted right now. "
    "If a customer's reply mentions a GSTIN or invoice discrepancy, call "
    "check_gstin or pause_for_dispute instead - the agent must know when "
    "NOT to chase money. If a dispute is instead delegated to you from the "
    "Conversational NLP Agent (it only has customer_id, never invoice_id - "
    "that's your data), call find_open_invoice_for_customer first to "
    "resolve which invoice is meant before calling pause_for_dispute; never "
    "guess an invoice_id.\n\n"
    "If a business customer's reply is an open-ended question you can't "
    "answer from the invoice context alone (e.g. asking about TDS handling, "
    "dispute process, or payment terms), follow this exact sequence: (1) call "
    "retrieve_policy_context with the question, (2) compose your reply "
    "strictly from the returned chunks if status is 'match', or a generic "
    "'let me check and get back to you' if 'no_match' - never invent a "
    "policy detail, (3) call log_faq_interaction with your decision, "
    "(4) call build_faq_reply_delegation with your composed reply text - you "
    "do NOT own the WhatsApp send yourself, this hands it to the "
    "Conversational NLP Agent via the Orchestrator's two-hop delegation."
)


class B2bReceivablesAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self._mcp_client = MultiServerMCPClient(SERVERS)
        self._llm: ChatOpenAI | None = None  # lazy — see _get_llm

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = get_chat_llm()
        return self._llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()

        tools = await self._mcp_client.get_tools()
        named_tools = {t.name: t for t in tools}
        llm_with_tools = self._get_llm().bind_tools(tools)

        # Multi-round loop (gap fix, 2026-09-03): this agent's own system
        # prompt has always required gather_decision_context THEN
        # execute_action as two sequential tool calls, and the FAQ flow now
        # adds a third/fourth step - but the old single-round loop only ever
        # executed the FIRST round's tool calls, then treated the next LLM
        # turn as pure text even if it contained more tool_calls (silently
        # dropped). execute_action itself may never have actually run under
        # that loop. This loop is a strict superset of the old behavior.
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
            await event_queue.enqueue_event(new_text_status_update_event(
                task_id=context.task_id, context_id=context.context_id,
                state=TaskState.TASK_STATE_COMPLETED, text=response.content,
            ))
        else:
            await event_queue.enqueue_event(new_text_message(response.content))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(new_text_message("Nothing in-flight to cancel."))
