"""Seeds demo B2B customers and invoices for Problem 9 (Receivables & Reconciliation).

Real gap this closes (found live, 2026-09-05, while verifying the reverse
two-hop dispute delegation): nothing anywhere in this codebase ever creates
an `invoices` document. Problem 9's tools (gather_decision_context,
execute_action, pause_for_dispute, check_gstin, find_open_invoice_for_customer,
...) all read one but nothing writes one - in the real system this would
come from the merchant's own ERP/accounting sync, which this project
correctly doesn't own or fake with a webhook. This script is that missing
seed step for demo/testing purposes only, not a stand-in for a real
integration.

Each invoice's escalation checkpoints are scheduled for real via the same
schedule_escalation_checkpoints() the (currently unreachable, Smart-Collect-
gated) mint_invoice_virtual_account would normally call - so the real
Watchdog Poller picks these up on its own next tick and drives them through
the real Kafka -> Orchestrator -> B2B Receivables Agent path, the same
live-infra testing discipline used everywhere else in this project, not a
one-off manual dispatch reproduction.

Idempotent: safe to re-run, upserts by customer/invoice id and re-schedules
checkpoints each time (clearing any it already fired, same as a fresh
invoice would).

Run:
    uv run python scripts/seed_b2b_invoices.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CODES_ROOT / "libs"))
sys.path.insert(0, str(_CODES_ROOT / "services" / "mcp-servers" / "prob9_recon"))

from rzp_common.mongo_client import get_db  # noqa: E402
from b2b_watchdog import schedule_escalation_checkpoints  # noqa: E402

NOW = datetime.now(timezone.utc)

# Clearly-fictional demo companies, not real businesses - GSTINs are
# made up in the correct 15-character format (except CUSTOMERS[2]'s invoice,
# deliberately malformed to exercise check_gstin's dispute path).
CUSTOMERS = [
    {
        "razorpay_customer_id": "cust_demo_b2b_1", "name": "Bluepeak Textiles Pvt Ltd",
        "phone": "+919812345001", "company_name": "Bluepeak Textiles Pvt Ltd",
        "gstin": "27AAAPL1234C1Z5", "payment_terms": "net_30",
        "payment_history_summary": {"invoices_total": 14, "invoices_paid_on_time": 11, "average_days_late": 4},
    },
    {
        "razorpay_customer_id": "cust_demo_b2b_2", "name": "Orion Logistics LLP",
        "phone": "+919812345002", "company_name": "Orion Logistics LLP",
        "gstin": "29BBBPL5678D1Z2", "payment_terms": "net_30",
        # A chronically-late payer - justifies this invoice's deep escalation
        # tiers actually firing rather than looking unrealistic.
        "payment_history_summary": {"invoices_total": 6, "invoices_paid_on_time": 2, "average_days_late": 22},
    },
    {
        "razorpay_customer_id": "cust_demo_b2b_3", "name": "Fernhill Consulting Services",
        "phone": "+919812345003", "company_name": "Fernhill Consulting Services",
        "gstin": "07CCCPL9012E1Z8", "payment_terms": "net_15",
        # A reliable payer, on purpose - this customer's invoice below has a
        # GSTIN mismatch, and it should read as a data error, not a pattern
        # of avoidance.
        "payment_history_summary": {"invoices_total": 9, "invoices_paid_on_time": 9, "average_days_late": 0},
    },
]

# due_date_offset_days is relative to NOW (negative = already overdue) -
# schedule_escalation_checkpoints anchors every checkpoint to due_date, so a
# due date far enough in the past means several checkpoints are already due
# and the real Watchdog Poller fires them on its very next tick.
INVOICES = [
    {
        "razorpay_invoice_id": "inv_demo_b2b_1", "customer_id": "cust_demo_b2b_1",
        "amount": 8_500_000, "gstin": "27AAAPL1234C1Z5",  # matches the customer's own
        "due_date_offset_days": -20,  # T-3/T+1/T+7/T+14 all already due
    },
    {
        "razorpay_invoice_id": "inv_demo_b2b_2", "customer_id": "cust_demo_b2b_2",
        "amount": 21_000_000, "gstin": "29BBBPL5678D1Z2",  # matches the customer's own
        "due_date_offset_days": -50,  # T-3 through T+45 all already due
    },
    {
        "razorpay_invoice_id": "inv_demo_b2b_3", "customer_id": "cust_demo_b2b_3",
        "amount": 4_500_000, "gstin": "07CCCPL9012E1Z",  # deliberately 14 chars, not 15 - check_gstin's format check fails
        "due_date_offset_days": -5,  # T-3/T+1 already due
    },
    {
        "razorpay_invoice_id": "inv_demo_b2b_4", "customer_id": "cust_demo_b2b_1",
        "amount": 6_200_000, "gstin": "27AAAPL1234C1Z5",
        "due_date_offset_days": 10,  # not yet due - no checkpoints fire yet, just realistic upcoming data
    },
]


def seed() -> tuple[int, int]:
    db = get_db()

    for customer in CUSTOMERS:
        db.customers.update_one(
            {"razorpay_customer_id": customer["razorpay_customer_id"]},
            {"$set": {**customer, "updated_at": NOW}, "$setOnInsert": {"created_at": NOW}},
            upsert=True,
        )
        print(f"Upserted customer '{customer['name']}' ({customer['razorpay_customer_id']})")

    for invoice in INVOICES:
        due_date = NOW + timedelta(days=invoice["due_date_offset_days"])
        db.invoices.update_one(
            {"razorpay_invoice_id": invoice["razorpay_invoice_id"]},
            {"$set": {
                "customer_id": invoice["customer_id"], "amount": invoice["amount"],
                "gstin": invoice["gstin"], "due_date": due_date, "status": "pending",
                "virtual_account_id": None,  # Smart Collect not enabled on this account - see README
            }, "$setOnInsert": {"created_at": NOW}},
            upsert=True,
        )
        schedule_escalation_checkpoints(invoice["razorpay_invoice_id"], due_date)
        overdue_days = -invoice["due_date_offset_days"]
        status_note = f"{overdue_days}d overdue" if overdue_days > 0 else f"due in {-overdue_days}d"
        print(f"Upserted invoice {invoice['razorpay_invoice_id']} "
              f"(Rs {invoice['amount'] / 100:.2f}, {status_note}) and scheduled its checkpoints")

    return len(CUSTOMERS), len(INVOICES)


if __name__ == "__main__":
    customer_count, invoice_count = seed()
    print(f"\nDone. {customer_count} customer(s), {invoice_count} invoice(s) seeded.")
    print("Any already-due escalation checkpoints will fire on the Watchdog Poller's next tick "
          "(services/watchdog_poller/main.py must be running) and flow through the real "
          "Kafka -> Orchestrator -> B2B Receivables Agent path.")
