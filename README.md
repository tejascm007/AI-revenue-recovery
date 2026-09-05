# AI Revenue Recovery Engine

A production-grade, multi-agent system that recovers revenue lost across a Razorpay merchant's payment lifecycle — checkout drop-off, failed subscriptions, hard/soft declines, B2B receivables — via 4 domain-specialist AI agents coordinated by a central orchestrator, each backed by its own set of deterministic tools, plus a cross-cutting Policy RAG layer for grounded FAQ answers.

Built for Razorpay's AI Buildathon (Track 03: AI Revenue Recovery), as a real system rather than a hackathon demo: every mechanism in this README has been verified against live infrastructure and live third-party APIs (Razorpay test mode, Meta WhatsApp Cloud API, OpenRouter) — not mocked. The full design rationale, decision history, and per-problem low-level designs live in `../Design_Spec_and_Decisions.md` (outside this folder, a dated changelog of the entire build) — that document is the source of truth for *why* every decision was made; this README is about *what exists and how to run it*.

## Current status

All 9 problems + the Policy RAG layer have been live-verified end-to-end (real webhook → real Kafka → real Orchestrator → real LLM agent → real tool/state change), most recently in a full pre-demo re-test pass. A companion merchant storefront (`circuitlane-pay-flow`, built separately, see [Frontend](#frontend-circuitlane-pay-flow) below) is built and connected to this backend's real APIs.

A few things worth knowing before you rely on this for a live demo:

- **OpenRouter credential health matters more than it looks.** A $0-balance account's free-tier models cap at **50 total requests/day** (shared across chat and embeddings), and even a funded account has a separate, smaller **in-flight request budget** tied to how much was actually topped up — the Conversational NLP Agent (the largest tool surface of the 4 agents) is the first to hit this under concurrent load. Keep a real, reasonably-funded key in `.env` if you want the agents to reason reliably during a demo, not just once.
- **Meta's test WhatsApp number only sends to allow-listed recipients.** A real, valid access token still gets a real `400` (`#131030 Recipient phone number not in allowed list`) for any phone number not explicitly added in the Meta dashboard. Add a number there before expecting to see a message land on it.
- **`META_WA_ACCESS_TOKEN` is a 24h temporary token** unless you've generated a permanent one — expect to refresh it from the Meta app's Getting Started page periodically.
- **This project's local Kafka/Mongo data directories live inside a OneDrive-synced folder**, which has caused a real Kafka storage corruption incident once already (OneDrive's sync agent interfering with memory-mapped index files). If Kafka won't start, check `logs/kafka-error.log` first — `scripts/start_infra.ps1` redirects its output there specifically because of this.

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
 │ - Tools:    │         │ Sub_Pause,  │            │            │ NLP_Extract,│         │ Dispute     │
 │ Links, Nudge│         │ Invoice_Gen │            │            │ Meta_WA_API │         │             │
 └─────────────┘         └─────────────┘            │            └──────┬──────┘         └──────┬──────┘
                                                    │                   │                       │
          *Problem 1 (Layer 0 Vault)* ◄─────────────┘                   └───────────┬───────────┘
      (Handled natively by FastAPI + MongoDB via standard Razorpay TokenHQ APIs. Zero AI overhead.)
                                                                                     ▼
                                                                        ┌───────────────────────┐
                                                                        │  FastMCP Srv 0 (RAG)  │
                                                                        │  - Policy RAG         │
                                                                        │  - Tools:             │
                                                                        │    Retrieve_Policy_   │
                                                                        │    Context,           │
                                                                        │    Log_FAQ_           │
                                                                        │    Interaction,       │
                                                                        │    Build_FAQ_Reply_   │
                                                                        │    Delegation         │
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

The diagram above is the *first hop* only (Orchestrator → owning agent → its tools). Every cross-agent side effect is a **second hop**, always re-routed through the Orchestrator (never agent-to-agent directly), resolved generically from the delegation artifact's own `action` field (`services/orchestrator/agent_registry.py`'s `DELEGATION_TARGET_URLS`) rather than a fixed shape:

