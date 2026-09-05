"""Webhook event derivation - one handler per Razorpay event type. Each does
any synchronous, non-agentic side effect (Problem 1's token upsert, Problem
2's telemetry recording, Problem 3's kill-switch, Problems 5/6's
duplicate-capture check) and/or publishes a derived event onto Kafka for the
agent mesh to react to.

Envelope shape and every event name below verified directly against
Razorpay's own webhook documentation (2026-09-03), not assumed - the
`contains` field (present on every webhook envelope) names which entity keys
are populated inside `payload`, used generically here (`payload[key]["entity"]`)
rather than hardcoding a literal key per event type.

Real, previously-undiscovered gap this closes: the original design assumed
Problem 5/6 (subscription decline classification) triggers off `payment.failed`
- verified this is wrong. A subscription charge failure fires
`subscription.pending` (NOT payment.failed), and that event carries only the
`subscription` entity, no payment entity at all - which turns out to be
exactly the `error_reason=None` case classify_decline's AFA-heuristic branch
already handled, so no logic change was needed there, only the trigger.
`subscription.activated`/`subscription.charged` (which DO carry a paired
payment entity) are used to keep our own `subscriptions` document populated
with the recurring amount, since nothing else in this codebase ever creates
one.
"""

from rzp_common.mongo_client import get_db

from kafka_producer import publish_event
from subscription_store import upsert_subscription
from vault_store import upsert_vault_token

from telemetry import record_downtime_event, record_payment_outcome
from watchdog import kill_switch as checkout_kill_switch
from rzp_common.subscription_lifecycle import kill_switch_subscription, record_capture_and_check_duplicate

EMI_METHODS = {"emi", "cardless_emi"}


def _entity(event: dict, key: str) -> dict:
    return event.get("payload", {}).get(key, {}).get("entity", {})


def handle_payment_captured(event: dict, razorpay_event_id: str) -> None:
    payment = _entity(event, "payment")
    order_id = payment.get("order_id")

    # Problem 1 (sync, no Kafka): the one path a token is ever written.
    token_id = payment.get("token_id")
    customer_id = payment.get("customer_id")
    card = payment.get("card")
    if token_id and customer_id and card:
        upsert_vault_token(customer_id, token_id, payment.get("method", "card"), {
            "last4": card.get("last4"), "network": card.get("network"),
            "card_type": card.get("type"), "issuer": card.get("issuer"),
        })

    # Problem 2 (sync, no Kafka): telemetry recording, not a decision.
    instrument_key = _route_instrument_key(payment)
    if instrument_key:
        record_payment_outcome(payment.get("method", ""), instrument_key, payment["id"], captured=True)

    # Problem 3 (sync, no Kafka): a normal payment completion clears the watchdog.
    if order_id:
        checkout_kill_switch(order_id)


def handle_order_paid(event: dict, razorpay_event_id: str) -> None:
    order = _entity(event, "order")
    if order.get("id"):
        checkout_kill_switch(order["id"])


def handle_payment_failed(event: dict, razorpay_event_id: str) -> None:
    payment = _entity(event, "payment")
    instrument_key = _route_instrument_key(payment)
    if instrument_key:
        record_payment_outcome(payment.get("method", ""), instrument_key, payment["id"],
                                captured=False, error_source=payment.get("error_source"))

    method = payment.get("method", "")
    problem_id = 4 if method in EMI_METHODS else 2
    extra: dict = {"instrument_key": instrument_key}
    if problem_id == 4:
        # Gap fix (2026-09-05, found live testing Problem 4): suggest_alternate_emi
        # needs to know which provider actually declined, or it can't avoid
        # re-suggesting the same one - same missing-field pattern as
        # instrument_key above. For card EMI ("emi"), card.issuer is the
        # documented issuing bank (verified against Razorpay's own webhook
        # payload docs). For "cardless_emi" (NBFC-financed), no field for
        # this is documented anywhere found - left None rather than guessed,
        # honestly logged as unresolved in Design_Spec_and_Decisions.md.
        extra["declined_provider"] = (payment.get("card") or {}).get("issuer") if method == "emi" else None
    publish_event("payment.failed", problem_id, {
        "order_id": payment.get("order_id"), "payment_id": payment.get("id"),
        "method": method, "amount": payment.get("amount"),
        "error_code": payment.get("error_code"), "error_reason": payment.get("error_reason"),
        "error_source": payment.get("error_source"), "customer_id": payment.get("customer_id"),
        **extra,
    }, event_id=razorpay_event_id)


