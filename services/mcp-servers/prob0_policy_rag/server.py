"""FastMCP server for the cross-cutting Policy RAG layer.

Design reference: Design_Spec_and_Decisions.md, section 11's Policy RAG LLD
(scope corrected 2026-09-03) - retrieval is used ONLY for genuinely
open-ended natural-language matching: Problem 8's FAQ answering and Problem
9's inbound dispute replies. Every fixed regulatory citation (AFA, NSF,
MSME/Section 43B(h)) is a known-at-design-time 1:1 mapping and lives as a
plain string in merchant_config instead - never routed through retrieval,
per that same correction.

Backed by MongoDB Atlas Hybrid Search ($rankFusion: vector + BM25 via
LangChain's MongoDBAtlasHybridSearchRetriever) on a SEPARATE MongoDB
deployment from the rest of this project (libs/rzp_common/rag_mongo_client.py
explains why - real Atlas Search/Vector Search indexes aren't available on
the main plain community mongod).

Run directly:
    uv run python services/mcp-servers/prob0_policy_rag/server.py
"""

import sys
from pathlib import Path

_CODES_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from fastmcp import FastMCP  # noqa: E402
from langchain_mongodb.retrievers.hybrid_search import MongoDBAtlasHybridSearchRetriever  # noqa: E402
from langchain_mongodb.vectorstores import MongoDBAtlasVectorSearch  # noqa: E402
from langchain_openai import OpenAIEmbeddings  # noqa: E402

from rzp_agent_kit.audit import write_audit_log  # noqa: E402
from rzp_common.mongo_client import get_db  # noqa: E402
from rzp_common.rag_mongo_client import get_rag_db  # noqa: E402

mcp = FastMCP("Prob0PolicyRag")

VECTOR_INDEX_NAME = "faq_vector_index"
TEXT_INDEX_NAME = "faq_text_index"
DEFAULT_TOP_K = 4
DEFAULT_CONFIDENCE_THRESHOLD = 0.65  # merchant_config.faq_min_confidence overrides this

# The only two decisions this layer's callers ever take, matching every other
# problem's fixed-action-set enforcement pattern (e.g. prob9's FIXED_ACTIONS) -
# never let a free-form string through unchecked into the audit trail.
FAQ_ACTIONS = {
    "ANSWER_FROM_RETRIEVED_CONTEXT": "replied",
    "ESCALATED_LOW_CONFIDENCE": "escalated",
}

_retriever: MongoDBAtlasHybridSearchRetriever | None = None


def _get_retriever() -> MongoDBAtlasHybridSearchRetriever:
    # Lazy and cached, same reason as every other OpenAI-touching client in
    # this project: OpenAIEmbeddings validates its API key eagerly at
    # construction, which would otherwise make this whole server fail to
    # start without OPENAI_API_KEY set, rather than failing only when a
    # retrieval actually happens.
    global _retriever
    if _retriever is None:
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        vectorstore = MongoDBAtlasVectorSearch(
            collection=get_rag_db()["faq_documents"], embedding=embeddings,
            index_name=VECTOR_INDEX_NAME, text_key="chunk_text", embedding_key="embedding",
        )
        _retriever = MongoDBAtlasHybridSearchRetriever(
            vectorstore=vectorstore, search_index_name=TEXT_INDEX_NAME, k=DEFAULT_TOP_K,
        )
    return _retriever


@mcp.tool()
def retrieve_policy_context(problem_id: int, query_text: str) -> dict:
    """Hybrid (vector + BM25, RRF-fused) retrieval against the FAQ/T&Cs/SOP
    corpus, gated by merchant_config.faq_min_confidence - the actual
    mechanism behind the design's "soft-fail" principle. Below-threshold
    results come back as status:"no_match", never a weak match answered
    from anyway; the caller must escalate to a human in that case, not
    invent a policy detail. problem_id is 8 (Conversational NLP FAQ) or 9
    (B2B dispute replies) in practice, kept generic for future callers."""
    db = get_db()
    config = db.merchant_config.find_one({"_id": "merchant_config"}) or {}
    threshold = config.get("faq_min_confidence", DEFAULT_CONFIDENCE_THRESHOLD)

    docs = _get_retriever().invoke(query_text)
    if not docs or docs[0].metadata.get("score", 0.0) < threshold:
        top_score = docs[0].metadata.get("score", 0.0) if docs else 0.0
        return {"status": "no_match", "top_score": top_score, "confidence_threshold": threshold, "chunks": []}

    return {
        "status": "match", "top_score": docs[0].metadata.get("score", 0.0), "confidence_threshold": threshold,
        "chunks": [
            {"text": d.page_content, "source": d.metadata.get("source"), "doc_type": d.metadata.get("doc_type"),
             "doc_id": str(d.metadata.get("_id")), "score": d.metadata.get("score")}
            for d in docs
        ],
    }


@mcp.tool()
def log_faq_interaction(problem_id: int, customer_id: str | None, question: str, action: str,
                         top_score: float, confidence_threshold: float,
                         source_doc_id: str | None, reasoning: str, reply_channel: str) -> dict:
    """Every FAQ interaction gets an audit_logs entry, answered or escalated
    alike (gap fix from the design's own LLD) - called by the agent after it
    decides what to do with retrieve_policy_context's result, since only the
    agent knows whether/how it actually replied."""
    if action not in FAQ_ACTIONS:
        raise ValueError(f"Action {action!r} is not in the fixed action set: {sorted(FAQ_ACTIONS)}")

    write_audit_log(
        problem_id=problem_id, tool_name="log_faq_interaction",
        entity_refs={"customer_id": customer_id},
        observation={"customer_question": question, "top_match_score": top_score,
                     "confidence_threshold": confidence_threshold},
        decision={"action": action, "source_doc_id": source_doc_id, "reasoning": reasoning},
        execution={"status": FAQ_ACTIONS[action], "reply_channel": reply_channel},
        mcp_server="prob0_policy_rag",
    )
    return {"status": "logged"}


@mcp.tool()
def build_faq_reply_delegation(customer_id: str | None, phone: str, reply_text: str) -> dict:
    """Packages an LLM-composed FAQ reply for two-hop delegation to the
    Conversational NLP Agent - needed by any caller that uses this server
    for FAQ answering but does NOT own Srv8 itself (today: the B2B
    Receivables Agent's dispute-reply flow; the Conversational NLP Agent
    owns Srv8 directly and calls send_freeform_reply itself, never needing
    this). Deliberately a free-form variant, separate from
    wa_templates.build_delegation_artifact's template-based shape: an FAQ
    reply is answered within an already-open Customer Service Window (the
    customer just messaged us), which is a free-form send, not a
    business-initiated template send - the two cases need different
    Graph API calls (send_freeform_reply vs send_whatsapp_message) and the
    artifact must say which. The reply_text itself is legitimately
    LLM-authored content, unlike every other field in this artifact family -
    the guardrail here is retrieve_policy_context's confidence gate and
    log_faq_interaction's audit trail, not determinism of the wording."""
    return {
        "status": "pending_two_hop_delegation",
        "artifact": {
            "action": "send_whatsapp_freeform",
            "customer_id": customer_id, "phone": phone, "text": reply_text,
        },
    }


if __name__ == "__main__":
    mcp.run()
