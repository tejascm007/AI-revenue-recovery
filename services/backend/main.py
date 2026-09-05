"""FastAPI backend - Design_Spec_and_Decisions.md, section 3's master
architecture diagram: "Razorpay Webhooks/Sync -> FastAPI Backend -> raw JSON
dump -> MongoDB -> Kafka Event -> Main Orchestrator". Also owns Problem 1's
vault API and the checkout S2S API (order creation), per section 4's tech
stack table.

Run:
    uv run python services/backend/main.py
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling imports: event_derivation, vault_store, etc.
sys.path.insert(0, str(_CODES_ROOT / "libs"))
# Cross-service imports (intentional - see telemetry.py/watchdog.py's own
# docstrings, both written expecting "the shared webhook handler, services/backend"
# to import them directly rather than duplicating their Redis-state logic).
sys.path.insert(0, str(_CODES_ROOT / "services" / "mcp-servers" / "prob2_route"))
sys.path.insert(0, str(_CODES_ROOT / "services" / "mcp-servers" / "prob3_otp_watch"))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api.checkout import router as checkout_router  # noqa: E402
from api.meta_webhooks import router as meta_webhooks_router  # noqa: E402
from api.vault import router as vault_router  # noqa: E402
from api.webhooks import router as webhooks_router  # noqa: E402

PORT = 8000

app = FastAPI(title="AI Revenue Recovery Engine - Backend")

# Wide open, no credentials (this API takes no cookies/session auth - every
# call is a plain stateless JSON request, or a webhook signature-verified on
# its own terms) - needed once the browser-based merchant storefront started
# calling /api/checkout/orders and /api/vault/* from a different origin than
# this backend's own. Real production hardening would scope this to the
# storefront's actual domain, but that domain doesn't exist yet at this
# stage of the project.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(webhooks_router)
app.include_router(meta_webhooks_router)
app.include_router(vault_router)
app.include_router(checkout_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
