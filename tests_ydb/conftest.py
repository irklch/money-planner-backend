"""Тесты нового backend на YDB (E1). PostgreSQL не нужен.

Интеграционные тесты (`@pytest.mark.ydb`) идут против локальной YDB:
    docker compose up -d ydb
    YDB_ENDPOINT=grpc://localhost:2136 pytest tests_ydb
Каждая сессия работает в своей папке таблиц (`mp_test_<random>`) и удаляет её в конце.
Без YDB_ENDPOINT интеграционные тесты пропускаются; в CI задан YDB_TESTS_REQUIRED=1 —
тогда отсутствие YDB считается ошибкой, а не пропуском.
"""

import os
import pathlib
import sys
import uuid

import pytest
import ydb
import ydb.aio

from app.core.config import Settings
from app.db_ydb.client import YdbClient, open_client
from app.db_ydb.migrate import Migrator

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

YDB_ENDPOINT = os.environ.get("YDB_ENDPOINT")
YDB_DATABASE = os.environ.get("YDB_DATABASE", "/local")
REQUIRED = os.environ.get("YDB_TESTS_REQUIRED") == "1"


def pytest_collection_modifyitems(config, items):
    if YDB_ENDPOINT:
        return
    if REQUIRED:
        raise pytest.UsageError("YDB_TESTS_REQUIRED=1, но YDB_ENDPOINT не задан")
    skip = pytest.mark.skip(reason="нет локальной YDB (YDB_ENDPOINT)")
    for item in items:
        if "ydb" in item.keywords:
            item.add_marker(skip)


def ydb_settings(prefix: str, **overrides) -> Settings:
    values = dict(
        env="test",
        ydb_endpoint=YDB_ENDPOINT,
        ydb_database=YDB_DATABASE,
        ydb_auth="anonymous",
        ydb_table_prefix=prefix,
        ydb_connect_timeout_s=15,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="session")
def prefix() -> str:
    return f"mp_test_{uuid.uuid4().hex[:10]}"


async def _drop_prefix(client: YdbClient, prefix: str) -> None:
    scheme = ydb.aio.scheme.SchemeClient(client.driver)
    path = f"{YDB_DATABASE}/{prefix}"
    try:
        listing = await scheme.list_directory(path)
    except ydb.issues.SchemeError:
        return
    for child in listing.children:
        if child.is_directory():
            await _drop_prefix(client, f"{prefix}/{child.name}")
        else:
            await client.ddl(f"DROP TABLE IF EXISTS `{prefix}/{child.name}`")
    await scheme.remove_directory(path)


@pytest.fixture(scope="session")
async def ydb_client(prefix):
    client = await open_client(ydb_settings(prefix))
    try:
        yield client
    finally:
        await _drop_prefix(client, prefix)
        await client.close()


@pytest.fixture(scope="session")
async def migrated(ydb_client) -> YdbClient:
    await Migrator(ydb_client).migrate()
    return ydb_client


@pytest.fixture
async def fresh_client():
    """Клиент с собственной пустой папкой — для тестов миграций с чистой базы."""
    p = f"mp_test_{uuid.uuid4().hex[:10]}"
    client = await open_client(ydb_settings(p))
    try:
        yield client
    finally:
        await _drop_prefix(client, p)
        await client.close()
