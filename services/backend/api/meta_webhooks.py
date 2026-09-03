"""The Meta WhatsApp Cloud API webhook — Design_Spec_and_Decisions.md,
section 11, Problem 7's Flow A: "Customer replies on WhatsApp -> Meta webhook
-> our WhatsApp webhook endpoint -> verify signature -> raw dump (Mongo) ->
Kafka publish {event:"whatsapp.inbound"} -> Orchestrator -> A2A ->
Conversational NLP Agent". A distinct integration from Razorpay's webhooks
(different provider, different verification scheme, different envelope
shape) - deliberately its own endpoint/router, not folded into webhooks.py.

Payload shape and the verification handshake both confirmed directly against
Meta's own developer docs (2026-09-03), not assumed:
entry[].changes[].value.{contacts[],messages[]} for inbound messages; a
one-time GET handshake with hub.mode/hub.verify_token/hub.challenge that
must echo hub.challenge back as plain text, separate from the ongoing POST
deliveries' X-Hub-Signature-256 verification.

Real, current (2026) finding worth flagging: as of a March 2026 WhatsApp
Cloud API change, a contact's phone number (messages[].from, contacts[].wa_id)
can be OMITTED once WhatsApp usernames are in play, replaced by a
Meta-scoped user_id (BSUID). This project's entire identity model is
phone-number-keyed (customers.phone unique index) with no BSUID-based lookup
built anywhere - a message missing `from` is recorded (raw dump always
happens) but cannot be resolved to a customer_id here. Not fixed in this
pass; flagged honestly rather than silently mis-handled.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query, Request, Response

from rzp_common.mongo_client import get_db
from rzp_common.redis_client import get_redis
from rzp_meta_wa_client.client import verify_subscription_challenge, verify_webhook_signature

from kafka_producer import publish_event

router = APIRouter()

CSW_WINDOW_SECONDS = 24 * 3600


@router.get("/webhooks/meta")
def verify_meta_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    if verify_subscription_challenge(hub_mode, hub_verify_token) and hub_challenge is not None:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/webhooks/meta")
async def meta_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)) -> dict:
    raw_body = await request.body()
    db = get_db()
    now = datetime.now(timezone.utc)

    try:
        event = await request.json()
    except ValueError:
        db.raw_webhook_events.insert_one({
            "razorpay_event_id": None, "event_type": "whatsapp.unparseable",
            "payload": None, "signature_valid": None, "received_at": now,
            "processed": False, "processing_error": "invalid JSON body",
        })
        return {"status": "ignored"}

    signature_valid = None
    if x_hub_signature_256:
        try:
            signature_valid = verify_webhook_signature(raw_body, x_hub_signature_256)
        except RuntimeError:
            signature_valid = None  # META_WA_APP_SECRET not configured in this environment

    result = db.raw_webhook_events.insert_one({
        "razorpay_event_id": None, "event_type": "whatsapp.inbound", "payload": event,
        "signature_valid": signature_valid, "received_at": now, "processed": False, "processing_error": None,
    })

    if signature_valid is False:
        db.raw_webhook_events.update_one(
            {"_id": result.inserted_id}, {"$set": {"processing_error": "signature verification failed"}}
        )
        return {"status": "rejected"}

    try:
        for entry in event.get("entry", []):
            for change in entry.get("changes", []):
                _handle_change(change.get("value", {}))
        db.raw_webhook_events.update_one({"_id": result.inserted_id}, {"$set": {"processed": True}})
    except Exception as exc:  # noqa: BLE001 - a bad/unexpected payload must not become a Meta retry storm
        db.raw_webhook_events.update_one(
            {"_id": result.inserted_id},
            {"$set": {"processed": False, "processing_error": f"{type(exc).__name__}: {exc}"}},
        )

    return {"status": "ok"}


def _handle_change(value: dict) -> None:
    contacts_by_wa_id = {c["wa_id"]: c for c in value.get("contacts", []) if c.get("wa_id")}

    for message in value.get("messages", []):
        phone = message.get("from")  # may be absent per the BSUID note above
        contact = contacts_by_wa_id.get(phone, {}) if phone else {}
        profile_name = contact.get("profile", {}).get("name")
        text = (message.get("text") or {}).get("body", "")

        customer_id = None
        if phone:
            db = get_db()
            customer = db.customers.find_one({"phone": f"+{phone}"})
            customer_id = customer["razorpay_customer_id"] if customer else None

        # CSW tracker (sync, per the design's own Redis mapping) - keyed the
        # same way send_whatsapp_message's quota tracking falls back
        # (customer_id if resolved, else the raw phone) so a later
        # check_csw_status call using the same identity finds this window.
        identity_key = customer_id or phone
        if identity_key:
            r = get_redis()
            r.set(f"csw_open_until:{identity_key}", str(int(datetime.now(timezone.utc).timestamp())
                                                          + CSW_WINDOW_SECONDS), ex=CSW_WINDOW_SECONDS)

        publish_event("whatsapp.inbound", 7, {
            "customer_id": customer_id, "phone": phone, "profile_name": profile_name,
            "message_id": message.get("id"), "text": text, "message_type": message.get("type"),
        }, event_id=message.get("id"))
