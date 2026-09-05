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

from a2a_dispatch import build_delegation_instruction, build_instruction, dispatch  # noqa: E402
from agent_registry import AGENT_NAMES, DELEGATION_TARGET_URLS, resolve_agent_url  # noqa: E402

KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC = "revenue-recovery-events"
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
MAX_DELEGATION_HOPS = 4  # generous for every known chain (e.g. NLP -> Checkout Salvage -> NLP is 3 hops), still bounds a runaway cycle


def already_processed(event_id: str) -> bool:
    """Redis SETNX-based idempotency guard — a Kafka event redelivered (e.g.
    after a consumer restart before offset commit) must never be dispatched
    twice to a sub-agent."""
    r = get_redis()
    return not r.set(f"processed_event:{event_id}", "1", nx=True, ex=IDEMPOTENCY_TTL_SECONDS)


async def dispatch_with_delegation(agent_url: str, instruction: str, hop: int = 1) -> None:
    """Dispatches one A2A call and recursively follows any further two-hop
    delegation artifacts the response carries, up to MAX_DELEGATION_HOPS.

    Gap fix (2026-09-05): this used to check only ONE level of delegation -
    correct for every case that existed until now (some agent -> Conversational
    NLP Agent, done), but not for a genuine 3-hop chain: the Conversational
    NLP Agent delegates a "resend my payment link" request to the Checkout
    Salvage Agent, which calls generate_recovery_link - a tool that ALREADY
    builds its own two-hop WhatsApp-send artifact when called normally. That
    third hop's delegation artifact was being silently dropped by the old
    single-level check: the link would be created for real but never actually
    sent. Recursion (bounded, not unconditional - hop count guards against a
    genuine cycle, e.g. a future bug where two agents keep re-delegating to
    each other) handles this and any future chain shape generically, rather
    than hardcoding "exactly 2 hops" as if that were structural.
    """
    label = AGENT_NAMES.get(agent_url, agent_url)
    print(f"[dispatch hop {hop}] -> {label}")
    try:
        result = await dispatch(agent_url, instruction)
        print(f"[result hop {hop}] {label}: {result['text'][:300]}")
    except Exception as exc:  # noqa: BLE001 - a bad agent response must not crash the consumer loop
        print(f"[error] dispatch hop {hop} to {label} failed: {type(exc).__name__}: {exc}")
        return

    if not result["delegation_artifacts"]:
        return
    if hop >= MAX_DELEGATION_HOPS:
        print(f"[warn] hop limit ({MAX_DELEGATION_HOPS}) reached at {label}, dropping further delegation artifacts")
        return

    for artifact in result["delegation_artifacts"]:
        action = artifact.get("action")
        target_url = DELEGATION_TARGET_URLS.get(action)
        if target_url is None:
            print(f"[warn] unrecognized delegation artifact action: {action!r}")
            continue
        delegation_instruction = build_delegation_instruction(artifact)
        print(f"[delegate hop {hop}->{hop + 1}] {label} -> {AGENT_NAMES[target_url]}: {action}")
        await dispatch_with_delegation(target_url, delegation_instruction, hop=hop + 1)


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
    await dispatch_with_delegation(agent_url, instruction)


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
