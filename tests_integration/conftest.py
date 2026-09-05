"""Shared sys.path setup for the live-infra integration suite - mirrors
tests/conftest.py's pattern (this project is a plain script layout, not an
installed package) plus the backend's own app module.

Unlike tests/, everything here needs a REAL MongoDB + Redis + Kafka
reachable at the same localhost defaults the application code itself
hardcodes/defaults to (see README.md's "Tests & CI" section for how CI
provides these). Nothing here needs a real Razorpay/Meta/OpenRouter
credential - see tests_integration/README.md for the exact scope boundary.
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parent.parent

for _p in (
    _CODES_ROOT / "libs",
    _CODES_ROOT / "services" / "backend",
    _CODES_ROOT / "services" / "mcp-servers" / "prob2_route",
    _CODES_ROOT / "services" / "mcp-servers" / "prob3_otp_watch",
):
    sys.path.insert(0, str(_p))

# Deliberately NOT adding services/orchestrator here: its main.py would
# collide with services/backend/main.py under the same bare "main" module
# name (this project's plain script layout names every entrypoint main.py),
# and nothing in this suite needs it - the Orchestrator's own dispatch is
# explicitly out of scope (see tests_integration/README.md).
