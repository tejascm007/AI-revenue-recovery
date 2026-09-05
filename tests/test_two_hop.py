"""Tests for libs/rzp_agent_kit/two_hop.py - the deterministic extraction of
a two-hop delegation signal out of a raw MultiServerMCPClient tool result.

extract_tool_result exists because of a real, verified discovery: a
LangChain MCP tool call does NOT return the plain dict a FastMCP tool's own
"-> dict" annotation implies - it returns a list of MCP content blocks with
the tool's actual JSON-encoded return value one level down. These tests
pin that exact shape so a future SDK change that breaks the assumption is
caught here, not silently in production.
"""

from rzp_agent_kit.two_hop import extract_tool_result, find_delegation_artifact

# The real, verified shape a MultiServerMCPClient tool call actually returns.
REAL_MCP_RESULT_SHAPE = [{"type": "text", "text": '{"classification": "hard", "afa_threshold_used": 15000}', "id": "lc_x"}]

DELEGATION_MCP_RESULT_SHAPE = [{
    "type": "text",
    "text": '{"status": "pending_two_hop_delegation", "artifact": {"action": "send_whatsapp", "customer_id": "c1"}}',
    "id": "lc_y",
}]


def test_extract_tool_result_unwraps_the_real_mcp_content_block_shape():
    assert extract_tool_result(REAL_MCP_RESULT_SHAPE) == {"classification": "hard", "afa_threshold_used": 15000}


def test_extract_tool_result_passes_through_a_plain_dict_unchanged():
    # Defensive case: if a future SDK version DOES return a plain dict
    # directly, this must still work rather than only supporting one shape.
    assert extract_tool_result({"already": "a dict"}) == {"already": "a dict"}


def test_extract_tool_result_returns_none_for_a_bare_string_result():
    assert extract_tool_result("just a string, no structure") is None


def test_extract_tool_result_returns_none_for_malformed_json():
    assert extract_tool_result([{"type": "text", "text": "{not valid json", "id": "x"}]) is None


def test_extract_tool_result_returns_none_for_empty_list():
    assert extract_tool_result([]) is None


def test_find_delegation_artifact_returns_none_when_no_tool_signals_delegation():
    assert find_delegation_artifact([REAL_MCP_RESULT_SHAPE]) is None


def test_find_delegation_artifact_finds_the_signal_among_mixed_results():
    artifact = find_delegation_artifact([REAL_MCP_RESULT_SHAPE, DELEGATION_MCP_RESULT_SHAPE])
    assert artifact == {"action": "send_whatsapp", "customer_id": "c1"}


def test_find_delegation_artifact_returns_none_for_empty_results():
    assert find_delegation_artifact([]) is None
