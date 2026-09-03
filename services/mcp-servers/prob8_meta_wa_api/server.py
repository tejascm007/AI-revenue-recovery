"""FastMCP server for Problem 8 — Hinglish WhatsApp Conversational Recovery.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 8.

This is the ONE place in the whole project that actually sends a WhatsApp
message — every other problem (3, 5, 6, 7, 9) reaches this capability only
via the two-hop A2A delegation through the Orchestrator, never a direct MCP
connection, per the corrected cross-agent delegation pattern (section 3).

Run directly:
    uv run python services/mcp-servers/prob8_meta_wa_api/server.py
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_agent_kit.wa_templates import TEMPLATE_CATALOG  # noqa: E402
from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.redis_client import get_redis  # noqa: E402
from rzp_meta_wa_client.client import send_template_message, send_text_message  # noqa: E402

mcp = FastMCP("Prob8MetaWaApi")

CSW_WINDOW_SECONDS = 24 * 3600
NEW_ACCOUNT_TIER_LIMIT = 250  # Meta's starting tier for a new Business Portfolio
# TEMPLATE_CATALOG now lives in libs/rzp_agent_kit/wa_templates.py — shared with
# every producer tool that builds a two-hop delegation artifact naming one of
# these templates, so the two copies can never drift apart (the concrete bug
# this fixed: Problem 9 referenced templates that were never added here).


def _format_template_var(value) -> str:
    """A variable arriving via the two-hop delegation path (verified,
    2026-09-03) has been round-tripped through a protobuf google.protobuf.Struct,
    whose Value type has no integer variant — a paise amount like 50000 comes
    back as 50000.0. Rendered as-is that reads as "Rs 50000.0" in the actual
    outbound message; whole-number floats are coerced back to int first so a
    direct MCP call and a delegated one produce byte-identical message text."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@mcp.tool()
def check_csw_status(customer_id: str | None, phone: str) -> dict:
    """Whether a free-form (non-template) send is currently allowed — the
    customer must have messaged us, or replied to a template, within the
    last 24 hours. Gap fix (2026-09-03, found by a real inbound WhatsApp
    message from a sender with no matching customer record): customer_id
    may be None for an unresolvable/guest sender, same as
    send_whatsapp_message's existing fallback - falls back to phone as the
    identity key, matching how meta_webhooks.py set this same key on
    inbound (customer_id or phone), so the two sides can never disagree on
    which key to check."""
    r = get_redis()
    identity_key = customer_id or phone
    open_until = r.get(f"csw_open_until:{identity_key}")
    return {"csw_open": bool(open_until), "csw_open_until": open_until}


def _check_and_record_quota() -> dict:
    """Rolling 24h count of unique customers messaged via template, against
    Meta's starting-tier limit. Real accounts unlock higher tiers based on
    business verification and sustained quality — this only enforces the
    conservative starting tier, not a merchant-configurable override, since
    the actual tier is Meta's own determination, not ours to set."""
    r = get_redis()
    r.zremrangebyscore("wa_quota_window", 0, time.time() - CSW_WINDOW_SECONDS)
    count = r.zcard("wa_quota_window")
    return {"count": count, "limit": NEW_ACCOUNT_TIER_LIMIT, "near_limit": count >= NEW_ACCOUNT_TIER_LIMIT}


