"""Фактические измерения обращений к YDB.

YDB возвращает стоимость каждого RPC в trailing metadata `x-ydb-consumed-units` — это значение
считает сам сервер (то же, что тарифицируется в Serverless). Python SDK его не отдаёт, поэтому
перехватываем gRPC-каналы драйвера: подменяем фабрику каналов в `ydb.aio.connection` и добавляем
interceptor-ы. Счётчики живут в contextvar: каждый запрос API или шаг benchmark меряется отдельно.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

import grpc
import ydb.aio.connection as _aio_conn

CONSUMED_UNITS = "x-ydb-consumed-units"
# Долгоживущий стрим сессии и служебные RPC не относятся к конкретному запросу.
_IGNORED = ("AttachSession", "ListEndpoints", "CreateSession", "DeleteSession")


@dataclass
class Meter:
    ru: int = 0
    calls: Counter[str] = field(default_factory=Counter)
    _pending: list[asyncio.Future[None]] = field(default_factory=list)

    @property
    def ydb_calls(self) -> int:
        return sum(self.calls.values())

    def _record(self, method: str, metadata: object) -> None:
        self.calls[method] += 1
        for key, value in metadata or ():  # type: ignore[union-attr]
            if key == CONSUMED_UNITS:
                self.ru += int(value)

    async def settle(self) -> None:
        """Дождаться trailing metadata всех стримов, начатых в этом контексте."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()


_current: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("ydb_meter", default=None)


@contextlib.contextmanager
def metered() -> Iterator[Meter]:
    m = Meter()
    token = _current.set(m)
    try:
        yield m
    finally:
        _current.reset(token)


def _short(method: object) -> str:
    name = method.decode() if isinstance(method, bytes) else str(method)
    return name.rsplit("/", 1)[-1]


class _UnaryStream(grpc.aio.UnaryStreamClientInterceptor):
    async def intercept_unary_stream(self, continuation, client_call_details, request):  # type: ignore[override]
        call = await continuation(client_call_details, request)
        meter = _current.get()
        method = _short(client_call_details.method)
        if meter is not None and method not in _IGNORED:

            async def collect() -> None:
                meter._record(method, await call.trailing_metadata())

            meter._pending.append(asyncio.ensure_future(collect()))
        return call


class _UnaryUnary(grpc.aio.UnaryUnaryClientInterceptor):
    async def intercept_unary_unary(self, continuation, client_call_details, request):  # type: ignore[override]
        call = await continuation(client_call_details, request)
        meter = _current.get()
        method = _short(client_call_details.method)
        if meter is not None and method not in _IGNORED:
            meter._record(method, await call.trailing_metadata())
        return call


class _Provider:
    """Подмена модуля grpc.aio для channel_factory: те же каналы + interceptor-ы."""

    _interceptors = [_UnaryStream(), _UnaryUnary()]

    def insecure_channel(self, target, options=None, compression=None):
        return grpc.aio.insecure_channel(target, options, compression, interceptors=self._interceptors)

    def secure_channel(self, target, credentials, options=None, compression=None):
        return grpc.aio.secure_channel(
            target, credentials, options, compression, interceptors=self._interceptors
        )


_installed = False


def install() -> None:
    """Включить учёт. Вызывать до создания ydb.aio.Driver."""
    global _installed
    if _installed:
        return
    original = _aio_conn.channel_factory

    def factory(endpoint, driver_config, channel_provider=None, endpoint_options=None):
        return original(endpoint, driver_config, _Provider(), endpoint_options=endpoint_options)

    _aio_conn.channel_factory = factory
    _installed = True
