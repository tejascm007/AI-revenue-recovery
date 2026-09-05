"""Tests for libs/rzp_agent_kit/wa_templates.py's build_delegation_artifact -
the single source of truth every producer tool (prob3/5/6/9) uses to build a
two-hop WhatsApp delegation artifact. A real bug this validation exists to
prevent: Problem 9 once referenced templates that were never added to the
catalog, and never supplied customer_id/phone at all - both would have
failed silently at send time without this check at construction time.
"""

import pytest

from rzp_agent_kit.wa_templates import TEMPLATE_CATALOG, build_delegation_artifact


def test_valid_artifact_has_the_standard_two_hop_shape():
    result = build_delegation_artifact(
        "cust_1", "+919999999999", "checkout_recovery_link",
        {"name": "Asha", "amount": 50000, "link": "https://rzp.io/i/abc"},
    )
    assert result["status"] == "pending_two_hop_delegation"
    assert result["artifact"]["action"] == "send_whatsapp"
    assert result["artifact"]["template_id"] == "checkout_recovery_link"
    assert result["artifact"]["customer_id"] == "cust_1"
    assert result["artifact"]["phone"] == "+919999999999"


def test_customer_id_may_be_none_for_a_guest_checkout():
    result = build_delegation_artifact(
        None, "+919999999999", "checkout_recovery_link",
        {"name": "Guest", "amount": 1000, "link": "https://rzp.io/i/xyz"},
    )
    assert result["artifact"]["customer_id"] is None


def test_missing_required_template_variable_raises():
    with pytest.raises(ValueError, match="Missing required template variables"):
        build_delegation_artifact("cust_1", "+919999999999", "checkout_recovery_link", {"name": "Asha"})


def test_unknown_template_id_raises():
    with pytest.raises(ValueError, match="Unknown template_id"):
        build_delegation_artifact("cust_1", "+919999999999", "not_a_real_template", {})


@pytest.mark.parametrize("template_id", ["b2b_reminder", "b2b_payment_link"])
def test_b2b_templates_exist_in_the_catalog(template_id):
    # Regression test for a real bug: Problem 9's execute_action once named
    # these two templates without either ever being added to the catalog -
    # a real send would have failed with no earlier warning.
    assert template_id in TEMPLATE_CATALOG


def test_every_catalog_template_is_actually_constructible():
    # Every template in the catalog should be buildable with dummy values
    # for its own declared required variables - catches a template whose
    # variable list was edited inconsistently with itself.
    for template_id, required_vars in TEMPLATE_CATALOG.items():
        variables = {var: "dummy" for var in required_vars}
        result = build_delegation_artifact("cust_1", "+919999999999", template_id, variables)
        assert result["artifact"]["template_id"] == template_id
