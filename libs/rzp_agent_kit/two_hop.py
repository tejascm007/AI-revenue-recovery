"""Shared two-hop delegation detection for every AgentExecutor.

Design reference: Design_Spec_and_Decisions.md, section 3's cross-agent
delegation pattern, and a2a_dispatch.py's own docstring on the second-hop
gap this closes.

Fixes the gap where a producer tool's "pending_two_hop_delegation" signal
(built by libs/rzp_agent_kit/wa_templates.build_delegation_artifact) got
absorbed into the LLM's own free-text final response instead of surfacing as
a real A2A artifact the Orchestrator could act on programmatically.

extract_tool_result exists because of a real, verified discovery
(2026-09-03): a LangChain MCP tool call through MultiServerMCPClient does NOT
return the plain dict a FastMCP tool's own "-> dict" annotation implies. It
returns a list of MCP content blocks, e.g.
    [{"type": "text", "text": '{"status": "pending_two_hop_delegation", ...}', "id": "..."}]
with the tool's actual JSON-serialized return value one level down, as a
string, inside the first block. Every executor needs this same unwrapping to
detect a delegation signal deterministically rather than trusting the LLM's
own final-turn text to restate it correctly.
"""

import json


def extract_tool_result(raw_result) -> dict | None:
    """Unwraps a MultiServerMCPClient tool-call result back into the plain
    dict the underlying FastMCP tool actually returned. Returns None if
    raw_result isn't in either the verified list-of-content-blocks shape or
    a plain dict (e.g. a tool that returns a bare string) — callers should
    treat None as "no structured signal available", not an error.
    """
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, list) and raw_result:
        first = raw_result[0]
        if isinstance(first, dict) and first.get("type") == "text":
            try:
                parsed = json.loads(first["text"])
            except (json.JSONDecodeError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def find_delegation_artifact(tool_results: list) -> dict | None:
    """Scans a list of raw tool-call results (as returned by
    named_tools[...].ainvoke(...), before any str()-ing for the LLM's own
    ToolMessage) for the first one carrying a
    {"status": "pending_two_hop_delegation", "artifact": {...}} signal.
    Returns just the inner artifact dict, or None if no tool call this turn
    produced one — the common case for every problem's non-messaging tools.
    """
    for raw_result in tool_results:
        parsed = extract_tool_result(raw_result)
        if parsed and parsed.get("status") == "pending_two_hop_delegation":
            return parsed.get("artifact")
    return None
