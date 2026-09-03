"""FastMCP server for Problem 2 — Payment Degradation & Smart Route Switching.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 2.

This is the first of the 8 problem-specific FastMCP servers. Its two tools are
deliberately deterministic (no LLM judgment) — the diagnosis-and-decision logic
here is rule evaluation over Redis-held telemetry, not reasoning over ambiguous
input. It still runs through the full A2A/MCP mesh (called by the Checkout
Salvage Agent) for audit-trail consistency with Problems 3/4, which share that
agent and genuinely need LLM reasoning.

Run directly:
    uv run python services/mcp-servers/prob2_route/server.py
"""

import sys
from pathlib import Path

# Make libs/ importable regardless of the current working directory this is
# launched from (a plain script run, not an installed package — same pattern
# every FastMCP server in this project uses).
_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.redis_client import get_redis  # noqa: E402
from telemetry import _route_prefix, prune_and_count  # noqa: E402

mcp = FastMCP("Prob2Route")

# Thresholds decided 2026-09-02 after real-world grounding (Design_Spec_and_Decisions.md,
# section 11, Problem 2) — recalibrated from an initial unverified 30%/15% guess once the
# failure counter was filtered to infra-caused errors only, whose healthy baseline is near
# 0%, not the blended 1-5% baseline that would have justified the original numbers.
MIN_SAMPLE_SIZE = 10
DEGRADE_THRESHOLD = 0.15
RECOVER_THRESHOLD = 0.05  # hysteresis: lower than DEGRADE_THRESHOLD to prevent flapping


def _compute_own_signal_state(prefix: str, attempts: int, failures: int) -> str:
    """Hysteresis state machine for our own rolling failure-rate signal.

    Below the minimum sample size the signal is inconclusive and the prior
    state is kept unchanged (never flips on noise). Above it, a currently
    degraded route only clears once the failure rate drops under the lower
    RECOVER_THRESHOLD; a currently healthy route only degrades once it crosses
    the higher DEGRADE_THRESHOLD. This asymmetry is the fix for a single
    attempt flipping the verdict back and forth right at one boundary value.
    """
    r = get_redis()
    state_key = f"{prefix}:own_signal_state"
    current_state = r.get(state_key) or "healthy"

    if attempts < MIN_SAMPLE_SIZE:
        return current_state

    failure_rate = failures / attempts
    if current_state == "degraded":
        new_state = "healthy" if failure_rate < RECOVER_THRESHOLD else "degraded"
    else:
        new_state = "degraded" if failure_rate > DEGRADE_THRESHOLD else "healthy"

    if new_state != current_state:
        r.set(state_key, new_state)
    return new_state


def _get_route_status_impl(method: str, instrument_key: str) -> dict:
    r = get_redis()
    prefix = _route_prefix(method, instrument_key)

    rzp_status = r.get(f"{prefix}:rzp_status")
    rzp_severity = r.get(f"{prefix}:rzp_severity")
    attempts = prune_and_count(f"{prefix}:attempts")
    failures = prune_and_count(f"{prefix}:failures")

    own_signal_state = _compute_own_signal_state(prefix, attempts, failures)
    degraded = (rzp_status == "degraded") or (own_signal_state == "degraded")

    return {
        "method": method,
        "instrument_key": instrument_key,
        "status": "degraded" if degraded else "healthy",
        "rzp_status": rzp_status or "healthy",
        "rzp_severity": rzp_severity,
        "own_attempts": attempts,
        "own_failures": failures,
        "own_failure_rate": round(failures / attempts, 4) if attempts else None,
        "own_signal_state": own_signal_state,
    }


def _suggest_alternate_impl(customer_id: str, failed_method: str, failed_instrument: str) -> dict:
    db = get_db()
    customer = db.customers.find_one({"razorpay_customer_id": customer_id})
    if not customer:
        return {"suggestion": None, "reason": "customer_not_found"}

    for token in customer.get("vault_tokens", []):
        if token.get("status") != "active":
            continue
        issuer = (token.get("masked") or {}).get("issuer", "unknown")
        if issuer == failed_instrument and token.get("method") == failed_method:
            continue  # don't re-suggest the exact same failing instrument
        alt_status = _get_route_status_impl(token.get("method", "card"), issuer)
        if alt_status["status"] == "healthy":
            return {
                "suggestion": {
                    "type": "saved_token",
                    "token_id": token["token_id"],
                    "masked": token["masked"],
                },
                "reason": "healthy_saved_alternate",
            }

    return {
        "suggestion": {"type": "generic_method_switch", "suggested_method": "upi"},
        "reason": "no_healthy_saved_alternate_found",
    }


@mcp.tool()
def get_route_status(method: str, instrument_key: str) -> dict:
    """Return the combined DEGRADED/HEALTHY verdict for a payment method+instrument.

    Combines Razorpay's own downtime feed (rzp_status, mirrored into Redis by the
    webhook handler) with our own rolling infra-caused failure rate. Deterministic —
    no LLM judgment. instrument_key is the bank code for netbanking, the issuer for
    cards, or the PSP/handle for UPI.
    """
    return _get_route_status_impl(method, instrument_key)


@mcp.tool()
def suggest_alternate_route(customer_id: str, failed_method: str, failed_instrument: str) -> dict:
    """Suggest a healthy alternate payment route for a customer whose selected route
    is degraded. Prefers a saved vault token (Problem 1) with a currently healthy
    status over a generic method switch. Deterministic — no LLM judgment.
    """
    return _suggest_alternate_impl(customer_id, failed_method, failed_instrument)


if __name__ == "__main__":
    mcp.run()
