"""FastMCP server for Problem 3 — Checkout Drop-off, OTP Abandonment & Ghost-Debit
Reconciliation.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 3.

Unlike Problem 2, this one genuinely needs the agent mesh for its decision
(stock check + template choice + the two-hop delegation to send a message) —
the tools here are the deterministic pieces (ground-truth verification, stock
check, link generation); the Checkout Salvage Agent's reasoning decides which
of them to call and builds the Artifact requesting a WhatsApp send.

Run directly:
    uv run python services/mcp-servers/prob3_otp_watch/server.py
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_agent_kit.wa_templates import build_delegation_artifact  # noqa: E402
from rzp_razorpay_client.client import create_payment_link, fetch_order_payments  # noqa: E402
from inventory import check_stock  # noqa: E402
from watchdog import claim_ghost_debit, schedule_stage2  # noqa: E402

mcp = FastMCP("Prob3OtpWatch")

# Real bug found by an actual Razorpay test-mode call (2026-09-03): Razorpay
# rejects payment_link.create with expire_by exactly 15 minutes out
# ("timestamp must be atleast 15 minutes in future") - it requires strictly
# MORE than 15 minutes, not >=, and by the time a request reaches Razorpay's
# server a small amount of wall-clock time has already elapsed since this
# constant was read. 960s (16 min) confirmed working live; kept as close to
# the design's intended 15-minute window as a real safety margin allows.
RECOVERY_LINK_EXPIRY_SECONDS = 960


@mcp.tool()
def verify_order_payment(order_id: str) -> dict:
    """Ground-truth check for whether an order was actually paid, independent of
    whether our webhook arrived. Calls GET /v1/orders/{id}/payments — this is the
    ONLY reliable way to detect a ghost debit (customer's app crashed mid-payment
    but the money actually moved). Returns the first captured payment found, if any.
    """
    result = fetch_order_payments(order_id)
    for item in result.get("items", []):
        if item.get("status") == "captured":
            return {
                "captured": True,
                "payment_id": item["id"],
                "amount": item.get("amount"),
                "acquirer_data": item.get("acquirer_data", {}),
            }
    return {"captured": False}


@mcp.tool()
def claim_order_as_ghost_debit(order_id: str, payment_id: str) -> dict:
    """Resolve a checkout session as paid after verify_order_payment found a
    captured payment the client never confirmed. Marks the session recovered
    and clears any remaining watchdog checkpoints so no recovery message ever
    fires for an order that already succeeded."""
    claim_ghost_debit(order_id, payment_id)
    write_audit_log(
        problem_id=3, tool_name="claim_order_as_ghost_debit",
        entity_refs={"order_id": order_id, "payment_id": payment_id},
        observation={"source": "agent_initiated_verification"},
        decision={"action": "CLAIM_GHOST_DEBIT"}, execution={"status": "claimed"},
        mcp_server="prob3_otp_watch",
    )
    return {"status": "claimed", "order_id": order_id, "payment_id": payment_id}


@mcp.tool()
def check_stock_for_recovery(sku_id: str) -> dict:
    """Check whether the item is still purchasable before sending a recovery
    link (Scenario 1: don't send a dead link for something already sold out).
    """
    return {"sku_id": sku_id, "in_stock": check_stock(sku_id)}


@mcp.tool()
def schedule_second_checkpoint(order_id: str) -> dict:
    """Called when stage1 fires and the order is still unpaid — schedules the
    T+15min stage2 checkpoint rather than messaging immediately (avoids the
    'under 15 minutes reads as intrusive' finding from the design's real-world
    grounding)."""
    schedule_stage2(order_id)
    write_audit_log(
        problem_id=3, tool_name="schedule_second_checkpoint", entity_refs={"order_id": order_id},
        observation={"stage": 1, "still_unpaid": True},
        decision={"action": "SCHEDULE_STAGE2", "reason": "avoid_under_15min_intrusiveness"},
        execution={"status": "stage2_scheduled"}, mcp_server="prob3_otp_watch",
    )
    return {"status": "stage2_scheduled", "order_id": order_id}


@mcp.tool()
def generate_recovery_link(order_id: str, amount: int, customer_name: str,
                            customer_contact: str, customer_id: str | None = None) -> dict:
    """Generate the one allowed recovery Payment Link for a genuinely unpaid,
    still-in-stock checkout — 15-minute expiry, per the design. Also builds the
    two-hop delegation artifact for the Orchestrator to hand to the
    Conversational NLP Agent — deterministic (the template and its variables
    are fully known from this tool's own arguments plus the link it just
    created), not left to the caller's LLM to restate correctly.
    customer_id is optional since a guest checkout may not have one yet."""
    link = create_payment_link(
        amount=amount,
        currency="INR",
        reference_id=order_id,
        description=f"Complete your payment for order {order_id}",
        expire_by=_expire_by_epoch(),
        customer={"name": customer_name, "contact": customer_contact},
    )
    delegation = build_delegation_artifact(
        customer_id, customer_contact, "checkout_recovery_link",
        {"name": customer_name, "amount": amount, "link": link["short_url"]},
    )
    write_audit_log(
        problem_id=3, tool_name="generate_recovery_link", entity_refs={"order_id": order_id},
        observation={"amount": amount},
        decision={"action": "SEND_RECOVERY_LINK", "template": "checkout_recovery_link"},
        execution={"status": "link_created", "payment_link_id": link["id"]},
        mcp_server="prob3_otp_watch",
    )
    return {"payment_link_id": link["id"], "short_url": link["short_url"], **delegation}


def _expire_by_epoch() -> int:
    import time

    return int(time.time()) + RECOVERY_LINK_EXPIRY_SECONDS


if __name__ == "__main__":
    mcp.run()
