"""FastMCP server for Problem 9 — B2B Receivables & Reconciliation Agent.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 9.

The fixed action set is enforced here, not left to the LLM's free-form
judgment: execute_action validates against FIXED_ACTIONS and against a
deterministic escalation-tier ceiling (never let a proposed action jump
ahead of what the firing checkpoint's day-offset actually permits) before
executing anything — the same enforcement/justification split used
throughout this project, applied to Problem 9's action set specifically.

Run directly:
    uv run python services/mcp-servers/prob9_recon/server.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_agent_kit.wa_templates import build_delegation_artifact  # noqa: E402
from rzp_common.email_client import send_email  # noqa: E402
from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.redis_client import get_redis  # noqa: E402
from rzp_razorpay_client.client import create_payment_link, create_virtual_account  # noqa: E402
from b2b_watchdog import kill_switch_invoice, schedule_escalation_checkpoints  # noqa: E402

mcp = FastMCP("Prob9Recon")

FIXED_ACTIONS = {
    "SEND_REMINDER", "SEND_PAYMENT_LINK", "WAIT", "SCHEDULE_FOLLOWUP",
    "REQUEST_INVOICE_CONFIRMATION", "PAUSE_FOR_DISPUTE",
    "ESCALATE_TO_PROCUREMENT", "ESCALATE_TO_FINANCE", "MARK_AS_RECOVERED",
}
# Deterministic ceiling: an action requiring escalation cannot fire before
# the schedule's own day-offset for that tier, regardless of what the LLM
# proposes — mirrors merchant_config.b2b_escalation_schedule_days
# ([-3, 1, 7, 14, 30, 45, 60]) without hardcoding a duplicate copy of it.
ESCALATION_MIN_DAY_OFFSET = {"ESCALATE_TO_PROCUREMENT": 14, "ESCALATE_TO_FINANCE": 30}


def _write_audit_log(invoice_id: str, observation: dict, decision: dict, execution: dict,
                      tool_name: str) -> None:
    # Reconciliation fix (2026-09-03): this used to be a private writer using
    # only a subset of audit_logs' schema. Now a thin wrapper over the shared
    # libs/rzp_agent_kit/audit.py writer every other problem's tools use too.
    #
    # Real bug found live (2026-09-05): tool_name used to be hardcoded to
    # "execute_action" here regardless of which tool actually called this
    # wrapper - match_bank_transfer_to_invoice and pause_for_dispute's own
    # audit entries were both silently mislabeled as "execute_action" ever
    # since this wrapper was introduced, discovered only by reading a real
    # pause_for_dispute audit entry back during the reverse two-hop
    # delegation's live verification and noticing the tool_name didn't match
    # what was actually called. Now threaded through explicitly per call
    # site, same as every other problem's tools already do.
    write_audit_log(
        problem_id=9, tool_name=tool_name, entity_refs={"invoice_id": invoice_id},
        observation=observation, decision=decision, execution=execution, mcp_server="prob9_recon",
    )


@mcp.tool()
def mint_invoice_virtual_account(invoice_id: str, amount: int, description: str,
                                  due_date_iso: str, customer_id: str | None = None) -> dict:
    """Creates the per-invoice Virtual Account (the redesigned reconciliation
    mechanism — one VA per invoice, not per customer) and schedules every
    escalation checkpoint upfront, anchored to due_date."""
    va = create_virtual_account(invoice_id, description, customer_id=customer_id)
    due_date = datetime.fromisoformat(due_date_iso)

    db = get_db()
    db.invoices.update_one(
        {"razorpay_invoice_id": invoice_id},
        {"$set": {"virtual_account_id": va["id"], "amount": amount, "due_date": due_date}},
    )
    schedule_escalation_checkpoints(invoice_id, due_date)
    return {"virtual_account_id": va["id"]}


@mcp.tool()
def match_bank_transfer_to_invoice(invoice_id: str, amount_received: int, utr: str) -> dict:
    """TDS-aware reconciliation. A "short" payment within the merchant's
    configured expected TDS% is treated as fully settled, not a shortfall —
    per the design's decision that TDS is a legal fact to configure, not a
    universal system default."""
    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    if invoice is None:
        return {"matched": False, "reason": "invoice_not_found"}

    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    tds_percent = config.get("tds_expected_percent", 0)
    expected_after_tds = invoice["amount"] * (1 - tds_percent / 100)
    tolerance = invoice["amount"] * 0.01  # 1% rounding tolerance, not a TDS substitute

    if amount_received >= expected_after_tds - tolerance:
        kill_switch_invoice(invoice_id)
        db.invoices.update_one({"razorpay_invoice_id": invoice_id}, {"$set": {"status": "recovered"}})
        db.recovery_actions.insert_one({
            "problem_id": 9, "action_type": "MARK_AS_RECOVERED",
            "entity_refs": {"invoice_id": invoice_id}, "amount_at_risk": invoice["amount"],
            "amount_recovered": amount_received, "status": "completed",
            "executed_at": datetime.now(timezone.utc), "audit_log_ref": None,
        })
        _write_audit_log(
            invoice_id,
            {"amount_received": amount_received, "utr": utr, "expected_after_tds": expected_after_tds},
            {"action": "MARK_AS_RECOVERED", "reason": "amount_matched_within_tds_tolerance"},
            {"status": "settled"}, tool_name="match_bank_transfer_to_invoice",
        )
        return {"matched": True, "action": "MARK_AS_RECOVERED"}

    _write_audit_log(
        invoice_id,
        {"amount_received": amount_received, "utr": utr, "expected_after_tds": expected_after_tds},
        {"action": "REQUEST_INVOICE_CONFIRMATION", "reason": "amount_below_expected_after_tds"},
        {"status": "flagged_for_review"}, tool_name="match_bank_transfer_to_invoice",
    )
    return {"matched": False, "action": "REQUEST_INVOICE_CONFIRMATION",
            "shortfall": expected_after_tds - amount_received}


@mcp.tool()
def check_gstin(invoice_id: str) -> dict:
    """Gap fix (2026-09-03): the GSTIN-mismatch stopping-rule flow from the
    original design was dropped in the first LLD pass. Format/record match
    only — no external GST-network validation, per the design's decision."""
    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    if invoice is None or not invoice.get("gstin"):
        return {"valid": None, "reason": "no_gstin_on_invoice"}

    customer = db.customers.find_one({"razorpay_customer_id": invoice.get("customer_id")}) or {}
    gstin = invoice["gstin"]
    format_valid = len(gstin) == 15
    matches_customer_record = customer.get("gstin") == gstin if customer.get("gstin") else True

    if format_valid and matches_customer_record:
        return {"valid": True}

    pause_for_dispute(invoice_id, "gstin_mismatch",
                       f"GSTIN {gstin!r} on invoice does not match merchant record or is malformed.")
    return {"valid": False, "action": "PAUSE_FOR_DISPUTE"}


