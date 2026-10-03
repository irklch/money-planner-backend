"""Единый формат ошибок (09 — Error Model).

{ "error": { "code", "message", "status", "retryable", "requestId", "details": [...] } }
message — только для логов клиента, никогда не содержит пользовательских данных.
"""

from dataclasses import dataclass, field
from typing import Any

# code -> (status, retryable, message)
ERROR_CATALOG: dict[str, tuple[int, bool, str]] = {
    "invalid_request": (400, False, "Request could not be parsed"),
    "access_token_expired": (401, True, "Access token expired"),
    "access_token_invalid": (401, False, "Access token invalid"),
    "refresh_token_invalid": (401, False, "Refresh token invalid"),
    "refresh_token_expired": (401, False, "Refresh token expired"),
    "user_deleted": (401, False, "User deleted"),
    "payload_too_large": (413, False, "Payload too large"),
    "rate_limited": (429, True, "Too many requests"),
    "internal_error": (500, True, "Internal error"),
    "service_unavailable": (503, True, "Service unavailable"),
    "validation_failed": (422, False, "Validation failed"),
    "category_not_found": (404, False, "Category not found"),
    "category_readonly": (403, False, "System category is read-only"),
    "category_archived": (409, False, "Category is archived"),
    "category_name_taken": (409, False, "Category name already taken"),
    "expense_not_found": (404, False, "Expense not found"),
    "day_has_expenses": (409, False, "Day already has expenses"),
    "idempotency_in_progress": (409, True, "Request with this idempotency key is in progress"),
    "idempotency_key_reused": (409, False, "Idempotency key reused with a different request"),
    "insights_unavailable": (503, True, "Insights unavailable"),
    # Ошибки парсинга
    "unsupported_format": (415, False, "Unsupported file format"),
    "file_too_large": (413, False, "File too large"),
    "corrupted_file": (422, False, "File cannot be read"),
    "unknown_bank": (422, False, "Statement structure is not recognized"),
    "empty_statement": (422, False, "Statement has no operations"),
    "income_only": (422, False, "Statement has only income operations"),
    "processing_failed": (500, True, "Statement processing failed"),
    "not_found": (404, False, "Not found"),
    "method_not_allowed": (405, False, "Method not allowed"),
}


@dataclass
class ErrorDetail:
    code: str
    field: str | None = None
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code}
        if self.index is not None:
            d["index"] = self.index
        if self.field is not None:
            d["field"] = self.field
        return d


@dataclass
class ApiError(Exception):
    code: str
    details: list[ErrorDetail] = field(default_factory=list)
    retry_after: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CATALOG:
            raise ValueError(f"Unknown error code {self.code}")

    @property
    def status(self) -> int:
        return ERROR_CATALOG[self.code][0]

    @property
    def retryable(self) -> bool:
        return ERROR_CATALOG[self.code][1]

    @property
    def message(self) -> str:
        return ERROR_CATALOG[self.code][2]


def validation_error(*details: ErrorDetail) -> ApiError:
    return ApiError("validation_failed", details=list(details))


def error_body(code: str, request_id: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    status, retryable, message = ERROR_CATALOG[code]
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status,
            "retryable": retryable,
            "requestId": request_id,
            "details": details or [],
        }
    }
