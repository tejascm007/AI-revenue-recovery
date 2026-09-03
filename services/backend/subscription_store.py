"""Problem 5/6 subscription persistence — the sync half of the webhook
handler that populates the `subscriptions` collection every one of their
MCP tools assumes already exists.

Gap fix (2026-09-03): nothing in Problems 5/6/7/9's code ever created a
subscriptions document - classify_decline, generate_hard_decline_link,
evaluate_next_touch, and lookup_active_ptp all read one but nothing wrote
one. The webhook handler is the correct place: subscription.activated and
subscription.charged both carry a paired payment entity giving us the actual
per-cycle amount (needed for the AFA-threshold heuristic), which the
subscription entity itself never carries.
"""

from datetime import datetime, timezone

from rzp_common.mongo_client import get_db


def upsert_subscription(razorpay_subscription_id: str, customer_id: str, plan_id: str | None,
                         status: str, amount: int | None = None) -> None:
    db = get_db()
    now = datetime.now(timezone.utc)
    update: dict = {"$set": {"status": status, "updated_at": now},
                     "$setOnInsert": {"razorpay_subscription_id": razorpay_subscription_id,
                                       "customer_id": customer_id, "created_at": now}}
    if plan_id:
        update["$set"]["plan_id"] = plan_id
    if amount is not None:
        update["$set"]["amount"] = amount
    db.subscriptions.update_one({"razorpay_subscription_id": razorpay_subscription_id}, update, upsert=True)