@mcp.tool()
def find_open_invoice_for_customer(customer_id: str) -> dict:
    """Resolves which invoice a customer is most likely messaging about, for
    the reverse two-hop delegation path (2026-09-05 gap fix): the
    Conversational NLP Agent only ever has customer_id from an inbound
    WhatsApp message, never invoice_id - that's this problem's own data,
    behind the same hard tool-isolation boundary every other cross-problem
    handoff in this project respects. Picks the nearest-due invoice not
    already resolved or disputed; returns found=False rather than guessing
    if the customer has none, so the caller can say so instead of inventing
    an invoice_id."""
    db = get_db()
    invoice = db.invoices.find_one(
        {"customer_id": customer_id, "status": {"$nin": ["recovered", "disputed", "cancelled"]}},
        sort=[("due_date", 1)],
    )
    if invoice is None:
        return {"found": False}
    return {
        "found": True, "invoice_id": invoice["razorpay_invoice_id"],
        "amount": invoice.get("amount"),
        "due_date": invoice["due_date"].isoformat() if invoice.get("due_date") else None,
    }


@mcp.tool()
def pause_for_dispute(invoice_id: str, dispute_type: str, description: str) -> dict:
    """Hard stopping rule — the agent must know when NOT to chase money.
    Clears every remaining escalation checkpoint and creates a billing
    ticket; resumes only after manual correction, never auto-resolved."""
    kill_switch_invoice(invoice_id)
    db = get_db()
    db.disputes.insert_one({
        "invoice_id": invoice_id, "type": dispute_type, "description": description,
        "status": "open", "raised_at": datetime.now(timezone.utc), "resolved_at": None,
    })
    db.invoices.update_one({"razorpay_invoice_id": invoice_id}, {"$set": {"status": "disputed"}})
    _write_audit_log(
        invoice_id, {"dispute_type": dispute_type, "description": description},
        {"action": "PAUSE_FOR_DISPUTE"}, {"status": "collection_paused"}, tool_name="pause_for_dispute",
    )
    return {"status": "paused", "dispute_type": dispute_type}


@mcp.tool()
def gather_decision_context(invoice_id: str) -> dict:
    """Assembles what the LLM needs to decide the next action: days overdue,
    customer payment-history behavior, active PTP lock (Problem 7 dependency),
    active dispute, and which escalation tiers are currently permitted by the
    deterministic ceiling given how many days overdue this invoice is."""
    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    if invoice is None:
        return {"error": "invoice_not_found"}

    days_overdue = (datetime.now(timezone.utc) - invoice["due_date"].replace(tzinfo=timezone.utc)).days
    customer = db.customers.find_one({"razorpay_customer_id": invoice.get("customer_id")}) or {}
    dispute = db.disputes.find_one({"invoice_id": invoice_id, "status": "open"})

    r = get_redis()
    ptp_active = bool(r.get(f"ptp_active:{invoice.get('customer_id')}"))

    return {
        "amount": invoice["amount"], "days_overdue": days_overdue,
        "payment_history_summary": customer.get("payment_history_summary", {}),
        "active_dispute": dispute is not None,
        "active_ptp": ptp_active,
        "allowed_actions": sorted(
            a for a in FIXED_ACTIONS
            if a not in ESCALATION_MIN_DAY_OFFSET or days_overdue >= ESCALATION_MIN_DAY_OFFSET[a]
        ),
    }


