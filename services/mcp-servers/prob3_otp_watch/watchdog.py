"""Problem 3 watchdog scheduling and kill-switch — Flows A and B from the design.

Plain functions, NOT MCP tools. Scheduling happens synchronously in the
checkout API (services/backend/api/checkout.py) the moment WE create the
order; the kill-switch runs synchronously in the shared webhook handler the
moment a real payment.captured/order.paid arrives. Neither involves an LLM
decision — only the reactive diagnosis once a checkpoint actually fires
(server.py) goes through the agent.
"""

import time
from datetime import datetime, timezone

from rzp_common.mongo_client import get_db
from rzp_common.redis_client import get_redis

STAGE1_DELAY_SECONDS = 120   # T+2min: ground-truth verification only, no message
STAGE2_DELAY_SECONDS = 900   # T+15min total: re-verify, then the one allowed message


def schedule_watchdog(order_id: str, customer_id: str | None, amount: int,
                       customer_name: str | None = None, customer_contact: str | None = None) -> None:
    """Flow A: called the moment our backend creates the order. Synchronous,
    non-agentic. customer_id is nullable (a guest checkout may not have one),
    but customer_name/customer_contact should be supplied whenever available -
    they're what generate_recovery_link actually needs to reach the customer,
    and re-deriving them from customer_id at watchdog-fire time wouldn't work
    for a guest checkout anyway (gap fix, 2026-09-03)."""
    db = get_db()
    db.checkout_sessions.update_one(
        {"_id": order_id},
        {"$set": {
            "customer_id": customer_id,
            "customer_name": customer_name,
            "customer_contact": customer_contact,
            "stage": "created",
            "amount": amount,
            "emi_suggestion_count": 0,
            "emi_declined_providers": [],
            "created_at": datetime.now(timezone.utc),
            "resolved_at": None,
        }},
        upsert=True,
    )
    r = get_redis()
    r.zadd("watchdog_queue", {f"{order_id}:stage1": time.time() + STAGE1_DELAY_SECONDS})


def schedule_stage2(order_id: str) -> None:
    """Called by the reactive diagnosis handler when stage1 fires and the order
    is still unpaid — schedules the second, message-sending checkpoint."""
    r = get_redis()
    r.zadd("watchdog_queue", {f"{order_id}:stage2": time.time() + STAGE2_DELAY_SECONDS})


def kill_switch(order_id: str, already_claimed_by_watchdog: bool = False) -> None:
    """Flow B: called synchronously by the webhook handler the instant a real
    payment.captured/order.paid webhook arrives for this order. Removes any
    pending watchdog checkpoints and marks the session resolved.

    already_claimed_by_watchdog guards the idempotency gap identified
    2026-09-03: if the watchdog's own ghost-debit claim already resolved this
    order, the webhook arriving moments later must update records silently
    without re-sending a reassurance message.
    """
    r = get_redis()
    r.zrem("watchdog_queue", f"{order_id}:stage1", f"{order_id}:stage2")

    db = get_db()
    session = db.checkout_sessions.find_one({"_id": order_id})
    if session and session.get("stage") in ("recovered", "recovered_link_sent"):
        return  # already resolved by the watchdog's own claim — don't re-notify

    db.checkout_sessions.update_one(
        {"_id": order_id},
        {"$set": {"stage": "recovered", "resolved_at": datetime.now(timezone.utc)}},
    )


def claim_ghost_debit(order_id: str, payment_id: str) -> None:
    """Called by the reactive diagnosis tool (server.py) when verify_order_payment
    finds a captured payment the client never got confirmation for. Marks the
    session resolved and clears any remaining checkpoints, same as kill_switch,
    but distinctly named since this path is agent-initiated (a real decision,
    logged to audit_logs by the caller) rather than a passive webhook side-effect.
    """
    r = get_redis()
    r.zrem("watchdog_queue", f"{order_id}:stage1", f"{order_id}:stage2")

    db = get_db()
    db.checkout_sessions.update_one(
        {"_id": order_id},
        {"$set": {
            "stage": "recovered",
            "resolved_at": datetime.now(timezone.utc),
            "claimed_payment_id": payment_id,
        }},
    )


def pop_due_checkpoints(now: float | None = None) -> list[str]:
    """Atomically claim every watchdog entry due by `now` (defaults to current
    time), removing them from the queue so no two poller instances double-process
    the same checkpoint. Returns the raw "{order_id}:{stage}" strings.
    """
    r = get_redis()
    now = now if now is not None else time.time()
    due = r.zrangebyscore("watchdog_queue", 0, now)
    if due:
        r.zrem("watchdog_queue", *due)
    return due
