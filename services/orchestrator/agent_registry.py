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

# Gap fix (2026-09-05, reverse two-hop): the delegation direction above is
# always "some agent -> Conversational NLP Agent, please send this." The
# reverse also exists now - the Conversational NLP Agent detects a B2B
# dispute intent in an inbound message it owns (Problem 7/8) but doesn't own
# invoice data to act on it, so it delegates to the B2B Receivables Agent
# instead. One shared dict, keyed by the artifact's own "action", so the
# Orchestrator resolves the target generically rather than hardcoding a
# second always-Conversational-NLP assumption for every future case.
DELEGATION_TARGET_URLS = {
    "send_whatsapp": CONVERSATIONAL_NLP_AGENT_URL,
    "send_whatsapp_freeform": CONVERSATIONAL_NLP_AGENT_URL,
    "flag_b2b_dispute": PROBLEM_TO_AGENT_URL[9],
    # Gap fix (2026-09-05): a second reverse-delegation case, generalizing
    # the pattern beyond just B2B disputes - a customer asking to resend
    # their payment link routes to the Checkout Salvage Agent (Problem 3's
    # owner), which itself then delegates the actual WhatsApp send back to
    # the Conversational NLP Agent (a third hop - see main.py's
    # dispatch_with_delegation). Adding a future cross-problem request type
    # here is just one more dict entry plus one small deterministic tool
    # pair, not a new mechanism each time.
    "request_payment_link_resend": PROBLEM_TO_AGENT_URL[3],
}


def resolve_agent_url(problem_id: int) -> str:
    if problem_id not in PROBLEM_TO_AGENT_URL:
        raise ValueError(f"No agent registered for problem_id={problem_id}")
    return PROBLEM_TO_AGENT_URL[problem_id]
