"""Tests for services/mcp-servers/prob9_recon/b2b_watchdog.py's pure
checkpoint-key encode/decode functions - the format the watchdog poller
parses to route a due B2B escalation checkpoint back to its invoice and
day-offset. schedule_escalation_checkpoints/kill_switch_invoice/
mark_escalation_stage_completed need live Mongo/Redis and aren't covered
here.
"""

import pytest

from b2b_watchdog import _checkpoint_key, parse_checkpoint_day_offset


@pytest.mark.parametrize("day_offset", [-3, 1, 7, 14, 30, 45, 60])
def test_checkpoint_key_round_trips_through_parse(day_offset):
    key = _checkpoint_key("inv_123", day_offset)
    # The watchdog poller only ever sees the suffix after "{invoice_id}:" -
    # simulate that split exactly as main.py's own checkpoint parser does.
    _, suffix = key.rsplit(":", 1)
    assert parse_checkpoint_day_offset(suffix) == day_offset


def test_checkpoint_key_uses_plus_minus_naming_not_a_bare_negative_sign():
    # Redis sorted-set member names and the poller's own suffix-matching
    # both work more reliably without a literal "-" in the key.
    assert _checkpoint_key("inv_1", -3) == "inv_1:t_minus_3"
    assert _checkpoint_key("inv_1", 7) == "inv_1:t_plus_7"


def test_parse_checkpoint_day_offset_returns_none_for_unrelated_suffixes():
    # Must not misinterpret another problem's checkpoint suffix (e.g.
    # Problem 3's "stage1" or Problem 6's "touch1") as a B2B escalation.
    for suffix in ["stage1", "stage2", "hard_decline", "touch1", "downgrade", "ptp_due"]:
        assert parse_checkpoint_day_offset(suffix) is None


def test_parse_checkpoint_day_offset_returns_none_for_malformed_input():
    assert parse_checkpoint_day_offset("t_plus_notanumber") is None
    assert parse_checkpoint_day_offset("t_sideways_5") is None
    assert parse_checkpoint_day_offset("garbage") is None
