"""
End-to-end MongoDB database setup for the AI Revenue Recovery Engine.

Consolidates every collection designed across Problems 1-9 and the cross-cutting
RAG layer (see Design_Spec_and_Decisions.md, section 11) into one script:
creates each collection with a $jsonSchema validator (validationLevel="moderate"
so it guides future writes without breaking on partial/evolving documents during
development) and the indexes decided in each problem's LLD.

Idempotent: safe to re-run. Existing collections are left as-is (schema/indexes
are still (re)ensured); nothing is dropped.

Usage:
    python scripts/db_setup.py
"""

import sys

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid

# Windows consoles default to cp1252, which can't encode the ₹ sign used in a
# couple of print statements below — force UTF-8 so this runs the same on any OS.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "revenue_recovery"

DAY = 86400
NINETY_DAYS = 90 * DAY
SEVEN_DAYS = 7 * DAY


def ensure_collection(db, name, validator=None):
    # Bug fix (2026-09-03): the original version only applied `validator` when
    # CREATING a collection - re-running after adding a field to an already-
    # existing collection's schema (e.g. ptp's "escalated"/"escalation_reason")
    # silently left the live validator stale, the same class of "new field
    # doesn't get applied to an existing document/collection" gap already
    # fixed once for merchant_config's seeding. `collMod` applies the current
    # validator to an existing collection too, so schema changes always land.
    if name in db.list_collection_names():
        if validator:
            db.command({
                "collMod": name,
                "validator": {"$jsonSchema": validator},
                "validationLevel": "moderate",
                "validationAction": "warn",
            })
        print(f"  - '{name}' already exists (validator updated via collMod, indexes still ensured)")
        return db[name]
    kwargs = {}
    if validator:
        kwargs["validator"] = {"$jsonSchema": validator}
        kwargs["validationLevel"] = "moderate"
        kwargs["validationAction"] = "warn"  # warn, don't reject, while the design is still evolving
    try:
        coll = db.create_collection(name, **kwargs)
        print(f"  - created '{name}'")
    except CollectionInvalid:
        coll = db[name]
        print(f"  - '{name}' already exists (race with a prior run)")
    return coll


