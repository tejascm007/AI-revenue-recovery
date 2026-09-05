"""Verifies scripts/db_setup.py's real, genuinely-enforced MongoDB
constraints against a real ephemeral MongoDB - not something a pure-logic
test could ever confirm, since these only exist as server-side behavior
once the collection is actually created with them.

Deliberately does NOT test $jsonSchema validator rejection: db_setup.py
sets validationAction="warn" everywhere, on purpose ("while the design is
still evolving") - MongoDB logs a warning but still accepts an invalid
document. Testing for a rejection there would be testing for behavior this
project deliberately doesn't have. Unique and TTL indexes are the real,
actually-enforced constraints worth checking instead.
"""

import uuid
from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from rzp_common.mongo_client import get_db


@pytest.fixture(autouse=True)
def _clean_up_test_docs():
    yield
    get_db().customers.delete_many({"razorpay_customer_id": {"$regex": "^cust_integration_test_"}})


def test_duplicate_razorpay_customer_id_is_rejected():
    customer_id = f"cust_integration_test_{uuid.uuid4().hex[:12]}"
    db = get_db()
    db.customers.insert_one({
        "razorpay_customer_id": customer_id, "phone": f"+91{uuid.uuid4().int % 10**10:010d}",
        "created_at": datetime.now(timezone.utc),
    })
    with pytest.raises(DuplicateKeyError):
        db.customers.insert_one({
            "razorpay_customer_id": customer_id, "phone": f"+91{uuid.uuid4().int % 10**10:010d}",
            "created_at": datetime.now(timezone.utc),
        })


def test_duplicate_phone_is_rejected_even_with_a_different_customer_id():
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    db = get_db()
    db.customers.insert_one({
        "razorpay_customer_id": f"cust_integration_test_{uuid.uuid4().hex[:12]}", "phone": phone,
        "created_at": datetime.now(timezone.utc),
    })
    with pytest.raises(DuplicateKeyError):
        db.customers.insert_one({
            "razorpay_customer_id": f"cust_integration_test_{uuid.uuid4().hex[:12]}", "phone": phone,
            "created_at": datetime.now(timezone.utc),
        })


def test_raw_webhook_events_has_a_real_90_day_ttl_index():
    # Not practical to wait 90 days out in a test - this at least confirms
    # the index genuinely exists with the exact retention db_setup.py
    # intends, catching a future accidental removal or a typo'd duration.
    db = get_db()
    indexes = {idx["name"]: idx for idx in db.raw_webhook_events.list_indexes()}
    assert "ttl_received_at" in indexes
    assert indexes["ttl_received_at"]["expireAfterSeconds"] == 90 * 86400
