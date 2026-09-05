"""Tests for services/mcp-servers/prob7_nlp_extract/temporal_resolver.py.

This module exists because dateparser (the originally-assumed library) was
found to fail on nearly every Hinglish PTP phrase this project needs, and
once, dangerously, to produce a confidently WRONG date rather than failing -
"on the 5th" silently resolved 8 months out. These tests exist to make sure
that specific class of regression can never come back unnoticed.
"""

from datetime import datetime, timedelta

from temporal_resolver import resolve

BASE = datetime(2026, 9, 3, 10, 0, 0)  # a fixed Thursday for reproducibility


def test_kal_and_tomorrow_resolve_to_plus_one_day():
    for phrase in ["kal", "tomorrow", "Kal karunga", "I'll pay tomorrow"]:
        result = resolve(phrase, BASE)
        assert result["resolved"] is True
        assert result["date"] == BASE + timedelta(days=1)
        assert result["method"] == "custom_plus_1"


def test_parso_and_day_after_tomorrow_resolve_to_plus_two_days():
    for phrase in ["parso", "day after tomorrow"]:
        result = resolve(phrase, BASE)
        assert result["resolved"] is True
        assert result["date"] == BASE + timedelta(days=2)


def test_agle_hafte_and_next_week_resolve_to_plus_seven_days():
    for phrase in ["agle hafte", "next week", "Main agle hafte pay kar dunga"]:
        result = resolve(phrase, BASE)
        assert result["resolved"] is True
        assert result["date"] == BASE + timedelta(days=7)


def test_in_n_days_resolves_correctly():
    result = resolve("in 5 days", BASE)
    assert result["resolved"] is True
    assert result["date"] == BASE + timedelta(days=5)


def test_bare_and_qualified_weekday_resolve_to_nearest_upcoming_occurrence():
    # BASE is a Thursday (weekday index 3). Friday is the very next day.
    for phrase in ["Friday", "next Friday", "this Friday", "coming Friday"]:
        result = resolve(phrase, BASE)
        assert result["resolved"] is True
        assert result["date"] == BASE + timedelta(days=1)
        assert result["method"] == "custom_weekday"


def test_weekday_never_resolves_to_today_even_if_named_explicitly():
    # BASE's own weekday is Thursday - asking for "Thursday" must mean NEXT
    # Thursday (7 days out), never today, since a same-day promise makes no
    # sense as a future commitment.
    result = resolve("Thursday", BASE)
    assert result["resolved"] is True
    assert result["date"] == BASE + timedelta(days=7)


def test_day_of_month_within_current_month():
    # BASE is Sept 3 - "on the 20th" should resolve within September.
    result = resolve("on the 20th", BASE)
    assert result["resolved"] is True
    assert result["date"] == BASE.replace(day=20)
    assert result["method"] == "custom_day_of_month"


def test_day_of_month_already_passed_rolls_to_next_month():
    # BASE is Sept 3 - "1 tarikh" has already passed this month, must roll
    # to October, not silently resolve to a past date.
    result = resolve("1 tarikh", BASE)
    assert result["resolved"] is True
    # replace(day=...) preserves BASE's own time-of-day (10:00:00), not midnight
    assert result["date"] == datetime(2026, 10, 1, 10, 0, 0)


def test_day_31_in_a_30_day_month_rolls_to_next_month_not_clamped():
    # The exact regression this module's own docstring documents: an earlier
    # version clamped day=31 to day=28 when September (30 days) couldn't
    # hold it, instead of correctly rolling to October 31st.
    result = resolve("31 tarikh", BASE)  # BASE is in September, a 30-day month
    assert result["resolved"] is True
    assert result["date"] == datetime(2026, 10, 31, 10, 0, 0)
    assert result["method"] == "custom_day_of_month"


def test_day_of_month_across_year_boundary():
    december_base = datetime(2026, 12, 20, 10, 0, 0)
    result = resolve("5 tarikh", december_base)
    assert result["resolved"] is True
    assert result["date"] == datetime(2027, 1, 5, 10, 0, 0)


def test_day_31_absent_from_current_month_rolls_forward_not_none():
    # April only has 30 days, so day=31 can't resolve within April itself -
    # must roll to May (which has 31) rather than returning None. No two
    # REAL consecutive calendar months both lack the same day-of-month for
    # any day 1-31 (the None branch in _resolve_day_of_month exists as a
    # deliberate safety net, not because a real date can trigger it) - this
    # confirms the roll-forward path specifically, a second data point
    # alongside the September->October case above.
    result = resolve("31 tarikh", datetime(2026, 4, 15))
    assert result["resolved"] is True
    assert result["date"] == datetime(2026, 5, 31)


def test_out_of_range_day_is_not_matched_at_all():
    # "35 tarikh" isn't a valid day-of-month under any interpretation - the
    # day_of_month branch explicitly guards 1 <= day <= 31 and `continue`s
    # past it rather than matching, so this correctly falls through to the
    # unrecognized/dateparser-fallback path rather than raising or guessing.
    result = resolve("35 tarikh", BASE)
    assert result["resolved"] is False


def test_completely_unrecognized_gibberish_never_guesses():
    result = resolve("asdkjfh qwoeiruqwoe", BASE)
    assert result["resolved"] is False
    assert result["reason"] in ("unrecognized_expression", "dateparser_result_outside_sanity_window")


def test_empty_string_never_resolves():
    result = resolve("", BASE)
    assert result["resolved"] is False
