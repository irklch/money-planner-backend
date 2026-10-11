"""Обработка ошибок и таймаутов YDB без сервера: заглушка пула имитирует ответы SDK."""

import asyncio

import pytest
import ydb

from app.db_ydb.client import YdbClient, YdbOptions
from app.db_ydb.errors import YdbUnavailable, is_transient

OPTIONS = YdbOptions(
    prefix="mp", query_timeout_s=1, retry_max_attempts=3, retry_budget_s=0.5, retry_after_s=2
)


class FakePool:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0
        self.retry_settings = None

    async def execute_with_retries(self, query, params=None, retry_settings=None, settings=None):
        self.calls += 1
        self.retry_settings = retry_settings
        return await self.behaviour()

    async def retry_tx_async(self, callee, tx_mode=None, retry_settings=None):
        self.calls += 1
        return await self.behaviour()

    async def stop(self):
        pass


def _raise(exc):
    async def b():
        raise exc

    return b


@pytest.mark.parametrize(
    "exc",
    [
        ydb.issues.Overloaded("x"),
        ydb.issues.Unavailable("x"),
        ydb.issues.Aborted("x"),
        ydb.issues.BadSession("x"),
        ydb.issues.SessionPoolEmpty("x"),
        ydb.issues.DeadlineExceed("x"),
        ydb.issues.ConnectionLost("x"),
        ydb.issues.Undetermined("x"),
    ],
)
async def test_transient_errors_become_unavailable(exc):
    client = YdbClient(FakePool(_raise(exc)), OPTIONS)
    with pytest.raises(YdbUnavailable) as info:
        await client.execute("SELECT 1;")
    assert info.value.retry_after == 2 and info.value.reason == type(exc).__name__
    with pytest.raises(YdbUnavailable):
        await client.transaction(lambda tx: asyncio.sleep(0))


@pytest.mark.parametrize(
    "exc",
    [
        ydb.issues.SchemeError("x"),
        ydb.issues.GenericError("x"),
        ydb.issues.BadRequest("x"),
        ValueError("bug"),
    ],
)
async def test_non_transient_errors_pass_through(exc):
    client = YdbClient(FakePool(_raise(exc)), OPTIONS)
    with pytest.raises(type(exc)):
        await client.execute("SELECT 1;")


async def test_retry_budget_timeout():
    async def slow():
        await asyncio.sleep(5)

    client = YdbClient(FakePool(slow), OPTIONS)
    with pytest.raises(YdbUnavailable) as info:
        await client.execute("SELECT 1;")
    assert info.value.reason == "TimeoutError"


async def test_retry_settings():
    class RS:
        rows = [{"x": 1}]

    async def ok():
        return [RS()]

    pool = FakePool(ok)
    client = YdbClient(pool, OPTIONS)
    assert await client.execute("SELECT 1;", idempotent=True) == [[{"x": 1}]]
    assert pool.retry_settings.max_retries == OPTIONS.retry_max_attempts - 1
    assert pool.retry_settings.idempotent is True
    await client.execute("SELECT 1;")
    assert pool.retry_settings.idempotent is False


def test_classification():
    assert is_transient(ydb.issues.Aborted("x")) and is_transient(TimeoutError())
    assert not is_transient(ydb.issues.SchemeError("x")) and not is_transient(ydb.issues.Unauthorized("x"))


def test_table_path():
    assert YdbClient(FakePool(None), OPTIONS).table("devices") == "mp/devices"