@mcp.tool()
def execute_action(invoice_id: str, action: str, reasoning: str) -> dict:
    """Validates the LLM's proposed action against FIXED_ACTIONS and the
    deterministic escalation ceiling before executing anything — an action
    beyond what the invoice's current age permits is rejected outright,
    never silently executed. This is the enforcement point; the LLM only
    ever proposes."""
    if action not in FIXED_ACTIONS:
        raise ValueError(f"Action {action!r} is not in the fixed action set: {FIXED_ACTIONS}")

    context = gather_decision_context(invoice_id)
    if action not in context.get("allowed_actions", []):
        return {"status": "rejected", "reason": "action_exceeds_current_escalation_ceiling"}

    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    execution: dict = {}

    if action == "WAIT" or action == "SCHEDULE_FOLLOWUP" or action == "REQUEST_INVOICE_CONFIRMATION":
        execution = {"status": "logged_no_action"}
    elif action in ("SEND_REMINDER", "SEND_PAYMENT_LINK"):
        # Gap fix (2026-09-03): this used to name templates ("b2b_reminder",
        # "b2b_payment_link") that were never added to prob8's catalog, and
        # never supplied customer_id/phone at all — a real two-hop send would
        # have failed validation with no earlier warning. Now built via the
        # shared build_delegation_artifact(), against real customer contact
        # details looked up from the invoice's linked customer record, the
        # same pattern as Problems 3/5/6.
        customer = db.customers.find_one({"razorpay_customer_id": invoice.get("customer_id")}) or {}
        if action == "SEND_PAYMENT_LINK":
            link = create_payment_link(
                amount=invoice["amount"], currency="INR", reference_id=invoice_id,
                description=f"Payment for invoice {invoice_id}",
                expire_by=int(datetime.now(timezone.utc).timestamp()) + 30 * 86400,
            )
            execution = build_delegation_artifact(
                invoice.get("customer_id"), customer.get("phone", ""), "b2b_payment_link",
                {"name": customer.get("name") or "Customer", "invoice_id": invoice_id,
                 "amount": invoice["amount"], "link": link["short_url"]},
            )
        else:
            execution = build_delegation_artifact(
                invoice.get("customer_id"), customer.get("phone", ""), "b2b_reminder",
                {"name": customer.get("name") or "Customer", "invoice_id": invoice_id,
                 "amount": invoice["amount"]},
            )
    elif action in ("ESCALATE_TO_PROCUREMENT", "ESCALATE_TO_FINANCE"):
        config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
        contact_key = "procurement" if action == "ESCALATE_TO_PROCUREMENT" else "finance"
        channel = (config.get("escalation_contacts") or {}).get(contact_key, "email")
        # Gap fix (2026-09-03): internal escalation is explicitly NOT the
        # WhatsApp channel — that's reserved for customer-facing communication.
        citation = ""
        if action == "ESCALATE_TO_FINANCE" and config.get("msme_registered"):
            citation = ("Section 43B(h), Income Tax Act - payment beyond 45 days to an "
                        "MSME-registered supplier risks disallowance of the buyer's tax "
                        "deduction on this expense.")  # hardcoded citation, not RAG-retrieved
        try:
            send_email(f"{contact_key}@merchant.internal",
                       f"Invoice {invoice_id} needs {contact_key} attention",
                       f"{reasoning}\n\n{citation}")
            execution = {"status": "escalated", "channel": channel}
        except RuntimeError as exc:
            execution = {"status": "escalation_email_unavailable", "error": str(exc)}
    elif action == "MARK_AS_RECOVERED":
        kill_switch_invoice(invoice_id)
        db.invoices.update_one({"razorpay_invoice_id": invoice_id}, {"$set": {"status": "recovered"}})
        execution = {"status": "settled"}
    elif action == "PAUSE_FOR_DISPUTE":
        return pause_for_dispute(invoice_id, "unspecified", reasoning)

    _write_audit_log(invoice_id, context, {"action": action, "reasoning": reasoning}, execution,
                      tool_name="execute_action")
    return {"status": "executed", "action": action, **execution}


@mcp.tool()
def fetch_and_resend_document(invoice_id: str) -> dict:
    """Gap fix (2026-09-03): the original design specifically called out
    answering "can you resend the invoice" by fetching and attaching the
    document automatically. Scoped down honestly — no PDF generation exists
    yet, so this returns a structured summary the Conversational NLP Agent
    can send as text, not an actual attached file."""
    db = get_db()
    invoice = db.invoices.find_one({"razorpay_invoice_id": invoice_id})
    if invoice is None:
        return {"found": False}
    return {
        "found": True,
        "summary": f"Invoice {invoice_id}: amount Rs {invoice['amount']/100:.2f}, "
                    f"due {invoice['due_date'].date().isoformat()}, status {invoice['status']}",
    }


if __name__ == "__main__":
    mcp.run()
