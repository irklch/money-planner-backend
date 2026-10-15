import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.errors import ERROR_CATALOG, ApiError, error_body
from app.db.session import DB_ERROR_CLASSES, DatabaseUnavailable, is_unavailable

log = logging.getLogger("app.http")

# Коды details[], которые могут прийти из Pydantic-валидаторов (PydanticCustomError.type).
DETAIL_CODES = {
    "amount_invalid",
    "amount_too_large",
    "date_invalid",
    "date_in_future",
    "comment_too_long",
    "category_not_found",
    "category_archived",
    "name_required",
    "name_too_long",
    "emoji_required",
    "emoji_invalid",
    "range_invalid",
    "range_too_large",
}

# Код по умолчанию для поля, если Pydantic вернул свою стандартную ошибку (missing, type и т.п.).
FIELD_DEFAULT_CODE = {
    "amount": "amount_invalid",
    "date": "date_invalid",
    "comment": "comment_too_long",
    "categoryId": "category_not_found",
    "name": "name_required",
    "emoji": "emoji_required",
    "from": "range_invalid",
    "to": "range_invalid",
    "compareFrom": "range_invalid",
    "compareTo": "range_invalid",
    "limit": "range_invalid",
    # E2: /v1/auth/guest, /v1/auth/refresh
    "platform": "platform_invalid",
    "appVersion": "app_version_invalid",
    "refreshToken": "refresh_token_malformed",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or f"req_{uuid.uuid4().hex}"


def _json_error(request: Request, code: str, details=None, headers=None) -> JSONResponse:
    rid = _request_id(request)
    status = ERROR_CATALOG[code][0]
    h = {"X-Request-Id": rid, **(headers or {})}
    return JSONResponse(error_body(code, rid, details), status_code=status, headers=h)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """requestId + access-лог без query string, тел запросов и токенов."""

    async def dispatch(self, request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Без traceback и текста исключения: сообщения драйверов БД могут содержать значения.
            log.error(
                "unhandled_error",
                extra={"request_id": request.state.request_id, "exc_type": type(exc).__name__},
            )
            response = _json_error(request, "internal_error")
        response.headers["X-Request-Id"] = request.state.request_id
        route = request.scope.get("route")
        log.info(
            "request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response


def map_validation_errors(errors) -> tuple[str, list[dict]]:
    details: list[dict] = []
    for err in errors:
        loc = [p for p in err.get("loc", ()) if p not in ("body", "query", "path", "header")]
        etype = err.get("type", "")
        if etype in ("json_invalid", "model_attributes_type", "dict_type", "list_type") and len(loc) <= 1:
            return "invalid_request", []
        index = next((p for p in loc if isinstance(p, int)), None)
        field = next((p for p in reversed(loc) if isinstance(p, str)), None)
        if etype in DETAIL_CODES:
            code = etype
        elif field in FIELD_DEFAULT_CODE:
            code = FIELD_DEFAULT_CODE[field]
        else:
            return "invalid_request", []
        d = {"code": code}
        if index is not None:
            d["index"] = index
        if field is not None:
            d["field"] = field
        details.append(d)
    return "validation_failed", details


def install_handlers(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        headers = dict(exc.headers)
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return _json_error(request, exc.code, [d.to_dict() for d in exc.details], headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        code, details = map_validation_errors(exc.errors())
        return _json_error(request, code, details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(
            exc.status_code, "invalid_request"
        )
        return _json_error(request, code)

    async def _db_unavailable(request: Request, exc: Exception):
        # Текст исключения не логируется и не отдаётся: в нём могут быть адрес, имя БД и параметры.
        reason = exc.reason if isinstance(exc, DatabaseUnavailable) else type(exc).__name__
        log.warning("db_unavailable", extra={"request_id": _request_id(request), "reason": reason})
        retry_after = str(get_settings().db_unavailable_retry_after_s)
        return _json_error(request, "service_unavailable", headers={"Retry-After": retry_after})

    async def _db_error(request: Request, exc: Exception):
        if is_unavailable(exc):
            return await _db_unavailable(request, exc)
        log.error(
            "unhandled_error", extra={"request_id": _request_id(request), "exc_type": type(exc).__name__}
        )
        return _json_error(request, "internal_error")

    # Конкретные классы, а не Exception: их обрабатывает ExceptionMiddleware внутри приложения,
    # до RequestContextMiddleware, который превратил бы любую ошибку в 500.
    app.add_exception_handler(DatabaseUnavailable, _db_unavailable)
    for cls in DB_ERROR_CLASSES:
        app.add_exception_handler(cls, _db_error)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Только тип исключения: сообщения драйверов могут содержать значения параметров.
        log.error(
            "unhandled_error", extra={"request_id": _request_id(request), "exc_type": type(exc).__name__}
        )
        return _json_error(request, "internal_error")
