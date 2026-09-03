"""Checkout S2S API — the ONLY caller of Problem 3's schedule_watchdog.

Real-world grounding correction (2026-09-03, verified against Razorpay's own
webhook docs): "order.created" is not a Razorpay webhook event at all - only
order.paid is. schedule_watchdog must therefore be called synchronously from
our own order-creation code, not a webhook listener, which is exactly what
watchdog.py's own docstring already said before this endpoint existed to
actually call it.

customer_id is optional (a guest checkout may not have one - the vault's
"identify" flow is opt-in, not mandatory to check out at all), but
customer_name/customer_contact are required regardless of registration
status: they're what a recovery link is actually sent to, and nothing else
in this system can recover them later for a guest.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from rzp_razorpay_client.client import create_order

from watchdog import schedule_watchdog

router = APIRouter()


class CreateOrderRequest(BaseModel):
    amount: int
    currency: str = "INR"
    customer_id: str | None = None
    customer_name: str
    customer_contact: str
    receipt: str


@router.post("/api/checkout/orders")
def create_checkout_order(body: CreateOrderRequest) -> dict:
    notes = {"customer_id": body.customer_id} if body.customer_id else {}
    order = create_order(body.amount, body.currency, body.receipt, notes=notes)
    schedule_watchdog(order["id"], body.customer_id, body.amount, body.customer_name, body.customer_contact)
    return {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]}
