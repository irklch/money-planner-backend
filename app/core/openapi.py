"""Документирование ошибок в OpenAPI: единый конверт (09 — Error Model) и коды по каждому endpoint."""

from collections import defaultdict
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.errors import ERROR_CATALOG

AUTH_ERRORS = ("access_token_expired", "access_token_invalid", "user_deleted")

# details[] для 400 invalid_request — по заголовкам.
HEADER_DETAIL_CODES = {
    "Idempotency-Key": "отсутствует или не UUID",
    "X-Timezone": "timezone_required — нет или пустой; timezone_invalid — не IANA-имя (например «+03:00»)",
}
ENDPOINT_HEADERS: dict[tuple[str, str], tuple[str, ...]] = {
    ("post", "/v1/auth/anonymous"): ("Idempotency-Key",),
    ("post", "/v1/categories"): ("Idempotency-Key",),
    ("post", "/v1/expenses"): ("Idempotency-Key", "X-Timezone"),
    ("patch", "/v1/expenses/{expense_id}"): ("X-Timezone",),
    ("post", "/v1/expenses/import"): ("Idempotency-Key", "X-Timezone"),
    ("put", "/v1/calendar/acknowledgements/{day}"): ("X-Timezone",),
}

# (method, path) → коды ошибок из 08 — Endpoint Contracts (+ docs/contract-changes.md)
ENDPOINT_ERRORS: dict[tuple[str, str], tuple[tuple[str, ...], bool]] = {
    ("post", "/v1/auth/anonymous"): (
        ("invalid_request", "idempotency_key_reused", "rate_limited", "service_unavailable"),
        False,
    ),
    ("post", "/v1/auth/refresh"): (
        (
            "refresh_token_invalid",
            "refresh_token_expired",
            "user_deleted",
            "rate_limited",
            "service_unavailable",
        ),
        False,
    ),
    ("delete", "/v1/me"): (("service_unavailable",), True),
    ("get", "/v1/categories"): ((), True),
    ("post", "/v1/categories"): (
        ("invalid_request", "validation_failed", "category_name_taken", "idempotency_key_reused"),
        True,
    ),
    ("patch", "/v1/categories/{category_id}"): (
        (
            "invalid_request",
            "category_not_found",
            "category_readonly",
            "category_archived",
            "category_name_taken",
            "validation_failed",
        ),
        True,
    ),
    ("delete", "/v1/categories/{category_id}"): (("category_not_found", "category_readonly"), True),
    ("post", "/v1/imports/parse"): (
        (
            "unsupported_format",
            "file_too_large",
            "corrupted_file",
            "unknown_bank",
            "empty_statement",
            "income_only",
            "rate_limited",
            "processing_failed",
        ),
        True,
    ),
    ("get", "/v1/expenses"): (("validation_failed", "invalid_request"), True),
    ("post", "/v1/expenses"): (("invalid_request", "validation_failed", "idempotency_key_reused"), True),
    ("patch", "/v1/expenses/{expense_id}"): (
        ("invalid_request", "expense_not_found", "validation_failed"),
        True,
    ),
    ("delete", "/v1/expenses/{expense_id}"): (("expense_not_found",), True),
    ("post", "/v1/expenses/import"): (
        (
            "invalid_request",
            "validation_failed",
            "idempotency_in_progress",
            "idempotency_key_reused",
            "payload_too_large",
            "rate_limited",
            "service_unavailable",
        ),
        True,
    ),
    ("get", "/v1/calendar/days"): (("validation_failed",), True),
    ("put", "/v1/calendar/acknowledgements/{day}"): (
        ("invalid_request", "day_has_expenses", "validation_failed"),
        True,
    ),
    ("delete", "/v1/calendar/acknowledgements/{day}"): (("validation_failed",), True),
    ("get", "/v1/analytics/summary"): (("validation_failed",), True),
    ("get", "/v1/analytics/dynamics"): (("validation_failed",), True),
    ("get", "/v1/insights"): (("validation_failed", "rate_limited", "insights_unavailable"), True),
}

ERROR_SCHEMA = {
    "ErrorDetail": {
        "type": "object",
        "required": ["code"],
        "properties": {
            "code": {"type": "string"},
            "field": {"type": "string"},
            "index": {"type": "integer", "description": "Индекс элемента массива (импорт)"},
        },
    },
    "ErrorEnvelope": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message", "status", "retryable", "requestId", "details"],
                "properties": {
                    "code": {"type": "string", "enum": sorted(ERROR_CATALOG)},
                    "message": {"type": "string", "description": "Только для логов, не показывать"},
                    "status": {"type": "integer"},
                    "retryable": {"type": "boolean"},
                    "requestId": {"type": "string"},
                    "details": {"type": "array", "items": {"$ref": "#/components/schemas/ErrorDetail"}},
                },
            }
        },
    },
}


def _responses(codes: tuple[str, ...], authed: bool, headers: tuple[str, ...]) -> dict[str, Any]:
    by_status: dict[int, list[str]] = defaultdict(list)
    for code in (*codes, *(AUTH_ERRORS if authed else ())):
        by_status[ERROR_CATALOG[code][0]].append(code)
    out = {}
    for status, cs in sorted(by_status.items()):
        lines = [f"`{c}`" + (" (retryable)" if ERROR_CATALOG[c][1] else "") for c in cs]
        desc = " · ".join(lines)
        if status == 400 and headers:
            desc += ". Заголовки: " + "; ".join(f"{h} — {HEADER_DETAIL_CODES[h]}" for h in headers)
        out[str(status)] = {
            "description": desc,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}},
        }
    return out


def install_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        components.update(ERROR_SCHEMA)
        for path, ops in schema.get("paths", {}).items():
            for method, op in ops.items():
                responses = op.setdefault("responses", {})
                responses.pop("422", None)  # стандартная ошибка FastAPI заменяется нашим конвертом
                codes, authed = ENDPOINT_ERRORS.get((method, path), ((), True))
                responses.update(_responses(codes, authed, ENDPOINT_HEADERS.get((method, path), ())))
                if authed:
                    op["security"] = [{"bearerAuth": []}]
        schema["components"]["securitySchemes"] = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
