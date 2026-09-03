"""Problem 9 escalation scheduling and kill-switch.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 9.
Reuses the same watchdog_queue pattern as Problems 3/5/6 rather than a
separate daily-sweep job — at invoice creation, ALL escalation checkpoints
(T-3/T+1/T+7/T+14/T+30/T+45/T+60, from merchant_config.b2b_escalation_schedule_days)
are scheduled upfront, anchored to the invoice's due_date, not its creation date.
"""

import time
from datetime import datetime, timedelta, timezone

from rzp_common.mongo_client import get_db
from rzp_common.redis_client import get_redis


def _checkpoint_key(invoice_id: str, day_offset: int) -> str:
    sign = "minus" if day_offset < 0 else "plus"
    return f"{invoice_id}:t_{sign}_{abs(day_offset)}"


def schedule_escalation_checkpoints(invoice_id: str, due_date: datetime) -> None:
    db = get_db()
    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    schedule_days = config.get("b2b_escalation_schedule_days", [-3, 1, 7, 14, 30, 45, 60])

    r = get_redis()
    mapping = {}
    for offset in schedule_days:
        checkpoint_time = due_date + timedelta(days=offset)
        mapping[_checkpoint_key(invoice_id, offset)] = checkpoint_time.timestamp()
    r.zadd("watchdog_queue", mapping)

    db.invoices.update_one(
        {"razorpay_invoice_id": invoice_id},
        {"$set": {"escalation_stage_completed": []}},
    )


def kill_switch_invoice(invoice_id: str) -> None:
    """Called on MARK_AS_RECOVERED or PAUSE_FOR_DISPUTE — clears every
    remaining escalation checkpoint for this invoice, same idempotency
    principle as every other kill-switch in this project."""
    db = get_db()
    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    schedule_days = config.get("b2b_escalation_schedule_days", [-3, 1, 7, 14, 30, 45, 60])
    keys = [_checkpoint_key(invoice_id, offset) for offset in schedule_days]

    r = get_redis()
    r.zrem("watchdog_queue", *keys)
