import asyncio
import ssl
from collections.abc import AsyncIterator

import asyncpg
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict = {}
    if settings.db_ssl_root_cert:
        ctx = ssl.create_default_context(cafile=settings.db_ssl_root_cert)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        connect_args["ssl"] = ctx
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        echo=False,  # echo логирует параметры — запрещено
        connect_args=connect_args,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = build_engine(settings or get_settings())
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


class DatabaseUnavailable(Exception):
    """БД недоступна: в ответе — 503 service_unavailable + Retry-After (контракт E0). reason — только тип."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Ошибки соединения, а не запроса: отказ подключения, разрыв, таймаут пула, перегрузка сервера БД.
_UNAVAILABLE_ORIG = (
    OSError,
    TimeoutError,
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.ConnectionDoesNotExistError,
)


# Классы, на которые вешается обработчик: внутри него is_unavailable отличает недоступность от ошибки запроса.
DB_ERROR_CLASSES = (sa_exc.DBAPIError, sa_exc.TimeoutError, *_UNAVAILABLE_ORIG)


def is_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (DatabaseUnavailable, sa_exc.TimeoutError, *_UNAVAILABLE_ORIG)):
        return True
    if isinstance(exc, sa_exc.DBAPIError):
        return (
            exc.connection_invalidated
            or isinstance(exc.orig, _UNAVAILABLE_ORIG)
            or (isinstance(exc.orig, Exception) and isinstance(exc.orig.__cause__, _UNAVAILABLE_ORIG))
        )
    return False


async def ping(limit_s: float) -> None:
    """Готовность БД для /healthz: SELECT 1 через пул за limit_s секунд."""
    try:
        async with asyncio.timeout(limit_s):
            async with init_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as e:
        raise DatabaseUnavailable(type(e).__name__) from None
