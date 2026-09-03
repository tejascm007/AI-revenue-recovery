"""Shared Redis client factory.

Redis is the hot path for Problem 2's route-health telemetry (see
Design_Spec_and_Decisions.md section 11) — sub-10ms reads, sliding-window
sorted sets for attempt/failure counts, TTL-based degradation status. One
client per process, decode_responses=True so callers get str, not bytes.
"""

import os
from functools import lru_cache

import redis

import rzp_common.env  # noqa: F401  (side-effect import: loads codes/.env)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)
