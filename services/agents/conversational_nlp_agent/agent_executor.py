"""AgentExecutor for the Conversational NLP Agent (Problems 7, 8).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problems 7/8). This is the one agent every other problem reaches
via the two-hop A2A delegation for WhatsApp sends — Srv8 (prob8_meta_wa_api)
is exclusively this agent's MCP connection, never shared directly.
"""

from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

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
    "actually provide."
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

        messages = [("system", SYSTEM_PROMPT), ("human", user_input)]
        response = await llm_with_tools.ainvoke(messages)

        if not getattr(response, "tool_calls", None):
            await event_queue.enqueue_event(new_text_message(response.content))
            return

        tool_messages = []
        for call in response.tool_calls:
            result = await named_tools[call["name"]].ainvoke(call["args"])
            tool_messages.append(ToolMessage(tool_call_id=call["id"], content=str(result)))

        final = await llm_with_tools.ainvoke([*messages, response, *tool_messages])
        await event_queue.enqueue_event(new_text_message(final.content))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(new_text_message("Nothing in-flight to cancel."))
