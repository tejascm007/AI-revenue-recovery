"""Verifies kafka_producer.publish_event actually produces a message a real
consumer can read back correctly - the exact shape services/orchestrator/
main.py's consume_loop depends on. A pure-logic test could check the dict
this function builds, but never that confluent_kafka's Producer/Consumer
pair genuinely round-trips it through a real broker with the right topic,
serialization, and delivery.
"""

import json
import uuid

from confluent_kafka import Consumer

from kafka_producer import TOPIC, publish_event


def _consume_matching(group_id: str, event_id: str, max_polls: int = 200, poll_timeout: float = 1.0) -> dict:
    # Reads from the true beginning ("earliest") and scans for the specific
    # event_id just published, rather than trusting the first message that
    # arrives to be it - this topic can carry real retained history (from
    # other test runs, or real manual dispatches against a long-lived local
    # broker), and a fresh consumer group starting from "latest" has a real
    # assignment-timing race against a message produced immediately after
    # subscribing. Bounded by max_polls, not wall-clock alone, since once
    # connected, scanning past already-fetched history is fast regardless of
    # how much of it there is.
    consumer = Consumer({
        "bootstrap.servers": "localhost:9092",
        "group.id": group_id,  # fresh group per test - no offset state to collide with
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([TOPIC])
    try:
        for _ in range(max_polls):
            msg = consumer.poll(poll_timeout)
            if msg is None or msg.error():
                continue
            event = json.loads(msg.value().decode("utf-8"))
            if event.get("event_id") == event_id:
                return event
        raise AssertionError(f"event_id {event_id!r} never arrived within {max_polls} polls")
    finally:
        consumer.close()


def test_a_published_event_round_trips_through_a_real_broker():
    marker = f"integration-test-{uuid.uuid4()}"
    event_id = publish_event("integration_test_event", problem_id=2, payload={"marker": marker})

    received = _consume_matching(group_id=f"integration-test-{uuid.uuid4()}", event_id=event_id)
    assert received["event_type"] == "integration_test_event"
    assert received["problem_id"] == 2
    assert received["payload"]["marker"] == marker


def test_publish_event_defaults_to_a_fresh_uuid_when_no_event_id_given():
    event_id_1 = publish_event("integration_test_event", problem_id=2, payload={})
    event_id_2 = publish_event("integration_test_event", problem_id=2, payload={})
    assert event_id_1 != event_id_2
