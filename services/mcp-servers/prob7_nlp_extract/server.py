"""FastMCP server for Problem 7 — Promise-to-Pay (PTP) Tracker & NLP Intent
Extractor.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 7.

Intent/sentiment classification is the agent's own direct LLM generation, NOT
an MCP tool — Srv7's tools are the deterministic state actions that follow
it: lookup_active_ptp, set_ptp_lock (which internally does the temporal
resolution via temporal_resolver.py, corrected after testing revealed
dateparser alone wasn't reliable for this), clear_ptp_lock, escalate_for_review.

Run directly:
    uv run python services/mcp-servers/prob7_nlp_extract/server.py
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.redis_client import get_redis  # noqa: E402
from temporal_resolver import resolve  # noqa: E402

mcp = FastMCP("Prob7NlpExtract")

PTP_CAP_DAYS = 14


@mcp.tool()
def lookup_active_ptp(subscription_id: str) -> dict:
    """Fast check for an active PTP lock — reads the Redis mirror first
    (what Problem 6's evaluate_next_touch also checks), falling back to Mongo
    if the mirror is somehow absent but a Mongo record says otherwise."""
    r = get_redis()
    cached = r.get(f"ptp_active:{subscription_id}")
    if cached:
        return {"active": True, "resolved_date": cached}

    db = get_db()
    doc = db.ptp.find_one({"subscription_id": subscription_id, "status": "active"})
    if doc:
        return {"active": True, "resolved_date": doc["resolved_date"].isoformat()}
    return {"active": False}


@mcp.tool()
def set_ptp_lock(subscription_id: str, customer_id: str, raw_message: str,
                  raw_temporal_expression: str, sentiment: str,
                  confidence: float, source_communication_id: str | None = None) -> dict:
    """Resolve the raw temporal expression deterministically and, if it
    resolves within the 14-day cap, lock the PTP state and suppress further
    dunning until that date. Returns status "locked", "ambiguous" (never
    guessed — the caller should send one clarifying question), or
    "beyond_cap" (routed to soft downgrade instead of held open-ended)."""
    now = datetime.now(timezone.utc)
    result = resolve(raw_temporal_expression, now.replace(tzinfo=None))

    if not result["resolved"]:
        db = get_db()
        db.ptp.insert_one({
            "customer_id": customer_id, "subscription_id": subscription_id,
            "source_communication_id": source_communication_id, "raw_message": raw_message,
            "extracted_intent": "PROMISE_TO_PAY", "raw_temporal_expression": raw_temporal_expression,
            "resolved_date": None, "resolution_method": None, "confidence": confidence,
            "sentiment": sentiment, "status": "ambiguous_pending_clarification",
            "grace_period_hours": 24, "created_at": now, "resolved_at": None,
        })
        return {"status": "ambiguous", "reason": result["reason"]}

    resolved_date = result["date"]
    if (resolved_date - now.replace(tzinfo=None)).days > PTP_CAP_DAYS:
        return {"status": "beyond_cap", "resolved_date": resolved_date.isoformat()}

    db = get_db()
    db.ptp.insert_one({
        "customer_id": customer_id, "subscription_id": subscription_id,
        "source_communication_id": source_communication_id, "raw_message": raw_message,
        "extracted_intent": "PROMISE_TO_PAY", "raw_temporal_expression": raw_temporal_expression,
        "resolved_date": resolved_date, "resolution_method": result["method"], "confidence": confidence,
        "sentiment": sentiment, "status": "active", "grace_period_hours": 24,
        "created_at": now, "resolved_at": None,
    })

    r = get_redis()
    ttl_seconds = int((resolved_date - now.replace(tzinfo=None)).total_seconds()) + (24 * 3600) + 3600
    r.set(f"ptp_active:{subscription_id}", resolved_date.isoformat(), ex=max(ttl_seconds, 60))
    r.zadd("watchdog_queue", {f"{subscription_id}:ptp_due": time.mktime(resolved_date.timetuple())})

    needs_escalation = sentiment == "HOSTILE"
    return {
        "status": "locked",
        "resolved_date": resolved_date.isoformat(),
        "resolution_method": result["method"],
        "escalation_needed": needs_escalation,
    }


@mcp.tool()
def clear_ptp_lock(subscription_id: str, outcome: str) -> dict:
    """outcome: "fulfilled" | "broken" | "expired". Clears the Redis mirror and
    updates the Mongo record — called on early payment (kill-switch pattern),
    on-time payment at the due-date check, or after the grace period passes
    unfulfilled."""
    r = get_redis()
    r.delete(f"ptp_active:{subscription_id}")

    db = get_db()
    db.ptp.update_one(
        {"subscription_id": subscription_id, "status": "active"},
        {"$set": {"status": outcome, "resolved_at": datetime.now(timezone.utc)}},
    )
    return {"subscription_id": subscription_id, "outcome": outcome}


@mcp.tool()
def escalate_for_review(subscription_id: str, reason: str) -> dict:
    """HOSTILE sentiment always escalates to human review regardless of
    whether a PTP lock was also successfully set — never let a visibly upset
    customer proceed through full automation unchecked."""
    db = get_db()
    db.ptp.update_one(
        {"subscription_id": subscription_id, "status": {"$in": ["active", "ambiguous_pending_clarification"]}},
        {"$set": {"escalated": True, "escalation_reason": reason}},
    )
    return {"subscription_id": subscription_id, "escalated": True, "reason": reason}


if __name__ == "__main__":
    mcp.run()
