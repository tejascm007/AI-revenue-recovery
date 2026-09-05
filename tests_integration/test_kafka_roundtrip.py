"""Verifies kafka_producer.publish_event actually produces a message a real
consumer can read back correctly - the exact shape services/orchestrator/
main.py's consume_loop depends on. A pure-logic test could check the dict
this function builds, but never that confluent_kafka's Producer/Consumer
pair genuinely round-trips it through a real broker with the right topic,
serialization, and delivery.
"""

import uuid

from kafka_producer import publish_event
from kafka_test_utils import consumer_positioned_at_end, wait_for_event


def test_a_published_event_round_trips_through_a_real_broker():
    consumer = consumer_positioned_at_end()
    try:
        marker = f"integration-test-{uuid.uuid4()}"
        event_id = publish_event("integration_test_event", problem_id=2, payload={"marker": marker})

        received = wait_for_event(consumer, event_id)
        assert received["event_type"] == "integration_test_event"
        assert received["problem_id"] == 2
        assert received["payload"]["marker"] == marker
    finally:
        consumer.close()


def test_publish_event_defaults_to_a_fresh_uuid_when_no_event_id_given():
    event_id_1 = publish_event("integration_test_event", problem_id=2, payload={})
    event_id_2 = publish_event("integration_test_event", problem_id=2, payload={})
    assert event_id_1 != event_id_2
