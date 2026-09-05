"""Tests for prob8_meta_wa_api/server.py's _format_template_var - the fix
for a real protobuf round-trip bug: google.protobuf.Struct's Value type has
no integer variant, so a paise amount like 50000, sent through a two-hop
delegation artifact, comes back as 50000.0 - rendered as-is that reads as
"Rs 50000.0" in an actual outbound WhatsApp message.

Loaded via importlib rather than a plain import: every problem's MCP server
is named server.py, so a bare "import server" would be ambiguous depending
on whichever directory happens to be earliest on sys.path.
"""

import importlib.util
from pathlib import Path

import pytest

_SERVER_PATH = Path(__file__).resolve().parent.parent / "services" / "mcp-servers" / "prob8_meta_wa_api" / "server.py"
_spec = importlib.util.spec_from_file_location("prob8_server_under_test", _SERVER_PATH)
prob8_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prob8_server)


@pytest.mark.parametrize("value,expected", [
    (50000.0, "50000"),        # the exact protobuf round-trip case
    (50000, "50000"),          # a real int, not float - unaffected
    (50000.5, "50000.5"),      # a genuine fractional value must NOT be truncated
    ("Asha", "Asha"),          # a non-numeric variable passes through unchanged
    (0.0, "0"),
])
def test_format_template_var(value, expected):
    assert prob8_server._format_template_var(value) == expected