```
 forward (send a WhatsApp message - only the Conversational NLP Agent may):

   Checkout Salvage   ─┐
   Recurring Revenue   ├─ send_whatsapp / send_whatsapp_freeform ─► Conversational NLP
   B2B Receivables     ─┘

 reverse (Conversational NLP Agent doesn't own the data needed to act, so it
 hands off to whichever agent does):

   Conversational NLP  ─ flag_b2b_dispute ───────────────► B2B Receivables
                            (resolves the invoice itself via
                             find_open_invoice_for_customer,
                             then pause_for_dispute)

   Conversational NLP  ─ request_payment_link_resend ────► Checkout Salvage
                            (resolves the order itself via
                             find_active_checkout_session_for_customer,
                             then generate_recovery_link - which itself
                             re-delegates back to Conversational NLP to
                             actually send it: a real 3-hop chain)
```

A chain like the last one is why `main.py`'s `dispatch_with_delegation` is recursive (bounded at `MAX_DELEGATION_HOPS = 4`), not a fixed two-step — an earlier version only ever checked one level of delegation and would have silently dropped that third hop, creating the link but never sending it.

**Why A2A + MCP, not one flat agent**: A2A (agent-to-agent) solves peer delegation between the 4 domain agents; MCP (model-context-protocol) solves each agent's own access to its specific tools, with hard tool isolation per problem (no agent can call another problem's tools directly). A cross-agent side effect never happens via shared tool access — it's always the two-hop delegation shown above, through the Orchestrator, the same pattern A2A's own "routing agent" design intends.

## Tech stack

