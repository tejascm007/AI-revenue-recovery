"""AgentExecutor for the Conversational NLP Agent (Problems 7, 8).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problems 7/8). This is the one agent every other problem reaches
via the two-hop A2A delegation for WhatsApp sends — Srv8 (prob8_meta_wa_api)
is exclusively this agent's MCP connection, never shared directly.
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

from rzp_agent_kit.two_hop import find_delegation_artifact

MAX_TOOL_ROUNDS = 6  # safety cap against a runaway tool-calling loop

SERVERS = {
    "prob7_nlp_extract": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob7_nlp_extract" / "server.py")],
    },
    "prob8_meta_wa_api": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob8_meta_wa_api" / "server.py")],
    },
    "prob0_policy_rag": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob0_policy_rag" / "server.py")],
    },
}

SYSTEM_PROMPT = (
    "You are the Conversational NLP Agent for a Razorpay merchant's revenue "
    "recovery system. You handle two problems: Promise-to-Pay extraction "
    "(Problem 7 - classify a customer's reply and, if it's a payment promise, "
    "resolve the date deterministically via set_ptp_lock) and all outbound/"
    "inbound WhatsApp messaging for the whole project (Problem 8 - you are "
    "the ONLY agent that sends WhatsApp; every other problem reaches you via "
    "a two-hop delegation through the Orchestrator, never directly). When "
    "another agent's request arrives asking you to send a specific template, "
    "just send it via send_whatsapp_message. When a customer's own message "
    "needs handling, classify its intent yourself, then call the right tool - "
    "set_ptp_lock for a payment promise, or send_freeform_reply for a direct "
    "answer within an open conversation window. HOSTILE sentiment always "
    "gets escalate_for_review called too, regardless of what else you do. "
    "Never invent a policy detail, discount, or date the customer didn't "
    "actually provide.\n\n"
    "For an open-ended question you cannot answer from what's already in "
    "this conversation (e.g. 'EMI option milega kya?', 'what happens if I "
    "don't pay?'), you MUST follow this exact sequence: (1) call "
    "retrieve_policy_context with the customer's question, (2) if it "
    "returns status 'match', compose your reply strictly from the returned "
    "chunks - never add a policy detail the chunks don't contain; if it "
    "returns 'no_match', your reply must be a generic 'let me check and get "
    "back to you' rather than a guess, (3) call log_faq_interaction with "
    "your decision (ANSWER_FROM_RETRIEVED_CONTEXT or ESCALATED_LOW_CONFIDENCE) "
    "before sending anything, (4) then actually send the reply via "
    "send_freeform_reply or send_whatsapp_message. Never skip step 3 - every "
    "FAQ interaction must be audited whether you answered or escalated."
)


class ConversationalNlpAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self._mcp_client = MultiServerMCPClient(SERVERS)
        self._llm: ChatOpenAI | None = None  # lazy — see _get_llm

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(model="gpt-5.6")
        return self._llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()

        tools = await self._mcp_client.get_tools()
        named_tools = {t.name: t for t in tools}
        llm_with_tools = self._get_llm().bind_tools(tools)

        # Multi-round loop (gap fix, 2026-09-03): the FAQ flow genuinely needs
        # several sequential, dependent tool calls - retrieve_policy_context,
        # then log_faq_interaction with the decision that retrieval enabled,
        # then the actual send - which a single fixed round of tool-calling
        # can't express. This loop is a strict superset of the old one-round
        # behavior: every other problem's LLM naturally stops issuing tool
        # calls after one round anyway, so nothing about their behavior changes.
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

            # No producer tool of Srv7/Srv8 emits this today (this agent
            # already owns the WhatsApp send, nothing to delegate outbound) -
            # kept for structural symmetry with the other 3 executors and as
            # the landing spot for a future reverse hop (e.g. an inbound
            # dispute intent delegated to the B2B Receivables Agent).
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
