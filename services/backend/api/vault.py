"""Problem 1 API surface — Design_Spec_and_Decisions.md, section 11, Problem 1.

Deliberately no save endpoint: a token is never created via a direct backend
call with card data — always a side effect of a real checkout flow (see
event_derivation.py's payment.captured handling), keeping PCI scope near-zero.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from rzp_agent_kit.audit import write_audit_log
from rzp_common.mongo_client import get_db
from rzp_razorpay_client.client import delete_token, identify_customer

from vault_store import get_masked_methods, remove_vault_token

router = APIRouter()


class IdentifyRequest(BaseModel):
    name: str
    contact: str
    email: str | None = None


@router.post("/api/customers/identify")
def identify(body: IdentifyRequest) -> dict:
    """Fetch-or-create Razorpay customer_id (fail_existing:"0" for
    idempotency), upsert our own local doc."""
    rzp_customer = identify_customer(body.name, body.email, body.contact)
    db = get_db()
    now = datetime.now(timezone.utc)
    db.customers.update_one(
        {"razorpay_customer_id": rzp_customer["id"]},
        {
            "$setOnInsert": {
                "razorpay_customer_id": rzp_customer["id"], "phone": body.contact,
                "email": body.email, "name": body.name,
                "vault_tokens": [], "saved_vpas": [], "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
    )
    return {"customer_id": rzp_customer["id"]}


@router.get("/api/vault/{customer_id}/methods")
def get_methods(customer_id: str) -> dict:
    """Zero Razorpay API calls at request time — reads our own Mongo doc only."""
    return get_masked_methods(customer_id)


@router.delete("/api/vault/{customer_id}/methods/{token_id}")
def delete_method(customer_id: str, token_id: str) -> dict:
    """Calls Razorpay's own delete first — only removes/revokes locally on
    success, never delete locally-only (would orphan a still-live token)."""
    try:
        delete_token(customer_id, token_id)
    except Exception as exc:  # noqa: BLE001 - any Razorpay-side failure must block the local delete
        raise HTTPException(status_code=502, detail=f"Razorpay token delete failed: {exc}") from exc

    removed = remove_vault_token(customer_id, token_id)
    if not removed:
        raise HTTPException(status_code=404, detail="token not found in vault")

    write_audit_log(
        problem_id=1, tool_name="delete_vault_method",
        entity_refs={"customer_id": customer_id, "token_id": token_id},
        observation={}, decision={"action": "DELETE_TOKEN"}, execution={"status": "deleted"},
    )
    return {"status": "deleted", "token_id": token_id}
