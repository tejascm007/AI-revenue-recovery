"""Problem 2 telemetry recording — Flow A from the design doc.

These are plain functions, NOT MCP tools. Recording telemetry on every payment
webhook is a synchronous, non-agentic side-effect performed directly by the
shared webhook handler (services/backend, not yet built) — the same pattern
already established for Problem 1's vault write and Problem 3's kill-switch.
No LLM/agent decision is involved in recording a data point; only diagnosing
and acting on the resulting state (server.py's tools) goes through the agent.

Combined verdict formula (Design_Spec_and_Decisions.md, section 11, Problem 2):
    DEGRADED if rzp_status == "degraded"
            OR (gateway_failures / attempts > 0.15 AND attempts >= 10)
    recovering only below 5% (hysteresis) — see server.py's _compute_own_signal_state.
"""

import time

from rzp_common.redis_client import get_redis

WINDOW_SECONDS = 300  # 5-minute sliding window, per the design

# error_source values that count as "our own signal" infra failures, per the
# 2026-09-02 correction: customer- and business-caused declines are explicitly
# excluded so ordinary customer mistakes never masquerade as a route outage.
INFRA_ERROR_SOURCES = {"gateway", "razorpay"}


def _route_prefix(method: str, instrument_key: str) -> str:
    return f"route:{method}:{instrument_key}"


def record_downtime_event(method: str, instrument_key: str, status: str,
                           severity: str | None = None, ttl_seconds: int = 1800) -> None:
    """Mirror a Razorpay payment.downtime.* webhook into Redis.

    status: "started" | "updated" -> sets rzp_status=degraded with a TTL derived
    from the event (default 30 min if no explicit `end` is known yet).
    status: "resolved" -> clears rzp_status immediately.
    """
    r = get_redis()
    prefix = _route_prefix(method, instrument_key)
    if status == "resolved":
        r.delete(f"{prefix}:rzp_status", f"{prefix}:rzp_severity")
        return
    r.set(f"{prefix}:rzp_status", "degraded", ex=ttl_seconds)
    if severity:
        r.set(f"{prefix}:rzp_severity", severity, ex=ttl_seconds)


def record_payment_outcome(method: str, instrument_key: str, payment_id: str,
                            captured: bool, error_source: str | None = None) -> None:
    """Record one payment attempt into the sliding-window sorted sets.

    Every attempt (captured or not) counts toward `:attempts`. Only a failure
    whose error_source is infra-caused (gateway/razorpay) counts toward
    `:failures` — a customer mistyping their PIN must never look like a route
    outage.
    """
    r = get_redis()
    prefix = _route_prefix(method, instrument_key)
    now = time.time()
    r.zadd(f"{prefix}:attempts", {payment_id: now})
    r.expire(f"{prefix}:attempts", WINDOW_SECONDS * 2)  # belt-and-suspenders vs. unbounded growth
    if not captured and error_source in INFRA_ERROR_SOURCES:
        r.zadd(f"{prefix}:failures", {payment_id: now})
        r.expire(f"{prefix}:failures", WINDOW_SECONDS * 2)


def prune_and_count(key: str, window_seconds: int = WINDOW_SECONDS) -> int:
    """Drop entries older than the sliding window and return the remaining count."""
    r = get_redis()
    cutoff = time.time() - window_seconds
    r.zremrangebyscore(key, 0, cutoff)
    return r.zcard(key)
