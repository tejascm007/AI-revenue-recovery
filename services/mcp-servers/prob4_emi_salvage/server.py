"""FastMCP server for Problem 4 — BNPL & High-Ticket EMI Decline Salvage.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 4.

Like Problem 2, this is deterministic — the actual cap enforcement is an
atomic Mongo update, not an LLM decision. Includes the STOP_IF_HARD_REJECT
gap fix: a fraud/risk-rejection decline must block ALL further credit
suggestions outright, not just consume the one suggestion slot like an
ordinary decline (RISK_REJECTED signals the provider's own risk engine
flagged this transaction — suggesting a different provider right after risks
compounding that same signal across providers).

Run directly:
    uv run python services/mcp-servers/prob4_emi_salvage/server.py
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_common.mongo_client import get_db  # noqa: E402

mcp = FastMCP("Prob4EmiSalvage")

HARD_REJECT_REASONS = {"RISK_REJECTED", "FRAUD_SUSPECTED"}
HARD_REJECT_BLOCK_VALUE = 999  # permanently exceeds the {$lt: 1} cap filter


def _suggest_alternate_impl(order_id: str, declined_provider: str, error_reason: str) -> dict:
    db = get_db()
    observation = {"declined_provider": declined_provider, "error_reason": error_reason}

    if error_reason in HARD_REJECT_REASONS:
        db.checkout_sessions.update_one(
            {"_id": order_id},
            {"$set": {"emi_suggestion_count": HARD_REJECT_BLOCK_VALUE},
             "$addToSet": {"emi_declined_providers": declined_provider}},
        )
        result = {
            "suggestion": None,
            "fallback_methods": ["upi", "netbanking"],
            "reason": "hard_reject_blocked",
        }
        write_audit_log(
            problem_id=4, tool_name="suggest_alternate_emi", entity_refs={"order_id": order_id},
            observation=observation, decision={"action": "STOP_IF_HARD_REJECT"},
            execution={"status": "blocked_permanently"}, mcp_server="prob4_emi_salvage",
        )
        return result

    find_result = db.checkout_sessions.find_one_and_update(
        {"_id": order_id, "emi_suggestion_count": {"$lt": 1}},
        {"$inc": {"emi_suggestion_count": 1},
         "$addToSet": {"emi_declined_providers": declined_provider}},
        return_document=True,
    )
    if find_result is None:
        result = {"suggestion": None, "reason": "cap_reached"}
        write_audit_log(
            problem_id=4, tool_name="suggest_alternate_emi", entity_refs={"order_id": order_id},
            observation=observation, decision={"action": "NO_SUGGESTION", "reason": "cap_reached"},
            execution={"status": "no_action"}, mcp_server="prob4_emi_salvage",
        )
        return result

    priority = db.merchant_config.find_one(
        {"_id": "merchant_config"}, {"emi_provider_priority": 1}
    ).get("emi_provider_priority", [])
    declined = set(find_result.get("emi_declined_providers", []))
    for provider in priority:
        if provider not in declined:
            result = {"suggestion": {"provider": provider}, "reason": "suggested_alternate"}
            write_audit_log(
                problem_id=4, tool_name="suggest_alternate_emi", entity_refs={"order_id": order_id},
                observation=observation, decision={"action": "SUGGEST_ALTERNATE", "provider": provider},
                execution={"status": "suggested"}, mcp_server="prob4_emi_salvage",
            )
            return result

    result = {"suggestion": None, "reason": "no_alternate_provider_available"}
    write_audit_log(
        problem_id=4, tool_name="suggest_alternate_emi", entity_refs={"order_id": order_id},
        observation=observation, decision={"action": "NO_SUGGESTION", "reason": "no_alternate_provider_available"},
        execution={"status": "no_action"}, mcp_server="prob4_emi_salvage",
    )
    return result


@mcp.tool()
def suggest_alternate_emi(order_id: str, declined_provider: str, error_reason: str) -> dict:
    """Suggest ONE alternate EMI/BNPL provider after a decline, enforcing the
    one-suggestion-per-checkout cap atomically. A hard risk rejection
    (RISK_REJECTED/FRAUD_SUSPECTED) instead permanently blocks all further
    credit-based suggestions for this checkout and falls back to upfront
    payment methods only — never re-suggests another credit provider right
    after a fraud flag."""
    return _suggest_alternate_impl(order_id, declined_provider, error_reason)


if __name__ == "__main__":
    mcp.run()
