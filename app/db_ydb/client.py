"""Подключение к YDB и выполнение запросов (Query Service, async).

- Пул 1–2 сессий на экземпляр (план §3.1): компиляция запросов на новой сессии стоит RU.
- Каждая операция — с повторами SDK (OCC-конфликты, перегрузка, недоступность) и общим бюджетом
  времени; таймаут одной попытки — `ydb_query_timeout_s`.
- Транзакция `transaction(fn)` повторяет `fn` целиком: внутри нельзя делать необратимых действий
  вне YDB (вызовов Apple, AI и т. п.).
- Исчерпан бюджет повторов → `YdbUnavailable` (→ 503 + Retry-After). Ошибки запроса и схемы не
  повторяются и пробрасываются как есть (→ 500).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import ydb
import ydb.iam

from app.core.config import Settings
from app.db_ydb import metering
from app.db_ydb.errors import YdbUnavailable, is_transient, reason_of

log = logging.getLogger("app.ydb")

T = TypeVar("T")
Rows = list[Any]


@dataclass(frozen=True)
class YdbOptions:
    prefix: str
    query_timeout_s: float
    retry_max_attempts: int
    retry_budget_s: float
    retry_after_s: int

    @classmethod
    def from_settings(cls, s: Settings) -> YdbOptions:
        return cls(
            prefix=s.ydb_table_prefix,
            query_timeout_s=s.ydb_query_timeout_s,
            retry_max_attempts=s.ydb_retry_max_attempts,
            retry_budget_s=s.ydb_retry_budget_s,
            retry_after_s=s.ydb_unavailable_retry_after_s,
        )


def credentials(settings: Settings) -> Any:
    if settings.ydb_auth == "anonymous":
        return ydb.AnonymousCredentials()  # только локальная YDB (проверяется в Settings)
    if settings.ydb_auth == "metadata":
        return ydb.iam.MetadataUrlCredentials()  # IAM сервисного аккаунта контейнера
    return ydb.credentials_from_env_variables()


class Tx:
    """Транзакция внутри `YdbClient.transaction`: запросы выполняются в одной serializable-транзакции."""

    def __init__(self, tx: Any, settings: ydb.BaseRequestSettings):
        self._tx = tx
        self._settings = settings

    async def execute(
        self, query: str, params: dict[str, Any] | None = None, *, commit: bool = False
    ) -> list[Rows]:
        it = await self._tx.execute(query, params, commit_tx=commit, settings=self._settings)
        return await collect(it)


async def collect(it: Any) -> list[Rows]:
    """Все result set-ы ответа: [[строки первого SELECT], [второго], ...]."""
    out: list[Rows] = []
    async with it as results:
        async for rs in results:
            if rs is None:
                continue
            # Результат одного SELECT может прийти несколькими частями с одним индексом.
            idx = rs.index if rs.index is not None else len(out)
            while len(out) <= idx:
                out.append([])
            out[idx].extend(rs.rows)
    return out


class YdbClient:
    """Доступ к YDB для репозиториев. Создаётся один раз на процесс (`open_client`)."""

    def __init__(self, pool: Any, options: YdbOptions, driver: Any = None):
        self.pool = pool
        self.driver = driver
        self.options = options

    def table(self, name: str) -> str:
        """Путь таблицы внутри базы с префиксом окружения: `mp/devices`."""
        return f"{self.options.prefix}/{name}"

    def _request_settings(self) -> ydb.BaseRequestSettings:
        t = self.options.query_timeout_s
        return ydb.BaseRequestSettings().with_timeout(t).with_operation_timeout(t)

    def _retry_settings(self, idempotent: bool) -> ydb.RetrySettings:
        return ydb.RetrySettings(
            max_retries=self.options.retry_max_attempts - 1,
            idempotent=idempotent,
            max_session_acquire_timeout=self.options.query_timeout_s,
        )

    async def _guard(self, op: Callable[[], Awaitable[T]], what: str) -> T:
        """Общий бюджет времени операции и перевод временных ошибок в YdbUnavailable."""
        try:
            return await asyncio.wait_for(op(), timeout=self.options.retry_budget_s)
        except YdbUnavailable:
            raise
        except BaseException as e:
            if is_transient(e):
                log.warning("ydb_unavailable", extra={"event": what, "reason": reason_of(e)})
                raise YdbUnavailable(reason_of(e), self.options.retry_after_s) from e
            raise

    async def execute(
        self, query: str, params: dict[str, Any] | None = None, *, idempotent: bool = False
    ) -> list[Rows]:
        """Один запрос в своей транзакции (serializable, commit). `idempotent` — повтор и при Undetermined."""

        async def op() -> list[Rows]:
            result_sets = await self.pool.execute_with_retries(
                query,
                params,
                retry_settings=self._retry_settings(idempotent),
                settings=self._request_settings(),
            )
            return [list(rs.rows) for rs in result_sets]

        return await self._guard(op, "execute")

    async def ddl(self, statement: str) -> None:
        """DDL вне транзакции (миграции). Повторяется: DDL миграций идемпотентен."""

        async def op() -> None:
            await self.pool.execute_with_retries(
                statement, retry_settings=self._retry_settings(True), settings=self._request_settings()
            )

        await self._guard(op, "ddl")

    async def transaction(self, fn: Callable[[Tx], Awaitable[T]], *, idempotent: bool = False) -> T:
        """Выполнить `fn(tx)` в serializable-транзакции; при OCC-конфликте `fn` повторяется целиком.

        Коммит выполняет SDK после `fn` (если последний запрос не был с `commit=True`).
        """
        settings = self._request_settings()

        async def callee(tx: Any) -> T:
            return await fn(Tx(tx, settings))

        async def op() -> T:
            return await self.pool.retry_tx_async(
                callee, ydb.QuerySerializableReadWrite(), self._retry_settings(idempotent)
            )

        return await self._guard(op, "transaction")

    async def ping(self) -> None:
        """Проверка доступности для /healthz."""
        await self.execute("SELECT 1;", idempotent=True)

    async def close(self) -> None:
        await self.pool.stop()
        if self.driver is not None:
            await self.driver.stop()


async def open_client(settings: Settings) -> YdbClient:
    """Подключиться к YDB (fail fast) и создать пул сессий. Без YDB_ENDPOINT — ошибка вызова."""
    if not settings.ydb_endpoint:
        raise ValueError("YDB_ENDPOINT is not set")
    metering.install()  # до создания драйвера: подменяет фабрику gRPC-каналов
    driver = ydb.aio.Driver(
        endpoint=settings.ydb_endpoint, database=settings.ydb_database, credentials=credentials(settings)
    )
    try:
        await driver.wait(timeout=settings.ydb_connect_timeout_s, fail_fast=True)
    except BaseException as e:
        await driver.stop(timeout=1)  # по умолчанию stop ждёт до 10 с — не задерживаем ошибку старта
        log.error("ydb_connect_failed", extra={"reason": reason_of(e)})
        raise YdbUnavailable(reason_of(e), settings.ydb_unavailable_retry_after_s) from e
    pool = ydb.aio.QuerySessionPool(driver, size=settings.ydb_pool_size)
    return YdbClient(pool, YdbOptions.from_settings(settings), driver)
