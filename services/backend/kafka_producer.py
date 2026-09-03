"""Lazy Kafka producer for derived events — the FastAPI backend's only write
path onto the `revenue-recovery-events` topic the Main Orchestrator consumes.

Lazy for the same reason every other external client in this codebase is
(Mongo/Redis/Razorpay/Meta all lazy via a cached getter): constructing a
confluent_kafka.Producer eagerly at import time would make the whole app fail
to start if the broker isn't reachable yet, rather than failing only when a
webhook actually needs to publish.
"""

import json
import uuid
from functools import lru_cache

from confluent_kafka import Producer

KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC = "revenue-recovery-events"


@lru_cache(maxsize=1)
def get_producer() -> Producer:
    return Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})


def publish_event(event_type: str, problem_id: int, payload: dict, event_id: str | None = None) -> str:
    """Publishes one derived event in the exact shape
    services/orchestrator/main.py's consume_loop expects. event_id defaults
    to a fresh UUID (most webhook payloads don't carry a stable ID usable as
    our own idempotency key across retries) — callers with a natural
    idempotent key (e.g. the Razorpay event's own id) should pass it explicitly
    so a webhook redelivery doesn't get dispatched twice."""
    event_id = event_id or str(uuid.uuid4())
    event = {"event_id": event_id, "event_type": event_type, "problem_id": problem_id, "payload": payload}
    get_producer().produce(TOPIC, json.dumps(event).encode("utf-8"))
    get_producer().poll(0)  # serve delivery-report callbacks without blocking the request
    return event_id
