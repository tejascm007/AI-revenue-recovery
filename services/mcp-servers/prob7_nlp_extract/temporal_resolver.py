"""Deterministic temporal expression resolution for Problem 7's PTP tracker.

Correction made while actually testing this (not just designed on paper):
dateparser, the library the design assumed would handle this, fails on nearly
every phrase this use case actually needs - "next Friday", "agle hafte", and
Romanized Hindi like "kal"/"5 tarikh" (it needs genuine Devanagari script for
Hindi, and doesn't support "next/this/coming + weekday" at all in English
either). Worse: "on the 5th" silently resolved to "2027-05-03" - a confidently
WRONG date, not a failure - which is more dangerous than returning nothing,
since it would sail past the design's "never guess on ambiguity" principle
undetected.

Fix: a small, explicit, testable resolver for the finite set of patterns this
use case actually sees (weekday-relative, day-of-month, day counts, tomorrow/
day-after), tried FIRST. dateparser is kept only as a fallback for anything
this doesn't match, and its result is sanity-checked (must fall within a
reasonable future window) rather than trusted blindly - given it already
proved capable of a confident wrong answer.
"""

import re
from datetime import datetime, timedelta

import dateparser

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FALLBACK_SANITY_WINDOW_DAYS = 30  # beyond this, treat dateparser's own output as suspect


def _next_weekday(base: datetime, target_weekday: int) -> datetime:
    """Nearest occurrence of target_weekday strictly after `base` (minimum
    tomorrow, never today) - deliberately treats "next X", "this X", "coming
    X", and bare "X" all the same way (the nearest upcoming occurrence). This
    is a simplification: "next Friday" is genuinely ambiguous even among
    native English speakers (this week's vs. next week's) - resolving all
    variants to "the very next one" matches the more common colloquial usage
    in casual promise-to-pay replies, at the cost of not handling the stricter
    "next week specifically" reading. Documented here rather than silently
    assumed."""
    days_ahead = (target_weekday - base.weekday() + 7) % 7
    days_ahead = days_ahead if days_ahead != 0 else 7
    return base + timedelta(days=days_ahead)


def _resolve_day_of_month(base: datetime, day: int) -> datetime | None:
    """'5 tarikh' / 'on the 5th' - the nearest FUTURE date with this day-of-month:
    this month if it hasn't passed yet, otherwise next month. Returns None
    (never a silently-clamped wrong date) if the day doesn't exist in either
    month, e.g. "31 tarikh" while evaluating from a 30-day month rolls
    correctly to next month, but "30 tarikh" evaluated in late January would
    correctly fail rather than silently becoming some other day - a bug
    caught by testing: an earlier version clamped day=31 in September to 28
    when the replace() call raised, producing Sept 28 instead of Oct 31."""

    def _try_month(year: int, month: int) -> datetime | None:
        try:
            return base.replace(year=year, month=month, day=day)
        except ValueError:
            return None

    candidate = _try_month(base.year, base.month)
    if candidate is not None and candidate > base:
        return candidate

    next_month = 1 if base.month == 12 else base.month + 1
    next_year = base.year + 1 if base.month == 12 else base.year
    return _try_month(next_year, next_month)


PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bparso\b|\bday after tomorrow\b", re.I), "plus_2"),
    (re.compile(r"\bkal\b|\btomorrow\b", re.I), "plus_1"),
    (re.compile(r"\bagle\s+hafte\b|\bnext\s+week\b", re.I), "plus_7"),
    (re.compile(r"\bin\s+(\d+)\s+days?\b", re.I), "plus_n_days"),
    (re.compile(r"\b(?:next|this|coming)\s+(" + "|".join(WEEKDAYS) + r")\b", re.I), "weekday"),
    (re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.I), "weekday"),
    (re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareek)\b", re.I), "day_of_month"),
    (re.compile(r"\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I), "day_of_month"),
]


def resolve(raw_expression: str, relative_base: datetime) -> dict:
    """Returns {"resolved": True, "date": datetime} or {"resolved": False,
    "reason": str}. Never guesses past FALLBACK_SANITY_WINDOW_DAYS."""
    text = raw_expression.strip().lower()

    for pattern, kind in PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if kind == "plus_1":
            return {"resolved": True, "date": relative_base + timedelta(days=1), "method": "custom_plus_1"}
        if kind == "plus_2":
            return {"resolved": True, "date": relative_base + timedelta(days=2), "method": "custom_plus_2"}
        if kind == "plus_7":
            return {"resolved": True, "date": relative_base + timedelta(days=7), "method": "custom_plus_7"}
        if kind == "plus_n_days":
            n = int(match.group(1))
            return {"resolved": True, "date": relative_base + timedelta(days=n), "method": "custom_plus_n_days"}
        if kind == "weekday":
            weekday_index = WEEKDAYS.index(match.group(1).lower())
            return {"resolved": True, "date": _next_weekday(relative_base, weekday_index), "method": "custom_weekday"}
        if kind == "day_of_month":
            day = int(match.group(1))
            if not (1 <= day <= 31):
                continue
            resolved_date = _resolve_day_of_month(relative_base, day)
            if resolved_date is None:
                return {"resolved": False, "reason": "invalid_day_of_month"}
            return {"resolved": True, "date": resolved_date, "method": "custom_day_of_month"}

    # Fallback: dateparser, sanity-checked rather than trusted blindly - it has
    # been directly observed to produce a confidently wrong date ("on the 5th"
    # -> 8 months out) rather than failing, so any result outside a reasonable
    # near-term window is treated as suspect, not authoritative.
    fallback = dateparser.parse(
        raw_expression, languages=["en"],
        settings={"RELATIVE_BASE": relative_base, "PREFER_DATES_FROM": "future"},
    )
    if fallback is None:
        return {"resolved": False, "reason": "unrecognized_expression"}
    delta_days = (fallback - relative_base).days
    if delta_days < 0 or delta_days > FALLBACK_SANITY_WINDOW_DAYS:
        return {"resolved": False, "reason": "dateparser_result_outside_sanity_window"}
    return {"resolved": True, "date": fallback, "method": "dateparser_fallback"}
