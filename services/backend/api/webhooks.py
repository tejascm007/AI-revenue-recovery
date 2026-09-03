"""The shared Razorpay webhook endpoint - Design_Spec_and_Decisions.md,
section 3's event lifecycle: verify signature -> raw dump -> derived
side-effects/Kafka publish, in that order, matching every problem's own
docstrings ("the shared webhook handler, services/backend").

Returns 200 even when signature verification fails or a handler raises -
Razorpay retries on non-2xx with exponential backoff for up to 24h, and a
malformed/replayed event should be recorded (with processing_error set) and
dropped, not hammered by retries that will never succeed differently.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request

from rzp_common.mongo_client import get_db
from rzp_razorpay_client.client import verify_webhook_signature

from event_derivation import dispatch

router = APIRouter()


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: str | None = Header(default=None)) -> dict:
    raw_body = await request.body()
    db = get_db()
    now = datetime.now(timezone.utc)

    try:
        event = await request.json()
    except ValueError:
        db.raw_webhook_events.insert_one({
            "razorpay_event_id": None, "event_type": "unparseable",
            "payload": None, "signature_valid": None, "received_at": now,
            "processed": False, "processing_error": "invalid JSON body",
        })
        return {"status": "ignored"}

    event_type = event.get("event", "unknown")
    razorpay_event_id = event.get("payload", {}).get("payment", {}).get("entity", {}).get("id") or event.get("created_at")
    razorpay_event_id = str(razorpay_event_id) if razorpay_event_id is not None else None

    signature_valid = None
    if x_razorpay_signature:
        try:
            signature_valid = verify_webhook_signature(raw_body, x_razorpay_signature)
        except RuntimeError:
            signature_valid = None  # RAZORPAY_WEBHOOK_SECRET not configured in this environment

    doc = {
        "razorpay_event_id": razorpay_event_id, "event_type": event_type, "payload": event,
        "signature_valid": signature_valid, "received_at": now, "processed": False, "processing_error": None,
    }
    result = db.raw_webhook_events.insert_one(doc)

    if signature_valid is False:
        db.raw_webhook_events.update_one(
            {"_id": result.inserted_id}, {"$set": {"processing_error": "signature verification failed"}}
        )
        return {"status": "rejected"}

    try:
        dispatch(event, razorpay_event_id or str(result.inserted_id))
        db.raw_webhook_events.update_one({"_id": result.inserted_id}, {"$set": {"processed": True}})
    except Exception as exc:  # noqa: BLE001 - a bad payload/handler bug must not become a Razorpay retry storm
        db.raw_webhook_events.update_one(
            {"_id": result.inserted_id},
            {"$set": {"processed": False, "processing_error": f"{type(exc).__name__}: {exc}"}},
        )

    return {"status": "ok"}
