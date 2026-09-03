"""FastMCP server for Problem 6 — Penalty-Aware Mandate Retry Sequencer
(soft-decline/NSF path only, per the 2026-09-02 classification split with Problem 5).

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 6.

"Penalty-aware" no longer means pausing Razorpay's own retry cadence (confirmed
impossible via API) - it means never suggesting the same failing instrument
again and never running in parallel with Razorpay's own cascade, only after
subscription.halted. The actual bounded sequence here is the real-world-
grounded exception to the project's general "max 1 outreach" rule.

Run directly:
    uv run python services/mcp-servers/prob6_dunning_sequencer/server.py
"""

import sys
import time
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_agent_kit.wa_templates import build_delegation_artifact  # noqa: E402
from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.redis_client import get_redis  # noqa: E402
from rzp_common.subscription_lifecycle import schedule_dunning_sequence  # noqa: E402
from rzp_razorpay_client.client import create_payment_link  # noqa: E402

mcp = FastMCP("Prob6DunningSequencer")

DUNNING_LINK_EXPIRY_SECONDS = 12 * 86400  # spans the full ~11-day campaign
STAGE_TEMPLATES = {1: "dunning_touch_1", 2: "dunning_touch_2", 3: "dunning_touch_3"}


@mcp.tool()
def start_sequence(subscription_id: str, amount: int, customer_name: str,
                    customer_contact: str) -> dict:
    """Called when subscription.halted fires (Razorpay's own retry cascade
    exhausted). Generates ONE long-lived Payment Link, reused across all
    touches (never regenerated), and schedules all four checkpoints upfront."""
    link = create_payment_link(
        amount=amount,
        currency="INR",
        reference_id=subscription_id,
        description="Reactivate your subscription",
        expire_by=int(time.time()) + DUNNING_LINK_EXPIRY_SECONDS,
        customer={"name": customer_name, "contact": customer_contact},
    )
    schedule_dunning_sequence(subscription_id, link["id"], link["short_url"])
    write_audit_log(
        problem_id=6, tool_name="start_sequence", entity_refs={"subscription_id": subscription_id},
        observation={"trigger": "subscription.halted"},
        decision={"action": "START_DUNNING_SEQUENCE"},
        execution={"status": "scheduled", "dunning_link_id": link["id"]},
        mcp_server="prob6_dunning_sequencer",
    )
    return {"dunning_link_id": link["id"], "short_url": link["short_url"]}


@mcp.tool()
def evaluate_next_touch(subscription_id: str, stage: int) -> dict:
    """Called as each of touch1/touch2/touch3 fires. Checks Problem 7's PTP
    lock first (forward dependency - the key simply won't exist until Problem
    7's server is built, which correctly reads as "no active PTP" rather than
    erroring) and defers this touch entirely if the customer already promised
    a date. Otherwise builds the two-hop delegation artifact for the
    Orchestrator to hand to the Conversational NLP Agent - this tool does not
    send anything itself, Srv8 is exclusively that agent's. Looks up the
    customer's name/phone from the subscription's linked record since this
    tool (unlike start_sequence) is only ever given IDs by the watchdog."""
    r = get_redis()
    if r.exists(f"ptp_active:{subscription_id}"):
        return {"action": "deferred_to_ptp"}

    db = get_db()
    subscription = db.subscriptions.find_one({"razorpay_subscription_id": subscription_id})
    if not subscription or not subscription.get("dunning_link_id"):
        return {"action": "skipped", "reason": "no_active_dunning_sequence"}

    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id}, {"$set": {"dunning_sequence_stage": stage}}
    )
    customer = db.customers.find_one({"razorpay_customer_id": subscription.get("customer_id")}) or {}
    template_id = STAGE_TEMPLATES[stage]
    delegation = build_delegation_artifact(
        subscription.get("customer_id"), customer.get("phone", ""), template_id,
        {"name": customer.get("name") or "Customer", "link": subscription.get("dunning_link_short_url", "")},
    )
    write_audit_log(
        problem_id=6, tool_name="evaluate_next_touch",
        entity_refs={"subscription_id": subscription_id}, observation={"stage": stage},
        decision={"action": "SEND_TOUCH", "template": template_id},
        execution={"status": "delegated"}, mcp_server="prob6_dunning_sequencer",
    )
    return {"action": "send_touch", "template": template_id,
            "dunning_link_id": subscription["dunning_link_id"], **delegation}


@mcp.tool()
def finalize_churn(subscription_id: str) -> dict:
    """Called at the downgrade checkpoint (T+11d) if still unresolved. Transitions
    the subscription to the merchant-configured terminal state - a real, logged
    decision, never a silent drop-off."""
    db = get_db()
    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    terminal_action = config.get("subscription_terminal_action", "paused")

    db.subscriptions.update_one(
        {"razorpay_subscription_id": subscription_id},
        {"$set": {"status": terminal_action, "dunning_sequence_stage": 4}},
    )
    write_audit_log(
        problem_id=6, tool_name="finalize_churn", entity_refs={"subscription_id": subscription_id},
        observation={"trigger": "downgrade_checkpoint_T+11d"},
        decision={"action": "FINALIZE_CHURN", "terminal_action": terminal_action},
        execution={"status": "transitioned"}, mcp_server="prob6_dunning_sequencer",
    )
    return {"subscription_id": subscription_id, "terminal_action": terminal_action}


if __name__ == "__main__":
    mcp.run()
