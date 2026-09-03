"""AgentExecutor for the B2B Receivables Agent (Problem 9).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problem 9). The only agent connected to Srv9 (prob9_recon) —
enforces the fixed action set via execute_action before anything happens;
this agent's LLM only ever proposes an action from gather_decision_context,
it never executes state changes directly.
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
    "prob9_recon": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob9_recon" / "server.py")],
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
    "NOT to chase money."
)


class B2bReceivablesAgentExecutor(AgentExecutor):
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
