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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from razorpay.errors import BadRequestError, GatewayError, ServerError

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
    # Real gap found live (2026-09-05): a genuine Razorpay-side rejection
    # (e.g. "Amount exceeds maximum amount allowed", hit for real while
    # wiring up the actual storefront - large carts can plausibly cross
    # Razorpay's own per-order ceiling) used to bubble up as an unhandled
    # exception -> FastAPI's generic 500 with no detail, leaving the
    # storefront's own error UI with nothing useful to show the customer.
    notes = {"customer_id": body.customer_id} if body.customer_id else {}
    try:
        order = create_order(body.amount, body.currency, body.receipt, notes=notes)
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GatewayError, ServerError) as exc:
        raise HTTPException(status_code=502, detail=f"Razorpay is temporarily unavailable: {exc}") from exc
    schedule_watchdog(order["id"], body.customer_id, body.amount, body.customer_name, body.customer_contact)
    return {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]}
