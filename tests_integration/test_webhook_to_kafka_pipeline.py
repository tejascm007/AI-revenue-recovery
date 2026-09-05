"""Verifies the real, deterministic slice of the master pipeline (Design_
Spec_and_Decisions.md, section 3: "Webhooks -> FastAPI Backend -> raw JSON
dump -> MongoDB -> Kafka Event") end-to-end, through the actual FastAPI app
- not a hand-rolled reproduction of what the handler is "supposed" to do.

Deliberately uses a payment.failed event: it's the one real webhook type
that needs zero Razorpay/Meta/OpenRouter credentials to process (no vault
token upsert, no outbound API call - just Redis telemetry + a Kafka
publish), keeping this suite inside the "no paid/third-party credentials"
boundary tests_integration/README.md states. Everything past this point
(the Orchestrator picking the event up and dispatching to a real LLM agent)
is out of scope here - see that same README for why.

Also re-verifies, against real infra, a bug found and fixed live this
session: handle_payment_failed used to compute instrument_key for Redis
telemetry but never put it in the published Kafka payload, so the receiving
agent had nothing real to work with. If this regresses, this test catches
it without needing to reproduce the original live A2A dispatch that first
caught it.
"""

import hashlib
import hmac
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from kafka_producer import TOPIC
from main import app

WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


def _signed_request(body: dict) -> tuple[bytes, str]:
    raw_body = json.dumps(body).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return raw_body, signature


@pytest.fixture(autouse=True)
def _require_webhook_secret():
    if not WEBHOOK_SECRET:
        pytest.skip("RAZORPAY_WEBHOOK_SECRET not set - see .github/workflows/integration-tests.yml")


def test_a_real_payment_failed_webhook_is_dumped_signature_verified_and_published_to_kafka():
    from confluent_kafka import Consumer
    from rzp_common.mongo_client import get_db

    payment_id = f"pay_integration_test_{uuid.uuid4().hex[:12]}"
    order_id = f"order_integration_test_{uuid.uuid4().hex[:12]}"
    body = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "method": "netbanking", "bank": "HDFC",
            "amount": 250000, "error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_cancelled",
            "error_source": "customer", "customer_id": None,
        }}},
    }
    raw_body, signature = _signed_request(body)

    # "earliest", not "latest": a fresh consumer group's very first poll can
    # race the assignment against a message produced immediately after
    # subscribing, silently missing it under "latest". Reading from the true
    # start and filtering by event_id below is slower on a topic with real
    # retained history but can't miss the message through that race.
    consumer = Consumer({
        "bootstrap.servers": "localhost:9092",
        "group.id": f"integration-test-webhook-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([TOPIC])

    client = TestClient(app)
    response = client.post(
        "/webhooks/razorpay", content=raw_body,
        headers={"content-type": "application/json", "x-razorpay-signature": signature},
    )
    assert response.status_code == 200

    db = get_db()
    doc = db.raw_webhook_events.find_one({"razorpay_event_id": payment_id})
    assert doc is not None, "webhook was not dumped to raw_webhook_events"
    assert doc["signature_valid"] is True
    assert doc["processed"] is True
    assert doc["processing_error"] is None

    kafka_message = None
    for _ in range(200):  # generous - "earliest" may need to scan real retained history first
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        event = json.loads(msg.value().decode("utf-8"))
        if event.get("event_id") == payment_id:
            kafka_message = event
            break
    consumer.close()

    assert kafka_message is not None, "payment.failed was never published to Kafka"
    assert kafka_message["event_type"] == "payment.failed"
    assert kafka_message["problem_id"] == 2  # netbanking isn't an EMI method
    assert kafka_message["payload"]["order_id"] == order_id
    # The real bug this test also guards against: instrument_key must be the
    # real bank code, not silently dropped from the payload.
    assert kafka_message["payload"]["instrument_key"] == "HDFC"

    db.raw_webhook_events.delete_one({"_id": doc["_id"]})


def test_a_badly_signed_webhook_is_recorded_but_rejected():
    from rzp_common.mongo_client import get_db

    payment_id = f"pay_integration_test_bad_sig_{uuid.uuid4().hex[:12]}"
    body = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": payment_id, "method": "upi"}}}}
    raw_body, _ = _signed_request(body)

    client = TestClient(app)
    response = client.post(
        "/webhooks/razorpay", content=raw_body,
        headers={"content-type": "application/json", "x-razorpay-signature": "not_the_real_signature"},
    )
    assert response.status_code == 200  # per design: always 200, Razorpay retries on non-2xx
    assert response.json() == {"status": "rejected"}

    db = get_db()
    doc = db.raw_webhook_events.find_one({"razorpay_event_id": payment_id})
    assert doc is not None
    assert doc["signature_valid"] is False
    assert doc["processing_error"] == "signature verification failed"

    db.raw_webhook_events.delete_one({"_id": doc["_id"]})
