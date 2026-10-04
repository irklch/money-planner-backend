import base64
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorDetail, validation_error
from app.core.schemas import format_money
from app.core.security import utcnow
from app.db.models import Expense, ExpenseImport, ExpenseSource
from app.modules.calendar.service import clear_free_days, lock_days
from app.modules.categories.service import CategoryResolver
from app.modules.expenses.schemas import ExpenseCreate, ExpenseOut, ExpenseUpdate, ImportRequest, ImportResult


def to_out(e: Expense) -> ExpenseOut:
    return ExpenseOut(
        id=e.id,
        date=e.date,
        amount=e.amount,
        category_id=e.category_id,
        comment=e.comment,
        source=e.source.value if isinstance(e.source, ExpenseSource) else e.source,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


# ---------- Чтение ----------


def _encode_cursor(e: Expense) -> str:
    raw = json.dumps([e.date.isoformat(), e.created_at.isoformat(), str(e.id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[date, datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        d, c, i = json.loads(raw)
        return date.fromisoformat(d), datetime.fromisoformat(c), uuid.UUID(i)
    except (ValueError, TypeError) as e:
        raise ApiError("invalid_request") from e


async def list_expenses(
    session: AsyncSession,
    user_id: uuid.UUID,
    start: date,
    end: date,
    category_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Expense], str | None]:
    q = select(Expense).where(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
    if category_id is not None:
        q = q.where(Expense.category_id == category_id)
    if cursor:
        cd, cc, ci = _decode_cursor(cursor)
        q = q.where(
            or_(
                Expense.date < cd,
                and_(Expense.date == cd, Expense.created_at < cc),
                and_(Expense.date == cd, Expense.created_at == cc, Expense.id < ci),
            )
        )
    q = q.order_by(Expense.date.desc(), Expense.created_at.desc(), Expense.id.desc()).limit(limit + 1)
    rows = list((await session.scalars(q)).all())
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


# ---------- Запись ----------


def _check_date(d: date, today: date, index: int | None = None) -> ErrorDetail | None:
    if d > today:
        return ErrorDetail(code="date_in_future", field="date", index=index)
    return None


async def create_expense(
    session: AsyncSession, user_id: uuid.UUID, key: uuid.UUID, body: ExpenseCreate, today: date
) -> tuple[Expense, bool]:
    """Idempotency-Key = id нового Expense: повтор с тем же ключом не создаёт дубль."""
    existing = await session.get(Expense, key)
    if existing is not None:
        if existing.user_id != user_id:
            raise ApiError("idempotency_key_reused")
        return existing, True

    errors = []
    if (err := _check_date(body.date, today)) is not None:
        errors.append(err)
    resolver = await CategoryResolver.load(session, user_id, {body.category_id})
    if (code := resolver.check(body.category_id)) is not None:
        errors.append(ErrorDetail(code=code, field="categoryId"))
    if errors:
        raise validation_error(*errors)

    await lock_days(session, user_id, [body.date])
    expense = Expense(
        id=key,
        user_id=user_id,
        category_id=body.category_id,
        amount=body.amount,
        date=body.date,
        comment=body.comment,
        source=ExpenseSource.manual,
    )
    session.add(expense)
    await clear_free_days(session, user_id, [body.date])
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        # Параллельный запрос с тем же ключом успел первым.
        existing = await session.get(Expense, key)
        if existing is not None and existing.user_id == user_id:
            return existing, True
        raise
    await session.refresh(expense)
    return expense, False


async def _get_owned(
    session: AsyncSession, user_id: uuid.UUID, expense_id: uuid.UUID, lock: bool = False
) -> Expense:
    q = select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id)
    if lock:
        q = q.with_for_update()
    e = await session.scalar(q)
    if e is None:
        raise ApiError("expense_not_found")
    return e


async def update_expense(
    session: AsyncSession,
    user_id: uuid.UUID,
    expense_id: uuid.UUID,
    body: ExpenseUpdate,
    today: date | None,
) -> Expense:
    e = await _get_owned(session, user_id, expense_id, lock=True)
    fields = body.model_fields_set
    errors = []
    if "date" in fields:
        if body.date is None:
            errors.append(ErrorDetail(code="date_invalid", field="date"))
        elif today is None:
            raise ApiError(
                "invalid_request", details=[ErrorDetail(code="timezone_required", field="X-Timezone")]
            )
        elif (err := _check_date(body.date, today)) is not None:
            errors.append(err)
    if "amount" in fields and body.amount is None:
        errors.append(ErrorDetail(code="amount_invalid", field="amount"))
    if "category_id" in fields:
        if body.category_id is None:
            errors.append(ErrorDetail(code="category_not_found", field="categoryId"))
        else:
            resolver = await CategoryResolver.load(session, user_id, {body.category_id})
            # Текущую категорию можно оставить, даже если она архивная.
            if (code := resolver.check(body.category_id, allow_archived_id=e.category_id)) is not None:
                errors.append(ErrorDetail(code=code, field="categoryId"))
    if errors:
        raise validation_error(*errors)

    if "date" in fields and body.date != e.date:
        await lock_days(session, user_id, [body.date])
        e.date = body.date
        await clear_free_days(session, user_id, [body.date])
    if "amount" in fields:
        e.amount = body.amount
    if "category_id" in fields:
        e.category_id = body.category_id
    if "comment" in fields:
        e.comment = body.comment
    e.updated_at = utcnow()
    await session.commit()
    await session.refresh(e)
    return e


async def delete_expense(session: AsyncSession, user_id: uuid.UUID, expense_id: uuid.UUID) -> None:
    result = await session.execute(
        delete(Expense).where(Expense.id == expense_id, Expense.user_id == user_id)
    )
    await session.commit()
    if result.rowcount == 0:
        raise ApiError("expense_not_found")


# ---------- Импорт ----------


def _import_lock_key(key: uuid.UUID) -> int:
    return int.from_bytes(key.bytes[:8], "big", signed=True)


def import_request_hash(body: ImportRequest) -> bytes:
    """SHA-256 нормализованного валидированного тела POST /expenses/import.

    Считается не по сырым байтам, а по уже провалидированной модели, поэтому не зависит
    от порядка JSON-ключей, пробелов и форматирования. Нормализация:
    - date — YYYY-MM-DD;
    - amount — decimal-строка ровно с 2 знаками («100», «100.0», «100.00» → «100.00»);
    - categoryId — UUID в каноническом виде (нижний регистр, с дефисами);
    - comment — после trim, пустой или отсутствующий → null;
    - порядок операций сохраняется: это часть тела.
    Префикс версии позволяет изменить нормализацию в будущем, не путая старые отпечатки.
    """
    canonical = [
        {
            "amount": format_money(x.amount),
            "categoryId": str(x.category_id),
            "comment": x.comment,
            "date": x.date.isoformat(),
        }
        for x in body.expenses
    ]
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"expense-import/v1\n" + raw.encode("utf-8")).digest()


def _stored_result(record: ExpenseImport) -> ImportResult:
    return ImportResult(
        imported_count=record.imported_count, date_from=record.date_from, date_to=record.date_to
    )


async def import_expenses(
    session: AsyncSession, user_id: uuid.UUID, key: uuid.UUID, body: ImportRequest, today: date
) -> tuple[ImportResult, bool]:
    """Атомарное сохранение финального импорта (03 — Import Flow).

    Одна транзакция: expense_imports (ключ, отпечаток тела, результат) + все expenses +
    снятие «Бесплатных дней» с дат импорта. Всё или ничего.

    Повтор с тем же ключом:
    - то же тело → сохранённый результат первоначального сохранения (Idempotent-Replayed),
      расходы не создаются; последующие правки и удаления расходов на ответ не влияют;
    - другое тело или чужой ключ → 409 idempotency_key_reused;
    - первый запрос ещё выполняется → 409 idempotency_in_progress + Retry-After.
    Проверка повтора идёт до валидации: если категорию архивировали после успешного
    сохранения, повтор всё равно вернёт сохранённый результат.
    """
    request_hash = import_request_hash(body)
    got_lock = await session.scalar(select(func.pg_try_advisory_xact_lock(_import_lock_key(key))))
    if not got_lock:
        await session.rollback()
        raise ApiError("idempotency_in_progress", retry_after=2)

    existing = await session.get(ExpenseImport, key)
    if existing is not None:
        same_owner = existing.user_id == user_id
        same_body = hmac.compare_digest(existing.request_hash, request_hash)
        result = _stored_result(existing)
        await session.rollback()
        if not (same_owner and same_body):
            raise ApiError("idempotency_key_reused")
        return result, True

    errors: list[ErrorDetail] = []
    resolver = await CategoryResolver.load(session, user_id, {x.category_id for x in body.expenses})
    for i, x in enumerate(body.expenses):
        if (err := _check_date(x.date, today, index=i)) is not None:
            errors.append(err)
        if (code := resolver.check(x.category_id)) is not None:
            errors.append(ErrorDetail(code=code, field="categoryId", index=i))
    if errors:
        await session.rollback()
        raise validation_error(*errors)

    dates = [x.date for x in body.expenses]
    record = ExpenseImport(
        id=key,
        user_id=user_id,
        request_hash=request_hash,
        imported_count=len(body.expenses),
        date_from=min(dates),
        date_to=max(dates),
    )
    await lock_days(session, user_id, dates)
    session.add(record)
    await session.flush()
    session.add_all(
        Expense(
            id=uuid.uuid4(),
            user_id=user_id,
            category_id=x.category_id,
            amount=x.amount,
            date=x.date,
            comment=x.comment,
            source=ExpenseSource.import_,
        )
        for x in body.expenses
    )
    await clear_free_days(session, user_id, dates)
    result = _stored_result(record)
    await session.commit()
    return result, False
