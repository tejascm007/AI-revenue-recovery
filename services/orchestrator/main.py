"""Main Orchestrator — consumes derived events from Kafka, checks Redis for
idempotency, routes each event via A2A to the sub-agent that owns its
problem_id.

Design reference: Design_Spec_and_Decisions.md, section 3. This is the one
place in the whole system that holds a connection to all 4 sub-agents;
no agent ever calls another agent directly.

Run:
    uv run python services/orchestrator/main.py
"""

import asyncio
import json
import sys
from pathlib import Path

from confluent_kafka import Consumer

_CODES_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_CODES_ROOT / "libs"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rzp_common.redis_client import get_redis  # noqa: E402

from a2a_dispatch import build_instruction, dispatch  # noqa: E402
from agent_registry import AGENT_NAMES, resolve_agent_url  # noqa: E402

KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC = "revenue-recovery-events"
IDEMPOTENCY_TTL_SECONDS = 24 * 3600


def already_processed(event_id: str) -> bool:
    """Redis SETNX-based idempotency guard — a Kafka event redelivered (e.g.
    after a consumer restart before offset commit) must never be dispatched
    twice to a sub-agent."""
    r = get_redis()
    return not r.set(f"processed_event:{event_id}", "1", nx=True, ex=IDEMPOTENCY_TTL_SECONDS)


async def handle_event(event: dict) -> None:
    event_id = event.get("event_id", "")
    if "problem_id" not in event or "event_type" not in event:
        print(f"[error] malformed event (missing problem_id/event_type), skipping: {event}")
        return
    problem_id = event["problem_id"]
    event_type = event["event_type"]
    payload = event.get("payload", {})

    if event_id and already_processed(event_id):
        print(f"[skip] event {event_id} already processed")
        return

    agent_url = resolve_agent_url(problem_id)
    instruction = build_instruction(event_type, problem_id, payload)
    print(f"[dispatch] problem={problem_id} -> {AGENT_NAMES[agent_url]}: {event_type}")

    try:
        result = await dispatch(agent_url, instruction)
        print(f"[result] {AGENT_NAMES[agent_url]}: {result[:300]}")
    except Exception as exc:  # noqa: BLE001 - a bad agent response must not crash the consumer loop
        print(f"[error] dispatch to {AGENT_NAMES[agent_url]} failed: {type(exc).__name__}: {exc}")


async def consume_loop() -> None:
    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "group.id": "main-orchestrator",
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([TOPIC])
    print(f"Orchestrator listening on '{TOPIC}' ...")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                await asyncio.sleep(0.1)
                continue
            if msg.error():
                print(f"[kafka error] {msg.error()}")
                continue
            try:
                event = json.loads(msg.value().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f"[error] malformed event, skipping: {exc}")
                continue
            await handle_event(event)
    finally:
        consumer.close()


if __name__ == "__main__":
    asyncio.run(consume_loop())
