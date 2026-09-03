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

from rzp_razorpay_client.client import create_payment_link, fetch_order_payments  # noqa: E402
from inventory import check_stock  # noqa: E402
from watchdog import claim_ghost_debit, schedule_stage2  # noqa: E402

mcp = FastMCP("Prob3OtpWatch")

RECOVERY_LINK_EXPIRY_SECONDS = 900  # 15 minutes, per the design


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
    return {"status": "stage2_scheduled", "order_id": order_id}


@mcp.tool()
def generate_recovery_link(order_id: str, amount: int, customer_name: str,
                            customer_contact: str) -> dict:
    """Generate the one allowed recovery Payment Link for a genuinely unpaid,
    still-in-stock checkout — 15-minute expiry, per the design. The actual send
    happens via the two-hop delegation to the Conversational NLP Agent, not here;
    this tool only creates the link Razorpay-side."""
    link = create_payment_link(
        amount=amount,
        currency="INR",
        reference_id=order_id,
        description=f"Complete your payment for order {order_id}",
        expire_by=_expire_by_epoch(),
        customer={"name": customer_name, "contact": customer_contact},
    )
    return {"payment_link_id": link["id"], "short_url": link["short_url"]}


def _expire_by_epoch() -> int:
    import time

    return int(time.time()) + RECOVERY_LINK_EXPIRY_SECONDS


if __name__ == "__main__":
    mcp.run()
