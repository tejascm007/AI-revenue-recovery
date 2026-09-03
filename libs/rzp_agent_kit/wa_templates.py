"""Single source of truth for Meta WhatsApp template IDs and their required
variables, plus the standardized two-hop delegation artifact shape.

Design reference: Design_Spec_and_Decisions.md, section 3's cross-agent
delegation pattern. Every problem that needs a WhatsApp send (3, 5, 6, 9)
builds its delegation artifact via build_delegation_artifact() below rather
than hand-rolling a dict — this is the fix for a real bug caught while
reconciling the two-hop delegation gap: Problem 9's execute_action referenced
template_ids "b2b_reminder"/"b2b_payment_link" that were never added to
prob8_meta_wa_api's own catalog, so a real send would have failed validation
with no earlier warning. prob8_meta_wa_api/server.py imports TEMPLATE_CATALOG
from here too (rather than keeping its own copy) so the two can never drift
apart again.

The artifact's fields are deliberately identical to
prob8_meta_wa_api.send_whatsapp_message's own parameters — the Orchestrator's
second-hop instruction to the Conversational NLP Agent is just "call
send_whatsapp_message with exactly these arguments", so producer and
consumer share one shape with nothing to translate.
"""

TEMPLATE_CATALOG = {
    "checkout_recovery_link": ["name", "amount", "link"],
    "ghost_debit_reassurance": ["name", "amount"],
    "hard_decline_link": ["name", "link"],
    "duplicate_charge_refunded": ["name", "amount"],
    "dunning_touch_1": ["name", "link"],
    "dunning_touch_2": ["name", "link"],
    "dunning_touch_3": ["name", "link"],
    "ptp_clarifying_question": ["name", "raw_expression"],
    "waitlist_notice": ["name"],
    "extension_granted": ["name", "new_date"],
    "extension_declined": ["name"],
    "b2b_reminder": ["name", "invoice_id", "amount"],
    "b2b_payment_link": ["name", "invoice_id", "amount", "link"],
}


def build_delegation_artifact(customer_id: str | None, phone: str, template_id: str,
                               variables: dict) -> dict:
    """Returns the standardized {"status": "pending_two_hop_delegation", ...}
    shape every producer tool returns when it needs the Conversational NLP
    Agent to send a WhatsApp message. Validates the template_id and its
    required variables at construction time — the same validation
    send_whatsapp_message itself does — so a mismatch is caught here, at the
    point of decision, rather than surfacing only when the second hop
    actually tries to send.
    """
    if template_id not in TEMPLATE_CATALOG:
        raise ValueError(f"Unknown template_id: {template_id!r}")
    required = TEMPLATE_CATALOG[template_id]
    missing = [v for v in required if v not in variables]
    if missing:
        raise ValueError(f"Missing required template variables for {template_id!r}: {missing}")

    return {
        "status": "pending_two_hop_delegation",
        "artifact": {
            "action": "send_whatsapp",
            "customer_id": customer_id,
            "phone": phone,
            "template_id": template_id,
            "variables": variables,
        },
    }