def main():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    print(f"Connected to {MONGO_URI} -> database '{DB_NAME}'")

    # ---------------------------------------------------------------
    # Permanent collections (system of record)
    # ---------------------------------------------------------------

    print("\n[Permanent] customers  (Problem 1 vault + Problem 9 B2B extension)")
    customers = ensure_collection(db, "customers", {
        "bsonType": "object",
        "required": ["razorpay_customer_id", "phone", "created_at"],
        "properties": {
            "razorpay_customer_id": {"bsonType": "string"},
            "phone": {"bsonType": "string"},
            "email": {"bsonType": ["string", "null"]},
            "name": {"bsonType": ["string", "null"]},
            "vault_tokens": {"bsonType": "array"},
            "saved_vpas": {"bsonType": "array"},
            "company_name": {"bsonType": ["string", "null"]},
            "gstin": {"bsonType": ["string", "null"]},
            "payment_terms": {"bsonType": ["string", "null"]},
            "payment_history_summary": {"bsonType": ["object", "null"]},
            "created_at": {"bsonType": "date"},
            "updated_at": {"bsonType": ["date", "null"]},
        },
    })
    customers.create_index([("razorpay_customer_id", ASCENDING)], unique=True, name="uniq_razorpay_customer_id")
    customers.create_index([("phone", ASCENDING)], unique=True, name="uniq_phone")
    customers.create_index([("vault_tokens.token_id", ASCENDING)], name="idx_vault_token_id")

    print("\n[Permanent] invoices  (Problems 5/6/9)")
    invoices = ensure_collection(db, "invoices", {
        "bsonType": "object",
        "required": ["razorpay_invoice_id", "customer_id", "amount", "status", "created_at"],
        "properties": {
            "razorpay_invoice_id": {"bsonType": "string"},
            "customer_id": {"bsonType": "string"},
            "type": {"bsonType": ["string", "null"]},
            "amount": {"bsonType": ["int", "long", "double"]},
            "tax_amount": {"bsonType": ["int", "long", "double", "null"]},
            "gstin": {"bsonType": ["string", "null"]},
            "gstin_verified": {"bsonType": ["bool", "null"]},
            "po_number": {"bsonType": ["string", "null"]},
            "status": {"bsonType": "string"},
            "virtual_account_id": {"bsonType": ["string", "null"]},
            "escalation_stage_completed": {"bsonType": ["array", "null"]},
            "gst_compliant": {"bsonType": ["bool", "null"]},
            "due_date": {"bsonType": ["date", "null"]},
            "created_at": {"bsonType": "date"},
        },
    })
    invoices.create_index([("razorpay_invoice_id", ASCENDING)], unique=True, name="uniq_razorpay_invoice_id")
    invoices.create_index([("customer_id", ASCENDING)], name="idx_customer_id")
    invoices.create_index([("virtual_account_id", ASCENDING)], name="idx_virtual_account_id")
    invoices.create_index([("status", ASCENDING), ("due_date", ASCENDING)], name="idx_status_due_date")

    print("\n[Permanent] payments  (all problems)")
    payments = ensure_collection(db, "payments", {
        "bsonType": "object",
        "required": ["razorpay_payment_id", "amount", "status"],
        "properties": {
            "razorpay_payment_id": {"bsonType": "string"},
            "order_id": {"bsonType": ["string", "null"]},
            "invoice_id": {"bsonType": ["string", "null"]},
            "subscription_id": {"bsonType": ["string", "null"]},
            "customer_id": {"bsonType": ["string", "null"]},
            "amount": {"bsonType": ["int", "long", "double"]},
            "status": {"bsonType": "string"},
            "method": {"bsonType": ["string", "null"]},
            "error_code": {"bsonType": ["string", "null"]},
            "error_description": {"bsonType": ["string", "null"]},
            "error_source": {"bsonType": ["string", "null"]},
            "error_step": {"bsonType": ["string", "null"]},
            "error_reason": {"bsonType": ["string", "null"]},
            "acquirer_data": {"bsonType": ["object", "null"]},
            "token_id": {"bsonType": ["string", "null"]},
            "captured_at": {"bsonType": ["date", "null"]},
        },
    })
    payments.create_index([("razorpay_payment_id", ASCENDING)], unique=True, name="uniq_razorpay_payment_id")
    payments.create_index([("order_id", ASCENDING)], name="idx_order_id")
    payments.create_index([("invoice_id", ASCENDING)], name="idx_invoice_id")
    payments.create_index([("subscription_id", ASCENDING)], name="idx_subscription_id")
    payments.create_index([("customer_id", ASCENDING)], name="idx_customer_id")

    print("\n[Permanent] subscriptions  (Problems 5/6, backbone of the classification split)")
    subscriptions = ensure_collection(db, "subscriptions", {
        "bsonType": "object",
        "required": ["razorpay_subscription_id", "customer_id", "status"],
        "properties": {
            "razorpay_subscription_id": {"bsonType": "string"},
            "customer_id": {"bsonType": "string"},
            "plan_id": {"bsonType": ["string", "null"]},
            # Gap fix (2026-09-03): nothing populated this document at all until
            # the backend's webhook handler was built - and nothing ever stored
            # the recurring amount, which classify_decline's AFA-threshold
            # heuristic and generate_hard_decline_link both require. Populated
            # from subscription.activated/subscription.charged's paired payment
            # entity (the amount actually charged that cycle).
            "amount": {"bsonType": ["int", "long", "double", "null"]},
            "status": {"bsonType": "string"},
            "auth_attempts": {"bsonType": ["int", "null"]},
            "current_cycle_start": {"bsonType": ["date", "null"]},
            "current_cycle_end": {"bsonType": ["date", "null"]},
            "last_charge_payment_id": {"bsonType": ["string", "null"]},
            "current_cycle_decline_classification": {"bsonType": ["string", "null"]},
            "hard_decline_link_sent": {"bsonType": ["bool", "null"]},
            "hard_decline_link_scheduled_at": {"bsonType": ["date", "null"]},
            "current_cycle_payment_ids": {"bsonType": ["array", "null"]},
            "dunning_sequence_stage": {"bsonType": ["int", "null"]},
            "dunning_link_id": {"bsonType": ["string", "null"]},
            # Gap fix (2026-09-03): only the Razorpay entity ID was stored, not
            # the short_url itself — evaluate_next_touch needs the actual link
            # to build each touch's WhatsApp delegation artifact and had no way
            # to recover it without this.
            "dunning_link_short_url": {"bsonType": ["string", "null"]},
            "dunning_started_at": {"bsonType": ["date", "null"]},
            "terminal_action_at": {"bsonType": ["date", "null"]},
        },
    })
    subscriptions.create_index([("razorpay_subscription_id", ASCENDING)], unique=True, name="uniq_razorpay_subscription_id")
    subscriptions.create_index([("customer_id", ASCENDING)], name="idx_customer_id")
    subscriptions.create_index([("status", ASCENDING)], name="idx_status")

    print("\n[Permanent] communications  (all outbound/inbound WhatsApp, Problem 8 system of record)")
    communications = ensure_collection(db, "communications", {
        "bsonType": "object",
        "required": ["channel", "direction", "sent_at"],
        "properties": {
            # Nullable (gap fix, 2026-09-03): a Problem 3 recovery send can be
            # for a guest checkout with no razorpay_customer_id yet, mirroring
            # the same nullability already on checkout_sessions/payments.
            # send_whatsapp_message falls back to phone as the identity key
            # for quota-tracking when this is null.
            "customer_id": {"bsonType": ["string", "null"]},
            "channel": {"bsonType": "string"},
            "direction": {"bsonType": "string"},
            "template_id": {"bsonType": ["string", "null"]},
            "free_text": {"bsonType": ["string", "null"]},
            "entity_refs": {"bsonType": ["object", "null"]},
            "sent_at": {"bsonType": "date"},
            "delivered_at": {"bsonType": ["date", "null"]},
            "meta_message_id": {"bsonType": ["string", "null"]},
            "quiet_hours_check": {"bsonType": ["object", "null"]},
            "frequency_cap_check": {"bsonType": ["object", "null"]},
        },
    })
    communications.create_index([("customer_id", ASCENDING)], name="idx_customer_id")
    communications.create_index([("sent_at", DESCENDING)], name="idx_sent_at")

    print("\n[Permanent] ptp  (Problem 7 Promise-to-Pay)")
    ptp = ensure_collection(db, "ptp", {
        "bsonType": "object",
        "required": ["customer_id", "extracted_intent", "status", "created_at"],
        "properties": {
            "customer_id": {"bsonType": "string"},
            "subscription_id": {"bsonType": ["string", "null"]},
            "invoice_id": {"bsonType": ["string", "null"]},
            "source_communication_id": {"bsonType": ["string", "null"]},
            "raw_message": {"bsonType": ["string", "null"]},
            "extracted_intent": {"bsonType": "string"},
            "raw_temporal_expression": {"bsonType": ["string", "null"]},
            "resolved_date": {"bsonType": ["date", "null"]},
            "resolution_method": {"bsonType": ["string", "null"]},
            "confidence": {"bsonType": ["double", "null"]},
            "sentiment": {"bsonType": ["string", "null"]},
            "status": {"bsonType": "string"},
            "grace_period_hours": {"bsonType": ["int", "null"]},
            "created_at": {"bsonType": "date"},
            "resolved_at": {"bsonType": ["date", "null"]},
            "escalated": {"bsonType": ["bool", "null"]},
            "escalation_reason": {"bsonType": ["string", "null"]},
        },
    })
    ptp.create_index([("subscription_id", ASCENDING)], name="idx_subscription_id")
    ptp.create_index([("status", ASCENDING)], name="idx_status")
    ptp.create_index([("resolved_date", ASCENDING)], name="idx_resolved_date")

    print("\n[Permanent] disputes  (Problem 9, GSTIN mismatch + line-item disputes)")
    disputes = ensure_collection(db, "disputes", {
        "bsonType": "object",
        "required": ["type", "status", "raised_at"],
        "properties": {
            "invoice_id": {"bsonType": ["string", "null"]},
            "customer_id": {"bsonType": ["string", "null"]},
            "type": {"bsonType": "string"},
            "description": {"bsonType": ["string", "null"]},
            "status": {"bsonType": "string"},
            "raised_at": {"bsonType": "date"},
            "resolved_at": {"bsonType": ["date", "null"]},
            "billing_ticket_ref": {"bsonType": ["string", "null"]},
        },
    })
    disputes.create_index([("invoice_id", ASCENDING)], name="idx_invoice_id")
    disputes.create_index([("customer_id", ASCENDING)], name="idx_customer_id")
    disputes.create_index([("status", ASCENDING)], name="idx_status")

    print("\n[Permanent] recovery_actions  (the business-level ₹-recovered ledger, all problems)")
    recovery_actions = ensure_collection(db, "recovery_actions", {
        "bsonType": "object",
        "required": ["problem_id", "action_type", "status", "executed_at"],
        "properties": {
            "problem_id": {"bsonType": ["int", "string"]},
            "action_type": {"bsonType": "string"},
            "entity_refs": {"bsonType": ["object", "null"]},
            "amount_at_risk": {"bsonType": ["int", "long", "double", "null"]},
            "amount_recovered": {"bsonType": ["int", "long", "double", "null"]},
            "status": {"bsonType": "string"},
            "executed_at": {"bsonType": "date"},
            "audit_log_ref": {"bsonType": ["string", "null"]},
        },
    })
    recovery_actions.create_index([("problem_id", ASCENDING)], name="idx_problem_id")
    recovery_actions.create_index([("executed_at", DESCENDING)], name="idx_executed_at")

    print("\n[Permanent] audit_logs  (append-only, 'the bar' requirement, every agentic decision)")
    audit_logs = ensure_collection(db, "audit_logs", {
        "bsonType": "object",
        "required": ["timestamp", "problem_id"],
        "properties": {
            "timestamp": {"bsonType": "date"},
            "problem_id": {"bsonType": ["int", "string"]},
            "agent_name": {"bsonType": ["string", "null"]},
            "mcp_server": {"bsonType": ["string", "null"]},
            "tool_name": {"bsonType": ["string", "null"]},
            "entity_refs": {"bsonType": ["object", "null"]},
            "observation": {"bsonType": ["object", "null"]},
            "decision": {"bsonType": ["object", "null"]},
            "execution": {"bsonType": ["object", "null"]},
            "stopping_rule_check": {"bsonType": ["object", "null"]},
            "policy_engine_check": {"bsonType": ["object", "null"]},
            "policy_rag_citation": {"bsonType": ["object", "null"]},
            "correlation_id": {"bsonType": ["string", "null"]},
            "idempotency_key": {"bsonType": ["string", "null"]},
        },
    })
    audit_logs.create_index([("correlation_id", ASCENDING)], name="idx_correlation_id")
    audit_logs.create_index([("timestamp", DESCENDING)], name="idx_timestamp")
    audit_logs.create_index([("problem_id", ASCENDING)], name="idx_problem_id")

    print("\n[Permanent] merchant_config  (consolidated config: AFA threshold, dunning spacing, "
          "PTP grace period, B2B escalation schedule, MSME/TDS settings)")
    merchant_config = ensure_collection(db, "merchant_config", {
        "bsonType": "object",
        "required": ["_id"],
        "properties": {
            "_id": {"bsonType": "string"},
            "afa_override_threshold": {"bsonType": ["int", "null"]},
            "ptp_grace_period_hours": {"bsonType": ["int", "null"]},
            "dunning_touch_spacing_days": {"bsonType": ["array", "null"]},
            "b2b_escalation_schedule_days": {"bsonType": ["array", "null"]},
            "msme_registered": {"bsonType": ["bool", "null"]},
            "tds_expected_percent": {"bsonType": ["int", "double", "null"]},
            "escalation_contacts": {"bsonType": ["object", "null"]},
            "faq_min_confidence": {"bsonType": ["double", "null"]},
            "emi_provider_priority": {"bsonType": ["array", "null"]},
            "subscription_terminal_action": {"bsonType": ["string", "null"]},
        },
    })
    # single-document collection; default _id index is sufficient, no extra indexes needed.
    # Seed the one config document with the decided defaults from the design doc, but only
    # on first creation ($setOnInsert) — re-running this script must never clobber a merchant's
    # own edits to these settings.
    # Bug fix (2026-09-03): a single $setOnInsert only applies when the whole
    # document is being newly created — it silently does NOT backfill a new
    # field onto a document that already exists (confirmed by hitting exactly
    # this when emi_provider_priority was added after merchant_config had
    # already been created on a prior run). Seeding per-field with an
    # {field: {"$exists": False}} filter backfills any missing default on every
    # re-run without ever touching a field the merchant has already customized.
    MERCHANT_CONFIG_DEFAULTS = {
        # Problem 5: RBI AFA threshold override for insurance/mutual-fund/credit-card-bill
        # categories (standard AFA threshold itself is the regulatory Rs 15,000, not stored
        # here since it's not merchant-configurable; this field is only the Rs 1,00,000
        # category exemption).
        "afa_override_threshold": 100000,
        # Problem 7: decided default (2026-09-03), still merchant-configurable.
        "ptp_grace_period_hours": 24,
        # Problem 6: Touch1@halted+0, Touch2@+4d, Touch3@+9d, downgrade-check@+11d.
        "dunning_touch_spacing_days": [0, 4, 9, 11],
        # Problem 9: T-3/T+1/T+7/T+14/T+30/T+45/T+60, research-backed cadence.
        "b2b_escalation_schedule_days": [-3, 1, 7, 14, 30, 45, 60],
        # Problem 9: Section 43B(h) citation at T+45 only fires if this is explicitly
        # true — never assumed. Defaults false; the merchant must confirm MSME status.
        "msme_registered": False,
        # Problem 9: no universal default exists (TDS % is transaction-type-specific,
        # a legal fact not a system tunable) — 0 until the merchant configures the
        # actual expected rate for their invoice categories.
        "tds_expected_percent": 0,
        "escalation_contacts": {"procurement": "email", "finance": "email"},
        # Problem 8: seed value from the doc's own worked audit example; needs empirical
        # tuning against the real FAQ corpus once one exists.
        "faq_min_confidence": 0.65,
        # Problem 4: confirmed Razorpay cardless EMI providers, priority order is a
        # reasonable default (no verified provider-quality data) - merchant can
        # reorder/trim to whichever providers they've actually enabled.
        "emi_provider_priority": ["zestmoney", "hdfc", "icic", "idfb", "kkbk"],
        # Problem 6: what happens to a subscription once the full dunning sequence
        # exhausts unresolved. "paused" (not "cancelled") is the safer default -
        # preserves the subscription so the customer can still reactivate later,
        # rather than destructively cancelling on the merchant's behalf.
        "subscription_terminal_action": "paused",
    }
    # Bug fix, same session: upsert=True on the per-field filter below tried to
    # INSERT a duplicate "_id":"merchant_config" whenever a field's $exists:False
    # filter matched nothing because the field simply already had a value (not
    # because the whole document was missing) — DuplicateKeyError. Splitting into
    # "ensure the shell document exists" (once, its own upsert) then "backfill
    # each missing field" (no upsert, the document is now guaranteed present)
    # avoids ever re-attempting to create an _id that's already there.
    merchant_config.update_one(
        {"_id": "merchant_config"}, {"$setOnInsert": {"_id": "merchant_config"}}, upsert=True
    )
    backfilled = []
    for field, default_value in MERCHANT_CONFIG_DEFAULTS.items():
        result = merchant_config.update_one(
            {"_id": "merchant_config", field: {"$exists": False}},
            {"$set": {field: default_value}},
        )
        if result.modified_count:
            backfilled.append(field)
    print(f"  seeded/backfilled config fields: {backfilled or '(none needed, already up to date)'}")

    print("\n[Moved, 2026-09-03] faq_documents now lives on a SEPARATE MongoDB deployment, not here.")
    print("  Real Atlas Search + Atlas Vector Search indexes (needed for the hybrid $rankFusion")
    print("  retrieval prob0_policy_rag is built on) are an Atlas-only / mongot-backed feature this")
    print("  plain community mongod cannot provide. Resolved by running the real")
    print("  'mongodb/mongodb-atlas-local' Docker image (a genuine local Atlas-equivalent deployment,")
    print("  GA and officially supported for exactly this case) as its own server on port 27018 -")
    print("  see libs/rzp_common/rag_mongo_client.py and scripts/rag_db_setup.py.")

    # ---------------------------------------------------------------
    # Ephemeral / TTL collections
    # ---------------------------------------------------------------

    print("\n[Ephemeral, TTL ~90d] raw_webhook_events  (audit-of-record for every inbound webhook)")
    raw_webhook_events = ensure_collection(db, "raw_webhook_events", {
        "bsonType": "object",
        "required": ["event_type", "received_at"],
        "properties": {
            "razorpay_event_id": {"bsonType": ["string", "null"]},
            "event_type": {"bsonType": "string"},
            "payload": {"bsonType": ["object", "null"]},
            "signature_valid": {"bsonType": ["bool", "null"]},
            "received_at": {"bsonType": "date"},
            "processed": {"bsonType": ["bool", "null"]},
            "processing_error": {"bsonType": ["string", "null"]},
        },
    })
    raw_webhook_events.create_index([("received_at", ASCENDING)], expireAfterSeconds=NINETY_DAYS, name="ttl_received_at")
    raw_webhook_events.create_index([("razorpay_event_id", ASCENDING)], name="idx_razorpay_event_id")

    print("\n[Ephemeral, TTL ~24h] checkout_sessions  (Problem 3 watchdog state, _id = order_id)")
    checkout_sessions = ensure_collection(db, "checkout_sessions", {
        "bsonType": "object",
        "required": ["stage", "created_at"],
        "properties": {
            "_id": {"bsonType": "string"},
            "customer_id": {"bsonType": ["string", "null"]},
            # Gap fix (2026-09-03): nothing stored the actual contact details
            # needed to reach a customer on a recovery link - customer_id alone
            # doesn't help for a guest checkout (nullable, by design), and even
            # for a registered customer, re-fetching name/phone from Razorpay
            # or the customers collection on every watchdog checkpoint is an
            # avoidable extra lookup. Captured once at order-creation time.
            "customer_name": {"bsonType": ["string", "null"]},
            "customer_contact": {"bsonType": ["string", "null"]},
            "stage": {"bsonType": "string"},
            "amount": {"bsonType": ["int", "long", "double", "null"]},
            "emi_suggestion_count": {"bsonType": ["int", "null"]},
            "emi_declined_providers": {"bsonType": ["array", "null"]},
            "created_at": {"bsonType": "date"},
            "resolved_at": {"bsonType": ["date", "null"]},
        },
    })
    checkout_sessions.create_index([("created_at", ASCENDING)], expireAfterSeconds=DAY, name="ttl_created_at")
    checkout_sessions.create_index([("stage", ASCENDING)], name="idx_stage")
    checkout_sessions.create_index([("customer_id", ASCENDING)], name="idx_customer_id")

    print("\n[Ephemeral, TTL ~7d] method_health_rollups  (Problem 2 dashboard/history snapshots)")
    method_health_rollups = ensure_collection(db, "method_health_rollups", {
        "bsonType": "object",
        "required": ["bucket_start"],
        "properties": {
            "method": {"bsonType": ["string", "null"]},
            "instrument_key": {"bsonType": ["string", "null"]},
            "bucket_start": {"bsonType": "date"},
            "attempts": {"bsonType": ["int", "null"]},
            "failures": {"bsonType": ["int", "null"]},
        },
    })
    method_health_rollups.create_index([("bucket_start", ASCENDING)], expireAfterSeconds=SEVEN_DAYS, name="ttl_bucket_start")

    print("\n[Permanent] inventory  (gap fix, 2026-09-03: Problem 3's stock-check/soft-lock "
          "logic depends on this and it was never added to the schema in the design pass)")
    inventory = ensure_collection(db, "inventory", {
        "bsonType": "object",
        "required": ["_id", "available_stock"],
        "properties": {
            "_id": {"bsonType": "string"},  # sku_id
            "available_stock": {"bsonType": "int"},
            "updated_at": {"bsonType": ["date", "null"]},
        },
    })
    # No extra indexes needed: the _id index covers point lookups, and reservation
    # is an atomic findOneAndUpdate keyed on _id (see prob3_otp_watch/inventory.py) —
    # no separate lock collection required, Mongo's own document-level atomicity on
    # a single findOneAndUpdate is exactly the "10-minute cart soft-lock" mechanism
    # the design called for, without needing a companion Redis lock.

    print(f"\nDone. Collections in '{DB_NAME}': {sorted(db.list_collection_names())}")


if __name__ == "__main__":
    main()
