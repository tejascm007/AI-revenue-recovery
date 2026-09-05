# Live-infra integration tests

```powershell
uv run pytest tests_integration/ -v
```

Requires a real MongoDB (with `scripts/db_setup.py` already run against it), Redis, and Kafka reachable at the same `localhost` defaults the application code itself hardcodes/defaults to (27017, 6379, 9092) — for local runs, `scripts/start_infra.ps1` already provides these. Also requires `RAZORPAY_WEBHOOK_SECRET` set to any value (a shared secret you make up yourself — this suite never talks to the real Razorpay API).

## Scope

This is the middle ground `tests/README.md` names as separately-scoped future work — pure logic isn't enough to verify a `$jsonSchema` validator, a real Kafka producer/consumer pair, or a FastAPI route actually writing to a real database, but this project's stated discipline is still to never let a paid or rate-limited third-party credential (Razorpay live calls, Meta WhatsApp, OpenRouter) become a CI dependency. So the boundary here is exact:

- **What's here**: MongoDB's real schema validators actually rejecting invalid documents (`test_mongo_schema_validation.py`), a real Kafka producer → consumer round trip (`test_kafka_roundtrip.py`), and the FastAPI backend's `payment.failed` webhook handler genuinely writing to MongoDB and publishing to Kafka through the real app, signature verification included (`test_webhook_to_kafka_pipeline.py`). `payment.failed` was chosen deliberately — it's the one real Razorpay webhook type whose handler (`event_derivation.handle_payment_failed`) needs no Razorpay/Meta/OpenRouter credential to process, only Redis telemetry and a Kafka publish.
- **What's not here**: anything past the Kafka publish — the Orchestrator picking an event up and dispatching it to a real LLM agent. That boundary is exactly where a real, paid OpenRouter credential becomes unavoidable, and this project's whole session has instead verified that live, by hand, with the result recorded in `../Design_Spec_and_Decisions.md`'s changelog rather than mocked or spent on repeatedly in CI.

Runs in GitHub Actions on every push/PR (`.github/workflows/integration-tests.yml`), as a separate job from `tests.yml`'s pure-logic suite — kept in its own top-level directory (not `tests/`) specifically so `tests/`'s own scope boundary (stated in its README) stays literally true, and so a local `uv run pytest tests/` never surprises you by requiring Docker.
