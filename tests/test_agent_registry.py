"""Tests for services/orchestrator/agent_registry.py - the static
problem_id -> agent URL routing table every Kafka event dispatch depends on.
"""

import pytest

from agent_registry import (
    AGENT_NAMES, CONVERSATIONAL_NLP_AGENT_URL, DELEGATION_TARGET_URLS,
    PROBLEM_TO_AGENT_URL, resolve_agent_url,
)


@pytest.mark.parametrize("problem_id,expected_port", [
    (2, 9002), (3, 9002), (4, 9002),
    (5, 9003), (6, 9003),
    (7, 9004), (8, 9004),
    (9, 9005),
])
def test_every_problem_routes_to_the_correct_agent_port(problem_id, expected_port):
    url = resolve_agent_url(problem_id)
    assert url == f"http://localhost:{expected_port}"


def test_unknown_problem_id_raises_rather_than_silently_routing_nowhere():
    with pytest.raises(ValueError, match="No agent registered"):
        resolve_agent_url(999)


def test_every_registered_url_has_a_display_name():
    for url in set(PROBLEM_TO_AGENT_URL.values()):
        assert url in AGENT_NAMES, f"{url} is routable but has no entry in AGENT_NAMES"


def test_conversational_nlp_agent_url_is_derived_not_a_second_hardcoded_value():
    # The two-hop delegation target must be the SAME URL problem 7/8 already
    # route to, not an independently-typed duplicate that could drift.
    assert CONVERSATIONAL_NLP_AGENT_URL == PROBLEM_TO_AGENT_URL[7]
    assert CONVERSATIONAL_NLP_AGENT_URL == PROBLEM_TO_AGENT_URL[8]


def test_delegation_target_urls_routes_whatsapp_sends_to_conversational_nlp():
    assert DELEGATION_TARGET_URLS["send_whatsapp"] == CONVERSATIONAL_NLP_AGENT_URL
    assert DELEGATION_TARGET_URLS["send_whatsapp_freeform"] == CONVERSATIONAL_NLP_AGENT_URL


def test_delegation_target_urls_routes_b2b_disputes_to_the_b2b_agent_not_conversational_nlp():
    # The reverse-direction case (2026-09-05): unlike every WhatsApp-send
    # artifact, a dispute delegation must NOT land back on the Conversational
    # NLP Agent - it owns no invoice data to act on it.
    assert DELEGATION_TARGET_URLS["flag_b2b_dispute"] == PROBLEM_TO_AGENT_URL[9]
    assert DELEGATION_TARGET_URLS["flag_b2b_dispute"] != CONVERSATIONAL_NLP_AGENT_URL


def test_delegation_target_urls_routes_payment_link_resend_to_checkout_salvage():
    # A second reverse-direction case, distinct from the B2B one - proves the
    # mechanism generalizes to any cross-problem request, not just disputes.
    assert DELEGATION_TARGET_URLS["request_payment_link_resend"] == PROBLEM_TO_AGENT_URL[3]
    assert DELEGATION_TARGET_URLS["request_payment_link_resend"] != CONVERSATIONAL_NLP_AGENT_URL
