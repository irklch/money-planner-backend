from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.config import get_settings
from app.core.handlers import install_handlers
from app.core.logging import setup_logging
from app.core.secrets import load_lockbox_into_env
from app.db.session import dispose_engine, init_engine
from app.modules.account.router import router as account_router
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.router import router as auth_router
from app.modules.calendar.router import router as calendar_router
from app.modules.categories.router import router as categories_router
from app.modules.expenses.router import router as expenses_router
from app.modules.imports.router import router as imports_router
from app.modules.insights.router import router as insights_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_lockbox_into_env()
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
            "с 2 знаками (RUB). Все запросы, кроме /auth/*, — с Authorization: Bearer <accessToken>. "
            "Ошибки — единый формат { error: { code, message, status, retryable, requestId, details } }."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )
    install_handlers(app)

    v1 = APIRouter(prefix="/v1")
    for r in (
        auth_router,
        account_router,
        categories_router,
        imports_router,
        expenses_router,
        calendar_router,
        analytics_router,
        insights_router,
    ):
        v1.include_router(r)
    app.include_router(v1)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
