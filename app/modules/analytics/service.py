"""Детерминированные расчёты аналитики (07 — Analytics). ИИ здесь не участвует.

Средние — по учтённым дням: дни с Expenses (любой категории) + «Бесплатные дни» (как 0 ₽).
Учтённость дня глобальная, даже при фильтре categoryId.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import iter_days
from app.core.schemas import CENT
from app.db.models import Expense, FreeDay

ZERO = Decimal("0.00")


def _q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class CategoryStat:
    category_id: uuid.UUID
    total: Decimal
    share: float
    expense_count: int
    average_expense: Decimal
    delta: Decimal | None
    delta_percent: float | None


@dataclass
class Summary:
    total: Decimal
    previous_total: Decimal | None  # None ⇔ previous = null
    categories: list[CategoryStat]
    data_range: tuple[date, date] | None


async def _by_category(session: AsyncSession, user_id: uuid.UUID, start: date, end: date):
    rows = await session.execute(
        select(Expense.category_id, func.sum(Expense.amount), func.count())
        .where(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .group_by(Expense.category_id)
    )
    return {cid: (Decimal(s), int(n)) for cid, s, n in rows.all()}


async def accounted_dates(session: AsyncSession, user_id: uuid.UUID, start: date, end: date) -> set[date]:
    q = union(
        select(Expense.date).where(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end),
        select(FreeDay.date).where(FreeDay.user_id == user_id, FreeDay.date >= start, FreeDay.date <= end),
    )
    return set((await session.scalars(q)).all())


async def summary(
    session: AsyncSession,
    user_id: uuid.UUID,
    start: date,
    end: date,
    compare: tuple[date, date] | None,
) -> Summary:
    current = await _by_category(session, user_id, start, end)
    total = _q2(sum((s for s, _ in current.values()), ZERO))

    previous: dict[uuid.UUID, tuple[Decimal, int]] | None = None
    previous_total: Decimal | None = None
    if compare is not None:
        if await accounted_dates(session, user_id, *compare):
            previous = await _by_category(session, user_id, *compare)
            previous_total = _q2(sum((s for s, _ in previous.values()), ZERO))

    stats = []
    for cid, (cat_total, count) in current.items():
        delta = delta_percent = None
        if previous is not None:
            prev = previous.get(cid, (ZERO, 0))[0]
            delta = _q2(cat_total - prev)
            if prev > 0:
                delta_percent = round(float((cat_total - prev) / prev * 100), 1)
        stats.append(
            CategoryStat(
                category_id=cid,
                total=_q2(cat_total),
                share=round(float(cat_total / total), 4) if total > 0 else 0.0,
                expense_count=count,
                average_expense=_q2(cat_total / count),
                delta=delta,
                delta_percent=delta_percent,
            )
        )
    stats.sort(key=lambda s: (-s.total, str(s.category_id)))

    lo, hi = (
        await session.execute(
            select(func.min(Expense.date), func.max(Expense.date)).where(Expense.user_id == user_id)
        )
    ).one()
    return Summary(
        total=total,
        previous_total=previous_total,
        categories=stats,
        data_range=(lo, hi) if lo is not None else None,
    )


@dataclass
class AverageBucket:
    accounted_days: int
    per_day: Decimal | None


@dataclass
class Dynamics:
    points: list[tuple[date, Decimal]]
    overall: AverageBucket
    weekday: AverageBucket
    weekend: AverageBucket


def _bucket(days: list[date], totals: dict[date, Decimal]) -> AverageBucket:
    if not days:
        return AverageBucket(0, None)
    s = sum((totals.get(d, ZERO) for d in days), ZERO)
    return AverageBucket(len(days), _q2(s / len(days)))


async def dynamics(
    session: AsyncSession, user_id: uuid.UUID, start: date, end: date, category_id: uuid.UUID | None
) -> Dynamics:
    q = (
        select(Expense.date, func.sum(Expense.amount))
        .where(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .group_by(Expense.date)
    )
    if category_id is not None:
        q = q.where(Expense.category_id == category_id)
    totals = {d: Decimal(s) for d, s in (await session.execute(q)).all()}
    accounted = sorted(await accounted_dates(session, user_id, start, end))
    points = [(d, _q2(totals.get(d, ZERO))) for d in iter_days(start, end)]
    return Dynamics(
        points=points,
        overall=_bucket(accounted, totals),
        weekday=_bucket([d for d in accounted if d.weekday() < 5], totals),
        weekend=_bucket([d for d in accounted if d.weekday() >= 5], totals),
    )
