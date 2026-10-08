"""Минимальный FastAPI: /health, POST /sync/push, GET /sync/pull.

Запуск: `uvicorn syncproto.api:app` (настройки из окружения, см. config.py).
Заголовки ответа `X-YDB-RU`, `X-YDB-Calls`, `X-YDB-Attempts`, `Server-Timing` — для benchmark.
"""

from __future__ import annotations

# json — заголовок статистики; os — размер пула; resource/sys — память процесса; time — замеры старта.
import json
import os
import resource
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

# ydb — ошибки YDB для ответа 503; FastAPI — маршруты, зависимости, заголовки.
import ydb
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import auth, schema
from .config import Settings
from .engine import InvalidMutation, OpStats, ResyncRequired, Store, SyncEngine
from .models import MAX_PULL_LIMIT, MAX_PUSH_BODY_BYTES, Mutation, PullResponse, PushRequest, PushResponse

# Момент импорта модуля ≈ старт процесса; и метрики старта экземпляра для /health (холодный старт).
_PROCESS_T0 = time.perf_counter()
_STARTUP: dict[str, Any] = {}


# Пиковое потребление памяти процессом, МБ (на macOS ru_maxrss в байтах, на Linux — в КБ).
def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


# Фабрика приложения. Тесты передают готовое хранилище; в облаке/контейнере оно создаётся при старте.
def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    # Старт и остановка экземпляра: подключение к YDB и замер времени каждого шага.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or Settings.from_env()
        s.validate()
        app.state.settings = s
        driver = None
        t0 = time.perf_counter()
        # Хранилище передано снаружи — подключаться к YDB не нужно.
        if getattr(app.state, "engine", None) is not None:  # хранилище передано снаружи (тесты)
            yield
            return
        if store is None:
            from .store_ydb import YdbStore, open_driver

            # Подключение к YDB (discovery, аутентификация).
            driver = await open_driver(s)
            t1 = time.perf_counter()
            # Пул сессий YDB; размер — по ожидаемой параллельности запросов в экземпляре.
            pool = ydb.aio.QuerySessionPool(driver, size=int(os.environ.get("YDB_POOL_SIZE", "10")))
            if s.auto_schema:
                await schema.apply(pool, schema.ddl(s.ydb_table_prefix))
            # Первое обращение к YDB: создание сессии + простой запрос.
            await pool.execute_with_retries("SELECT 1")
            t2 = time.perf_counter()
            app.state.store = YdbStore(pool, s.ydb_table_prefix, collect_stats=s.collect_stats)
            _STARTUP.update(
                driver_ready_ms=round((t1 - t0) * 1000, 1), first_query_ms=round((t2 - t1) * 1000, 1)
            )
        # Движок sync с выбранной стратегией.
        app.state.engine = SyncEngine(app.state.store, s.sync_strategy)
        _STARTUP.update(
            since_process_start_ms=round((time.perf_counter() - _PROCESS_T0) * 1000, 1), requests_served=0
        )
        # Остановка экземпляра: закрыть сессии и драйвер.
        try:
            yield
        finally:
            if driver is not None:
                await app.state.store.pool.stop()
                await driver.stop()

    # Без Swagger и ReDoc — прототип не публикует документацию наружу.
    app = FastAPI(title="Money Planner sync prototype", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Хранилище передано (тесты): состояние задаём сразу — ASGI-транспорт httpx не запускает lifespan.
    if store is not None:
        if settings is None:
            raise ValueError("settings are required with an injected store")
        settings.validate()
        app.state.settings = settings
        app.state.store = store
        app.state.engine = SyncEngine(store, settings.sync_strategy)

    # Ошибка формы запроса → 422 со списком полей (без значений).
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "validation_error", "details": errors}, status_code=422)

    # Ограничение размера тела по заголовку + счётчик запросов экземпляра (признак холодного старта).
    @app.middleware("http")
    async def _limits(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > MAX_PUSH_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        _STARTUP["requests_served"] = _STARTUP.get("requests_served", 0) + 1
        return await call_next(request)

    # Пользователь — только из подписанного токена Authorization: Bearer <JWT>.
    def current_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        s: Settings = request.app.state.settings
        if not authorization or not authorization.startswith("Bearer "):
            raise _Unauthorized()
        try:
            return auth.verify(authorization.removeprefix("Bearer "), s.jwt_secret, s.jwt_audience)
        except auth.AuthError as e:
            raise _Unauthorized() from e

    # Нет токена или он неверный → 401.
    @app.exception_handler(_Unauthorized)
    async def _unauth(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )

    # Курсор старше горизонта очистки tombstones → 410: клиенту нужна полная перезагрузка.
    @app.exception_handler(ResyncRequired)
    async def _resync(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse({"error": "resync_required"}, status_code=410)

    @app.exception_handler(ydb.issues.Aborted)
    @app.exception_handler(ydb.issues.Overloaded)
    @app.exception_handler(ydb.issues.Unavailable)
    async def _busy(_: Request, __: Exception) -> JSONResponse:
        # Повторы внутри запроса исчерпаны (конкурентные push одного пользователя или перегрузка).
        # Push идемпотентен по mutationId — клиент безопасно повторит.
        return JSONResponse({"error": "busy_retry"}, status_code=503, headers={"Retry-After": "1"})

    # Мутация не подходит стратегии (например, нет HLC) → 422.
    @app.exception_handler(InvalidMutation)
    async def _invalid(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "invalid_mutation", "detail": str(exc)}, status_code=422)

    # Проверка живости. Подробности (времена старта, память) — только с валидным токеном.
    @app.get("/health")
    async def health(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        s: Settings = app.state.settings
        try:
            current_user(request, authorization)
        except _Unauthorized:
            return {"status": "ok"}  # публично — без деталей экземпляра
        return {
            "status": "ok",
            "strategy": s.sync_strategy,
            "env": s.env,
            "startup": {k: v for k, v in _STARTUP.items() if k != "requests_served"},
            "requestsServedByInstance": _STARTUP.get("requests_served", 0),
            "uptimeMs": round((time.perf_counter() - _PROCESS_T0) * 1000, 1),
            "maxRssMb": _rss_mb(),
        }

    # POST /sync/push — применить пачку мутаций пользователя.
    @app.post("/sync/push", response_model=PushResponse, response_model_by_alias=True)
    async def push(
        body: PushRequest, response: Response, user_id: str = Depends(current_user)
    ) -> PushResponse:
        t0 = time.perf_counter()
        # Перевод мутаций во внутренний формат и вызов движка.
        results, stats = await app.state.engine.push(
            user_id, body.device_id, [Mutation.from_api(m) for m in body.mutations]
        )
        _stats_headers(response, stats, t0)
        return PushResponse(results=[r.to_api() for r in results])

    # GET /sync/pull?cursor=N&limit=M — изменения после курсора, по возрастанию версии.
    @app.get("/sync/pull", response_model=PullResponse, response_model_by_alias=True)
    async def pull(
        response: Response,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_PULL_LIMIT)] = MAX_PULL_LIMIT,
        user_id: str = Depends(current_user),
    ) -> PullResponse:
        t0 = time.perf_counter()
        page, stats = await app.state.engine.pull(user_id, cursor, limit)
        _stats_headers(response, stats, t0)
        return PullResponse(
            records=[r.to_api() for r in page.records], next_cursor=page.next_cursor, has_more=page.has_more
        )

    return app


# Внутреннее исключение для 401.
class _Unauthorized(Exception):
    pass


# Заголовки ответа с измерениями: RU, обращения к YDB, попытки транзакции, время обработки.
def _stats_headers(response: Response, stats: OpStats, t0: float) -> None:
    response.headers["X-YDB-RU"] = str(stats.ru)
    response.headers["X-YDB-Calls"] = str(stats.ydb_calls)
    response.headers["X-YDB-Attempts"] = str(stats.attempts)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    if stats.tables:  # только при YdbStore(collect_stats=True) — benchmark
        response.headers["X-YDB-Stats"] = json.dumps(
            {
                "readRows": stats.read_rows,
                "readBytes": stats.read_bytes,
                "writeRows": stats.write_rows,
                "writeBytes": stats.write_bytes,
                "cpuUs": stats.cpu_us,
                "ruIoFormula": stats.ru_io_formula,
                "tables": stats.tables,
            },
            separators=(",", ":"),
        )


def __getattr__(name: str) -> Any:
    # `uvicorn syncproto.api:app` — приложение создаётся лениво, чтобы импорт модуля в тестах
    # не требовал переменных окружения.
    if name == "app":
        return create_app()
    raise AttributeError(name)
