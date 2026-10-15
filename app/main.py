from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.config import get_settings
from app.core.handlers import install_handlers
from app.core.logging import setup_logging
from app.core.openapi import install_openapi
from app.db.session import dispose_engine, init_engine, ping
from app.modules.auth.router import router as auth_router
from app.modules.imports.router import router as imports_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings.cache_clear()
    settings = get_settings()
    setup_logging(settings.log_level)
    settings.check_production_ready()
    init_engine(settings)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Money Planner API",
        version="1.0.0",
        description=(
            "Контракт для iOS и будущего Android-клиента. Даты — YYYY-MM-DD, суммы — decimal-строки "
            "с 2 знаками (RUB). Все запросы, кроме /auth/*, — с Authorization: Bearer <accessToken> "
            "(гостевой токен устройства из POST /v1/auth/guest). "
            "Ошибки — единый формат { error: { code, message, status, retryable, requestId, details } }."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )
    install_handlers(app)
    install_openapi(app)

    # E1 (план §11.2): expenses, categories CRUD, calendar, analytics, insights уходят на iOS или вне MVP
    # и отключены из приложения. E2: /auth/guest и /auth/refresh на таблицах MVP; старый /auth/anonymous
    # заменён, старый DELETE /me (субъект — legacy_users) отключён до DELETE /account (E7).
    # Код отключённых модулей удаляется в E11; imports заменяется в E3–E4.
    v1 = APIRouter(prefix="/v1")
    for r in (auth_router, imports_router):
        v1.include_router(r)
    app.include_router(v1)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Контракт E0: 200 {status: ok}, если БД отвечает; иначе 503 service_unavailable + Retry-After."""
        await ping(get_settings().db_health_timeout_s)
        return {"status": "ok"}

    return app


app = create_app()