@mcp.tool()
def send_whatsapp_message(customer_id: str | None, phone: str, template_id: str,
                           variables: dict) -> dict:
    """The one send path for the whole project. Business-initiated sends
    (outside CSW) always use the pre-approved template regardless of what
    check_csw_status says; free-form is never used for a proactive send even
    when the window happens to be open, since every use case here is a
    structured template already. Records to `communications` regardless of
    outcome. customer_id may be None for a guest checkout (Problem 3) — quota
    tracking falls back to phone as the identity key in that case, since a
    None key can't dedupe anything."""
    if template_id not in TEMPLATE_CATALOG:
        raise ValueError(f"Unknown template_id: {template_id!r}")

    required_vars = TEMPLATE_CATALOG[template_id]
    missing = [v for v in required_vars if v not in variables]
    if missing:
        raise ValueError(f"Missing required template variables: {missing}")

    identity_key = customer_id or phone
    quota = _check_and_record_quota()
    db = get_db()
    now = datetime.now(timezone.utc)

    if quota["near_limit"]:
        db.communications.insert_one({
            "customer_id": customer_id, "channel": "whatsapp", "direction": "outbound",
            "template_id": template_id, "free_text": None, "entity_refs": None,
            "sent_at": now, "delivered_at": None, "meta_message_id": None,
            "quiet_hours_check": None, "frequency_cap_check": {"queued": True, "reason": "near_tier_limit"},
        })
        write_audit_log(
            problem_id=8, tool_name="send_whatsapp_message",
            entity_refs={"customer_id": customer_id, "phone": phone},
            observation={"template_id": template_id, "quota": quota},
            decision={"action": "QUEUE", "reason": "near_tier_limit"},
            execution={"status": "queued"}, mcp_server="prob8_meta_wa_api",
        )
        return {"status": "queued", "reason": "near_tier_limit"}

    body_parameters = [_format_template_var(variables[v]) for v in required_vars]
    result = send_template_message(phone, template_id, body_parameters=body_parameters)

    r = get_redis()
    r.zadd("wa_quota_window", {identity_key: time.time()})

    db.communications.insert_one({
        "customer_id": customer_id, "channel": "whatsapp", "direction": "outbound",
        "template_id": template_id, "free_text": None, "entity_refs": None,
        "sent_at": now, "delivered_at": None, "meta_message_id": result.get("messages", [{}])[0].get("id"),
        "quiet_hours_check": None, "frequency_cap_check": {"queued": False},
    })
    write_audit_log(
        problem_id=8, tool_name="send_whatsapp_message",
        entity_refs={"customer_id": customer_id, "phone": phone},
        observation={"template_id": template_id, "variables": variables},
        decision={"action": "SEND"}, execution={"status": "sent"},
        mcp_server="prob8_meta_wa_api",
    )
    return {"status": "sent", "meta_message_id": result.get("messages", [{}])[0].get("id")}


@mcp.tool()
def send_freeform_reply(customer_id: str | None, phone: str, text: str) -> dict:
    """Free-form reply within an open CSW — e.g. answering a customer's
    follow-up question, or the PTP acknowledgment. Refuses outside the
    window rather than silently falling back to a template, since a template
    can't carry arbitrary conversational text anyway. customer_id may be
    None for an unresolvable/guest sender (gap fix, 2026-09-03)."""
    csw = check_csw_status(customer_id, phone)
    if not csw["csw_open"]:
        raise RuntimeError(
            f"CSW is not open for customer {customer_id} - cannot send free-form "
            "text. This must be a business-initiated send, which requires "
            "send_whatsapp_message with an approved template instead."
        )
    result = send_text_message(phone, text)
    db = get_db()
    db.communications.insert_one({
        "customer_id": customer_id, "channel": "whatsapp", "direction": "outbound",
        "template_id": None, "free_text": text, "entity_refs": None,
        "sent_at": datetime.now(timezone.utc), "delivered_at": None,
        "meta_message_id": result.get("messages", [{}])[0].get("id"),
        "quiet_hours_check": None, "frequency_cap_check": None,
    })
    write_audit_log(
        problem_id=8, tool_name="send_freeform_reply",
        entity_refs={"customer_id": customer_id, "phone": phone},
        observation={"csw_open": True}, decision={"action": "SEND_FREEFORM"},
        execution={"status": "sent"}, mcp_server="prob8_meta_wa_api",
    )
    return {"status": "sent", "meta_message_id": result.get("messages", [{}])[0].get("id")}


if __name__ == "__main__":
    mcp.run()
