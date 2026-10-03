import uuid
from datetime import date

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, SessionDep
from app.core.errors import ErrorDetail, validation_error
from app.core.schemas import ApiModel, Money, parse_date_param
from app.modules.analytics import service

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_ANALYTICS_DAYS = 3660  # защитный предел; периоды UX — до 90 дней


def parse_range(from_: str, to: str) -> tuple[date, date]:
    start, end = parse_date_param(from_, "range_invalid"), parse_date_param(to, "range_invalid")
    if start > end:
        raise validation_error(ErrorDetail(code="range_invalid"))
    if (end - start).days + 1 > MAX_ANALYTICS_DAYS:
        raise validation_error(ErrorDetail(code="range_too_large"))
    return start, end


def parse_compare(compare_from: str | None, compare_to: str | None) -> tuple[date, date] | None:
    if compare_from is None and compare_to is None:
        return None
    if compare_from is None or compare_to is None:
        raise validation_error(ErrorDetail(code="range_invalid"))
    return parse_range(compare_from, compare_to)


class PreviousOut(ApiModel):
    total: Money


class CategorySummaryOut(ApiModel):
    category_id: uuid.UUID
    total: Money
    share: float
    expense_count: int
    average_expense: Money
    delta: Money | None
    delta_percent: float | None


class DataRangeOut(ApiModel):
    first_date: date
    last_date: date


class SummaryOut(ApiModel):
    total: Money
    previous: PreviousOut | None
    categories: list[CategorySummaryOut]
    data_range: DataRangeOut | None


class PointOut(ApiModel):
    date: date
    total: Money


class BucketOut(ApiModel):
    accounted_days: int
    per_day: Money | None


class AveragesOut(ApiModel):
    accounted_days: int
    per_day: Money | None
    weekday: BucketOut
    weekend: BucketOut


class DynamicsOut(ApiModel):
    points: list[PointOut]
    averages: AveragesOut


@router.get("/summary", response_model=SummaryOut)
async def get_summary(
    user: CurrentUserDep,
    session: SessionDep,
    from_: str = Query(alias="from"),
    to: str = Query(),
    compareFrom: str | None = None,  # noqa: N803
    compareTo: str | None = None,  # noqa: N803
) -> SummaryOut:
    start, end = parse_range(from_, to)
    s = await service.summary(session, user.id, start, end, parse_compare(compareFrom, compareTo))
    return SummaryOut(
        total=s.total,
        previous=PreviousOut(total=s.previous_total) if s.previous_total is not None else None,
        categories=[CategorySummaryOut(**c.__dict__) for c in s.categories],
        data_range=DataRangeOut(first_date=s.data_range[0], last_date=s.data_range[1])
        if s.data_range
        else None,
    )


@router.get("/dynamics", response_model=DynamicsOut)
async def get_dynamics(
    user: CurrentUserDep,
    session: SessionDep,
    from_: str = Query(alias="from"),
    to: str = Query(),
    categoryId: uuid.UUID | None = None,  # noqa: N803
) -> DynamicsOut:
    start, end = parse_range(from_, to)
    d = await service.dynamics(session, user.id, start, end, categoryId)
    return DynamicsOut(
        points=[PointOut(date=day, total=t) for day, t in d.points],
        averages=AveragesOut(
            accounted_days=d.overall.accounted_days,
            per_day=d.overall.per_day,
            weekday=BucketOut(**d.weekday.__dict__),
            weekend=BucketOut(**d.weekend.__dict__),
        ),
    )