def handle_payment_downtime(status: str):
    def _handler(event: dict, razorpay_event_id: str) -> None:
        downtime = _entity(event, "payment.downtime")
        instrument = downtime.get("instrument", {}) or {}
        instrument_key = (instrument.get("bank") or instrument.get("issuer")
                           or instrument.get("psp") or instrument.get("vpa_handle") or "unknown")
        record_downtime_event(downtime.get("method", ""), instrument_key, status, downtime.get("severity"))
    return _handler


def handle_subscription_pending(event: dict, razorpay_event_id: str) -> None:
    """No payment entity in this payload (verified) - error_reason is
    genuinely unavailable here, which is exactly the null case
    classify_decline's AFA-heuristic branch already exists to handle."""
    subscription = _entity(event, "subscription")
    sub_id = subscription.get("id")
    if not sub_id:
        return
    db = get_db()
    doc = db.subscriptions.find_one({"razorpay_subscription_id": sub_id}) or {}
    publish_event("subscription.pending", 5, {
        "subscription_id": sub_id, "error_reason": None, "amount": doc.get("amount", 0),
    }, event_id=razorpay_event_id)


def handle_subscription_halted(event: dict, razorpay_event_id: str) -> None:
    """Gap fix (2026-09-05): the published payload used to carry only
    subscription_id, but prob6_dunning_sequencer's start_sequence (the tool
    this event is documented to trigger) requires amount/customer_name/
    customer_contact to build a real payment link - none of those are on the
    subscription entity itself, and with nothing else in the payload the LLM
    had no real values to pass, only ones it could invent. Same DB-lookup
    pattern as watchdog_poller.handle_hard_decline's _customer_contact."""
    subscription = _entity(event, "subscription")
    sub_id = subscription.get("id")
    if not sub_id:
        return
    db = get_db()
    doc = db.subscriptions.find_one({"razorpay_subscription_id": sub_id}) or {}
    customer = db.customers.find_one({"razorpay_customer_id": doc.get("customer_id")}) or {}
    publish_event("subscription.halted", 6, {
        "subscription_id": sub_id, "amount": doc.get("amount", 0),
        "customer_name": customer.get("name"), "customer_contact": customer.get("phone"),
    }, event_id=razorpay_event_id)


def handle_subscription_charged(event: dict, razorpay_event_id: str) -> None:
    """Sync only, no Kafka - keeps our own subscriptions doc populated
    (customer_id/plan_id/amount) and runs the shared double-capture check, a
    kill-switch on any dunning/hard-decline sequence already in flight."""
    subscription = _entity(event, "subscription")
    payment = _entity(event, "payment")
    sub_id = subscription.get("id")
    if not sub_id:
        return
    upsert_subscription(sub_id, subscription.get("customer_id", ""), subscription.get("plan_id"),
                         subscription.get("status", "active"), amount=payment.get("amount"))
    if payment.get("id"):
        check = record_capture_and_check_duplicate(sub_id, payment["id"])
        if not check["is_duplicate"]:
            kill_switch_subscription(sub_id)


def handle_virtual_account_credited(event: dict, razorpay_event_id: str) -> None:
    va = _entity(event, "virtual_account")
    payment = _entity(event, "payment")
    invoice_id = (va.get("notes") or {}).get("invoice_id")
    if not invoice_id:
        return
    publish_event("virtual_account.credited", 9, {
        "invoice_id": invoice_id, "amount_received": payment.get("amount"),
        "utr": (payment.get("acquirer_data") or {}).get("bank_transaction_id")
               or (payment.get("acquirer_data") or {}).get("rrn"),
    }, event_id=razorpay_event_id)


def _route_instrument_key(payment: dict) -> str | None:
    method = payment.get("method")
    if method == "card":
        return (payment.get("card") or {}).get("issuer")
    if method == "netbanking":
        return payment.get("bank")
    if method == "upi":
        return payment.get("vpa")
    return None


HANDLERS = {
    "payment.captured": handle_payment_captured,
    "payment.failed": handle_payment_failed,
    "order.paid": handle_order_paid,
    "payment.downtime.started": handle_payment_downtime("started"),
    "payment.downtime.updated": handle_payment_downtime("updated"),
    "payment.downtime.resolved": handle_payment_downtime("resolved"),
    "subscription.pending": handle_subscription_pending,
    "subscription.halted": handle_subscription_halted,
    "subscription.charged": handle_subscription_charged,
    "subscription.activated": handle_subscription_charged,
    "virtual_account.credited": handle_virtual_account_credited,
}


def dispatch(event: dict, razorpay_event_id: str) -> None:
    handler = HANDLERS.get(event.get("event", ""))
    if handler is not None:
        handler(event, razorpay_event_id)
