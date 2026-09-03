"""Shared audit-trail writer for the whole project.

Design reference: Design_Spec_and_Decisions.md, section 5 — every agentic
problem must produce an observation->decision->execution audit JSON for every
action. Reconciliation fix (2026-09-03): Problem 9 originally wrote audit_logs
via its own private _write_audit_log, while Problems 2-8's tools wrote none
at all. This is the one shared writer every problem's MCP tools call for
every state-changing action, matching the full audit_logs schema in
scripts/db_setup.py (not just the observation/decision/execution subset
Problem 9's own version used).

Read-only lookup tools (get_route_status, verify_order_payment,
check_stock_for_recovery, gather_decision_context, check_csw_status,
lookup_active_ptp) deliberately do NOT call this — the audit trail is for
actions/decisions, not every query, matching the design's own
"observation -> decision -> execution" framing.
"""

from datetime import datetime, timezone

from rzp_common.mongo_client import get_db


def write_audit_log(
    problem_id: int,
    tool_name: str,
    entity_refs: dict,
    observation: dict,
    decision: dict,
    execution: dict,
    *,
    agent_name: str | None = None,
    mcp_server: str | None = None,
    stopping_rule_check: dict | None = None,
    policy_engine_check: dict | None = None,
    policy_rag_citation: dict | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    db = get_db()
    db.audit_logs.insert_one({
        "timestamp": datetime.now(timezone.utc),
        "problem_id": problem_id,
        "agent_name": agent_name,
        "mcp_server": mcp_server,
        "tool_name": tool_name,
        "entity_refs": entity_refs,
        "observation": observation,
        "decision": decision,
        "execution": execution,
        "stopping_rule_check": stopping_rule_check,
        "policy_engine_check": policy_engine_check,
        "policy_rag_citation": policy_rag_citation,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
    })
