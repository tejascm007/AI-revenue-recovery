# AI Revenue Recovery Engine

A production-grade, multi-agent system that recovers revenue lost across a Razorpay merchant's payment lifecycle — checkout drop-off, failed subscriptions, hard/soft declines, B2B receivables — via 4 domain-specialist AI agents coordinated by a central orchestrator, each backed by its own set of deterministic tools.

Built for Razorpay's AI Buildathon (Track 03: AI Revenue Recovery), as a real system rather than a hackathon demo: every mechanism below has been verified against live infrastructure and, where credentials exist, live third-party APIs (Razorpay test mode, Meta WhatsApp Cloud API, OpenRouter) — not mocked.

The full design rationale, decision history, and per-problem low-level designs live in `../Design_Spec_and_Decisions.md` (outside this folder) — that document is the source of truth for _why_; this README is about _how to run it_.

## Architecture

```
[ Razorpay Ecosystem ] ──(Webhooks/Sync)──► [ FastAPI Backend ] ──(Raw JSON Dump)──► [ MongoDB ]
                                                    │
                                               (Kafka Event)
                                                    │
                                                    ▼
                                     [ MAIN ORCHESTRATOR AGENT ] ◄────(State & Locks)────► [ Redis ]
                                                    │
                                         (A2A Protocol Routing)
                                                    │
        ┌───────────────────────┬───────────────────┼───────────────────┬───────────────────────┐
        ▼                       ▼                   │                   ▼                       ▼
[ Checkout Salvage ]  [ Recurring Revenue ]         │         [ Conversational NLP ]  [ B2B Receivables ]
   (MCP Client)            (MCP Client)             │              (MCP Client)            (MCP Client)
        │                       │                   │                   │                       │
        ▼                       ▼                   │                   ▼                       ▼
 ┌─────────────┐         ┌─────────────┐            │            ┌─────────────┐         ┌─────────────┐
 │FastMCP Srv A│         │FastMCP Srv B│            │            │FastMCP Srv C│         │FastMCP Srv D│
 │ - Prob 2    │         │ - Prob 5    │            │            │ - Prob 7    │         │ - Prob 9    │
 │ - Prob 3    │         │ - Prob 6    │            │            │ - Prob 8    │         │ - Tools:    │
 │ - Prob 4    │         │ - Tools:    │            │            │ - Tools:    │         │ Recon, Esc, │
 │ - Tools:    │         │ Sub_Pause,  │            │            │ NLP_Extract,│         │ ERP_Mock    │
 │ Links, Nudge│         │ Invoice_Gen │            │            │ Meta_WA_API │         │             │
 └─────────────┘         └─────────────┘            │            └──────┬──────┘         └──────┬──────┘
                                                    │                   │                       │
          *Problem 1 (Layer 0 Vault)* ◄─────────────┘                   └───────────┬───────────┘
      (Handled natively by FastAPI + MongoDB via standard Razorpay TokenHQ APIs. Zero AI overhead.)
                                                                                     ▼
                                                                        ┌───────────────────────┐
                                                                        │  FastMCP Srv 0 (RAG)  │
                                                                        │  - Policy RAG          │
                                                                        │  - Tools: Retrieve_    │
                                                                        │    Policy_Context,     │
                                                                        │    Log_FAQ_Interaction │
                                                                        └───────────┬───────────┘
                                                                                     ▼
                                                                     [ MongoDB Atlas Hybrid Search ]
                                                                     (real $vectorSearch + $search/BM25,
                                                                      via mongodb-atlas-local, separate
                                                                      from the plain mongod above)

A separate Watchdog Poller drains a shared Redis checkpoint queue and feeds
scheduled follow-ups (stage-2 checkout checks, dunning touches, PTP
due-dates, B2B escalations) back through the same Kafka → Orchestrator path.
```

Problem 1 (RBI-compliant tokenized card vault) sits outside the agent mesh entirely — it's not agentic, just plain FastAPI + MongoDB + Razorpay TokenHQ, since compliant token storage is a memory problem, not a judgment problem.

