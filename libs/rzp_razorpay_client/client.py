"""Thin wrapper around Razorpay's official Python SDK.

Design reference: Design_Spec_and_Decisions.md section 3 ("Razorpay's own
webhook retries use exponential backoff...") and the cross-cutting research
findings: no published rate limits for standard Payments/Orders APIs, and no
idempotency-key mechanism for Orders/Payments create calls (dedupe is the
CALLER's job via receipt/reference_id + Redis, not this client's).

Correction made while actually wiring this up (not just designed on paper):
the official SDK exposes no HTTP status code on its exceptions at all — it
raises BadRequestError/GatewayError/ServerError based on Razorpay's own
business error `code` field, not the HTTP status, so a 429 specifically
cannot be distinguished from any other server-side issue. See with_backoff
below for how retry scope was corrected once this was discovered.

Test vs live mode is just a different key pair via the same env vars — no
code branching needed, matching the confirmed API research.
"""

import hashlib
import hmac
import os
import random
import time
from functools import lru_cache, wraps
from typing import Any, Callable

import razorpay
from razorpay.errors import GatewayError, ServerError

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 0.5


@lru_cache(maxsize=1)
def get_client() -> razorpay.Client:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
            "Razorpay test-mode account/API keys are still an open item "
            "(Design_Spec_and_Decisions.md section 10) - set these env vars "
            "once a test account exists."
        )
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


# Correction, made while wiring this up: the SDK does NOT expose an HTTP status
# code on its exceptions (verified by reading razorpay/client.py directly) — it
# raises BadRequestError / GatewayError / ServerError based on Razorpay's own
# business error `code` field in the response body, not the HTTP status. There
# is no way to specifically detect "this was a 429" from these exception types.
# The SDK also already retries connection-level errors (ConnectionError, Timeout)
# internally with its own backoff+jitter — this wrapper only needs to add a
# second layer for the business-level errors the SDK does NOT retry on its own:
# GatewayError and ServerError represent transient bank/gateway/Razorpay-side
# issues worth retrying; BadRequestError is Razorpay telling us the request
# itself was wrong, which retrying cannot fix, so it is deliberately NOT caught
# here and propagates immediately.
RETRYABLE_ERRORS = (GatewayError, ServerError)


def with_backoff(func: Callable) -> Callable:
    """Exponential backoff with jitter for business-level transient errors
    (GatewayError/ServerError). No numeric rate limit is published for these
    APIs, so this is a generic, conservative retry, not tuned against a
    documented threshold — see the module-level note on RETRYABLE_ERRORS for
    why BadRequestError is excluded."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except RETRYABLE_ERRORS as exc:
                last_exc = exc
                delay = BASE_DELAY_SECONDS * (2**attempt) + random.uniform(0, 0.25)
                time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    return wrapper


@with_backoff
def fetch_order_payments(order_id: str) -> dict:
    """GET /v1/orders/{id}/payments — the ground-truth reconciliation call used
    by Problem 3's watchdog (and Problem 7's PTP-due check) to verify whether an
    order was actually paid, independent of whether a webhook arrived."""
    return get_client().order.payments(order_id)


@with_backoff
def create_payment_link(amount: int, currency: str, reference_id: str,
                         description: str, expire_by: int,
                         customer: dict | None = None,
                         method_preference: str | None = None) -> dict:
    """POST /v1/payment_links/ — used by Problems 3/5/6/9 for recovery links.

    method_preference is informational (e.g. "upi") for the caller's own
    template selection — Razorpay's Payment Links don't take a method-priority
    parameter, the customer still sees all enabled methods on the link.
    """
    payload: dict[str, Any] = {
        "amount": amount,
        "currency": currency,
        "reference_id": reference_id,
        "description": description,
        "expire_by": expire_by,
        "reminder_enable": True,
    }
    if customer:
        payload["customer"] = customer
        payload["notify"] = {"sms": False, "email": False}  # WhatsApp send is our own path
    return get_client().payment_link.create(payload)


@with_backoff
def cancel_payment_link(payment_link_id: str) -> dict:
    """POST /v1/payment_links/{id}/cancel — safe against an already-paid link
    per the confirmed research: cancelling one that's already paid errors
    cleanly rather than double-processing. Callers should catch and swallow
    that specific case, not treat it as a failure."""
    return get_client().payment_link.cancel(payment_link_id)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 over the RAW request body, per Razorpay's documented scheme.
    Must be called with the body exactly as received — parsing to JSON first
    and re-serializing will not reproduce the same bytes and will always fail.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET is not set.")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
