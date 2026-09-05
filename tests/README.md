# Tests

```
uv run pytest tests/ -v
```

## Scope

This suite covers the pure, infra-free logic across the codebase — no live MongoDB/Redis/Kafka, no real Razorpay/Meta/OpenRouter credentials needed. That's a deliberate boundary, not an oversight:

- **What's here**: deterministic functions with no I/O — `temporal_resolver.py`'s Hinglish date resolution (the most-corrected piece of custom logic in this project), the two-hop delegation artifact/extraction logic, agent routing, instruction-building, checkpoint key encode/decode, and the protobuf float-coercion fix. These run in CI on every push (`.github/workflows/tests.yml`) with zero external dependencies.
- **What's not here**: everything that touches live Mongo/Redis/Kafka/Docker, a real LLM, or a real third-party API (Razorpay, Meta). This project's actual verification discipline all session has been to run real code against real infrastructure and real credentials, catch what breaks, and document it — not to mock those boundaries. `../Design_Spec_and_Decisions.md`'s changelog is the record of that verification: every MCP tool, every agent, the full Kafka→Orchestrator→agent pipeline, real Razorpay test-mode calls, and a genuine end-to-end WhatsApp round trip have all been run and read, just not captured as an automated integration suite here. Building that out (likely `docker-compose`-based ephemeral Mongo/Redis/Kafka in CI, with the paid/rate-limited third-party calls still out of scope for CI) is real, valuable, not-yet-done follow-up work.