**Why A2A + MCP, not one flat agent**: A2A (agent-to-agent) solves peer delegation between the 4 domain agents; MCP (model-context-protocol) solves each agent's own access to its specific tools, with hard tool isolation per problem (no agent can call another problem's tools directly). A cross-agent side effect never happens via shared tool access — it's a two-hop delegation through the Orchestrator, the same pattern A2A's own "routing agent" design intends. This runs in both directions: forward (e.g. the Checkout Salvage Agent needing a WhatsApp message sent, a capability only the Conversational NLP Agent owns) and reverse (the Conversational NLP Agent recognizing a B2B billing dispute in an inbound WhatsApp message, but not owning invoice data to act on it, so it delegates to the B2B Receivables Agent instead). The Orchestrator resolves each delegation artifact's target agent generically from its own `action` field (`services/orchestrator/agent_registry.py`'s `DELEGATION_TARGET_URLS`), not by assuming every artifact is WhatsApp-bound.

## Tech stack

Python (`uv` for dependency management) · FastAPI · MongoDB (a plain local `mongod`, plus a separate `mongodb-atlas-local` deployment specifically for the RAG layer's real vector/text search indexes, which a plain community `mongod` can't provide) · Kafka (KRaft mode, no ZooKeeper) · Redis · `a2a-sdk` · FastMCP · LangChain/`langchain-mcp-adapters` · OpenRouter (one key for both the LLM and embeddings) · Razorpay Python SDK · Meta WhatsApp Cloud API (direct, no BSP).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python dependency manager)
- MongoDB Community Server (a plain `mongod` binary)
- Java 11+ and a local [Kafka 4.x](https://kafka.apache.org/downloads) distribution (KRaft mode)
- Docker Desktop (for Redis and the RAG deployment)
- An [OpenRouter](https://openrouter.ai) account + API key (LLM + embeddings)
- Optional, for real webhook delivery: a Razorpay test-mode account, a Meta Developer app with the WhatsApp product added, and a tunnel tool ([Cloudflare Tunnel](https://github.com/cloudflare/cloudflared/releases) — `ngrok` was tried first but got flagged by Windows Defender as a false positive on this machine; `cloudflared`'s standalone binary had no such issue)

## First-time setup

```powershell
uv sync

# MongoDB - create all collections, indexes, and validators
uv run python scripts/db_setup.py

# Kafka - one-time cluster formatting (skip if your Kafka is already formatted)
cd <your-kafka-dir>
java -cp "libs/*" kafka.tools.StorageTool random-uuid                              # note the printed UUID
java -cp "libs/*" kafka.tools.StorageTool format -t <uuid> -c config/server.properties
cd -

# Start infra, then create the Kafka topic (needs the broker up)
powershell -ExecutionPolicy Bypass -File scripts/start_infra.ps1
java -cp "<kafka-dir>/libs/*" org.apache.kafka.tools.TopicCommand --create `
    --topic revenue-recovery-events --partitions 3 --replication-factor 1 `
    --bootstrap-server localhost:9092

# RAG deployment - real Atlas Vector Search + Atlas Search (BM25) indexes
uv run python scripts/rag_db_setup.py
uv run python scripts/ingest_faq_documents.py   # needs OPENROUTER_API_KEY in .env first

# Credentials
cp .env.example .env
# fill in .env - see .env.example's own comments for where to get each value
```

## Running

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_infra.ps1      # Mongo, Kafka, Redis, RAG deployment
powershell -ExecutionPolicy Bypass -File scripts/start_services.ps1   # backend, orchestrator, poller, all 4 agents
```

Both scripts are idempotent — safe to re-run, they skip anything already up. To stop the application layer only (leaving infra running): `scripts/stop_services.ps1`. To also stop the Docker-based infra: `scripts/stop_infra.ps1` (mongod/Kafka are native processes with no script-managed stop hook — close their windows or kill by port).

Ports: backend `8000`, Checkout Salvage Agent `9002`, Recurring Revenue Agent `9003`, Conversational NLP Agent `9004`, B2B Receivables Agent `9005`. Each agent spawns its own MCP server subprocesses on first request — nothing extra to start for those.

## Public webhooks (optional)

Razorpay and Meta both need a public HTTPS URL to deliver real webhooks — `localhost:8000` isn't reachable from their servers. Get one with `cloudflared`:

```powershell
cloudflared.exe tunnel --url http://localhost:8000
```

This prints a random `https://<name>.trycloudflare.com` URL (no Cloudflare account needed for this "quick tunnel" mode — it's ephemeral and changes on every restart). Register `<url>/webhooks/razorpay` in Razorpay's Dashboard → Webhooks, and `<url>/webhooks/meta` in the Meta app's WhatsApp → Configuration page (with the same `META_WA_VERIFY_TOKEN` from `.env`).

**A real, non-obvious gotcha found the hard way**: getting the app's own webhook config right (URL + verify token + subscribed fields) is not sufficient — the WhatsApp Business Account itself must also be explicitly subscribed to your app (`POST /{WABA_ID}/subscribed_apps`), a separate step some test WABAs don't do automatically (ours defaulted to subscribing to a Meta-internal app instead). If inbound messages aren't arriving despite a correct-looking webhook config, check `GET /{WABA_ID}/subscribed_apps` directly.

## Verifying it's working

```powershell
curl http://localhost:8000/health
curl http://localhost:9002/.well-known/agent-card.json
```

To see a real event flow end-to-end: produce a message onto the `revenue-recovery-events` Kafka topic (any Python script using `confluent_kafka.Producer`, shape `{"event_id", "event_type", "problem_id", "payload"}`) and watch `services/orchestrator/main.py`'s own stdout — it logs each dispatch and the agent's response.

## Project structure

```
libs/                    # shared clients: Mongo, Redis, Razorpay, Meta WA, OpenRouter LLM/embeddings, agent-kit (audit/two-hop/templates)
services/
  backend/               # FastAPI: Razorpay + Meta webhook ingestion, Problem 1 vault API, checkout S2S API
  orchestrator/          # Kafka consumer + Redis idempotency + A2A routing + two-hop delegation dispatch
  watchdog_poller/       # drains the shared Redis checkpoint queue, republishes due events onto Kafka
  agents/                # 4 A2A sub-agents, each an AgentExecutor + MultiServerMCPClient
  mcp-servers/           # 9 FastMCP servers - prob2 through prob9 (one per problem) + prob0 (cross-cutting RAG)
scripts/                 # db_setup, rag_db_setup, ingest_faq_documents, start/stop scripts
tests/                   # pytest - the deterministic logic only, see tests/README.md for scope
.github/workflows/       # CI: runs tests/ on every push/PR
```

## Tests & CI

```powershell
uv run pytest tests/ -v
```

Runs in GitHub Actions on every push (`.github/workflows/tests.yml`). Deliberately scoped to pure, infra-free logic — see `tests/README.md` for exactly what's covered and why the broader system's verification (every MCP tool, agent, and the full pipeline, including real Razorpay/Meta calls) has instead been live-run-and-read, documented in `../Design_Spec_and_Decisions.md`'s changelog, rather than mocked into this suite.

## What's not built yet

- The merchant's own storefront/checkout page (a prompt for generating one with Lovable exists in this project's conversation history, not yet built)
- A merchant-facing dashboard (recovery-rate tiles, audit-trail drill-down) — explicitly out of scope for every LLD in this project so far, not just unbuilt
- Live-infra integration tests in CI (ephemeral Mongo/Redis/Kafka via `docker-compose`, say) — the automated suite that exists covers pure logic only, see `tests/README.md`
- Razorpay Smart Collect (Virtual Accounts) needs enabling on your Razorpay account separately — confirmed via a real API call that it's an account-level gap, not a code issue; Problem 9's reconciliation flow needs it
- Nothing in this codebase ever creates an `invoices` document (Problem 9's B2B receivables tools all assume one already exists, populated from some out-of-band ERP/accounting sync this project doesn't own) — verified by finding the collection genuinely empty while live-testing the reverse two-hop delegation below; a seed script or real ERP integration is separately-scoped future work
