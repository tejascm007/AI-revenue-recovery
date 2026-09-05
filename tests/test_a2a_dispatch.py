"""Tests for services/orchestrator/a2a_dispatch.py's pure instruction-
building functions (build_instruction, build_delegation_instruction) - the
actual network dispatch() function needs a live A2A agent and isn't covered
here, see Design_Spec_and_Decisions.md's changelog for how it was verified
live instead.
"""

from a2a_dispatch import build_delegation_instruction, build_instruction


def test_build_instruction_includes_event_type_problem_id_and_payload():
    instruction = build_instruction("payment.failed", 2, {"order_id": "order_123", "method": "upi"})
    assert "payment.failed" in instruction
    assert "Problem: 2" in instruction
    assert "order_123" in instruction
    assert "upi" in instruction


def test_build_delegation_instruction_for_a_template_send_names_the_right_tool():
    artifact = {
        "action": "send_whatsapp", "customer_id": "cust_1", "phone": "+919999999999",
        "template_id": "checkout_recovery_link", "variables": {"name": "Asha", "amount": 50000},
    }
    instruction = build_delegation_instruction(artifact)
    assert "send_whatsapp_message" in instruction
    assert "send_freeform_reply" not in instruction
    assert "checkout_recovery_link" in instruction
    assert "cust_1" in instruction


def test_build_delegation_instruction_for_a_freeform_send_names_the_right_tool():
    artifact = {
        "action": "send_whatsapp_freeform", "customer_id": None, "phone": "+919999999999",
        "text": "Hello! How can we help?",
    }
    instruction = build_delegation_instruction(artifact)
    assert "send_freeform_reply" in instruction
    assert "send_whatsapp_message" not in instruction
    assert "Hello! How can we help?" in instruction


def test_build_delegation_instruction_for_a_b2b_dispute_names_the_right_tools_in_order():
    artifact = {
        "action": "flag_b2b_dispute", "customer_id": "cust_1", "phone": "+919999999999",
        "raw_message": "We already paid this invoice last week.",
        "dispute_reason": "customer claims prior payment",
    }
    instruction = build_delegation_instruction(artifact)
    assert "find_open_invoice_for_customer" in instruction
    assert "pause_for_dispute" in instruction
    assert "send_whatsapp_message" not in instruction
    assert "send_freeform_reply" not in instruction
    assert "cust_1" in instruction
    assert "We already paid this invoice last week." in instruction
    # the resolve-invoice step must be told to happen before the pause step
    assert instruction.index("find_open_invoice_for_customer") < instruction.index("pause_for_dispute")


def test_build_delegation_instruction_for_a_payment_link_resend_names_the_right_tools_in_order():
    artifact = {
        "action": "request_payment_link_resend", "customer_id": "cust_2", "phone": "+919999999999",
        "raw_message": "Can you send that payment link again, I lost it.",
    }
    instruction = build_delegation_instruction(artifact)
    assert "find_active_checkout_session_for_customer" in instruction
    assert "generate_recovery_link" in instruction
    assert "send_whatsapp_message" not in instruction
    assert "cust_2" in instruction
    assert "Can you send that payment link again, I lost it." in instruction
    assert instruction.index("find_active_checkout_session_for_customer") < instruction.index("generate_recovery_link")


def test_build_delegation_instruction_defaults_to_template_shape_for_unknown_action():
    # An artifact with no "action" key (or a future third kind) falls back
    # to the template-based instruction rather than raising - the safer
    # default given send_whatsapp_message rejects an unknown template_id
    # itself, whereas guessing free-form text for a business-initiated send
    # could violate Meta's own policy.
    instruction = build_delegation_instruction({"customer_id": "c1", "phone": "+91123"})
    assert "send_whatsapp_message" in instruction
