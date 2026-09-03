"""Watchdog poller - drains the shared `watchdog_queue` Redis sorted set that
Problems 3, 5, 6, 7, and 9 all schedule checkpoints into, and publishes the
Kafka event that routes each due checkpoint to its owning agent for reactive
diagnosis.

Gap fix (2026-09-03): every one of those problems' scheduling side was real
(schedule_watchdog, schedule_hard_decline_link, schedule_dunning_sequence,
the PTP due-date zadd, schedule_escalation_checkpoints all write real entries
into watchdog_queue) but nothing ever drained it - pop_due_checkpoints existed
in prob3_otp_watch/watchdog.py from the start (it operates on the shared key,
not a prob3-only one) but had no caller. This is that caller.

Checkpoint key format is "{entity_id}:{suffix}", the same convention every
scheduler above already uses; suffix -> (problem_id, event_type) is a plain
dispatch table below, with a regex fallback for Problem 9's parameterized
"t_plus_N"/"t_minus_N" suffixes.

Run:
    uv run python services/watchdog_poller/main.py
"""

import asyncio
import sys
import time
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_CODES_ROOT / "libs"))
sys.path.insert(0, str(_CODES_ROOT / "services" / "backend"))
sys.path.insert(0, str(_CODES_ROOT / "services" / "mcp-servers" / "prob3_otp_watch"))
sys.path.insert(0, str(_CODES_ROOT / "services" / "mcp-servers" / "prob9_recon"))

from rzp_common.mongo_client import get_db  # noqa: E402

from kafka_producer import publish_event  # noqa: E402
from watchdog import pop_due_checkpoints  # noqa: E402
from b2b_watchdog import mark_escalation_stage_completed, parse_checkpoint_day_offset  # noqa: E402

POLL_INTERVAL_SECONDS = 5
DUNNING_STAGE_BY_SUFFIX = {"touch1": 1, "touch2": 2, "touch3": 3}


def _customer_contact(customer_id: str | None) -> tuple[str | None, str | None]:
    """Looks up (name, phone) for a subscription/invoice-linked customer_id.
    Returns (None, None) rather than raising if the customer record is
    missing - the receiving agent's own tools already handle absent contact
    details defensively, same as every other lookup in this codebase."""
    if not customer_id:
        return None, None
    db = get_db()
    customer = db.customers.find_one({"razorpay_customer_id": customer_id})
    if not customer:
        return None, None
    return customer.get("name"), customer.get("phone")


def handle_checkout_stage(order_id: str, stage: str) -> None:
    db = get_db()
    session = db.checkout_sessions.find_one({"_id": order_id})
    if not session or session.get("stage") in ("recovered", "recovered_link_sent"):
        return  # already resolved (webhook kill-switch beat the poller here) - nothing to do
    publish_event(f"checkout.watchdog_{stage}_due", 3, {
        "order_id": order_id, "customer_id": session.get("customer_id"),
        "customer_name": session.get("customer_name"), "customer_contact": session.get("customer_contact"),
        "amount": session.get("amount"), "stage": stage,
    })


def handle_hard_decline(subscription_id: str) -> None:
    db = get_db()
    subscription = db.subscriptions.find_one({"razorpay_subscription_id": subscription_id})
    if not subscription or subscription.get("status") == "cancelled":
        return
    name, phone = _customer_contact(subscription.get("customer_id"))
    publish_event("subscription.hard_decline_due", 5, {
        "subscription_id": subscription_id,
        # No real Razorpay invoice is tracked for a subscription cycle in this
        # schema - subscription_id doubles as generate_hard_decline_link's own
        # reference_id, which only needs to be a stable string WE choose.
        "invoice_id": subscription_id,
        "amount": subscription.get("amount", 0), "customer_name": name, "customer_contact": phone,
    })


def handle_dunning_touch(subscription_id: str, stage: int) -> None:
    db = get_db()
    subscription = db.subscriptions.find_one({"razorpay_subscription_id": subscription_id})
    if not subscription or not subscription.get("dunning_link_id"):
        return
    publish_event("subscription.dunning_touch_due", 6, {"subscription_id": subscription_id, "stage": stage})


def handle_dunning_downgrade(subscription_id: str) -> None:
    publish_event("subscription.dunning_downgrade_due", 6, {"subscription_id": subscription_id})


def handle_ptp_due(subscription_id: str) -> None:
    publish_event("subscription.ptp_due", 7, {"subscription_id": subscription_id})


def handle_b2b_escalation(invoice_id: str, day_offset: int) -> None:
    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    if not invoice or invoice.get("status") in ("recovered", "disputed"):
        return  # kill_switch_invoice already cleared remaining checkpoints in the common case;
        # this is a defensive second check for the same race pop_due_checkpoints can hit.
    mark_escalation_stage_completed(invoice_id, day_offset)
    publish_event("invoice.escalation_checkpoint_due", 9, {"invoice_id": invoice_id, "day_offset": day_offset})


def handle_checkpoint(checkpoint: str) -> None:
    entity_id, _, suffix = checkpoint.rpartition(":")
    if not entity_id:
        print(f"[warn] malformed checkpoint, skipping: {checkpoint!r}")
        return

    if suffix in ("stage1", "stage2"):
        handle_checkout_stage(entity_id, suffix)
    elif suffix == "hard_decline":
        handle_hard_decline(entity_id)
    elif suffix in DUNNING_STAGE_BY_SUFFIX:
        handle_dunning_touch(entity_id, DUNNING_STAGE_BY_SUFFIX[suffix])
    elif suffix == "downgrade":
        handle_dunning_downgrade(entity_id)
    elif suffix == "ptp_due":
        handle_ptp_due(entity_id)
    else:
        day_offset = parse_checkpoint_day_offset(suffix)
        if day_offset is not None:
            handle_b2b_escalation(entity_id, day_offset)
        else:
            print(f"[warn] unrecognized checkpoint suffix {suffix!r} on {checkpoint!r}")


async def poll_loop() -> None:
    print(f"Watchdog poller running, polling every {POLL_INTERVAL_SECONDS}s ...")
    while True:
        due = pop_due_checkpoints(time.time())
        for checkpoint in due:
            try:
                handle_checkpoint(checkpoint)
                print(f"[fired] {checkpoint}")
            except Exception as exc:  # noqa: BLE001 - one bad checkpoint must not stop the whole poller
                print(f"[error] checkpoint {checkpoint!r} failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(poll_loop())
