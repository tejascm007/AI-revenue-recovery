"""Shared MongoDB client factory.

One MongoClient per process (pymongo's own connection pooling handles the rest),
reused by every FastMCP server and service instead of each opening its own
connection. Database name and URI come from the environment so the same code
runs against the local mongod instance now and a real Atlas cluster later.
"""

import os
from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("MONGO_DB_NAME", "revenue_recovery")


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    return MongoClient(MONGO_URI)


def get_db() -> Database:
    return get_client()[DB_NAME]
