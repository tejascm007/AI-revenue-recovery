# Tests

```
uv run pytest tests/ -v
```

## Scope

This suite covers the pure, infra-free logic across the codebase — no live MongoDB/Redis/Kafka, no real Razorpay/Meta/OpenRouter credentials needed. That's a deliberate boundary, not an oversight:

- **What's here**: deterministic functions with no I/O — `temporal_resolver.py`'s Hinglish date resolution (the most-corrected piece of custom logic in this project), the two-hop delegation artifact/extraction logic, agent routing, instruction-building, checkpoint key encode/decode, and the protobuf float-coercion fix. These run in CI on every push (`.github/workflows/tests.yml`) with zero external dependencies.
- **What's not here**: everything that touches live Mongo/Redis/Kafka, a real LLM, or a real third-party API (Razorpay, Meta). This project's actual verification discipline all session has been to run real code against real infrastructure and real credentials, catch what breaks, and document it — not to mock those boundaries. `../Design_Spec_and_Decisions.md`'s changelog is the record of that verification: every MCP tool, every agent, the full Kafka→Orchestrator→agent pipeline, real Razorpay test-mode calls, and a genuine end-to-end WhatsApp round trip have all been run and read.
- The middle ground — real MongoDB/Kafka behavior that pure logic can't reach, but without a paid/rate-limited credential — is `../tests_integration/` (see its own README), running in CI via `.github/workflows/integration-tests.yml` against real ephemeral service containers. Nothing here needs to duplicate that; a future contributor looking for "why isn't there a live-infra test for X" should look there before assuming it's missing.
