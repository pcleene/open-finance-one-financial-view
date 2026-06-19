"""Shared MongoDB client factories.

PyMongo Async (`AsyncMongoClient`) everywhere — Motor is deprecated and must not
be introduced. Sync client only for one-off setup scripts and the seeder
(multiprocessing workers each build their own).
"""

import time

from pymongo import AsyncMongoClient, MongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database

from acme_ofv.config import settings
from acme_ofv.query_log import log_query


def _kwargs(max_pool: int) -> dict:
    s = settings()
    return dict(
        tls=True,
        tlsCertificateKeyFile=s.atlas_cert_path,
        maxPoolSize=max_pool,
        retryWrites=True,
        serverSelectionTimeoutMS=15000,
    )


def make_async_client(max_pool: int = 50) -> AsyncMongoClient:
    return AsyncMongoClient(settings().atlas_uri, **_kwargs(max_pool))


def make_sync_client(max_pool: int = 20) -> MongoClient:
    return MongoClient(settings().atlas_uri, **_kwargs(max_pool))


def ofv_db(client: AsyncMongoClient) -> AsyncDatabase:
    return client[settings().db_name]


def mock_db(client: AsyncMongoClient) -> AsyncDatabase:
    return client[settings().mock_db_name]


def ofv_db_sync(client: MongoClient) -> Database:
    return client[settings().db_name]


def mock_db_sync(client: MongoClient) -> Database:
    return client[settings().mock_db_name]


async def aggregate_list(coll, pipeline: list[dict]) -> list[dict]:
    """PyMongo Async: aggregate() must be awaited to obtain the cursor.

    Single choke point for aggregations — also feeds the Query Inspector
    (captures collection, pipeline, duration, and a result preview)."""
    t0 = time.perf_counter()
    cursor = await coll.aggregate(pipeline)
    docs = await cursor.to_list(None)
    log_query(getattr(coll, "name", "?"), "aggregate", pipeline,
              (time.perf_counter() - t0) * 1000, result=docs)
    return docs
