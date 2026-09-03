"""Problem 1 vault persistence — the Mongo-side half of the vault, shared by
the API layer (api/vault.py) and the webhook handler's synchronous token-save
side effect (event_derivation.py's payment.captured handling).

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 1. Our
own MongoDB is the source of truth for "what can this customer pay with" —
never a live Razorpay list-tokens call, per the design's own decision (works
around the unconfirmed "list all tokens" endpoint gap by never needing it).
"""

from datetime import datetime, timezone

from rzp_common.mongo_client import get_db


def upsert_vault_token(razorpay_customer_id: str, token_id: str, method: str, masked: dict) -> None:
    """The one path a token is ever written (Flow A) — a synchronous side
    effect of the shared webhook handler on payment.captured, never a direct
    API call, to keep PCI scope near-zero. Idempotent, keyed by token_id: a
    webhook redelivery for the same token just no-ops rather than duplicating
    the array entry."""
    db = get_db()
    now = datetime.now(timezone.utc)

    existing = db.customers.find_one(
        {"razorpay_customer_id": razorpay_customer_id, "vault_tokens.token_id": token_id}
    )
    if existing:
        return  # already recorded — a webhook redelivery, not a new token

    db.customers.update_one(
        {"razorpay_customer_id": razorpay_customer_id},
        {"$push": {"vault_tokens": {
            "token_id": token_id, "method": method, "masked": masked,
            "status": "active", "created_at": now,
            "last_used_at": None, "last_used_payment_id": None,
        }}},
    )


def record_token_usage(razorpay_customer_id: str, token_id: str, payment_id: str) -> None:
    """Flow C's success side effect — update the token's last-used markers
    after a saved-token retry succeeds."""
    db = get_db()
    db.customers.update_one(
        {"razorpay_customer_id": razorpay_customer_id, "vault_tokens.token_id": token_id},
        {"$set": {
            "vault_tokens.$.last_used_at": datetime.now(timezone.utc),
            "vault_tokens.$.last_used_payment_id": payment_id,
        }},
    )


def get_masked_methods(razorpay_customer_id: str) -> dict:
    """Flow B — zero Razorpay API calls at request time, per the design."""
    db = get_db()
    customer = db.customers.find_one({"razorpay_customer_id": razorpay_customer_id})
    if not customer:
        return {"vault_tokens": [], "saved_vpas": []}
    return {
        "vault_tokens": [
            {"token_id": t["token_id"], "method": t["method"], "masked": t["masked"], "status": t["status"]}
            for t in customer.get("vault_tokens", [])
        ],
        "saved_vpas": customer.get("saved_vpas", []),
    }


def remove_vault_token(razorpay_customer_id: str, token_id: str) -> bool:
    """Flow D's local-side step — MUST only be called after Razorpay's own
    POST /tokens/delete has already succeeded (the caller's responsibility),
    never delete locally-only, which would orphan a still-live token on
    Razorpay's side."""
    db = get_db()
    result = db.customers.update_one(
        {"razorpay_customer_id": razorpay_customer_id},
        {"$pull": {"vault_tokens": {"token_id": token_id}}},
    )
    return result.modified_count > 0
