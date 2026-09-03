"""Lazy client for the RAG deployment - a SEPARATE MongoDB server from the
project's main `revenue_recovery` database (rzp_common/mongo_client.py).

Design reference: Design_Spec_and_Decisions.md, section 11's Policy RAG LLD
calls for MongoDB Atlas Hybrid Search ($rankFusion, vector + BM25) on
`faq_documents`, which db_setup.py's own comment already flagged as
unavailable on a plain community mongod - Atlas Search/Vector Search indexes
are an Atlas-only, mongot-backed feature.

Resolved (2026-09-03) by running the real `mongodb/mongodb-atlas-local`
Docker image (a genuine local Atlas-equivalent deployment bundling mongot,
GA and officially supported for exactly this development scenario) as a
second, independent MongoDB deployment on host port 27018 - verified
directly to support real `db.collection.createSearchIndex()` calls with
type "vectorSearch", not simulated. Kept entirely separate from the main
mongod rather than migrating the whole project onto it: `faq_documents` is
the only collection anywhere in this system that actually needs search
indexes, and touching the other 14 already-verified-working collections for
one collection's sake would be a large, unnecessary blast radius.

Run (must exist before this client can connect):
    docker run -d --name revenue-recovery-atlas-local -p 27018:27017 mongodb/mongodb-atlas-local:latest
"""

from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

RAG_MONGO_URI = "mongodb://localhost:27018/?directConnection=true"
RAG_DB_NAME = "revenue_recovery"


@lru_cache(maxsize=1)
def get_rag_client() -> MongoClient:
    return MongoClient(RAG_MONGO_URI)


def get_rag_db() -> Database:
    return get_rag_client()[RAG_DB_NAME]
