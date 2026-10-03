import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import iter_days, latest_allowed_date
from app.core.errors import ApiError, ErrorDetail, validation_error
from app.db.models import Expense, FreeDay

MAX_RANGE_DAYS = 366


async def clear_free_days(session: AsyncSession, user_id: uuid.UUID, dates: Iterable[date]) -> None:
    """Инвариант Expense XOR «Бесплатный день»: вызывается в транзакции записи Expense."""
    dates = list(set(dates))
    if dates:
        await session.execute(delete(FreeDay).where(FreeDay.user_id == user_id, FreeDay.date.in_(dates)))


async def get_days(session: AsyncSession, user_id: uuid.UUID, start: date, end: date) -> list[dict]:
    counts = dict(
        (
            await session.execute(
                select(Expense.date, func.count())
                .where(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
                .group_by(Expense.date)
            )
        ).all()
    )
    free = set(
        (
            await session.scalars(
                select(FreeDay.date).where(
                    FreeDay.user_id == user_id, FreeDay.date >= start, FreeDay.date <= end
                )
            )
        ).all()
    )
    out = []
    for d in iter_days(start, end):
        n = int(counts.get(d, 0))
        ack = d in free and n == 0
        out.append(
            {"date": d, "expense_count": n, "is_acknowledged_empty": ack, "is_accounted": n > 0 or ack}
        )
    return out


async def day_state(session: AsyncSession, user_id: uuid.UUID, d: date) -> dict:
    return (await get_days(session, user_id, d, d))[0]


async def acknowledge(session: AsyncSession, user_id: uuid.UUID, d: date) -> dict:
    if d > latest_allowed_date():
        raise validation_error(ErrorDetail(code="date_in_future", field="date"))
    # Сериализуем с записью расходов этого пользователя на ту же дату.
    await session.execute(select(func.pg_advisory_xact_lock(day_lock_key(user_id, d))))
    has_expenses = await session.scalar(
        select(func.count()).select_from(Expense).where(Expense.user_id == user_id, Expense.date == d)
    )
    if has_expenses:
        await session.rollback()
        raise ApiError("day_has_expenses")
    await session.execute(insert(FreeDay).values(user_id=user_id, date=d).on_conflict_do_nothing())
    await session.commit()
    return await day_state(session, user_id, d)


async def unacknowledge(session: AsyncSession, user_id: uuid.UUID, d: date) -> dict:
    await session.execute(delete(FreeDay).where(FreeDay.user_id == user_id, FreeDay.date == d))
    await session.commit()
    return await day_state(session, user_id, d)


def day_lock_key(user_id: uuid.UUID, d: date) -> int:
    return int.from_bytes(user_id.bytes[:8], "big", signed=True) ^ d.toordinal()


async def lock_days(session: AsyncSession, user_id: uuid.UUID, dates: Iterable[date]) -> None:
    """Advisory-локи по (user, date) в стабильном порядке.

    Защищают инвариант от гонки PUT «Бесплатный день» и записи Expense на ту же дату.
    """
    keys = sorted({day_lock_key(user_id, d) for d in dates})
    if keys:
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock(k) FROM "
                "(SELECT unnest(CAST(:keys AS bigint[])) AS k ORDER BY k) AS s"
            ),
            {"keys": keys},
        )
