"""Problem 3 inventory soft-lock — the exact-millisecond race (Scenario 3) and
the live re-check (Scenario 2), gap-fixed 2026-09-03.

The "10-minute cart soft-lock" from the design doesn't need a separate Redis
lock: a single atomic `findOneAndUpdate` in MongoDB (available_stock: {$gt: 0},
$inc: -1) IS the soft-lock — only one of two simultaneous requests can
successfully decrement 1 -> 0, the other gets no match. Releasing an unused
reservation is scheduled the same way as everything else in this problem: a
watchdog_queue entry checked 10 minutes later.
"""

from datetime import datetime, timezone

from rzp_common.mongo_client import get_db


def check_stock(sku_id: str) -> bool:
    """Read-only check — used before sending a recovery link (Scenario 1) and
    on the landing page before initializing the Checkout SDK (Scenario 2)."""
    db = get_db()
    doc = db.inventory.find_one({"_id": sku_id}, {"available_stock": 1})
    return bool(doc and doc.get("available_stock", 0) > 0)


def reserve_stock(sku_id: str) -> bool:
    """Atomic decrement — the actual soft-lock. Returns False if sold out."""
    db = get_db()
    result = db.inventory.find_one_and_update(
        {"_id": sku_id, "available_stock": {"$gt": 0}},
        {"$inc": {"available_stock": -1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    return result is not None


def release_stock(sku_id: str) -> None:
    """Increment back — called when a reserved unit's checkout session times out
    unpaid (the watchdog-scheduled release), or when a genuine race-condition
    over-accept needs correcting. Deliberately NOT called for a confirmed sale."""
    db = get_db()
    db.inventory.update_one(
        {"_id": sku_id},
        {"$inc": {"available_stock": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
