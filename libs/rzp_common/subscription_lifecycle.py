"""Shared subscription-lifecycle helpers for Problems 5 and 6.

Design reference: Design_Spec_and_Decisions.md, section 11, Problems 5/6.
Plain functions, not MCP tools — scheduling and the kill-switch are
synchronous, non-agentic side-effects of webhook processing (same pattern
established in Problem 3's watchdog.py), shared here because Problems 5 and 6
both act on the same `subscriptions` document and the same `watchdog_queue`,
even though they're built as two separate FastMCP servers.
"""

import time
from datetime import datetime, timezone

from rzp_common.mongo_client import get_db
from rzp_common.redis_client import get_redis

HARD_DECLINE_DELAY_SECONDS = 5 * 3600          # T+5h, per Problem 5's real-world grounding
DUNNING_TOUCH_OFFSETS_SECONDS = [0, 4 * 86400, 9 * 86400, 11 * 86400]  # touch1/2/3/downgrade


def schedule_hard_decline_link(subscription_id: str) -> None:
    """Problem 5: schedule the deliberately-delayed hard-decline link send."""
    r = get_redis()
    r.zadd("watchdog_queue", {f"{subscription_id}:hard_decline": time.time() + HARD_DECLINE_DELAY_SECONDS})
    db = get_db()
    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {"hard_decline_link_scheduled_at": datetime.now(timezone.utc)}},
    )


def schedule_dunning_sequence(subscription_id: str, dunning_link_id: str,
                               dunning_link_short_url: str) -> None:
    """Problem 6: schedule all four checkpoints upfront (touch1 fires immediately,
    per the design's finding that halted is already the culmination of several
    days, so an instant first touch doesn't read as robotic). Stores the link's
    short_url alongside its entity ID (gap fix, 2026-09-03) so evaluate_next_touch
    can build each touch's WhatsApp delegation artifact without a second Razorpay
    lookup — the ID alone was never enough to construct the message."""
    r = get_redis()
    now = time.time()
    mapping = {
        f"{subscription_id}:touch1": now + DUNNING_TOUCH_OFFSETS_SECONDS[0],
        f"{subscription_id}:touch2": now + DUNNING_TOUCH_OFFSETS_SECONDS[1],
        f"{subscription_id}:touch3": now + DUNNING_TOUCH_OFFSETS_SECONDS[2],
        f"{subscription_id}:downgrade": now + DUNNING_TOUCH_OFFSETS_SECONDS[3],
    }
    r.zadd("watchdog_queue", mapping)
    db = get_db()
    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {
            "dunning_sequence_stage": 0,
            "dunning_link_id": dunning_link_id,
            "dunning_link_short_url": dunning_link_short_url,
            "dunning_started_at": datetime.now(timezone.utc),
        }},
    )


def kill_switch_subscription(subscription_id: str) -> None:
    """Called synchronously by the webhook handler the instant a real payment
    resolves this subscription's cycle (manual link paid, or - defensively -
    Razorpay's own retry succeeding). Clears every remaining checkpoint from
    both Problems 5 and 6, since only one of them will have been active for
    a given cycle but the ZREM is harmless either way."""
    r = get_redis()
    r.zrem(
        "watchdog_queue",
        f"{subscription_id}:hard_decline",
        f"{subscription_id}:touch1",
        f"{subscription_id}:touch2",
        f"{subscription_id}:touch3",
        f"{subscription_id}:downgrade",
    )
    db = get_db()
    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {"terminal_action_at": datetime.now(timezone.utc)}},
    )


def record_capture_and_check_duplicate(subscription_id: str, payment_id: str) -> dict:
    """Shared double-capture detection (2026-09-03 correction) — checked on
    EVERY subscription-linked payment.captured, covering both Problem 5's
    AFA-heuristic subset (a resolvable-in-place hurdle the customer could clear
    independently of our link) and Problem 6's soft-decline path (subscription.
    halted isn't independently confirmed to foreclose every future automated
    attempt). Returns whether this is the first capture for the cycle or a
    genuine duplicate needing a refund.
    """
    db = get_db()
    result = db.subscriptions.find_one_and_update(
        {"razorpay_subscription_id": subscription_id},
        {"$push": {"current_cycle_payment_ids": payment_id}},
        return_document=True,
    )
    payment_ids = (result or {}).get("current_cycle_payment_ids", [])
    if len(payment_ids) <= 1:
        return {"is_duplicate": False, "first_payment_id": payment_id}
    return {"is_duplicate": True, "first_payment_id": payment_ids[0], "second_payment_id": payment_id}


def reset_cycle_payment_ids(subscription_id: str) -> None:
    """Called when a new billing cycle starts, so the duplicate check above
    only ever compares captures within the SAME cycle, not across cycles."""
    db = get_db()
    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {"current_cycle_payment_ids": []}},
    )
