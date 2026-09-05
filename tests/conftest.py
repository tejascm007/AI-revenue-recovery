"""Shared sys.path setup for tests - every module under test uses the same
sys.path.insert(_CODES_ROOT / "libs") pattern its own scripts/servers use
(this project is a plain script layout, not an installed package), so tests
need the same paths on sys.path to import them directly.

Deliberately scoped to modules with NO external dependency (no live Mongo/
Redis/Kafka, no real API credentials) - see tests/README.md for why the
broader system is verified live rather than replicated here.
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parent.parent

for _p in (
    _CODES_ROOT / "libs",
    _CODES_ROOT / "services" / "orchestrator",
    _CODES_ROOT / "services" / "mcp-servers" / "prob7_nlp_extract",
    _CODES_ROOT / "services" / "mcp-servers" / "prob8_meta_wa_api",
    _CODES_ROOT / "services" / "mcp-servers" / "prob9_recon",
    _CODES_ROOT / "services" / "watchdog_poller",
):
    sys.path.insert(0, str(_p))
