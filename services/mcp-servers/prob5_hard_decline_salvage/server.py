"""FastMCP server for Problem 5 — Failed-Subscription & Mandate Bounces
(hard-decline path only, per the 2026-09-02 classification split with Problem 6).

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 5.

Tool named per the design's own correction: the original `Sub_Pause` name
assumed pausing Razorpay's own retry cadence was achievable via API, which
was confirmed impossible (POST .../subscriptions/:id/pause only works from
the `active` state, not `pending`) — renamed for honesty.

Run directly:
    uv run python services/mcp-servers/prob5_hard_decline_salvage/server.py
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.subscription_lifecycle import (  # noqa: E402
    record_capture_and_check_duplicate,
    schedule_hard_decline_link,
)
from rzp_razorpay_client.client import create_payment_link, get_client  # noqa: E402

mcp = FastMCP("Prob5HardDeclineSalvage")

HARD_DECLINE_REASONS = {
    "card_expired",
    "mandate_not_active",
    "debit_instrument_blocked",
    "debit_instrument_inactive",
    "bank_account_invalid",
}
DEFAULT_AFA_THRESHOLD = 15000  # RBI's own regulatory figure, not merchant-tunable
HARD_DECLINE_LINK_EXPIRY_SECONDS = 5 * 3600  # matches the 5h delay before it's even sent


def _get_afa_threshold(is_exempt_category: bool) -> int:
    """The base RBI AFA threshold (Rs 15,000) is a regulatory constant, not a
    merchant setting. merchant_config.afa_override_threshold is only the higher
    Rs 1,00,000 exemption for insurance/mutual-fund/credit-card-bill categories
    - it applies only when the caller explicitly flags this transaction as
    belonging to one of those categories. We don't yet model transaction
    categories anywhere in the schema, so is_exempt_category defaults to False
    (base threshold) rather than silently assuming every transaction qualifies.
    """
    if not is_exempt_category:
        return DEFAULT_AFA_THRESHOLD
    db = get_db()
    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    return config.get("afa_override_threshold", DEFAULT_AFA_THRESHOLD)


@mcp.tool()
def classify_decline(subscription_id: str, error_reason: str | None, amount: int,
                      is_exempt_category: bool = False) -> dict:
    """Classify a subscription payment failure as HARD (structurally
    unrecoverable - card expired, mandate revoked, or the AFA-heuristic: a
    generic/null decline above the AFA threshold) or SOFT (insufficient_funds,
    genuinely retryable - Problem 6's territory, no action taken here).

    STOP_IF_USER_CANCELLED is checked first: if the subscription was already
    voluntarily cancelled before this failure, no classification or action
    happens at all - never send a "your payment failed" message for a plan
    the customer already ended themselves.
    """
    db = get_db()
    subscription = db.subscriptions.find_one({"razorpay_subscription_id": subscription_id})
    if subscription and subscription.get("status") == "cancelled":
        return {"classification": "skipped", "reason": "STOP_IF_USER_CANCELLED"}

    threshold = _get_afa_threshold(is_exempt_category)
    is_hard = error_reason in HARD_DECLINE_REASONS or (
        (error_reason is None or error_reason in ("authentication_failed", "card_declined"))
        and amount > threshold
    )
    classification = "hard" if is_hard else "soft"

    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {"current_cycle_decline_classification": classification}},
    )

    if is_hard:
        result = db.subscriptions.find_one_and_update(
            {"razorpay_subscription_id": subscription_id, "hard_decline_link_sent": {"$ne": True}},
            {"$set": {"hard_decline_link_sent": True}},
        )
        if result is not None:
            schedule_hard_decline_link(subscription_id)

    return {"classification": classification, "afa_threshold_used": threshold}


@mcp.tool()
def generate_hard_decline_link(subscription_id: str, invoice_id: str, amount: int,
                                customer_name: str, customer_contact: str) -> dict:
    """Called when the T+5h watchdog checkpoint fires and the subscription is
    still genuinely unpaid (re-verify before calling this - the caller's job,
    same STOP_IF_ALREADY_RESOLVED idempotency principle as Problem 3). Creates
    the one manual Payment Link tied to the specific pending invoice."""
    link = create_payment_link(
        amount=amount,
        currency="INR",
        reference_id=invoice_id,
        description=f"Your subscription payment for invoice {invoice_id} needs attention",
        expire_by=_expire_by_epoch(),
        customer={"name": customer_name, "contact": customer_contact},
    )
    return {"payment_link_id": link["id"], "short_url": link["short_url"]}


@mcp.tool()
def reverse_duplicate_capture(subscription_id: str, payment_id: str) -> dict:
    """Shared double-capture safety net (2026-09-03 correction) - checked by the
    webhook handler on every subscription-linked payment.captured. If this is
    a genuine second capture for the same cycle, issues a REAL refund (unlike
    Problem 3's deliberately-scoped-down inventory-race edge case, a duplicate
    charge is real customer financial harm, not a cosmetic one) and reports it
    so the caller can proactively notify the customer via the two-hop
    delegation to the Conversational NLP Agent."""
    check = record_capture_and_check_duplicate(subscription_id, payment_id)
    if not check["is_duplicate"]:
        return {"duplicate_detected": False}

    refund = get_client().payment.refund(check["second_payment_id"], {})
    return {
        "duplicate_detected": True,
        "first_payment_id": check["first_payment_id"],
        "refunded_payment_id": check["second_payment_id"],
        "refund_id": refund.get("id"),
    }


def _expire_by_epoch() -> int:
    import time

    return int(time.time()) + HARD_DECLINE_LINK_EXPIRY_SECONDS


if __name__ == "__main__":
    mcp.run()
