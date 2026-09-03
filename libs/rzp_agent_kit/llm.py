"""Shared LLM/embeddings construction, routed through OpenRouter.

Decided 2026-09-03: one OpenRouter key covers both chat and embeddings,
rather than a direct OpenAI key. Verified directly against OpenRouter's own
docs before wiring this in: the API is OpenAI-compatible (same request/
response schema, Bearer auth) at base URL https://openrouter.ai/api/v1, and
OpenRouter genuinely has a /v1/embeddings endpoint too (not something every
OpenAI-compatible gateway supports) - confirmed model identifier
"openai/text-embedding-3-small", 1536 dimensions, matching the dimension
already locked into the RAG deployment's vector index.

langchain_openai's ChatOpenAI/OpenAIEmbeddings work against OpenRouter
unmodified - their actual pydantic field names are openai_api_key/
openai_api_base (not the api_key/base_url a first guess might reach for;
verified directly against the installed classes' model_fields), which is
all a same-schema OpenAI-compatible gateway needs.

LLM_MODEL and EMBEDDING_MODEL are env-configurable rather than hardcoded -
OpenRouter's catalog of exact model slugs shifts over time (this project's
own earlier placeholder, "gpt-5.6", already noted this same staleness risk
for a direct OpenAI model name; it applies just as much to an OpenRouter
slug). Verify the exact current slug at https://openrouter.ai/models before
relying on the default here.
"""

import os

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-5.6")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "openai/text-embedding-3-small")


def _require_key() -> None:
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Sign up at https://openrouter.ai, "
            "add credits, generate a key at https://openrouter.ai/keys, and "
            "set it as an environment variable."
        )


def get_chat_llm() -> ChatOpenAI:
    """Lazy-callable, not constructed at import/startup - same "fail only at
    the point of use" pattern every other credentialed client in this
    project follows."""
    _require_key()
    return ChatOpenAI(model=LLM_MODEL, openai_api_key=OPENROUTER_API_KEY, openai_api_base=OPENROUTER_BASE_URL)


def get_embeddings() -> OpenAIEmbeddings:
    _require_key()
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL, openai_api_key=OPENROUTER_API_KEY, openai_api_base=OPENROUTER_BASE_URL
    )
