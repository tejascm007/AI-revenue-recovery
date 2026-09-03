"""Static registry mapping each problem to its owning A2A sub-agent.

Design reference: Design_Spec_and_Decisions.md, section 3 — the Orchestrator
is an ordinary A2A client holding one connection per sub-agent; there is no
protocol-level "delegate" primitive, all routing intelligence lives here.

Ports match each agent's own main.py (9002-9005), verified live individually
before this registry was written.
"""

PROBLEM_TO_AGENT_URL = {
    2: "http://localhost:9002", 3: "http://localhost:9002", 4: "http://localhost:9002",
    5: "http://localhost:9003", 6: "http://localhost:9003",
    7: "http://localhost:9004", 8: "http://localhost:9004",
    9: "http://localhost:9005",
}

AGENT_NAMES = {
    "http://localhost:9002": "Checkout Salvage Agent",
    "http://localhost:9003": "Recurring Revenue Agent",
    "http://localhost:9004": "Conversational NLP Agent",
    "http://localhost:9005": "B2B Receivables Agent",
}

# Named separately (2026-09-03, two-hop delegation fix): every WhatsApp send
# delegation artifact from any of the other 3 agents targets this one agent
# specifically, regardless of which problem originated it - derived from the
# same mapping above rather than a second hardcoded URL.
CONVERSATIONAL_NLP_AGENT_URL = PROBLEM_TO_AGENT_URL[7]


def resolve_agent_url(problem_id: int) -> str:
    if problem_id not in PROBLEM_TO_AGENT_URL:
        raise ValueError(f"No agent registered for problem_id={problem_id}")
    return PROBLEM_TO_AGENT_URL[problem_id]
