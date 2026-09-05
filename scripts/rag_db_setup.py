"""Sets up `faq_documents` on the RAG deployment (a separate MongoDB server
from the main revenue_recovery database - see libs/rzp_common/rag_mongo_client.py
for why) with real Atlas Vector Search + Atlas Search (BM25) indexes, both
verified to work against the local `mongodb-atlas-local` Docker deployment.

Embedding dimensions (2048) match nvidia/nemotron-3-embed-1b:free, the
OpenRouter embedding model ingest_faq_documents.py uses (switched 2026-09-03
from openai/text-embedding-3-small's 1536 dims to a free model, verified
live before switching) - if EMBEDDING_MODEL ever changes again, this
index's numDimensions must be rebuilt to match, since a vector index is
dimension-locked at creation. This script only creates an index if one by
this name doesn't already exist - a dimension change requires dropping the
old one first (faq_documents.drop_search_index(VECTOR_INDEX_NAME)), which
this script does NOT do automatically, to avoid silently destroying a real
corpus's index over a routine re-run.

Idempotent - safe to re-run. Requires the RAG deployment container running:
    docker run -d --name revenue-recovery-atlas-local -p 27018:27017 mongodb/mongodb-atlas-local:latest

Run:
    uv run python scripts/rag_db_setup.py
"""

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_CODES_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CODES_ROOT / "libs"))

from rzp_common.rag_mongo_client import get_rag_db  # noqa: E402

EMBEDDING_DIMENSIONS = 2048
VECTOR_INDEX_NAME = "faq_vector_index"
TEXT_INDEX_NAME = "faq_text_index"


def _wait_until_queryable(collection, index_name: str, timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        indexes = {idx["name"]: idx for idx in collection.list_search_indexes()}
        if index_name in indexes and indexes[index_name].get("queryable"):
            print(f"  '{index_name}' is READY and queryable.")
            return
        time.sleep(2)
    raise TimeoutError(f"Search index '{index_name}' did not become queryable within {timeout_seconds}s")


def main() -> None:
    db = get_rag_db()
    print(f"Connected to RAG deployment -> database '{db.name}'")

    if "faq_documents" not in db.list_collection_names():
        db.create_collection("faq_documents")
        print("Created 'faq_documents'")
    else:
        print("'faq_documents' already exists")

    faq_documents = db["faq_documents"]
    existing_indexes = {idx["name"] for idx in faq_documents.list_search_indexes()}

    if VECTOR_INDEX_NAME not in existing_indexes:
        faq_documents.create_search_index({
            "name": VECTOR_INDEX_NAME,
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {"type": "vector", "path": "embedding",
                     "numDimensions": EMBEDDING_DIMENSIONS, "similarity": "cosine"},
                ],
            },
        })
        print(f"Submitted '{VECTOR_INDEX_NAME}' (vectorSearch, {EMBEDDING_DIMENSIONS} dims, cosine)")
    else:
        print(f"'{VECTOR_INDEX_NAME}' already exists")

    if TEXT_INDEX_NAME not in existing_indexes:
        faq_documents.create_search_index({
            "name": TEXT_INDEX_NAME,
            "definition": {"mappings": {"dynamic": False, "fields": {"chunk_text": {"type": "string"}}}},
        })
        print(f"Submitted '{TEXT_INDEX_NAME}' (Atlas Search / BM25 on chunk_text)")
    else:
        print(f"'{TEXT_INDEX_NAME}' already exists")

    print("Waiting for both indexes to become queryable...")
    _wait_until_queryable(faq_documents, VECTOR_INDEX_NAME)
    _wait_until_queryable(faq_documents, TEXT_INDEX_NAME)

    faq_documents.create_index([("effective_date", -1)], name="idx_effective_date")
    faq_documents.create_index([("superseded_by", 1)], name="idx_superseded_by")
    print("Done.")


if __name__ == "__main__":
    main()
