"""Shared Kafka test helper.

Real bug found live (2026-09-05): both Kafka-dependent tests originally read
from "earliest" and scanned a bounded number of messages looking for their
own event_id - robust against the assignment-timing race "latest" has, but
not against a topic that keeps growing. Against a long-lived local dev
Kafka topic carrying a full day's worth of manual live-testing history, the
scan eventually stopped finding its own message before running out of
polls. Increasing the bound again would only postpone the same failure.

The actual fix: capture each partition's current high-water mark BEFORE
producing anything, `assign()` (not `subscribe()` - this bypasses consumer-
group coordination entirely, avoiding the "latest" race, since we set the
starting offset ourselves rather than asking the broker to resolve one at
join time) directly to those offsets, and only poll forward from there.
Correct and fast regardless of how much retained history precedes it -
verified against this session's own topic, which by now carries a full
day's history.
"""

import json
import time
import uuid

from confluent_kafka import Consumer, TopicPartition

from kafka_producer import TOPIC


def consumer_positioned_at_end(group_id: str | None = None) -> Consumer:
    """Returns a Consumer assigned to every partition of TOPIC, seeked to
    each partition's current end - call this BEFORE producing/triggering
    whatever event the test is waiting for."""
    consumer = Consumer({
        "bootstrap.servers": "localhost:9092",
        "group.id": group_id or f"integration-test-{uuid.uuid4()}",
        "enable.auto.commit": False,
    })
    metadata = consumer.list_topics(TOPIC, timeout=10)
    partitions = []
    for partition_id in metadata.topics[TOPIC].partitions:
        _, high = consumer.get_watermark_offsets(TopicPartition(TOPIC, partition_id), timeout=10)
        partitions.append(TopicPartition(TOPIC, partition_id, high))
    consumer.assign(partitions)
    return consumer


def wait_for_event(consumer: Consumer, event_id: str, timeout_seconds: float = 20.0) -> dict:
    """Polls `consumer` until an event with this event_id arrives, or raises
    after timeout_seconds. Only ever sees messages produced after the
    consumer was positioned (see consumer_positioned_at_end), so this never
    has to scan past retained history."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        event = json.loads(msg.value().decode("utf-8"))
        if event.get("event_id") == event_id:
            return event
    raise AssertionError(f"event_id {event_id!r} never arrived within {timeout_seconds}s")