- **Language/runtime**: Python 3.13, `uv` for dependency management
- **Web framework**: FastAPI (backend) + `uvicorn`
- **Agent protocol**: `a2a-sdk` (Agent-to-Agent, protobuf-based) — one Orchestrator, 4 domain sub-agents
- **Tool protocol**: FastMCP + `langchain-mcp-adapters` (Model-Context-Protocol) — 9 problem-specific tool servers + 1 cross-cutting RAG server, spawned as stdio subprocesses per agent
- **LLM/embeddings**: OpenRouter (`langchain-openai` pointed at OpenRouter's OpenAI-compatible endpoint) — one key covers both chat (`openai/gpt-4o` by default) and embeddings (`nvidia/nemotron-3-embed-1b:free`)
- **Persistence**: MongoDB — a plain local `mongod` for everything except the RAG corpus, plus a separate `mongodb-atlas-local` Docker deployment specifically for real Atlas Vector Search + Atlas Search (BM25) indexes, which a plain community `mongod` can't provide
- **Event bus**: Kafka (KRaft mode, no ZooKeeper) — one topic, `revenue-recovery-events`
- **Hot-path state**: Redis — idempotency keys, route-health sliding windows, PTP/CSW flags, the shared watchdog checkpoint queue
- **Payments**: Razorpay Python SDK (test mode)
- **Messaging**: Meta WhatsApp Cloud API, direct (no BSP)
- **Frontend**: React 19 + TanStack Start/Router + Tailwind + shadcn/ui, in a separate repo (`circuitlane-pay-flow`)

## Repo structure

```
libs/
  rzp_common/            # Mongo/Redis clients, .env loader, email client, subscription-lifecycle helpers
  rzp_razorpay_client/    # Razorpay SDK wrapper - orders, payment links, tokens, webhook signature verification
  rzp_meta_wa_client/     # Meta Graph API wrapper - send template/text messages
  rzp_agent_kit/          # shared across all 4 agent executors: LLM/embeddings factory, audit-log writer,
                           #   two-hop delegation artifact extraction, WhatsApp template catalog
services/
  backend/                # FastAPI: Razorpay + Meta webhook ingestion, Problem 1 vault API, checkout S2S API
    api/                  # webhooks.py (Razorpay), meta_webhooks.py, vault.py, checkout.py
    event_derivation.py    # one handler per Razorpay webhook event type - sync side-effects + Kafka publish
  orchestrator/           # Kafka consumer + Redis idempotency + A2A routing + two-hop delegation dispatch
    agent_registry.py      # problem_id -> agent URL, and delegation action -> target agent URL
    a2a_dispatch.py         # builds instructions, sends A2A calls, recursively follows delegation artifacts
  watchdog_poller/        # drains the shared Redis checkpoint queue, republishes due events onto Kafka
  agents/                 # 4 A2A sub-agents, each an AgentExecutor + MultiServerMCPClient
    checkout_salvage_agent/       # Problems 2, 3, 4  (port 9002)
    recurring_revenue_agent/      # Problems 5, 6     (port 9003)
    conversational_nlp_agent/     # Problems 7, 8     (port 9004) - the only WhatsApp sender
    b2b_receivables_agent/        # Problem 9         (port 9005)
  mcp-servers/            # 9 FastMCP servers, one per problem, hard tool isolation, + prob0 cross-cutting RAG
scripts/                  # db_setup, rag_db_setup, ingest_faq_documents, seed_b2b_invoices, start/stop scripts
tests/                    # pytest - pure, infra-free logic only (see tests/README.md)
tests_integration/        # pytest - real Mongo/Redis/Kafka, no paid credentials (see tests_integration/README.md)
.github/workflows/        # CI: tests.yml (pure logic) + integration-tests.yml (real ephemeral infra)
```

## The nine problems (+ cross-cutting RAG)

Every problem below has its full low-level design (data models, flow diagrams, stopping rules, real-world grounding) in `../Design_Spec_and_Decisions.md`, §11. This table is the *quick-reference* version — what it solves, who owns it, and the real mechanism, not the full rationale.

| # | Problem | Owning agent (port) | MCP tools | Mechanism |
|---|---|---|---|---|
| **1** | RBI-compliant tokenized vault | *(none — not agentic)* | — | `payment.captured` with `token_id`+`customer_id`+`card` synchronously upserts a masked token into `customers.vault_tokens` — no LLM judgment, compliant storage is a memory problem. `POST /api/customers/identify` resolves a stable `razorpay_customer_id` from name/email/phone (Razorpay's own fetch-or-create dedup — this is how a returning customer gets recognized without any login system). `GET/DELETE /api/vault/{customer_id}/methods` read/remove the locally-mirrored masked tokens, zero Razorpay API calls on read. |
| **2** | Payment route degradation & smart switching | Checkout Salvage (9002) | `get_route_status`, `suggest_alternate_route` | Every payment webhook synchronously updates Redis sliding-window counters (`:attempts`, `:failures` — infra-caused failures only, customer mistakes never count) and mirrors Razorpay's own downtime feed. `payment.failed` publishes to Kafka with the real bank/card-issuer/UPI-handle `instrument_key`; the agent calls `get_route_status` (deterministic: degraded if *either* signal says so) then `suggest_alternate_route` (checks Problem 1's vault for a healthy saved alternate first). |
| **3** | Checkout drop-off, OTP abandonment & ghost-debit reconciliation | Checkout Salvage (9002) | `verify_order_payment`, `claim_order_as_ghost_debit`, `check_stock_for_recovery`, `schedule_second_checkpoint`, `find_active_checkout_session_for_customer`, `generate_recovery_link` | `POST /api/checkout/orders` schedules a stage1 watchdog checkpoint. Stage1 fires → agent deliberately just schedules stage2 (never messages under 15 min). Stage2 fires → `verify_order_payment` (ground-truth check via Razorpay's own `/payments` endpoint, catches ghost debits) → if genuinely unpaid and in stock, `generate_recovery_link` (one real 16-min-expiry Payment Link) + two-hop WhatsApp delegation. `find_active_checkout_session_for_customer` powers the reverse two-hop ("resend my link" from just a `customer_id`). |
| **4** | BNPL & high-ticket EMI decline salvage | Checkout Salvage (9002) | `suggest_alternate_emi` | `payment.failed` with `method` in `{emi, cardless_emi}` carries the real declined provider (`card.issuer` for card EMI). `suggest_alternate_emi` atomically enforces a one-suggestion-per-checkout cap via `find_one_and_update`, and a fraud/risk rejection permanently blocks further suggestions rather than just consuming the slot. |
| **5** | Failed-subscription & mandate bounces (hard decline) | Recurring Revenue (9003) | `classify_decline`, `generate_hard_decline_link`, `reverse_duplicate_capture` | `subscription.pending` (not `payment.failed` — verified this subscription-charge-failure event carries no payment entity at all) → `classify_decline` applies a deterministic AFA-amount heuristic when `error_reason` is null → hard declines get one manual Payment Link at the T+5h watchdog checkpoint, looked up from the real subscription/customer record, never invented. |
| **6** | Penalty-aware mandate retry sequencer (soft decline / NSF) | Recurring Revenue (9003) | `start_sequence`, `evaluate_next_touch`, `finalize_churn` | Only acts *after* Razorpay's own retry cascade exhausts (`subscription.halted` — pausing/rescheduling Razorpay's own retries mid-flight is a real, confirmed API limitation, not a design choice). `start_sequence` mints ONE long-lived Payment Link reused across all touches and schedules 4 checkpoints upfront (days 0/4/9/11). Every touch checks Problem 7's PTP lock first — never nags someone who already promised a date. |
| **7** | Promise-to-pay tracker & NLP intent extraction | Conversational NLP (9004) | `lookup_active_ptp`, `set_ptp_lock`, `clear_ptp_lock`, `escalate_for_review`, `flag_payment_link_resend_request`, `flag_b2b_dispute` | Inbound WhatsApp message → the agent's own LLM classifies intent/sentiment directly (not a tool) → `set_ptp_lock` resolves the raw temporal expression via a custom deterministic resolver (handles Hinglish weekday/relative-date phrasing `dateparser` alone got wrong), locks a 14-day-capped promise, suppresses dunning until then. HOSTILE sentiment always escalates regardless of a successful lock. Reverse two-hop tools hand off requests this agent can't itself resolve (a dispute, a link-resend ask). |
| **8** | Hinglish WhatsApp conversational recovery | Conversational NLP (9004) | `check_csw_status`, `send_whatsapp_message`, `send_freeform_reply` | The *only* agent allowed to send WhatsApp — every other problem's WhatsApp need reaches it via two-hop delegation, never directly. `send_whatsapp_message` (business-initiated template, quota/frequency-cap checked) vs. `send_freeform_reply` (only inside an open 24h Customer Service Window — refuses rather than silently falling back to a template). Direct-to-Meta Cloud API, no BSP. |
| **9** | B2B receivables & reconciliation | B2B Receivables (9005) | `mint_invoice_virtual_account`, `match_bank_transfer_to_invoice`, `check_gstin`, `find_open_invoice_for_customer`, `pause_for_dispute`, `gather_decision_context`, `execute_action`, `fetch_and_resend_document` | One Virtual Account per invoice; `match_bank_transfer_to_invoice` reconciles TDS-aware (a short payment within the configured expected TDS% counts as fully settled). `gather_decision_context` assembles days-overdue/payment-history/active-dispute/active-PTP → `execute_action` enforces a fixed action set against a deterministic escalation-tier ceiling (can never jump ahead of what the day-offset actually permits). `pause_for_dispute` is a hard stop — clears every remaining checkpoint, never auto-resolved. `find_open_invoice_for_customer` powers the reverse two-hop (a dispute reported over WhatsApp, where the NLP agent only ever has `customer_id`). |
| **0** | Policy RAG *(cross-cutting)* | Conversational NLP (9004) **and** B2B Receivables (9005) — the two agents that answer open-ended customer questions | `retrieve_policy_context`, `log_faq_interaction`, `build_faq_reply_delegation` | Real MongoDB Atlas Hybrid Search (vector + BM25) over an ingested FAQ/T&Cs/SOP corpus. The confidence gate is a **raw `$vectorSearch` cosine-similarity check**, deliberately *not* the hybrid retriever's own RRF-fused score (which has no absolute meaning and would gate every query identically regardless of relevance — a real bug this project found and fixed). Used strictly for citation/justification in the audit trail — never as the actual enforcement decision for stopping rules or policy limits, which stays deterministic everywhere else in this system. |

**Configurable behavior** lives in `merchant_config` (seeded by `scripts/db_setup.py`), not hardcoded: `ptp_grace_period_hours` (24), `dunning_touch_spacing_days` (`[0, 4, 9, 11]`), `b2b_escalation_schedule_days` (`[-3, 1, 7, 14, 30, 45, 60]`), `faq_min_confidence` (0.65), `emi_provider_priority`, `tds_expected_percent`, `msme_registered`.

## Frontend (`circuitlane-pay-flow`)

A separate repository — a laptop e-commerce storefront (browse → cart → checkout → real Razorpay payment → confirmation) that exercises Problems 1–4's checkout-time path. Built with Lovable, connects to this backend's real API:

- `POST /api/checkout/orders` → Razorpay Standard Checkout.js, using the real `order_id`/`amount`/`currency` this backend returns
- CORS is enabled on this backend (`services/backend/main.py`) specifically so a browser-hosted storefront on a different origin can call it
- Env vars on the frontend side: `VITE_BACKEND_URL` (defaults to `http://localhost:8000`), `VITE_RAZORPAY_KEY_ID` (the *publishable* key_id — safe client-side, never the key_secret)
- Deliberately guest-checkout only right now (`customer_id: null` on every order) — Problem 1's `identify` endpoint exists and works, but nothing in the storefront calls it yet, so no vault token ever gets associated with a repeat customer today. Wiring that up just needs the phone number the checkout page already collects passed to `/api/customers/identify` before payment.
- A known, accepted rough edge: several of the AI-generated product images had real brand-logo mismatches or generation artifacts, partially fixed by reassigning to the cleanest available images — not pursued further per product decision.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python dependency manager)
- MongoDB Community Server (a plain `mongod` binary)
- Java 11+ and a local [Kafka 4.x](https://kafka.apache.org/downloads) distribution (KRaft mode)
- Docker Desktop (for Redis and the RAG deployment)
- An [OpenRouter](https://openrouter.ai) account + API key (LLM + embeddings) — see the balance/rate-limit notes in [Current status](#current-status) before relying on it for a demo
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

# Optional, demo/testing only - Problem 9 has no real ERP sync to source
# invoices from; this seeds a few so the B2B Receivables Agent has something
# to actually act on
uv run python scripts/seed_b2b_invoices.py

# Credentials
cp .env.example .env
# fill in .env - see .env.example's own comments for where to get each value
```

## Running

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_infra.ps1      # Mongo, Kafka, Redis, RAG deployment
powershell -ExecutionPolicy Bypass -File scripts/start_services.ps1   # backend, orchestrator, poller, all 4 agents
```

Both scripts are idempotent — safe to re-run, they skip anything already up (matching by the real bound port for the 5 HTTP services, and by command line for the 2 headless loop services — a PID recorded in `.run/*.pid` is `uv`'s own wrapper process, not necessarily the one actually holding the resource, a real gotcha found live and fixed in both scripts). To stop the application layer only (leaving infra running): `scripts/stop_services.ps1`. To also stop the Docker-based infra: `scripts/stop_infra.ps1` (mongod/Kafka are native processes with no script-managed stop hook — close their windows or kill by port).

Ports: backend `8000`, Checkout Salvage Agent `9002`, Recurring Revenue Agent `9003`, Conversational NLP Agent `9004`, B2B Receivables Agent `9005`. Each agent spawns its own MCP server subprocesses on first request — nothing extra to start for those.

If you change `.env` (a new API key, a different model), **restart the affected services** — every credentialed client reads its env var once at import time, so a running process keeps using whatever was set when it started.

## Public webhooks (optional)

Razorpay and Meta both need a public HTTPS URL to deliver real webhooks — `localhost:8000` isn't reachable from their servers. Get one with `cloudflared`:

```powershell
cloudflared.exe tunnel --url http://localhost:8000
```

This prints a random `https://<name>.trycloudflare.com` URL (no Cloudflare account needed for this "quick tunnel" mode — it's ephemeral and changes on every restart). Register `<url>/webhooks/razorpay` in Razorpay's Dashboard → Webhooks, and `<url>/webhooks/meta` in the Meta app's WhatsApp → Configuration page (with the same `META_WA_VERIFY_TOKEN` from `.env`).

**Programmatic webhook creation doesn't work for a plain merchant key** — `client.webhook.create`/`.edit`/`.fetch` all require the Partner API's account-scoped path and return `Access Denied` otherwise; `client.webhook.all()` (list) is the one exception, and is genuinely useful for sanity-checking what's registered. Create/edit webhooks via the Dashboard.

**A real, non-obvious gotcha found the hard way**: getting the app's own webhook config right (URL + verify token + subscribed fields) is not sufficient — the WhatsApp Business Account itself must also be explicitly subscribed to your app (`POST /{WABA_ID}/subscribed_apps`), a separate step some test WABAs don't do automatically (ours defaulted to subscribing to a Meta-internal app instead). If inbound messages aren't arriving despite a correct-looking webhook config, check `GET /{WABA_ID}/subscribed_apps` directly.

## Verifying it's working

```powershell
curl http://localhost:8000/health
curl http://localhost:9002/.well-known/agent-card.json
```

To see a real event flow end-to-end: produce a message onto the `revenue-recovery-events` Kafka topic (any Python script using `confluent_kafka.Producer`, shape `{"event_id", "event_type", "problem_id", "payload"}`) and watch `services/orchestrator/main.py`'s own stdout — it logs each dispatch and the agent's response. (Its console runs hidden by `start_services.ps1`'s `-WindowStyle Hidden` — for live stdout, run `uv run python services/orchestrator/main.py` directly in a foreground terminal instead.)

A faster way to check a specific tool/agent without waiting on Kafka: reproduce the exact call the Orchestrator itself would make, using its own dispatch code —

```python
import sys, asyncio
sys.path.insert(0, "libs"); sys.path.insert(0, "services/orchestrator")
from a2a_dispatch import build_instruction, dispatch

instruction = build_instruction("payment.failed", 2, {"order_id": "...", "method": "netbanking", "instrument_key": "HDFC", ...})
result = asyncio.run(dispatch("http://localhost:9002", instruction))
print(result["text"], result["delegation_artifacts"])
```

This is genuinely how most of this project's own live verification was done — real MCP tools, real agent reasoning, real Mongo/Redis state changes, just without needing to wait on a real webhook or Kafka round trip first.

## Tests & CI

```powershell
uv run pytest tests/ -v                  # pure logic, no infra needed
uv run pytest tests_integration/ -v      # needs real Mongo/Redis/Kafka - see tests_integration/README.md
```

Two separate suites, kept in separate directories on purpose:

- **`tests/`** — pure, infra-free logic (`tests.yml` in CI). See `tests/README.md` for exactly what's covered.
- **`tests_integration/`** — real MongoDB constraints (unique/TTL indexes), a real Kafka producer→consumer round trip, and the FastAPI backend's webhook handler genuinely writing to Mongo and publishing to Kafka, all against real ephemeral infra (`integration-tests.yml` in CI, via GitHub Actions service containers — no `docker-compose` needed). Deliberately still stops short of anything needing a real Razorpay/Meta/OpenRouter credential — see `tests_integration/README.md` for the exact boundary and why.

Both run in GitHub Actions on every push/PR. The broader system's fuller verification (every MCP tool, every agent, the full LLM-driven pipeline, real Razorpay/Meta calls) has instead been live-run-and-read by hand, documented in `../Design_Spec_and_Decisions.md`'s changelog, rather than mocked or spent repeatedly against paid APIs in CI.

## Known operational gotchas

Real issues found while actually running this system, not hypothetical — each fixed, but worth knowing if something looks broken:

- **A hidden window has no logs.** Every service started by `start_services.ps1` runs with `-WindowStyle Hidden`, so a crash's real error is invisible unless you re-run that one process in the foreground. Kafka specifically now redirects to `logs/kafka.log`/`logs/kafka-error.log` (`start_infra.ps1`) after this exact blind spot cost real debugging time once.
- **A `uv run <script>` process's recorded PID isn't necessarily the one holding the resource.** `uv` wraps the real Python interpreter in a parent process; killing the wrapper can leave the real process (and whatever port/consumer-group it holds) running as an invisible orphan. Both `start_services.ps1`/`stop_services.ps1` now match by real command line for the 2 headless loop services (Orchestrator, Watchdog Poller) instead of trusting the `.run/*.pid` file, after this exact drift caused 2 duplicate instances of each to silently accumulate across sessions.
- **This Kafka install's data directory lives inside OneDrive.** A real corruption incident (`NullPointerException` in `OffsetIndex.mmap()` on startup, replaying the internal `__cluster_metadata` log) was traced to this — OneDrive's sync agent is documented to interfere with memory-mapped files on Windows. Recovery is the standard, targeted fix: delete only the affected segment's `.index`/`.timeindex` files (never the `.log` itself), let Kafka rebuild them on next start.
- **A Kafka consumer test/script reading from `"latest"` right after subscribing can race the assignment** and miss a message produced immediately after. Reading from `"earliest"` avoids that race but doesn't scale to a long-lived topic's retained history — `tests_integration/kafka_test_utils.py`'s approach (capture each partition's current high-water-mark offset and `assign()` directly to it *before* producing) is the pattern to reach for instead of tuning a poll-count bound.
- **OpenRouter's rate limits are two different things**: a $0-balance account's free-tier models cap at 50 requests/day total (chat + embeddings shared); a funded account instead has an "in-flight request budget" sized to the top-up amount, which the largest-tool-surface agent (Conversational NLP, 3 MCP servers) hits first under concurrent load. Neither resolves by waiting — the first needs the daily UTC-midnight reset or ≥$10 to unlock 1000/day; the second needs a larger top-up.
- **Meta's test WhatsApp number needs recipients allow-listed.** A valid token still gets rejected (`#131030`) for any number not added in the Meta dashboard — confirmed by a direct API call bypassing the LLM/agent layer entirely when a live send looked like it might be an agent-side problem and wasn't.

## What's not built (by design, not oversight)

- A merchant-facing dashboard (recovery-rate tiles, audit-trail drill-down) — explicitly out of scope for every LLD in this project, not just unbuilt.
- Razorpay Smart Collect (Virtual Accounts) needs enabling on the Razorpay account separately — confirmed via a real API call that it's an account-level gap, not a code issue; Problem 9's reconciliation flow needs it for the Virtual-Account-per-invoice mechanism to actually create accounts.
- A real ERP/accounting sync that creates `invoices` documents from a merchant's own systems — out of scope for this project. `scripts/seed_b2b_invoices.py` fills that gap for demo/testing purposes only.
- The VPA-typo/switch-downtime auto-correction idea explored early in design — considered, explicitly dropped by product decision, not silently missing (see `../Design_Spec_and_Decisions.md`, §10).
