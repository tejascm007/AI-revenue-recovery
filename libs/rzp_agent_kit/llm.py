"""Shared LLM/embeddings construction, routed through OpenRouter.

Decided 2026-09-03: one OpenRouter key covers both chat and embeddings,
rather than a direct OpenAI key. Verified directly against OpenRouter's own
docs before wiring this in: the API is OpenAI-compatible (same request/
response schema, Bearer auth) at base URL https://openrouter.ai/api/v1, and
OpenRouter genuinely has a /v1/embeddings endpoint too (not something every
OpenAI-compatible gateway supports).

EMBEDDING_MODEL switched to "nvidia/nemotron-3-embed-1b:free" (2026-09-03,
user's own choice, given the account's limited balance) - verified live via
a real API call (cost: 0, 2048 dimensions) before switching, and picked over
the similarly-free "nvidia/llama-nemotron-embed-vl-1b-v2:free" tried first:
that one is a vision-language model (embeds text+image jointly), while
nemotron-3-embed-1b is purpose-built for text retrieval specifically and
released more recently (July 2026 vs. February) - a better fit for a
text-only FAQ corpus. The RAG deployment's vector index is rebuilt to match
this model's 2048 dimensions (scripts/rag_db_setup.py) - a vector index is
dimension-locked at creation, so this isn't just a config swap.

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

import rzp_common.env  # noqa: F401  (side-effect import: loads codes/.env)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free")
# Gap fix (2026-09-03, found by actually calling a paid model on a low-balance
# OpenRouter account): langchain_openai's own default max_tokens ceiling for
# gpt-4o (~16384) genuinely exceeds what a small account balance can afford,
# producing a 402 "requires more credits, or fewer max_tokens" error on every
# single call - not a rate limit, a per-request affordability check. Capped
# here so every agent call actually goes through rather than failing by
# default; raise via LLM_MAX_TOKENS once real budget exists.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1000"))


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
    return ChatOpenAI(
        model=LLM_MODEL, openai_api_key=OPENROUTER_API_KEY, openai_api_base=OPENROUTER_BASE_URL,
        max_tokens=LLM_MAX_TOKENS,
    )


def get_embeddings() -> OpenAIEmbeddings:
    _require_key()
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL, openai_api_key=OPENROUTER_API_KEY, openai_api_base=OPENROUTER_BASE_URL,
        # Gap fix (2026-09-03, found by a real 400 from OpenRouter): by
        # default OpenAIEmbeddings pre-tokenizes text into integer token ID
        # arrays client-side (via tiktoken) before calling the API - fine
        # for a real OpenAI embedding model, but nvidia/nemotron-3-embed-1b
        # rejects that shape outright ("Invalid input format... supports
        # strings and multimodal inputs"). Disabling this makes it send raw
        # text strings instead, which this model accepts.
        check_embedding_ctx_length=False,
        # Second real 400 found right after the first fix: the underlying
        # openai SDK defaults encoding_format to "base64" (its own internal
        # optimization for real OpenAI models), which this model also
        # rejects ("do not support base64 encoding_format. Use float
        # instead"). _invocation_params doesn't set this itself, so
        # model_kwargs is the only way to force it through to the actual
        # API call.
        model_kwargs={"encoding_format": "float"},
    )
