import uuid

from fastapi import APIRouter, Query, Request, Response
from pydantic import ValidationError

from app.core.deps import CurrentUserDep, IdempotencyKeyDep, SessionDep, SettingsDep
from app.core.errors import ApiError, ErrorDetail, validation_error
from app.core.handlers import map_validation_errors
from app.core.rate_limit import limiter
from app.core.schemas import parse_date_param
from app.modules.expenses import service
from app.modules.expenses.schemas import (
    ExpenseCreate,
    ExpenseOut,
    ExpensePage,
    ExpenseUpdate,
    ImportRequest,
    ImportResult,
)

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.get("", response_model=ExpensePage)
async def list_expenses(
    user: CurrentUserDep,
    session: SessionDep,
    from_: str = Query(alias="from"),
    to: str = Query(),
    categoryId: uuid.UUID | None = None,  # noqa: N803
    limit: int = Query(default=50),
    cursor: str | None = None,
) -> ExpensePage:
    start, end = parse_date_param(from_, "range_invalid"), parse_date_param(to, "range_invalid")
    if start > end or not 1 <= limit <= 200:
        raise validation_error(ErrorDetail(code="range_invalid"))
    rows, next_cursor = await service.list_expenses(session, user.id, start, end, categoryId, limit, cursor)
    return ExpensePage(items=[service.to_out(e) for e in rows], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=ExpenseOut)
async def create_expense(
    body: ExpenseCreate, response: Response, user: CurrentUserDep, key: IdempotencyKeyDep, session: SessionDep
) -> ExpenseOut:
    e, replayed = await service.create_expense(session, user.id, key, body)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return service.to_out(e)


@router.patch("/{expense_id}", response_model=ExpenseOut)
async def update_expense(
    expense_id: uuid.UUID, body: ExpenseUpdate, user: CurrentUserDep, session: SessionDep
) -> ExpenseOut:
    return service.to_out(await service.update_expense(session, user.id, expense_id, body))


@router.delete("/{expense_id}", status_code=204)
async def delete_expense(expense_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> Response:
    await service.delete_expense(session, user.id, expense_id)
    return Response(status_code=204)


@router.post(
    "/import",
    status_code=201,
    response_model=ImportResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ImportRequest"}}},
        }
    },
)
async def import_expenses(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    key: IdempotencyKeyDep,
    session: SessionDep,
    settings: SettingsDep,
) -> ImportResult:
    limiter.hit(f"import:{user.id}", settings.rate_import_per_minute)
    raw = await _read_limited(request, settings.max_import_body_bytes)
    try:
        body = ImportRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
        if any(tuple(e.get("loc", ())) == ("expenses",) for e in errors):
            # Пустой массив (или не массив) — импорт отклоняется, запись идемпотентности не создаётся.
            raise ApiError("validation_failed") from None
        code, details = map_validation_errors(errors)
        if code != "validation_failed":
            raise ApiError(code) from None
        raise ApiError(code, details=[ErrorDetail(**d) for d in details]) from None
    if len(body.expenses) > settings.max_import_expenses:
        raise ApiError("payload_too_large")
    result, replayed = await service.import_expenses(session, user.id, key, body)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError("payload_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError("payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)
