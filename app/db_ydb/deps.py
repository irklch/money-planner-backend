"""Зависимость FastAPI: клиент YDB процесса (создаётся в lifespan)."""

from typing import Annotated

from fastapi import Depends, Request

from app.db_ydb.client import YdbClient
from app.db_ydb.errors import YdbNotConfigured


def ydb_client(request: Request) -> YdbClient:
    client = getattr(request.app.state, "ydb", None)
    if client is None:
        raise YdbNotConfigured("YDB_ENDPOINT is not set")
    return client


YdbDep = Annotated[YdbClient, Depends(ydb_client)]
