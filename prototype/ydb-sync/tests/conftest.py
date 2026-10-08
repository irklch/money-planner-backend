"""Общие фикстуры. Тесты с маркером `ydb` идут против локальной YDB (docker compose up -d ydb).

Все данные синтетические. Пользователи и устройства — фиксированные тестовые UUID.
Секрет JWT генерируется случайно на каждый запуск.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import ydb

from client.client import SyncClient
from client.transport import FaultyTransport, HttpTransport
from syncproto import auth, schema
from syncproto.api import create_app
from syncproto.config import Settings
from syncproto.store_ydb import YdbStore, open_driver

USER_A = "00000000-0000-4000-8000-0000000000a1"
USER_B = "00000000-0000-4000-8000-0000000000b1"
PERM_USERS = [f"00000000-0000-4000-8000-00000000c{i:03d}" for i in range(8)]
ALL_TEST_USERS = [USER_A, USER_B, *PERM_USERS]

TEST_PREFIX = os.environ.get("YDB_TEST_PREFIX", "ydbsync_test")
SECRET = secrets.token_urlsafe(48)
SCENARIO_STRATEGIES = ["version", "hlc_dw"]


def make_settings(strategy: str) -> Settings:
    return Settings(
        env="test",
        ydb_endpoint=os.environ.get("YDB_ENDPOINT", "grpc://localhost:2136"),
        ydb_database=os.environ.get("YDB_DATABASE", "/local"),
        ydb_table_prefix=TEST_PREFIX,
        sync_strategy=strategy,
        jwt_secret=SECRET,
    )


def token(user_id: str) -> str:
    return auth.issue_test_token(SECRET, user_id, make_settings("version").jwt_audience)


class SkewClock:
    """Часы устройства: реальное время + сдвиг (неправильные часы) + ручное «ожидание»."""

    def __init__(self, offset: dt.timedelta = dt.timedelta(0)) -> None:
        self.offset = offset

    def __call__(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += dt.timedelta(seconds=seconds)


@pytest.fixture(scope="session")
async def ydb_pool() -> AsyncIterator[ydb.aio.QuerySessionPool]:
    s = make_settings("version")
    try:
        driver = await open_driver(s)
    except (ydb.issues.ConnectionError, ydb.issues.Unavailable, TimeoutError) as e:  # pragma: no cover
        pytest.skip(f"local YDB is not available at {s.ydb_endpoint}: {e}")
    pool = ydb.aio.QuerySessionPool(driver, size=50)
    await schema.apply(pool, schema.drop_ddl(TEST_PREFIX) + schema.ddl(TEST_PREFIX))
    yield pool
    await pool.stop()
    await driver.stop()


@pytest.fixture
async def store(ydb_pool: ydb.aio.QuerySessionPool) -> YdbStore:
    st = YdbStore(ydb_pool, TEST_PREFIX)
    await st.wipe_users(ALL_TEST_USERS)
    return st


@pytest.fixture(params=SCENARIO_STRATEGIES)
def strategy(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
async def http(store: YdbStore, strategy: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(strategy), store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sync.test") as c:
        yield c


@pytest.fixture
def device(tmp_path: Path, http: httpx.AsyncClient) -> Callable[..., SyncClient]:
    """Фабрика устройств. Повторный вызов с тем же именем = перезапуск приложения."""

    def make(user_id: str, name: str, clock: SkewClock | None = None, **kw: Any) -> SyncClient:
        transport = FaultyTransport(HttpTransport(http, token(user_id)))
        return SyncClient(str(tmp_path / f"{user_id}-{name}.sqlite"), name, transport, clock=clock, **kw)

    return make


def server_view(state: dict) -> dict:
    return {k: (None if r.deleted else r.payload, r.deleted) for k, r in state.items()}


async def assert_converged(store: YdbStore, user_id: str, *clients: SyncClient) -> dict:
    """Два раунда sync всех устройств, затем каждое обязано совпасть с сервером."""
    for _ in range(2):
        for c in clients:
            report = await c.sync()
            assert report.ok, f"{c.device_id}: sync failed"
    expected = server_view(await store.server_state(user_id))
    for c in clients:
        assert c.outbox_size() == 0, f"{c.device_id}: outbox not empty"
        assert c.snapshot() == expected, f"{c.device_id} diverged from server"
    return expected
