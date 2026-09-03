"""AgentExecutor for the Recurring Revenue Agent (Problems 5, 6).

Design reference: Design_Spec_and_Decisions.md, section 3 (architecture) and
section 11 (Problems 5/6). Same structure as the Checkout Salvage Agent
(services/agents/checkout_salvage_agent) — the pattern is now proven, this
is a direct application of it to a different pair of MCP servers.
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
    "prob5_hard_decline_salvage": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob5_hard_decline_salvage" / "server.py")],
    },
    "prob6_dunning_sequencer": {
        "transport": "stdio",
        "command": "uv",
        "args": ["run", "fastmcp", "run",
                 str(_CODES_ROOT / "services" / "mcp-servers" / "prob6_dunning_sequencer" / "server.py")],
    },
}

SYSTEM_PROMPT = (
    "You are the Recurring Revenue Agent for a Razorpay merchant's revenue "
    "recovery system. You handle two problems: hard-decline subscription "
    "salvage (Problem 5 - card expired, mandate revoked, or an AFA-amount "
    "heuristic; classify_decline tells you which) and the penalty-aware "
    "dunning sequence for soft/NSF declines (Problem 6 - fires only after "
    "Razorpay's own retry cascade is exhausted). Your tools already enforce "
    "the deterministic rules (classification, the escalation cadence, the "
    "PTP-active deferral check) — your job is to pick the right tool and "
    "explain your reasoning concisely for the audit trail. Never invent a "
    "customer name, amount, or date that wasn't given to you."
)


class RecurringRevenueAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self._mcp_client = MultiServerMCPClient(SERVERS)
        self._llm: ChatOpenAI | None = None  # lazy — see _get_llm

    def _get_llm(self) -> ChatOpenAI:
        # Lazy for the same reason as every other agent in this project:
        # ChatOpenAI validates its API key eagerly at construction, which
        # would otherwise make the whole A2A app fail to start without
        # OPENAI_API_KEY set. Pin to whatever OpenAI model is current at
        # build time - "gpt-5.6" was current as of this design session.
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
